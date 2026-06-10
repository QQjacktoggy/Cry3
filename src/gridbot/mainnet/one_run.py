"""Telegram-triggered one-run mainnet validation manager."""

from __future__ import annotations

import asyncio
import json
import math
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from html import escape

from binance import BinanceAPIException

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from config.settings import Settings
from src.gridbot.binance.client import BinanceFuturesClient
from src.gridbot.binance.models import PositionInfo
from src.gridbot.storage.repositories import FuturesTradeRepository, MainnetRunRepository
from src.gridbot.strategy.long_pullback import Candle
from src.gridbot.strategy.wildcat_live import (
    WildcatLiveDecision,
    evaluate_dca_guard,
    evaluate_entry_trend_guard,
    generate_wildcat_v2_adverse_guard_live_decision,
)
from src.gridbot.utils.logging import get_logger

logger = get_logger(__name__)
PARTIAL_TP_SUFFIX = "_tp1"
MID_TP_SUFFIX = "_tp2"
FINAL_TP_SUFFIX = "_tp3"

# app_config key for the catch-up/rescue runtime kill-switch (Telegram /rescue).
RESCUE_CONFIG_KEY = "mainnet_rescue_enabled"
# app_config keys for Telegram-adjustable runtime config (2026-06-10):
# single-ticket notional and loop cumulative-loss protection cap.
NOTIONAL_CONFIG_KEY = "mainnet_notional_usdc"
LOOP_LOSS_CAP_CONFIG_KEY = "mainnet_loop_loss_cap_usdc"
# Telegram-selectable choices (buttons in _buttons()).
NOTIONAL_CHOICES = (200, 300, 500, 1000)
LOOP_LOSS_CAP_CHOICES = (0.0, 2.0, 5.0, 10.0, 20.0)


TERMINAL_STATUSES = {"COMPLETED", "ENTRY_EXPIRED", "FAILED", "CANCELLED", "EMERGENCY_CLOSED"}


class GTXSlippageExceeded(Exception):
    """Raised when price slippage exceeds tolerance after GTX Post-Only rejection."""


@dataclass(frozen=True)
class RunStatus:
    text: str
    reply_markup: InlineKeyboardMarkup | None = None


class MainnetOneRunManager:
    """Owns one Telegram-approved mainnet lifecycle at a time."""

    def __init__(
        self,
        settings: Settings,
        client: BinanceFuturesClient,
        repo: MainnetRunRepository,
        telegram_app=None,
        trade_repo: FuturesTradeRepository | None = None,
        config_repo=None,
    ) -> None:
        self._settings = settings
        self._client = client
        self._repo = repo
        self._trade_repo = trade_repo
        self._telegram_app = telegram_app
        # Runtime kill-switch for the catch-up/rescue branch, persisted in
        # app_config (key RESCUE_CONFIG_KEY) so it survives restarts and is
        # shared with the Telegram /rescue command. None = not yet loaded.
        self._config_repo = config_repo
        self._rescue_enabled: bool | None = None
        # Telegram-adjustable runtime config (notional ticket size and the
        # loop loss-protection cap), persisted in app_config so they survive
        # restarts.  Loaded lazily on the first status()/arm()/run_cycle().
        self._runtime_config_loaded: bool = False
        self._loop_loss_cap: float = float(settings.mainnet_loop_loss_cap_usdc)
        # Cumulative NET PnL (realized − commission) across the current loop
        # chain; reset when a new loop is armed or the loop ends/stops.
        self._loop_net_pnl: float = 0.0
        self._protection_sent: set[str] = set()
        self._partial_taken: set[str] = set()
        self._partial_order_armed: set[str] = set()
        self._recovery_counts: dict[str, int] = {}
        self._dca_preloaded: dict[str, int] = {}   # run_id -> pre-placed DCA order_id
        # Trailing take-profit state, keyed by run_id.  _trail_peak holds the
        # best favorable mark price seen since entry (highest for LONG, lowest
        # for SHORT); _trail_armed marks runs whose peak has crossed the arm
        # threshold so the lock-exit is live.  See _run_running.
        self._trail_peak: dict[str, float] = {}
        self._trail_armed: set[str] = set()
        # Fast trail watcher: once armed, a dedicated asyncio task polls the
        # mark every mainnet_trail_watch_interval_seconds so a sub-minute dump
        # is caught between 10s manage cycles.  _trail_exiting de-duplicates
        # the watcher and the manage-cycle trigger paths (whoever fires first
        # owns the close; the other side skips).
        self._trail_watch_tasks: dict[str, asyncio.Task] = {}
        self._trail_exiting: set[str] = set()
        # DCA guard cooldown: maps run_id -> ms timestamp of last guard block.
        # Prevents regime-flicker from bypassing the guard within the cooldown
        # window (mainnet_dca_guard_cooldown_seconds).
        self._dca_block_times: dict[str, int] = {}
        # Partial-exit flag: once TP1 fires (qty shrinks), DCA is forbidden so
        # we never average into a runner that already booked partial profit.
        self._partial_exits: set[str] = set()
        self._mid_order_armed: set[str] = set()
        self._final_order_armed: set[str] = set()
        self._final_taken: set[str] = set()
        # Per-run, per-level placed TP qty (tp1/tp2/tp3) so a fill notification
        # reports that level's qty rather than the whole position drop.
        self._tp_layer_qty: dict[str, dict[str, float]] = {}
        # Conservative entry requote state — counts and last-requote
        # timestamps keyed by run_id, so we can throttle requotes
        # without bumping run.updated_at_ms (which would also push out
        # the TTL check).
        self._entry_requote_counts: dict[str, int] = {}
        self._entry_requote_last_ms: dict[str, int] = {}
        # Loop state: when user arms N>1 runs, we chain automatic re-arms
        # after each run completes.  remaining = runs left to arm (the
        # currently active run counts as the first).
        self._loop_total: int = 0
        self._loop_completed: int = 0
        self._loop_run_ids: list[str] = []
        # Cooldown tracker for loop chains: key = (side, strategy_label),
        # value = cooldown_until_ms.  After an SL exit, the same side +
        # strategy combination is blocked for the configured duration so
        # we do not chain into an identical losing setup.
        self._loop_cooldowns: dict[tuple[str, str], int] = {}
        self._loop_cooldown_minutes: int = self._settings.mainnet_loop_cooldown_minutes
        # Consecutive net-loss streak across the loop chain.  Cooldown escalates
        # with the streak (base + step*(streak-1)) so a losing macro regime is
        # throttled harder the longer it persists; any net-win resets it to 0.
        self._loss_streak: int = 0
        # When a loop chain-arm is skipped because of an active cooldown, the
        # pending arm is recorded here so run_cycle can resume it once the
        # cooldown expires.  Without this, the loop would stall forever after a
        # cooldown skip (the COMPLETED run is gone and nothing re-arms it).
        self._loop_resume: dict | None = None
        # Run ids that have already emitted an entry-trend-guard skip notice, so
        # an armed run skipping a counter-trend signal every cycle does not spam
        # Telegram/DB until either the trend flips or signal_timeout fires.
        self._entry_guard_notified: set[str] = set()
        # Rescue spike filter: track run_ids already notified to avoid per-cycle spam.
        self._rescue_spike_notified: set[str] = set()
        # Stale algo-order id cache — populated by deprecated maker SL path, kept for
        # compatibility; cleared harmlessly in _cancel_stop_loss_order.
        self._sl_order_ids: dict[str, int] = {}

    async def status(self) -> RunStatus:
            await self._ensure_runtime_config_loaded()
            latest = await self._repo.get_latest_run()
            active = await self._repo.get_active_run()
            entry_notional = self._settings.mainnet_effective_entry_notional_usdc
            entry_margin = self._settings.mainnet_effective_entry_margin_usdc
            loss_cap_label = (
                f"−${self._loop_loss_cap:.0f} USDC" if self._loop_loss_cap > 0 else "關閉"
            )
            lines = [
                "🧪 <b>Mainnet One-Run 驗證</b>",
                f"狀態：<b>{'已啟用' if self._settings.mainnet_one_run_enabled else '未啟用'}</b>",
                f"交易對：<code>{escape(self._settings.mainnet_symbol)}</code>",
                f"策略：<code>{escape(self._settings.mainnet_strategy_label)}</code>",
                f"資金上限：<b>${self._settings.mainnet_equity_cap_usdc:.2f} USDC</b>",
                f"單筆名目/槓桿：<b>${entry_notional:.2f}</b> / <b>{self._settings.mainnet_leverage}x</b>",
                f"預估保證金：<b>${entry_margin:.4f}</b>",
                f"🛡 Loop 虧損保護：<b>{loss_cap_label}</b>",
                "",
                "訊號仍會照常推送；按下啟動後，只會把下一個符合條件的 wildcat 訊號接成一個自動 run。",
            ]
            if self._loop_total > 0:
                lines.append("")
                lines.append(
                    f"🔁 <b>Loop 進行中：{self._loop_completed}/{self._loop_total}</b>"
                )
                lines.append(
                    f"Loop 累計淨損益：<b>{self._loop_net_pnl:+.4f} USDC</b>"
                )
                if self._loop_cooldowns:
                    now_ms = int(time.time() * 1000)
                    active_cooldowns = [
                        (k, v - now_ms)
                        for k, v in self._loop_cooldowns.items()
                        if v > now_ms
                    ]
                    if active_cooldowns:
                        lines.append("⏳ <b>Cooldown 倒數：</b>")
                        for (side, strat), remaining_ms in active_cooldowns[:5]:
                            remaining_s = int(remaining_ms // 1000)
                            lines.append(
                                f"   • {side} {strat}: <b>{remaining_s}s</b>"
                            )
            if active:
                lines.extend(
                    [
                        "",
                        f"目前 active run：<code>{escape(active['run_id'])}</code>",
                        f"狀態：<b>{escape(active['status'])}</b>",
                        f"方向：<b>{escape(str(active.get('side') or '-'))}</b>",
                    ]
                )
            # Determine if latest run is cancellable (not in terminal state)
            terminal_states = {"COMPLETED", "FAILED", "CANCELLED"}
            latest_cancellable = (
                latest
                and latest.get("status") not in terminal_states
                and latest.get("run_id") != (active.get("run_id") if active else None)
            )
            if latest:
                lines.extend(
                    [
                        "",
                        f"最近 run：<code>{escape(latest['run_id'])}</code>",
                        f"狀態：<b>{escape(latest['status'])}</b>",
                        f"結果：<code>{escape(str(latest.get('exit_reason') or '-'))}</code>",
                    ]
                )
            markup = self._buttons(active=bool(active), show_cancel=latest_cancellable)
            logger.info(
                "mainnet_status_reply",
                has_markup=markup is not None,
                active=bool(active),
                loop_total=self._loop_total,
                latest_cancellable=latest_cancellable,
            )
            return RunStatus("\n".join(lines), markup)

    async def arm(self, actor: str = "telegram", loop_count: int = 1) -> str:
        await self._ensure_runtime_config_loaded()
        if not self._settings.mainnet_one_run_enabled:
            return "❌ Mainnet one-run 尚未啟用。請設定 MAINNET_ONE_RUN_ENABLED=true。"
        if not self._settings.mainnet_api_key or not self._settings.mainnet_api_secret:
            return "❌ 尚未設定 MAINNET_API_KEY / MAINNET_API_SECRET。"
        if loop_count < 1:
            return "❌ loop_count 必須 >= 1。"
        active = await self._repo.get_active_run()
        if active:
            return f"⚠️ 已有 active run：<code>{escape(active['run_id'])}</code>，狀態 <b>{escape(active['status'])}</b>。"

        try:
            preflight_error = await self._preflight()
        except Exception as exc:  # noqa: BLE001
            logger.warning("mainnet_one_run_preflight_failed", error=str(exc))
            return f"❌ Mainnet one-run preflight 失敗：<code>{escape(str(exc)[:400])}</code>"
        if preflight_error:
            return preflight_error

        run_id = f"{self._settings.mainnet_client_order_prefix}_{int(time.time() * 1000)}"
        # Set up loop state on the FIRST run of a chain only.  When arm()
        # is invoked from _finish_flat_run (loop chain), we keep the
        # existing loop_total / loop_completed and only bump the
        # remaining counter implicitly.
        is_chain = actor == "telegram_loop"
        if not is_chain:
            self._loop_total = int(loop_count)
            self._loop_completed = 0
            self._loop_run_ids = [run_id]
            self._loss_streak = 0
            self._loop_net_pnl = 0.0
        else:
            self._loop_run_ids.append(run_id)
        params = {
            "actor": actor,
            "symbol": self._settings.mainnet_symbol,
            "strategy": self._settings.mainnet_strategy_label,
            "equity_cap_usdc": self._settings.mainnet_equity_cap_usdc,
            "initial_notional_usdc": self._settings.mainnet_effective_entry_notional_usdc,
            "max_cumulative_notional_usdc": self._settings.mainnet_effective_max_cumulative_notional_usdc,
            "leverage": self._settings.mainnet_leverage,
            "maker_first": True,
            "loop_count": int(loop_count),
            "loop_loss_cap_usdc": self._loop_loss_cap,
        }
        await self._repo.create_run(
            {
                "run_id": run_id,
                "symbol": self._settings.mainnet_symbol,
                "strategy_label": self._settings.mainnet_strategy_label,
                "status": "ARMED",
                "params": params,
            }
        )
        await self._repo.log_event(run_id, "armed", params)
        # For loop chains, show position (next/N) instead of (1/N).
        if self._loop_total > 1:
            next_index = (self._loop_completed + 1) if is_chain else 1
            return (
                f"✅ <b>Mainnet one-run 已啟動 ({next_index}/{self._loop_total})</b>\n"
                f"Run：<code>{escape(run_id)}</code>\n"
                f"接下來只會等待下一個 wildcat 訊號；沒訊號時不會下單。"
            )
        return (
            "✅ <b>Mainnet one-run 已啟動</b>\n"
            f"Run：<code>{escape(run_id)}</code>\n"
            "接下來只會等待下一個 wildcat 訊號；沒訊號時不會下單。"
        )

    async def cancel(self) -> str:
        active = await self._repo.get_active_run()
        if not active:
            return "目前沒有 active mainnet run。"
        was_in_loop = self._loop_total > 0
        loop_completed = self._loop_completed
        loop_total = self._loop_total
        symbol = active["symbol"]
        try:
            for order in await self._client.get_open_orders(symbol):
                cid = str(order.get("clientOrderId") or "")
                if cid.startswith(self._settings.mainnet_client_order_prefix):
                    await self._client.cancel_order(symbol, int(order["orderId"]))
        except Exception as exc:  # noqa: BLE001
            logger.warning("mainnet_one_run_cancel_orders_failed", error=str(exc))
        await self._repo.complete_run(active["run_id"], "CANCELLED", "telegram_cancel")
        await self._repo.log_event(active["run_id"], "cancelled", {"source": "telegram"})
        # Clear loop state on cancel
        self._loop_total = 0
        self._loop_completed = 0
        self._loop_run_ids = []
        self._loop_net_pnl = 0.0
        if was_in_loop:
            return (
                f"🛑 已取消 run：<code>{escape(active['run_id'])}</code>。\n"
                f"Loop 已中止（已完成 {loop_completed}/{loop_total}）。"
            )
        return f"🛑 已取消 run：<code>{escape(active['run_id'])}</code>。"

    async def stop_loop(self) -> str:
        """Stop the loop chain without cancelling the current run.

        Clears loop counter, loop completions, and all cooldowns.  The
        currently active run (if any) is left alone — only future
        chain-arms are suppressed.  Use this to recover from a stuck
        loop state (e.g. after a previous run FAILED before chain-arm
        could fire).
        """
        if self._loop_total == 0:
            return "目前沒有進行中的 loop。"
        was_in_loop_total = self._loop_total
        was_completed = self._loop_completed
        n_cooldowns = len(
            [v for v in self._loop_cooldowns.values() if v > int(time.time() * 1000)]
        )
        self._loop_total = 0
        self._loop_completed = 0
        self._loop_run_ids = []
        self._loop_cooldowns.clear()
        self._loss_streak = 0
        self._loop_net_pnl = 0.0
        logger.info(
            "mainnet_one_run_loop_stopped",
            completed=was_completed,
            total=was_in_loop_total,
            cooldowns_cleared=n_cooldowns,
        )
        return (
            f"⏹ <b>Loop 已停止</b>\n"
            f"先前進度：<b>{was_completed}/{was_in_loop_total}</b>\n"
            f"Cooldown 已清空：<b>{n_cooldowns}</b> 項。\n"
            "目前 active run 不受影響，會繼續執行到結束。"
        )

    async def run_cycle(self) -> None:
        if not self._settings.mainnet_one_run_enabled:
            return
        await self._ensure_runtime_config_loaded()
        active = await self._repo.get_active_run()
        if not active:
            # No active run: if a loop arm was deferred by a cooldown, resume
            # it once the cooldown expires.
            await self._maybe_resume_pending_loop()
            return
        try:
            status = active["status"]
            if status == "ARMED":
                await self._run_armed(active)
            elif status == "ENTRY_PENDING":
                await self._run_entry_pending(active)
            elif status in {"RUNNING", "CLOSING"}:
                await self._run_running(active)
        except Exception as exc:  # noqa: BLE001
            logger.error("mainnet_one_run_cycle_failed", run_id=active.get("run_id"), error=str(exc))
            await self._repo.complete_run(active["run_id"], "FAILED", "exception", str(exc)[:500])
            await self._notify(
                "❌ <b>Mainnet one-run 失敗</b>\n"
                f"Run：<code>{escape(active['run_id'])}</code>\n"
                f"錯誤：<code>{escape(str(exc)[:500])}</code>"
            )

    async def _is_rescue_enabled(self) -> bool:
        """Return the catch-up/rescue toggle, defaulting to enabled.

        Cached in memory after the first DB read; the Telegram /rescue command
        updates both the DB and this cache via set_rescue_enabled().
        """
        if self._rescue_enabled is not None:
            return self._rescue_enabled
        enabled = True
        if self._config_repo is not None:
            try:
                raw = await self._config_repo.get(RESCUE_CONFIG_KEY)
                if raw is not None:
                    enabled = raw == "1"
            except Exception as exc:  # noqa: BLE001
                logger.warning("mainnet_rescue_toggle_read_failed", error=str(exc))
        self._rescue_enabled = enabled
        return enabled

    async def set_rescue_enabled(self, enabled: bool) -> None:
        """Persist the catch-up/rescue toggle and refresh the in-memory cache."""
        self._rescue_enabled = enabled
        if self._config_repo is not None:
            await self._config_repo.set(RESCUE_CONFIG_KEY, "1" if enabled else "0")

    async def _ensure_runtime_config_loaded(self) -> None:
        """Load Telegram-adjustable runtime config from app_config (once).

        The persisted notional is applied onto the live settings object so
        every downstream consumer — arm params, DCA preplace sizing, the
        effective_* properties — sees the override without plumbing changes.
        """
        if self._runtime_config_loaded:
            return
        self._runtime_config_loaded = True
        if self._config_repo is None:
            return
        try:
            raw = await self._config_repo.get(NOTIONAL_CONFIG_KEY)
            if raw:
                self._apply_notional(float(raw))
                logger.info("mainnet_notional_config_loaded", notional=float(raw))
        except Exception as exc:  # noqa: BLE001
            logger.warning("mainnet_notional_config_read_failed", error=str(exc))
        try:
            raw = await self._config_repo.get(LOOP_LOSS_CAP_CONFIG_KEY)
            if raw is not None:
                self._loop_loss_cap = max(0.0, float(raw))
        except Exception as exc:  # noqa: BLE001
            logger.warning("mainnet_loop_loss_cap_read_failed", error=str(exc))

    def _apply_notional(self, usdc: float) -> None:
        """Apply a ticket size to the live settings object.

        Cap, ticket and max-cumulative scale together: max cumulative =
        ticket × (recovery_steps + 1), i.e. the entry plus every DCA layer
        (200 → 800 at steps=3), preserving the deployed ×4 ratio.
        """
        layers = max(0, int(self._settings.mainnet_recovery_steps)) + 1
        self._settings.mainnet_equity_cap_usdc = float(usdc)
        self._settings.mainnet_initial_notional_usdc = float(usdc)
        self._settings.mainnet_max_cumulative_notional_usdc = float(usdc) * layers

    async def set_notional(self, usdc: float) -> str:
        """Telegram 💰 buttons: set the one-run ticket notional (USDC)."""
        await self._ensure_runtime_config_loaded()
        if int(usdc) not in NOTIONAL_CHOICES:
            choices = "/".join(str(c) for c in NOTIONAL_CHOICES)
            return f"❌ 金額僅支援 {choices} USDC。"
        active = await self._repo.get_active_run()
        if active:
            # Mid-run resize would desync DCA preplace sizing and the
            # cumulative-notional bookkeeping of the live run.
            return "⚠️ 有 active run 進行中，請等它結束（或先取消）再調整金額。"
        self._apply_notional(float(usdc))
        if self._config_repo is not None:
            await self._config_repo.set(NOTIONAL_CONFIG_KEY, str(int(usdc)))
        logger.info("mainnet_notional_set", notional=float(usdc))
        return (
            f"💰 單筆名目已設為 <b>${usdc:.0f} USDC</b>\n"
            f"DCA 累計上限：<b>${self._settings.mainnet_max_cumulative_notional_usdc:.0f} USDC</b>"
            f"（進場 + {self._settings.mainnet_recovery_steps} 層 DCA）\n"
            f"預估保證金（單筆）：<b>${self._settings.mainnet_effective_entry_margin_usdc:.2f}</b>"
        )

    async def set_loop_loss_cap(self, cap: float) -> str:
        """Telegram 🛡 buttons: set the loop cumulative-loss break cap (USDC)."""
        await self._ensure_runtime_config_loaded()
        cap = max(0.0, float(cap))
        self._loop_loss_cap = cap
        if self._config_repo is not None:
            await self._config_repo.set(LOOP_LOSS_CAP_CONFIG_KEY, f"{cap:g}")
        logger.info("mainnet_loop_loss_cap_set", cap=cap)
        if cap <= 0:
            return "🛡 Loop 虧損保護已<b>關閉</b>。"
        return (
            f"🛡 Loop 虧損保護已設為 <b>−${cap:.2f} USDC</b>\n"
            f"loop 累計淨損益（已實現−手續費）跌破 −${cap:.2f} 時，"
            "立即停止後續 run（目前 run 不受影響）。"
        )

    async def _run_armed(self, run: dict) -> None:
        if int(time.time() * 1000) - int(run["armed_at_ms"]) > self._settings.mainnet_one_run_signal_timeout_minutes * 60_000:
            await self._repo.complete_run(run["run_id"], "ENTRY_EXPIRED", "signal_timeout")
            await self._notify(f"⌛ Mainnet one-run 等待訊號逾時，已停止：<code>{escape(run['run_id'])}</code>")
            await self._advance_loop_after_entry_failure(run, "signal_timeout")
            return
        candles = await self._load_candles(run["symbol"])
        decision = generate_wildcat_v2_adverse_guard_live_decision(
            candles,
            target_daily_usdc=self._settings.mainnet_equity_cap_usdc * 0.03,
            notional_usdc=self._settings.mainnet_effective_entry_notional_usdc,
            leverage=self._settings.mainnet_leverage,
            rescue_enabled=await self._is_rescue_enabled(),
        )
        if decision is None:
            return
        allow_entry, entry_reason = evaluate_entry_trend_guard(candles, decision.side)
        if not allow_entry:
            # Counter-trend signal (falling-knife / spike-short). Skip this bar
            # but stay ARMED — keep waiting for an aligned signal until
            # signal_timeout, which then advances the loop (Bug 9 path). Notify
            # only once per run to avoid per-cycle spam.
            if run["run_id"] not in self._entry_guard_notified:
                self._entry_guard_notified.add(run["run_id"])
                await self._repo.log_event(
                    run["run_id"],
                    "entry_trend_skipped",
                    {"side": decision.side, "strategy": decision.strategy, "reason": entry_reason},
                )
                await self._notify(
                    "🛡️ <b>進場守門：跳過逆勢訊號</b>\n"
                    f"Run：<code>{escape(run['run_id'])}</code>\n"
                    f"方向：{escape(decision.side)}｜策略：{escape(decision.strategy)}\n"
                    f"原因：{escape(entry_reason)}\n"
                    "（續等順勢訊號，逾時則前進下一個 loop run）"
                )
            else:
                logger.info(
                    "entry_trend_guard_skip run=%s side=%s reason=%s",
                    run["run_id"],
                    decision.side,
                    entry_reason,
                )
            return
        # Rescue spike filter: skip this cycle if the latest candle shows a sharp
        # adverse move — entering into a spike amplifies loss without DCA rescue.
        signal_reasons = getattr(decision.signal, "reasons", []) or []
        is_rescue = any("rescue" in str(r) for r in signal_reasons)
        if is_rescue and candles:
            last = candles[-1]
            if last.open > 0:
                candle_move_pct = (last.close - last.open) / last.open
                spike_adverse = (
                    (decision.side == "SHORT" and candle_move_pct > 0.0012) or
                    (decision.side == "LONG" and candle_move_pct < -0.0012)
                )
                if spike_adverse:
                    if run["run_id"] not in self._rescue_spike_notified:
                        self._rescue_spike_notified.add(run["run_id"])
                        await self._repo.log_event(
                            run["run_id"],
                            "rescue_spike_skip",
                            {"side": decision.side, "candle_move_pct": round(candle_move_pct * 100, 3)},
                        )
                        await self._notify(
                            f"⚡ Rescue {decision.side} 跳過（急速波動 {candle_move_pct*100:+.2f}%）："
                            f"<code>{escape(run['run_id'])}</code>"
                        )
                    return  # stay ARMED, retry next cycle
        await self._place_entry(run, decision)

    async def _run_entry_pending(self, run: dict) -> None:
        symbol = run["symbol"]
        open_orders = await self._client.get_open_orders(symbol)
        order_id = int(run["entry_order_id"]) if run.get("entry_order_id") else None
        still_open = any(int(row.get("orderId", 0)) == order_id for row in open_orders)
        position = await self._client.get_position(symbol)
        if position:
            await self._repo.update_run(
                run["run_id"],
                status="RUNNING",
                avg_entry_price=position.entry_price,
                qty=abs(position.position_amt),
            )
            await self._repo.log_event(
                run["run_id"],
                "entry_filled",
                {"entry_price": position.entry_price, "qty": abs(position.position_amt)},
            )
            await self._sync_take_profit_orders(run, position, json.loads(run.get("signal_json") or "{}"))
            # Place initial stop-loss maker order if enabled
            if self._settings.mainnet_sl_use_maker:
                signal = json.loads(run.get("signal_json") or "{}")
                sl_price = float(signal.get("stop_loss") or 0.0)
                if sl_price > 0:
                    await self._place_stop_loss_maker(
                        symbol=symbol,
                        side="SELL" if position.position_amt > 0 else "BUY",
                        qty_str=await self._client.format_quantity(symbol, abs(position.position_amt)),
                        sl_price=sl_price,
                        run_id=run["run_id"],
                        reason="SL",
                        run=run,
                    )
            # Pre-place DCA #1 right at entry fill so the exchange catches the
            # first adverse touch without waiting for the poll path.
            run["avg_entry_price"] = position.entry_price
            run["qty"] = abs(position.position_amt)
            await self._preplace_next_dca(run, position)
            # Start the TRAIL fast-watcher at entry fill so peak-tracking and
            # arming run on the 2s clock, not the 10s manage cycle (which let
            # cry3mn_1781054933311 exit BELOW entry — spike+dump inside one cycle
            # meant the peak was never recorded).  Idempotent / no-op if disabled.
            trail_sig = json.loads(run.get("signal_json") or "{}")
            trail_tp_pct = float(trail_sig.get("wildcat", {}).get("tp_pct") or 0.0)
            trail_side = "LONG" if position.position_amt > 0 else "SHORT"
            trail_close_side = "SELL" if position.position_amt > 0 else "BUY"
            self._start_trail_watch(run, trail_side, trail_close_side, trail_tp_pct)
            await self._notify(
                "✅ <b>Mainnet one-run 已成交</b>\n"
                f"Run：<code>{escape(run['run_id'])}</code>\n"
                f"方向：<b>{escape(str(run.get('side') or ''))}</b>\n"
                f"均價：<b>${position.entry_price:.4f}</b>\n"
                f"數量：<code>{abs(position.position_amt):.6f}</code>"
            )
            return
        if not still_open:
            await self._repo.complete_run(run["run_id"], "ENTRY_EXPIRED", "entry_not_open_no_position")
            await self._notify(f"⌛ Entry 掛單已不在 open orders 且沒有持倉，run 已停止：<code>{escape(run['run_id'])}</code>")
            await self._advance_loop_after_entry_failure(run, "entry_not_open_no_position")
            return
        # Ladder entry: uses a dedicated per-signal deadline instead of the global TTL.
        # Skip reprice (we intentionally sit at the offset price; chasing defeats the purpose).
        signal_j = json.loads(run.get("signal_json") or "{}")
        ladder_deadline_ms = signal_j.get("entry_ladder_deadline_ms")
        if ladder_deadline_ms is not None:
            if int(time.time() * 1000) >= ladder_deadline_ms:
                if order_id:
                    await self._client.cancel_order(symbol, order_id)
                await self._repo.complete_run(run["run_id"], "ENTRY_EXPIRED", "entry_ttl_expired")
                await self._notify(
                    f"⌛ Ladder 進場逾時（{self._settings.mainnet_entry_limit_ttl_bars} 根 K 棒）未成交，已取消："
                    f"<code>{escape(run['run_id'])}</code>"
                )
                await self._advance_loop_after_entry_failure(run, "entry_ttl_expired")
            return  # still within TTL — sit tight, no reprice
        # Conservative entry requote: if the maker has been on the book
        # long enough, the mark has drifted beyond the configured
        # threshold, and we are still under the requote cap, cancel the
        # existing order and place a new one at a fresh passive price.
        # On success the run stays in ENTRY_PENDING with the new order
        # id, and the next manage_cycle tick picks it up.
        requoted = await self._maybe_requote_entry(run, order_id, open_orders)
        if requoted:
            return
        age_ms = int(time.time() * 1000) - int(run["updated_at_ms"])
        if age_ms >= self._settings.mainnet_entry_order_ttl_seconds * 1000:
            if order_id:
                await self._client.cancel_order(symbol, order_id)
            await self._repo.complete_run(run["run_id"], "ENTRY_EXPIRED", "entry_ttl_expired")
            await self._notify(f"⌛ Entry maker 掛單逾時未成交，已取消：<code>{escape(run['run_id'])}</code>")
            await self._advance_loop_after_entry_failure(run, "entry_ttl_expired")

    async def _run_running(self, run: dict) -> None:
        symbol = run["symbol"]
        position = await self._client.get_position(symbol)
        if not position:
            await self._finish_flat_run(run, "flat_detected")
            return
        if run["run_id"] in self._trail_exiting:
            # TRAIL lock-exit in progress (fast watcher is running the maker
            # reprice loop, up to 12s).  Skip management this cycle so we
            # never double-close or DCA into a position that is being exited;
            # the flat check above finishes the run once the exit fills.
            return
        current_qty = abs(position.position_amt)
        prev_qty = float(run.get("qty") or 0.0)
        if current_qty > prev_qty + 1e-9:
            # DCA filled (qty grew) — record the fill, then cancel old SL and
            # re-arm at the new average entry price.
            await self._repo.log_event(
                run["run_id"],
                "recovery_entry_filled",
                {
                    "qty": current_qty,
                    "added_qty": current_qty - prev_qty,
                    "prev_qty": prev_qty,
                    "avg_price": position.entry_price,
                    "notional_usdc": current_qty * position.entry_price,
                },
            )
            # E1: reset the TRAIL baseline on every DCA fill.  Averaging moves
            # the cost basis TOWARD the market, so a pre-DCA peak measured
            # against the NEW basis instantly satisfies arm_mfe — the trail
            # re-arms with a hair trigger and fires on the first 2s noise tick,
            # exiting ~breakeven instead of letting the recomputed TP ladder
            # work (06-10 08:32 loss run: stale peak 1632.58 vs new avg 1633.72
            # armed AT the fill and stole a TP touched one minute later).
            # Start fresh from the current mark; the watcher (running since
            # entry fill) shares these dicts, and has its own fast-path reset.
            self._trail_peak[run["run_id"]] = position.mark_price
            self._trail_armed.discard(run["run_id"])
            logger.info(
                "trail_reset_on_dca_fill",
                run_id=run["run_id"],
                mark=position.mark_price,
                new_avg=position.entry_price,
            )
            # Pre-placed DCA bookkeeping: the resting GTC limit just filled, so
            # consume the slot — count the layer and the added notional (the
            # poll path in _maybe_recovery counts at placement time instead).
            if run["run_id"] in self._dca_preloaded:
                self._dca_preloaded.pop(run["run_id"], None)
                self._recovery_counts[run["run_id"]] = self._recovery_counts.get(run["run_id"], 0) + 1
                entry_notional = self._settings.mainnet_effective_entry_notional_usdc
                new_cumulative = float(run.get("cumulative_notional_usdc") or entry_notional) + entry_notional
                await self._repo.update_run(run["run_id"], cumulative_notional_usdc=new_cumulative)
                run["cumulative_notional_usdc"] = new_cumulative
            await self._cancel_stop_loss_order(symbol, run["run_id"])
            signal = json.loads(run.get("signal_json") or "{}")
            sl_pct = float(signal.get("wildcat", {}).get("sl_pct") or 0.0)
            if sl_pct > 0 and position.entry_price > 0:
                # Widen SL by widen×dca_count per layer (backtest parity):
                # a freshly averaged position needs more room or it is
                # swept the moment it is filled.
                new_avg_entry = position.entry_price
                dca_count = self._recovery_counts.get(run["run_id"], 0)
                widened_sl_pct = sl_pct * (
                    1 + self._settings.mainnet_recovery_sl_widen_per_layer * dca_count
                )
                if position.position_direction == "LONG":
                    new_sl = new_avg_entry * (1 - widened_sl_pct)
                else:
                    new_sl = new_avg_entry * (1 + widened_sl_pct)
                # Persist the widened stop + new average into signal_json so the
                # software backstop (_hit_stop) and TRAIL anchor track the DCA'd
                # position — otherwise _hit_stop still fires at the ORIGINAL
                # tight stop and closes the position seconds after averaging
                # (root cause of run cry3mn_1781028928037, -0.48).
                signal["stop_loss"] = new_sl
                new_signal_json = json.dumps(signal)
                await self._repo.update_run(
                    run["run_id"],
                    signal_json=new_signal_json,
                    avg_entry_price=new_avg_entry,
                )
                run["signal_json"] = new_signal_json
                run["avg_entry_price"] = new_avg_entry
                if self._settings.mainnet_sl_use_maker:
                    await self._place_stop_loss_maker(
                        symbol=symbol,
                        side="SELL" if position.position_amt > 0 else "BUY",
                        qty_str=await self._client.format_quantity(symbol, current_qty),
                        sl_price=new_sl,
                        run_id=run["run_id"],
                        reason="SL",
                        run=run,
                    )
            await self._preplace_next_dca(run, position)
            await self._repo.update_run(run["run_id"], qty=current_qty)
            run["qty"] = current_qty
        elif abs(current_qty - prev_qty) > 1e-9:
            # Qty shrank (TP partial fills) — sync tracking only, do NOT touch SL
            self._partial_exits.add(run["run_id"])
            # TP partial fill — cancel any pre-placed DCA (no averaging into a winner)
            if run["run_id"] in self._dca_preloaded:
                try:
                    await self._client.cancel_order(symbol, self._dca_preloaded.pop(run["run_id"]))
                except Exception:
                    self._dca_preloaded.pop(run["run_id"], None)
            await self._repo.update_run(run["run_id"], qty=current_qty)
            run["qty"] = current_qty
        if run["status"] == "CLOSING":
            logger.info(
                "mainnet_one_run_waiting_close_fill",
                run_id=run["run_id"],
                symbol=symbol,
                qty=position.position_amt,
                unrealized_pnl=position.unrealized_pnl,
            )
            return
        signal = json.loads(run.get("signal_json") or "{}")
        side = str(run.get("side") or signal.get("side") or "").upper()
        mark = position.mark_price
        entry = float(run.get("avg_entry_price") or position.entry_price)
        qty = current_qty
        sl_price = float(signal.get("stop_loss") or 0.0)
        close_side = "SELL" if position.position_amt > 0 else "BUY"
        hold_start_ms = await self._get_hold_start_ms(run)
        run_age_bars = max(0, int((int(time.time() * 1000) - hold_start_ms) / 60_000))

        # Residual "dust" cleanup: after partial TP fills the remaining position
        # may be tiny.  The ideal-price TP can then sit unfilled (price moved
        # away) until a reverse move triggers the SL.  Instead, place a
        # reduce-only POST-ONLY (maker, 0 USDC fee) order at the top of book so
        # the dust fills quickly WITHOUT paying taker fees.  reduce-only orders
        # are exempt from the min-notional rule, so even sub-20-USDC dust works.
        # The exchange-side STOP_MARKET SL stays armed as a backstop.
        residual_notional = qty * mark
        if 0 < residual_notional < self._settings.mainnet_residual_cleanup_notional_usdc:
            # Cancel any stale TP / dust orders so we can re-quote at the book.
            open_orders = await self._client.get_open_orders(symbol)
            for o in open_orders:
                cid = str(o.get("clientOrderId") or "")
                if cid.startswith(run["run_id"]):
                    try:
                        await self._client.cancel_order(symbol, int(o["orderId"]))
                    except BinanceAPIException as exc:
                        if exc.code not in {-2011, -2022}:
                            raise
            book = await self._client.get_book_ticker(symbol)
            # POST-ONLY maker: SELL sits at best ask, BUY at best bid (queue
            # head, never crosses the spread → always maker, never taker).
            dust_price = (
                float(book["askPrice"]) if close_side == "SELL" else float(book["bidPrice"])
            )
            qty_str = await self._client.format_quantity(symbol, qty)
            try:
                await self._client.create_reduce_only_limit_order(
                    symbol,
                    close_side,
                    qty_str,
                    dust_price,
                    client_order_id=f"{run['run_id']}_dust",
                    post_only=True,
                )
                logger.info(
                    "mainnet_residual_dust_maker_placed",
                    run_id=run["run_id"],
                    qty=qty,
                    price=dust_price,
                    notional=residual_notional,
                )
            except BinanceAPIException as exc:
                if exc.code == -2022:
                    logger.info("mainnet_residual_dust_position_gone", run_id=run["run_id"])
                elif exc.code == -5022:
                    # Book moved; will re-quote next cycle.
                    logger.info("mainnet_residual_dust_postonly_rejected_retry_next", run_id=run["run_id"])
                else:
                    raise
            return

        await self._refresh_partial_fill_state(run, position, prev_qty=prev_qty)
        await self._sync_take_profit_orders(run, position, signal)
        if await self._maybe_recovery(run, signal, position):
            return
        # Ensure the TRAIL fast-watcher is alive — covers bot restart, where the
        # in-memory asyncio task is lost while the run stays RUNNING.  Idempotent.
        self._start_trail_watch(
            run, side, close_side, float(signal.get("wildcat", {}).get("tp_pct") or 0.0)
        )
        if await self._maybe_trailing_exit(run, signal, position, side, mark, entry, qty, close_side):
            return
        if self._hit_stop(side, mark, sl_price):
            await self._close_position(symbol, close_side, qty, "SL", run)
            return
        unrealized_loss_limit = -float(
            run.get("cumulative_notional_usdc") or self._settings.mainnet_effective_entry_notional_usdc
        ) * self._settings.mainnet_adverse_exit_loss_pct
        if run_age_bars >= self._settings.mainnet_adverse_exit_bars and position.unrealized_pnl <= unrealized_loss_limit:
            await self._close_position(symbol, close_side, qty, "ADVERSE_EXIT", run)
            return
        if run_age_bars >= self._settings.mainnet_max_holding_bars:
            reason = "MAX_HOLD_WIN" if position.unrealized_pnl >= 0 else "MAX_HOLD_LOSS"
            await self._close_position(symbol, close_side, qty, reason, run)

    async def _get_hold_start_ms(self, run: dict) -> int:
        cached = run.get("_hold_start_ms")
        if cached is not None:
            return int(cached)
        entry_filled_at_ms = await self._repo.get_first_event_time(run["run_id"], "entry_filled")
        hold_start_ms = int(entry_filled_at_ms or run.get("armed_at_ms") or int(time.time() * 1000))
        run["_hold_start_ms"] = hold_start_ms
        return hold_start_ms

    async def _place_entry(self, run: dict, decision: WildcatLiveDecision) -> None:
        await self._ensure_fee_guard(run["symbol"])
        await self._client.set_leverage(run["symbol"], self._settings.mainnet_leverage)
        side = "BUY" if decision.side == "LONG" else "SELL"
        entry_notional = self._settings.mainnet_effective_entry_notional_usdc
        qty = await self._client.format_quantity(
            run["symbol"],
            entry_notional / decision.signal.price,
        )
        client_order_id = f"{run['run_id']}_entry"
        ladder_offset = self._settings.mainnet_entry_limit_offset
        ladder_deadline_ms: int | None = None
        entry_note = ""
        if ladder_offset > 0.0:
            # Ladder entry: place GTC LIMIT at a better price and wait up to TTL bars
            tick = float(await self._client.price_tick_size(run["symbol"]))
            raw_limit = decision.signal.price * (1 - ladder_offset if side == "BUY" else 1 + ladder_offset)
            # Floor for BUY (wait for dip), ceil for SELL (wait for pop)
            if side == "BUY":
                limit_price = math.floor(raw_limit / tick) * tick
            else:
                limit_price = math.ceil(raw_limit / tick) * tick
            limit_price = round(limit_price, 8)
            order = None
            try:
                # GTX (post-only) so the ladder NEVER fills as taker.  The old
                # GTC limit crossed when signal->placement latency pushed price
                # past our limit, fee 0.04% = 0.080/200 notional (3 such fills on
                # 06-10 = -0.24 = 28% of the night's net; two of them turned a
                # gross-profit TRAIL into a net loss).
                order = await self._client.create_limit_order_raw(
                    symbol=run["symbol"],
                    side=side,
                    quantity=qty,
                    price=limit_price,
                    time_in_force="GTX",
                    reduce_only=False,
                    client_order_id=client_order_id,
                )
            except BinanceAPIException as exc:
                if exc.code == -5022:
                    # Price already moved past the ladder limit, so a crossing
                    # order would have been the taker leak.  Re-quote GTX at the
                    # passive top-of-book (BUY sits at bid, SELL at ask) — price
                    # moved our way, so this is an equal-or-better entry and
                    # guaranteed maker.
                    book = await self._client.get_book_ticker(run["symbol"])
                    passive_price = round(
                        float(book["bidPrice"]) if side == "BUY" else float(book["askPrice"]),
                        8,
                    )
                    logger.warning(
                        "entry_ladder_post_only_rejected_requote_book",
                        run_id=run["run_id"],
                        ladder_price=limit_price,
                        book_price=passive_price,
                        side=side,
                    )
                    try:
                        order = await self._client.create_limit_order_raw(
                            symbol=run["symbol"],
                            side=side,
                            quantity=qty,
                            price=passive_price,
                            time_in_force="GTX",
                            reduce_only=False,
                            client_order_id=client_order_id,
                        )
                        limit_price = passive_price
                    except BinanceAPIException as requote_exc:
                        exc = requote_exc  # book moved again; fall through to reject
                if order is None:
                    err_detail = str(exc)[:500]
                    await self._repo.complete_run(run["run_id"], "ENTRY_REJECTED", "ladder_rejected", err_detail)
                    await self._repo.log_event(run["run_id"], "entry_rejected", {
                        "reason": "ladder_rejected", "detail": err_detail,
                    })
                    await self._notify(
                        "⚠️ <b>Mainnet ladder entry 被拒</b>\n"
                        f"Run：<code>{escape(run['run_id'])}</code>\n"
                        f"詳情：<code>{escape(err_detail[:400])}</code>"
                    )
                    await self._advance_loop_after_entry_failure(run, "ladder_rejected")
                    return
            ladder_deadline_ms = int(time.time() * 1000) + self._settings.mainnet_entry_limit_ttl_bars * 60_000
            final_price = limit_price
            entry_note = (
                f"\n🪜 Ladder：${limit_price:.4f}（訊號 ${decision.signal.price:.4f} - {ladder_offset*10000:.0f}bp）"
                f"｜TTL {self._settings.mainnet_entry_limit_ttl_bars} 根 K 棒"
            )
        else:
            try:
                order = await self._place_post_only_with_retry(
                    symbol=run["symbol"],
                    side=side,
                    quantity=qty,
                    signal_price=decision.signal.price,
                    client_order_id=client_order_id,
                    slippage_bps=self._settings.mainnet_entry_slippage_bps,
                    fallback_to_gtc=self._settings.mainnet_entry_fallback_to_gtc,
                    reduce_only=False,
                )
            except GTXSlippageExceeded as exc:
                err_detail = str(exc)[:500]
                await self._repo.complete_run(run["run_id"], "ENTRY_REJECTED", "slippage_exceeded", err_detail)
                await self._repo.log_event(run["run_id"], "entry_rejected", {
                    "reason": "slippage_exceeded",
                    "detail": err_detail,
                })
                await self._notify(
                    "⚠️ <b>Mainnet one-run entry 被拒</b>\n"
                    f"Run：<code>{escape(run['run_id'])}</code>\n"
                    f"原因：滑價超出容忍範圍\n"
                    f"詳情：<code>{escape(str(exc)[:400])}</code>"
                )
                await self._advance_loop_after_entry_failure(run, "slippage_exceeded")
                return
            final_price = float(order.get("price", 0) or decision.signal.price)
            used_gtc = order.get("timeInForce") != "GTX"
            entry_note = "\n⚠️ 使用 GTC 限價單進場（maker 保護已關閉）" if used_gtc else ""
        payload = {
            "side": decision.side,
            "strategy": decision.strategy,
            "price": decision.signal.price,
            "entry_price": final_price,
            "stop_loss": decision.signal.stop_loss,
            "take_profits": decision.signal.take_profits,
            "take_profit": decision.signal.take_profits[0] if decision.signal.take_profits else None,
            "score": decision.signal.score,
            "reasons": decision.signal.reasons,
            "wildcat": {
                "tp_pct": decision.tp_pct,
                "sl_pct": decision.sl_pct,
                "partial_exit_pct": decision.partial_exit_pct,
                "partial_tp_pct": decision.partial_tp_pct,
                "recovery_steps": decision.recovery_steps,
                "recovery_trigger_pct": decision.recovery_trigger_pct,
                "recovery_tp_shrink": decision.recovery_tp_shrink,
                "adverse_exit_bars": decision.adverse_exit_bars,
                "adverse_exit_loss_pct": decision.adverse_exit_loss_pct,
                "max_holding_bars": decision.max_holding_bars,
            },
            "entry_ladder_deadline_ms": ladder_deadline_ms,
        }
        await self._repo.update_run(
            run["run_id"],
            status="ENTRY_PENDING",
            side=decision.side,
            signal_json=payload,
            entry_order_id=int(order.get("orderId", 0) or 0),
            entry_client_order_id=client_order_id,
            entry_price=final_price,
            cumulative_notional_usdc=entry_notional,
        )
        await self._repo.log_event(run["run_id"], "entry_placed", {"order": order, "signal": payload})
        await self._notify(
            f"{'🟢' if decision.side == 'LONG' else '🔴'} <b>AUTO {('做多' if decision.side == 'LONG' else '做空')} 已掛 maker 單</b>\n"
            f"Run：<code>{escape(run['run_id'])}</code>\n"
            f"策略：<b>{escape(decision.strategy)}</b> | score=<code>{decision.signal.score}</code>\n"
            f"Entry：<b>${final_price:.4f}</b> | Qty：<code>{escape(str(qty))}</code>\n"
            f"Stop：<b>${float(decision.signal.stop_loss or 0):.4f}</b> | TP：<b>${float(decision.signal.take_profits[0] if decision.signal.take_profits else 0):.4f}</b>{entry_note}\n"
            "若 maker 掛單逾時未成交，本 run 會停止，不會追價硬吃 taker。"
        )

    async def _preflight(self) -> str | None:
        symbol = self._settings.mainnet_symbol
        await self._ensure_fee_guard(symbol)
        position = await self._client.get_position(symbol)
        if position:
            return f"❌ mainnet 已有 {symbol} 持倉，為避免接管錯倉，拒絕啟動 one-run。"
        open_orders = await self._client.get_open_orders(symbol)
        unmanaged = [
            row
            for row in open_orders
            if not str(row.get("clientOrderId") or "").startswith(self._settings.mainnet_client_order_prefix)
        ]
        if unmanaged:
            return f"❌ mainnet 已有 {len(unmanaged)} 筆非本系統掛單，請先人工處理後再啟動。"
        stale_own = [
            row
            for row in open_orders
            if str(row.get("clientOrderId") or "").startswith(self._settings.mainnet_client_order_prefix)
        ]
        for order in stale_own:
            await self._client.cancel_order(symbol, int(order["orderId"]))
        if stale_own:
            logger.warning("mainnet_one_run_stale_orders_cancelled", symbol=symbol, count=len(stale_own))
        return None

    async def _ensure_fee_guard(self, symbol: str) -> None:
        rate = await self._client.get_commission_rate(symbol)
        maker = float(rate.get("makerCommissionRate", rate.get("makerCommission", 1)))
        if self._settings.mainnet_require_zero_maker_fee and abs(maker) > 1e-12:
            raise RuntimeError(f"{symbol} maker fee is {maker}; refusing mainnet one-run")

    async def _load_candles(self, symbol: str) -> list[Candle]:
        rows = await self._client.get_klines(symbol=symbol, interval="1m", limit=300)
        return [Candle.from_binance_kline(row) for row in rows]

    async def _passive_price(self, symbol: str, side: str, signal_price: float) -> Decimal:
        book = await self._client.get_book_ticker(symbol)
        tick = await self._client.price_tick_size(symbol)
        bid = Decimal(str(book["bidPrice"]))
        ask = Decimal(str(book["askPrice"]))
        if side == "BUY":
            price = min(ask - tick, Decimal(str(signal_price)))
        else:
            price = max(bid + tick, Decimal(str(signal_price)))
        return price

    async def _place_post_only_with_retry(
        self,
        symbol: str,
        side: str,
        quantity: float | str,
        signal_price: float,
        client_order_id: str,
        slippage_bps: float,
        fallback_to_gtc: bool = False,
        reduce_only: bool = False,
    ) -> dict:
        """Place a maker (GTX) order with -5022 Post-Only rejection handling.

        On -5022 (would fill as taker), re-quotes with fresh book data and
        retries up to mainnet_gtx_retry_attempts times.
        On each retry, checks that the new price is within slippage_bps of
        the original signal_price.
        If all GTX retries fail:
        - fallback_to_gtc=True → places a GTC limit order at passive price
        - fallback_to_gtc=False → raises BinanceAPIException from last attempt
        If slippage exceeds tolerance at any point, raises GTXSlippageExceeded.
        """
        max_attempts = self._settings.mainnet_gtx_retry_attempts
        last_exc: BinanceAPIException | None = None

        for attempt in range(1, max_attempts + 1):
            # Always get fresh book data for each attempt
            price = await self._passive_price(symbol, side, signal_price)

            # Slippage check (skip on first attempt — initial price is strategy-driven)
            if attempt > 1:
                deviation_bps = abs(float(price) - signal_price) / signal_price * 10_000
                if deviation_bps > slippage_bps:
                    raise GTXSlippageExceeded(
                        f"GTX retry attempt {attempt}: slippage {deviation_bps:.2f} bps "
                        f"exceeds tolerance {slippage_bps} bps "
                        f"(price={float(price)}, signal={signal_price})"
                    )

            try:
                order = await self._client.create_limit_order_raw(
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    price=float(price),
                    time_in_force="GTX",
                    reduce_only=reduce_only,
                    client_order_id=client_order_id,
                )
                if attempt > 1:
                    logger.info(
                        "gtx_retry_success",
                        run_id=client_order_id.split("_")[0],
                        attempt=attempt,
                        side=side,
                        price=float(price),
                        signal_price=signal_price,
                    )
                return order
            except BinanceAPIException as exc:
                if exc.code != -5022:
                    raise
                last_exc = exc
                logger.warning(
                    "gtx_post_only_rejected_retrying",
                    run_id=client_order_id.split("_")[0],
                    attempt=attempt,
                    max_attempts=max_attempts,
                    side=side,
                    price=float(price),
                    signal_price=signal_price,
                    error=str(exc),
                )

        # All GTX retries exhausted — try GTC fallback if enabled
        if fallback_to_gtc:
            price = await self._passive_price(symbol, side, signal_price)
            deviation_bps = abs(float(price) - signal_price) / signal_price * 10_000
            if deviation_bps <= slippage_bps:
                logger.warning(
                    "gtx_fallback_to_gtc",
                    run_id=client_order_id.split("_")[0],
                    side=side,
                    price=float(price),
                    signal_price=signal_price,
                    slippage_bps=deviation_bps,
                )
                return await self._client.create_limit_order(
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    price=float(price),
                    reduce_only=reduce_only,
                    client_order_id=client_order_id,
                )
            # Slippage too large even for GTC fallback
            raise GTXSlippageExceeded(
                f"GTX retries exhausted, GTC fallback slippage {deviation_bps:.2f} bps "
                f"exceeds tolerance {slippage_bps} bps"
            )

        # No GTC fallback — all GTX retries exhausted; surface as a typed
        # exception so _place_entry handles it as ENTRY_REJECTED instead of
        # letting the raw BinanceAPIException propagate to run_cycle as FAILED.
        raise GTXSlippageExceeded(
            f"GTX entry retries exhausted ({max_attempts} attempts, fallback disabled)"
        ) from last_exc

    async def _maybe_requote_entry(
        self,
        run: dict,
        order_id: int | None,
        open_orders: list[dict],
    ) -> bool:
        """Conservatively re-quote a stuck maker entry.

        Returns True if a requote was performed (caller should `return`
        and let the next manage_cycle tick pick up the new state).
        Returns False if no requote was warranted.

        Triggers (all must hold):
          - entry has been on the book for at least
            mainnet_entry_requote_min_age_seconds
          - the mark price has drifted by more than
            mainnet_entry_max_deviation_bps from the entry price
          - the requote cooldown has elapsed since the last requote
            (mainnet_entry_reprice_interval_seconds)
          - the requote count is below mainnet_entry_reprice_max_updates

        On requote, the existing order is cancelled, a fresh passive
        price is computed from the current book, and a new GTX order
        is placed via _place_post_only_with_retry (which itself
        enforces the mainnet_entry_slippage_bps cap and may raise
        GTXSlippageExceeded — that case is logged and swallowed, the
        run keeps its existing order and the next tick retries).
        """
        run_id = run["run_id"]
        symbol = run["symbol"]
        s = self._settings

        # Fetch the order we have on the book so we can read its price
        # and confirm it is still ours.  If order_id is None or the
        # order is not actually in open_orders, there is nothing to
        # requote — the caller will hit the not_open path next tick.
        if not order_id:
            return False
        existing = next(
            (row for row in open_orders if int(row.get("orderId", 0)) == order_id),
            None,
        )
        if existing is None:
            return False

        # Counters (initialised lazily, so first call counts as 0)
        count = self._entry_requote_counts.get(run_id, 0)
        if count >= s.mainnet_entry_reprice_max_updates:
            return False

        # Age gate — uses armed_at_ms (not updated_at_ms) so requote
        # does not interact with the TTL window.
        armed_at = int(run.get("armed_at_ms") or 0)
        if armed_at <= 0:
            return False
        age_ms = int(time.time() * 1000) - armed_at
        if age_ms < s.mainnet_entry_requote_min_age_seconds * 1000:
            return False

        # Cooldown gate
        last_ms = self._entry_requote_last_ms.get(run_id, 0)
        if last_ms and (int(time.time() * 1000) - last_ms) < s.mainnet_entry_reprice_interval_seconds * 1000:
            return False

        # Drift gate — current mark vs. the existing entry price.
        # Fall back to the price recorded on the order if mark is
        # unavailable.
        existing_price = float(existing.get("price") or 0)
        if existing_price <= 0:
            return False
        try:
            book = await self._client.get_book_ticker(symbol)
            mark = float(book.get("bidPrice", 0)) if str(run.get("side") or "").upper() == "SELL" else float(book.get("askPrice", 0))
            if mark <= 0:
                mark = float(existing_price)
        except Exception as exc:  # noqa: BLE001
            logger.warning("entry_requote_book_fetch_failed", run_id=run_id, error=str(exc)[:200])
            return False
        deviation_bps = abs(mark - existing_price) / existing_price * 10_000
        if deviation_bps <= s.mainnet_entry_max_deviation_bps:
            return False

        # Side for the new order — must match the original run side.
        side = str(run.get("side") or "").upper()
        if side not in {"BUY", "SELL"}:
            # Fall back to inferring from position direction if any.
            return False

        # Quantity: re-read from the run (cumulative_notional_usdc /
        # mark gives a fresh estimate, but we re-use the original
        # entry_order_id-quantity to avoid rounding drift).
        entry_notional = float(run.get("cumulative_notional_usdc") or 0)
        if entry_notional <= 0 or mark <= 0:
            return False
        try:
            quantity = await self._client.format_quantity(symbol, entry_notional / mark)
        except Exception as exc:  # noqa: BLE001
            logger.warning("entry_requote_qty_format_failed", run_id=run_id, error=str(exc)[:200])
            return False

        # The slippage cap is enforced inside _place_post_only_with_retry
        # (it raises GTXSlippageExceeded when the new passive price walks
        # past mainnet_entry_slippage_bps of the signal_price we hand
        # it).  We deliberately do NOT add a second pre-check here — by
        # the time we reach this point, the mark has already moved past
        # mainnet_entry_max_deviation_bps, so the deviation vs.
        # signal_price will be at least that big, and the helper's own
        # cap (mainnet_entry_slippage_bps, which is independently
        # tunable) is the real authority.
        #
        # We hand it the existing entry price as the "signal" — that
        # way a successful requote can re-anchor at a price within
        # mainnet_entry_slippage_bps of where the previous order was,
        # rather than reaching all the way back to the original
        # strategy price (which may have been many bps ago).
        signal_price = existing_price

        # Use a unique client_order_id for the requote — Binance
        # rejects reuse of a clientOrderId within 24h.
        new_client_order_id = f"{run_id}_entry_r{count + 1}"
        attempt_no = count + 1

        # 1) Cancel the existing order first.  We do this even if the
        # new order might fail — we are committed to leaving the run
        # with a fresh passive price, and a partial cancel/replace
        # race on Binance is rare for our small size.
        try:
            await self._client.cancel_order(symbol, order_id)
        except Exception as exc:  # noqa: BLE001
            # If the order is already gone (filled or already
            # cancelled), the next tick will sort it out via the
            # position check.  Anything else is fatal for this run.
            logger.warning("entry_requote_cancel_failed", run_id=run_id, order_id=order_id, error=str(exc)[:200])
            return False

        # 2) Place the new maker order via the existing helper, which
        # already implements GTX retry + slippage cap + GTC fallback.
        try:
            new_order = await self._place_post_only_with_retry(
                symbol=symbol,
                side=side,
                quantity=quantity,
                signal_price=signal_price,
                client_order_id=new_client_order_id,
                slippage_bps=s.mainnet_entry_slippage_bps,
                fallback_to_gtc=s.mainnet_entry_fallback_to_gtc,
                reduce_only=False,
            )
        except GTXSlippageExceeded as exc:
            # Slippage exceeded mid-retry — log, do NOT bump the
            # requote counter, leave the run with no open order; the
            # next tick will hit the not_open path and complete the
            # run as ENTRY_EXPIRED.
            await self._repo.log_event(
                run_id,
                "entry_requote_skipped",
                {"reason": "gtx_slippage", "detail": str(exc)[:300], "attempt": attempt_no},
            )
            logger.info("entry_requote_skipped_gtx", run_id=run_id, attempt=attempt_no, detail=str(exc)[:200])
            return False
        except BinanceAPIException as exc:
            # Some non-5022 failure on the new order — log and bail;
            # the run keeps no open order and the next tick will
            # complete it as ENTRY_EXPIRED.
            await self._repo.log_event(
                run_id,
                "entry_requote_failed",
                {"reason": "binance_error", "code": exc.code, "detail": str(exc)[:300], "attempt": attempt_no},
            )
            logger.warning("entry_requote_failed_binance", run_id=run_id, attempt=attempt_no, code=exc.code, error=str(exc)[:200])
            return False

        # 3) Persist the new order id and bump counters.
        new_order_id = int(new_order.get("orderId", 0) or 0)
        await self._repo.update_run(
            run_id,
            entry_order_id=new_order_id,
            entry_client_order_id=new_client_order_id,
            entry_price=float(new_order.get("price", 0) or existing_price),
        )
        self._entry_requote_counts[run_id] = attempt_no
        self._entry_requote_last_ms[run_id] = int(time.time() * 1000)
        await self._repo.log_event(
            run_id,
            "entry_requoted",
            {
                "attempt": attempt_no,
                "old_order_id": order_id,
                "old_price": existing_price,
                "new_order_id": new_order_id,
                "new_price": float(new_order.get("price", 0) or 0),
                "mark_price": mark,
                "deviation_bps": deviation_bps,
                "new_client_order_id": new_client_order_id,
            },
        )
        await self._notify(
            f"🔁 <b>Entry 已 requote #{attempt_no}</b>\n"
            f"Run：<code>{escape(run_id)}</code>\n"
            f"舊價：<code>${existing_price:.4f}</code> → 新價：<code>${float(new_order.get('price', 0) or 0):.4f}</code>\n"
            f"Mark 偏離：<b>{deviation_bps:.2f} bps</b>（門檻 {s.mainnet_entry_max_deviation_bps:.1f} bps）\n"
            f"剩餘 requote 額度：<b>{s.mainnet_entry_reprice_max_updates - attempt_no}</b>"
        )
        return True

    async def _refresh_partial_fill_state(
        self, run: dict, position: PositionInfo, prev_qty: float = 0.0
    ) -> None:
        run_id = run["run_id"]
        check_tp1 = run_id not in self._partial_taken and run_id in self._partial_order_armed
        check_mid = run_id in self._mid_order_armed
        check_final = run_id in self._final_order_armed and run_id not in self._final_taken
        if not check_tp1 and not check_mid and not check_final:
            return
        current_qty = abs(position.position_amt)
        # Use the pre-update qty so detection works in the same cycle the fill lands.
        # run["qty"] is already updated to current_qty by the time we get here.
        ref_qty = prev_qty if prev_qty > 0 else float(run.get("qty") or 0.0)
        # Only infer TP fills when qty SHRANK. Qty growth is a DCA fill — running
        # the inference there falsely marked TP1 filled on run cry3mn_1781028928037
        # (qty 0.121→0.242) and blocked further DCA via _partial_exits.
        if current_qty >= ref_qty - 1e-9:
            return
        open_orders = await self._client.get_open_orders(position.symbol)
        if check_tp1:
            partial_open = any(
                str(order.get("clientOrderId") or "") == f"{run_id}{PARTIAL_TP_SUFFIX}"
                for order in open_orders
            )
            if not partial_open:
                qty_closed = self._tp_layer_qty.get(run_id, {}).get("tp1") or max(0.0, ref_qty - current_qty)
                qty_text = await self._client.format_quantity(position.symbol, qty_closed) if qty_closed > 0 else "unknown"
                self._partial_order_armed.discard(run_id)
                await self._repo.log_event(run_id, "partial_exit", {"qty": qty_text, "position_qty": current_qty})
                await self._notify(
                    f"✅ Mainnet one-run 已部分獲利了結：<code>{escape(run_id)}</code> qty=<code>{escape(str(qty_text))}</code>"
                )
        if check_mid:
            mid_open = any(
                str(order.get("clientOrderId") or "") == f"{run_id}{MID_TP_SUFFIX}"
                for order in open_orders
            )
            if not mid_open:
                qty_closed = self._tp_layer_qty.get(run_id, {}).get("tp2") or max(0.0, ref_qty - current_qty)
                qty_text = await self._client.format_quantity(position.symbol, qty_closed) if qty_closed > 0 else "unknown"
                self._mid_order_armed.discard(run_id)
                await self._repo.log_event(run_id, "mid_exit", {"qty": qty_text, "position_qty": current_qty})
                await self._notify(
                    f"✅ Mainnet one-run TP2 +{self._settings.mainnet_mid_tp_pct*100:.2f}% 已出場：<code>{escape(run_id)}</code> qty=<code>{escape(str(qty_text))}</code>"
                )
        if check_final:
            final_open = any(
                str(order.get("clientOrderId") or "") == f"{run_id}{FINAL_TP_SUFFIX}"
                for order in open_orders
            )
            if not final_open:
                qty_closed = self._tp_layer_qty.get(run_id, {}).get("tp3") or max(0.0, ref_qty - current_qty)
                qty_text = await self._client.format_quantity(position.symbol, qty_closed) if qty_closed > 0 else "unknown"
                self._final_order_armed.discard(run_id)
                await self._repo.log_event(run_id, "final_exit", {"qty": qty_text, "position_qty": current_qty})
                await self._notify(
                    f"✅ Mainnet one-run TP3 (signal) 已出場：<code>{escape(run_id)}</code> qty=<code>{escape(str(qty_text))}</code>"
                )

    async def _sync_take_profit_orders(self, run: dict, position: PositionInfo, signal: dict) -> None:
        run_id = run["run_id"]
        side = str(run.get("side") or signal.get("side") or position.position_direction).upper()
        if side not in {"LONG", "SHORT"}:
            return
        current_qty = abs(position.position_amt)
        if current_qty <= 0:
            return
        close_side = "SELL" if position.position_direction == "LONG" else "BUY"
        desired = await self._desired_take_profit_orders(run, position, signal, close_side)
        existing_orders = await self._client.get_open_orders(position.symbol)
        existing_tp = [
            order for order in existing_orders
            if str(order.get("clientOrderId") or "").startswith(f"{run_id}_tp")
        ]
        current_qty = abs(position.position_amt)
        if self._take_profit_orders_match(existing_tp, desired, current_qty):
            return
        for order in existing_tp:
            try:
                await self._client.cancel_order(position.symbol, int(order["orderId"]))
            except BinanceAPIException as exc:
                if exc.code in {-2011, -2022}:
                    logger.info(
                        "tp_cancel_order_not_found",
                        run_id=run_id,
                        order_id=int(order["orderId"]),
                        code=exc.code,
                        msg=exc.message,
                    )
                else:
                    raise
        for client_order_id, qty, price in desired:
            try:
                await self._client.create_reduce_only_limit_order(
                    position.symbol,
                    close_side,
                    qty,
                    price,
                    client_order_id=client_order_id,
                    post_only=True,
                )
            except BinanceAPIException as exc:
                if exc.code == -2022:
                    # Position is gone (race with exchange-side SL/TP fill).
                    # Stop placing TP orders; next cycle will detect flat.
                    logger.info(
                        "tp_order_reduce_only_rejected_position_gone",
                        run_id=run_id,
                        client_order_id=client_order_id,
                        code=exc.code,
                    )
                    return
                if exc.code == -5022 and self._settings.mainnet_tp_fallback_to_gtc:
                    # Market is already past this TP level.  Re-quote as a
                    # POST_ONLY at the passive top-of-book instead of crossing
                    # as taker: the old GTC fallback filled instantly at market
                    # and the taker fee ate ~65% of the layer's gross (run
                    # cry3mn_1781048052462: fee 0.019 on +0.029 gross).  The
                    # passive quote fills on the next touch at a BETTER price
                    # with 0 maker fee; SL/TRAIL remain the safety exits, so
                    # giving up the guaranteed instant fill only risks upside,
                    # never the floor.
                    book = await self._client.get_book_ticker(position.symbol)
                    book_price = (
                        float(book["askPrice"]) if close_side == "SELL" else float(book["bidPrice"])
                    )
                    logger.warning(
                        "tp_post_only_rejected_requote_book",
                        run_id=run_id,
                        client_order_id=client_order_id,
                        tp_price=price,
                        book_price=book_price,
                        side=close_side,
                    )
                    try:
                        await self._client.create_reduce_only_limit_order(
                            position.symbol,
                            close_side,
                            qty,
                            book_price,
                            client_order_id=client_order_id,
                            post_only=True,
                        )
                    except BinanceAPIException as requote_exc:
                        if requote_exc.code == -2022:
                            logger.info(
                                "tp_requote_reduce_only_rejected_position_gone",
                                run_id=run_id,
                                client_order_id=client_order_id,
                                code=requote_exc.code,
                            )
                            return
                        if requote_exc.code == -5022:
                            # Book moved between fetch and place; leave this
                            # level for the next sync cycle to re-quote.
                            logger.info(
                                "tp_requote_post_only_rejected_retry_next",
                                run_id=run_id,
                                client_order_id=client_order_id,
                            )
                            continue
                        raise
                else:
                    raise
        # Record each level's placed qty so fill notifications report the per-
        # level fill (not the whole position drop, which double-counts when
        # several levels fill in the same cycle).
        layer_qty = self._tp_layer_qty.setdefault(run_id, {})
        for client_order_id, qty, _ in desired:
            if client_order_id.endswith(PARTIAL_TP_SUFFIX):
                layer_qty["tp1"] = float(qty)
            elif client_order_id.endswith(MID_TP_SUFFIX):
                layer_qty["tp2"] = float(qty)
            elif client_order_id.endswith(FINAL_TP_SUFFIX):
                layer_qty["tp3"] = float(qty)
        if any(client_order_id.endswith(PARTIAL_TP_SUFFIX) for client_order_id, _, _ in desired):
            self._partial_order_armed.add(run_id)
        if any(client_order_id.endswith(MID_TP_SUFFIX) for client_order_id, _, _ in desired):
            self._mid_order_armed.add(run_id)
        if any(client_order_id.endswith(FINAL_TP_SUFFIX) for client_order_id, _, _ in desired):
            self._final_order_armed.add(run_id)
        await self._repo.log_event(
            run_id,
            "take_profit_synced",
            {
                "orders": [
                    {"client_order_id": client_order_id, "qty": qty, "price": price}
                    for client_order_id, qty, price in desired
                ]
            },
        )

    async def _desired_take_profit_orders(
        self,
        run: dict,
        position: PositionInfo,
        signal: dict,
        close_side: str,
    ) -> list[tuple[str, str, float]]:
        run_id = run["run_id"]
        current_qty = abs(position.position_amt)
        if current_qty <= 0:
            return []
        full_tp_price = float(signal.get("take_profit") or 0.0)
        # TP3 is always derived from the LIVE cost basis (ladder fill or
        # post-DCA average) × tp_pct.  signal.take_profit is an absolute price
        # computed from the SIGNAL price, so a ladder fill 3bp below it
        # silently pushed TP3 to 9bp from the actual entry — run
        # cry3mn_1781048052462 peaked at 1638.03 and missed the signal-priced
        # TP3 1638.32 by 0.29, while the cost-basis TP3 (1637.82) would have
        # filled.  The absolute price is kept only as fallback when tp_pct is
        # missing from the signal.
        current_avg = position.entry_price
        tp_pct = float(signal.get("wildcat", {}).get("tp_pct") or 0.0)
        
        # Determine the shrink factor
        shrink = 1.0
        dca_count = self._recovery_counts.get(run_id, 0)
        if dca_count > 0:
            # settings is the live knob; signal value (from preset) is fallback.
            shrink = float(self._settings.mainnet_recovery_tp_shrink or signal.get("wildcat", {}).get("recovery_tp_shrink") or 0.55)
            shrink = max(0.0, min(1.0, shrink))
            tp_pct *= shrink

        if tp_pct > 0 and current_avg > 0:
            if position.position_direction == "LONG":
                full_tp_price = current_avg * (1 + tp_pct)
            elif position.position_direction == "SHORT":
                full_tp_price = current_avg * (1 - tp_pct)
        partial_price = self._partial_take_profit_price(position, shrink)
        mid_price = self._mid_take_profit_price(position, shrink)

        # Cap partial_price and mid_price at full_tp_price to prevent inverted orders leaving a tail
        if full_tp_price > 0:
            if position.position_direction == "LONG":
                if partial_price > 0:
                    partial_price = min(partial_price, full_tp_price)
                if mid_price > 0:
                    mid_price = min(mid_price, full_tp_price)
            elif position.position_direction == "SHORT":
                if partial_price > 0:
                    partial_price = max(partial_price, full_tp_price)
                if mid_price > 0:
                    mid_price = max(mid_price, full_tp_price)

        orders: list[tuple[str, str, float]] = []
        remaining_qty = current_qty
        if (
            self._settings.mainnet_partial_exit_pct > 0
            and partial_price > 0
            and abs(partial_price - full_tp_price) > 0.01
        ):
            partial_qty_raw = current_qty * self._settings.mainnet_partial_exit_pct
            partial_qty = await self._client.format_quantity(position.symbol, partial_qty_raw)
            if float(partial_qty) > 0:
                orders.append((f"{run_id}{PARTIAL_TP_SUFFIX}", partial_qty, partial_price))
                remaining_qty = max(0.0, current_qty - float(partial_qty))
        if (
            mid_price > 0
            and self._settings.mainnet_mid_exit_pct > 0
            and remaining_qty > 0
            and abs(mid_price - full_tp_price) > 0.01
        ):
            mid_qty_raw = remaining_qty * self._settings.mainnet_mid_exit_pct
            mid_qty = await self._client.format_quantity(position.symbol, mid_qty_raw)
            if float(mid_qty) > 0:
                orders.append((f"{run_id}{MID_TP_SUFFIX}", mid_qty, mid_price))
                remaining_qty = max(0.0, remaining_qty - float(mid_qty))
        if full_tp_price > 0 and remaining_qty > 0:
            # When TP2 (mid) is disabled, apply mid_exit_pct to TP3 so the
            # remaining fraction is left unallocated for TRAIL to handle.
            final_exit_frac = (
                self._settings.mainnet_mid_exit_pct
                if mid_price <= 0 and self._settings.mainnet_mid_exit_pct > 0
                else 1.0
            )
            final_qty_raw = remaining_qty * final_exit_frac
            final_qty = await self._client.format_quantity(position.symbol, final_qty_raw)
            if float(final_qty) > 0:
                orders.append((f"{run_id}{FINAL_TP_SUFFIX}", final_qty, full_tp_price))
        return orders

    def _partial_take_profit_price(self, position: PositionInfo, shrink: float = 1.0) -> float:
        pct = self._settings.mainnet_partial_tp_pct * shrink
        if position.position_direction == "LONG":
            return position.entry_price * (1 + pct)
        if position.position_direction == "SHORT":
            return position.entry_price * (1 - pct)
        return 0.0

    def _mid_take_profit_price(self, position: PositionInfo, shrink: float = 1.0) -> float:
        mid_pct = self._settings.mainnet_mid_tp_pct * shrink
        if mid_pct <= 0:
            return 0.0
        if position.position_direction == "LONG":
            return position.entry_price * (1 + mid_pct)
        if position.position_direction == "SHORT":
            return position.entry_price * (1 - mid_pct)
        return 0.0

    def _take_profit_orders_match(
        self,
        existing_orders: list[dict],
        desired_orders: list[tuple[str, str, float]],
        current_qty: float,
    ) -> bool:
        """Check if existing TP orders match the desired set (price+qty per level).

        Compares each desired order against existing orders by price within a
        tolerance.  If every desired level either (a) has a matching existing
        order with the same qty, or (b) has qty==0 (already filled), the set
        matches and no cancel/rebuild is needed.
        """
        if not desired_orders:
            return len(existing_orders) == 0

        existing_by_price: dict[float, float] = {}
        for o in existing_orders:
            p = float(o.get("price", 0) or 0)
            q = float(o.get("origQty", 0) or 0)
            existing_by_price[p] = existing_by_price.get(p, 0.0) + q

        for _, desired_qty_str, desired_price in desired_orders:
            desired_qty = float(desired_qty_str)
            if desired_qty < 1e-9:
                continue
            matched = False
            for ep, eq in existing_by_price.items():
                if abs(ep - desired_price) < 0.005 and abs(eq - desired_qty) < 1e-9:
                    matched = True
                    break
            if not matched:
                return False

        for o in existing_orders:
            p = float(o.get("price", 0) or 0)
            desired_prices = {dp for _, _, dp in desired_orders if abs(dp - p) < 0.005}
            if not desired_prices:
                return False

        return True

    async def _maybe_recovery(self, run: dict, signal: dict, position: PositionInfo) -> bool:
        if not self._settings.mainnet_recovery_enabled:
            return False
        count = self._recovery_counts.get(run["run_id"], 0)
        if count >= self._settings.mainnet_recovery_steps:
            return False
        # Block DCA after TP1 partial fill: never average into a runner that
        # already booked partial profit.
        if run["run_id"] in self._partial_exits:
            logger.info("dca_blocked_partial_exit", run_id=run["run_id"])
            return False
        # Block DCA within cooldown after a guard block to prevent regime-flicker
        # from briefly re-classifying the market as range and bypassing the guard.
        cooldown_ms = self._settings.mainnet_dca_guard_cooldown_seconds * 1000
        last_block_ms = self._dca_block_times.get(run["run_id"], 0)
        if cooldown_ms > 0 and last_block_ms > 0:
            now_ms = int(time.time() * 1000)
            if now_ms - last_block_ms < cooldown_ms:
                logger.info(
                    "dca_blocked_guard_cooldown",
                    run_id=run["run_id"],
                    remaining_ms=cooldown_ms - (now_ms - last_block_ms),
                )
                return False
        entry_notional = self._settings.mainnet_effective_entry_notional_usdc
        cumulative = float(run.get("cumulative_notional_usdc") or entry_notional)
        if cumulative + entry_notional > self._settings.mainnet_effective_max_cumulative_notional_usdc:
            return False
        # If a pre-placed DCA limit order is already on the book, let it fill.
        if run["run_id"] in self._dca_preloaded:
            return False
        trigger_pct = self._settings.mainnet_recovery_trigger_pct * (count + 1)
        if position.position_direction == "LONG":
            hit = position.mark_price <= position.entry_price * (1 - trigger_pct)
            side = "BUY"
        else:
            hit = position.mark_price >= position.entry_price * (1 + trigger_pct)
            side = "SELL"
        if not hit:
            return False
        # DCA risk gate: averaging down doubles the position (and the loss if SL
        # then triggers).  Block DCA when the adverse move looks like a trend or
        # the momentum has reversed against us, instead of a range pullback.
        # When mainnet_dca_guard_enabled is False the directional guard is OFF
        # (user-chosen 2026-06-09): DCA fires on any trigger hit.  The structural
        # brakes (steps cap, cumulative notional cap, TP1-then-no-DCA) still apply.
        if self._settings.mainnet_dca_guard_enabled:
            candles = await self._load_candles(position.symbol)
            allow_dca, guard_reason = evaluate_dca_guard(candles, position.position_direction)
            if not allow_dca:
                self._dca_block_times[run["run_id"]] = int(time.time() * 1000)
                logger.info(
                    "dca_blocked_by_guard",
                    run_id=run["run_id"],
                    dca_number=count + 1,
                    side=position.position_direction,
                    reason=guard_reason,
                )
                # Persist a DB event so we can later count guard blocks that were
                # SAVES (price kept falling) vs MISFIRES (V-bounce above TP).  Live
                # evidence is 1:1 so far (cry3mn_1781051773405 misfire -0.29 vs
                # cry3mn_1781055344337 save); accumulate samples before deciding
                # whether to keep the guard.  Records mark/entry for the post-hoc
                # counterfactual against the would-be DCA fill and widened SL.
                await self._repo.log_event(run["run_id"], "dca_guard_blocked", {
                    "dca_number": count + 1,
                    "side": position.position_direction,
                    "reason": guard_reason,
                    "mark_price": position.mark_price,
                    "entry_price": position.entry_price,
                    "trigger_pct": trigger_pct,
                    "path": "poll",
                })
                await self._notify(
                    f"🛡️ DCA #{count + 1} 已跳過（風險守門）：<code>{escape(guard_reason)}</code>"
                )
                return False
        open_orders = await self._client.get_open_orders(position.symbol)
        if any(str(row.get("clientOrderId") or "").startswith(f"{run['run_id']}_dca") for row in open_orders):
            return False
        qty = await self._client.format_quantity(
            position.symbol,
            entry_notional / max(position.mark_price, 1e-9),
        )
        client_order_id = f"{run['run_id']}_dca{count + 1}"
        try:
            order = await self._place_post_only_with_retry(
                symbol=position.symbol,
                side=side,
                quantity=qty,
                signal_price=position.mark_price,
                client_order_id=client_order_id,
                slippage_bps=self._settings.mainnet_dca_slippage_bps,
                fallback_to_gtc=False,  # DCA 不用 GTC 追價
                reduce_only=False,
            )
        except (GTXSlippageExceeded, BinanceAPIException) as exc:
            logger.warning(
                "dca_order_failed_skipping",
                run_id=run["run_id"],
                dca_number=count + 1,
                error=str(exc)[:300],
            )
            await self._notify(
                f"⚠️ DCA #{count + 1} 掛單失敗，跳過：<code>{escape(str(exc)[:200])}</code>"
            )
            return False
        self._recovery_counts[run["run_id"]] = count + 1
        await self._repo.update_run(run["run_id"], cumulative_notional_usdc=cumulative + entry_notional)
        await self._repo.log_event(run["run_id"], "recovery_entry_placed", {"order": order, "signal": signal})
        await self._notify(f"🧩 Mainnet one-run 已掛 DCA maker 單 #{count + 1}：<code>{escape(run['run_id'])}</code>")
        return True

    async def _maybe_trailing_exit(
        self,
        run: dict,
        signal: dict,
        position: PositionInfo,
        side: str,
        mark: float,
        entry: float,
        qty: float,
        close_side: str,
    ) -> bool:
        """Lock a runner's gain when it spikes toward TP2 then reverses.

        Mirrors the backtest wildcat_v3_trail_c logic: track the peak favorable
        mark since entry; once the peak reaches arm_frac * tp_pct of the move,
        arm a trailing stop that market-closes the remaining position if mark
        retraces giveback_frac of the peak run.  The exchange-side TP2 fills on
        its own if price reaches it first, so this only fires on the sub-TP
        retracement path.  Returns True if a lock-exit was submitted.
        """
        if not self._settings.mainnet_trail_enabled:
            return False
        tp_pct = float(signal.get("wildcat", {}).get("tp_pct") or 0.0)
        if tp_pct <= 0 or entry <= 0 or mark <= 0 or side not in {"LONG", "SHORT"}:
            return False
        run_id = run["run_id"]
        if run_id in self._trail_exiting:
            # The fast watcher already owns the lock-exit; report handled so
            # the manage cycle skips its SL/ADVERSE/MAX_HOLD close paths.
            return True
        peak = self._trail_peak.get(run_id)
        arm_mfe = tp_pct * self._settings.mainnet_trail_arm_frac
        keep = 1.0 - self._settings.mainnet_trail_giveback_frac
        # E2: the profit floor needs an epsilon margin, not just mark > entry.
        # The zero-margin floor was passed by 0.002 on the 06-10 08:32 loss
        # run — firing AT breakeven leaves nothing for the maker exit to lose
        # before going net negative.  Require at least floor_bp of gain.
        floor_bp = self._settings.mainnet_trail_profit_floor_bp

        if side == "LONG":
            # Check the lock against the peak from prior cycles BEFORE updating
            # it with the current mark (no same-tick lookahead).
            # Profit floor: only lock while clearly ABOVE cost basis.  A V-crash
            # gaps mark from above trail_stop to below entry in one step; firing
            # there would cancel the real SL and stop-loss at maker speed (the 5
            # below-entry TRAIL losses on 06-10, −1.40).  Below the floor, hand
            # back to the SL/DCA path (which can still rescue via averaging).
            if run_id in self._trail_armed and peak is not None:
                trail_stop = entry + (peak - entry) * keep
                if mark <= trail_stop and mark > entry * (1 + floor_bp / 10_000):
                    self._trail_exiting.add(run_id)
                    if await self._close_position(position.symbol, close_side, qty, "TRAIL", run):
                        return True
                    # E3 anchor gate aborted the fire (book already below the
                    # floor) — fall through and keep managing the position.
            new_peak = mark if peak is None else max(peak, mark)
            self._trail_peak[run_id] = new_peak
            if run_id not in self._trail_armed and (new_peak - entry) / entry >= arm_mfe:
                self._trail_armed.add(run_id)
                self._start_trail_watch(run, side, close_side, tp_pct)
        else:
            if run_id in self._trail_armed and peak is not None:
                trail_stop = entry - (entry - peak) * keep
                if mark >= trail_stop and mark < entry * (1 - floor_bp / 10_000):
                    self._trail_exiting.add(run_id)
                    if await self._close_position(position.symbol, close_side, qty, "TRAIL", run):
                        return True
            new_peak = mark if peak is None else min(peak, mark)
            self._trail_peak[run_id] = new_peak
            if run_id not in self._trail_armed and (entry - new_peak) / entry >= arm_mfe:
                self._trail_armed.add(run_id)
                self._start_trail_watch(run, side, close_side, tp_pct)
        return False

    def _start_trail_watch(self, run: dict, side: str, close_side: str, tp_pct: float) -> None:
        """Spawn the fast trail watcher for a run (idempotent).

        Started at ENTRY FILL (not at arming): the watcher tracks the peak and
        arms itself in the 2s loop, so a sub-cycle spike is recorded even before
        the 10s manage cycle wakes.
        """
        run_id = run["run_id"]
        if not self._settings.mainnet_trail_enabled or tp_pct <= 0:
            return
        existing = self._trail_watch_tasks.get(run_id)
        if existing is not None and not existing.done():
            return
        self._trail_watch_tasks[run_id] = asyncio.create_task(
            self._trail_watch_loop(run, side, close_side, tp_pct)
        )

    async def _trail_watch_loop(self, run: dict, side: str, close_side: str, tp_pct: float) -> None:
        """Fast trail watcher: tracks peak, arms, and fires the lock-exit.

        The 10s manage cycle is too coarse for BOTH peak-tracking and the
        lock-exit.  Two separate leaks proved this:
          * cry3mn_1781048052462 peaked 1638.03 (trigger 1637.73) but the dump
            took ~15s and the next cycle woke at 1636.9 — the trail gain was gone.
          * cry3mn_1781054933311 spiked and dumped inside ONE cycle, so arming
            never happened, the peak was never logged, and TRAIL exited at 1640.31
            BELOW the 1640.36 entry.
        Running from entry fill at mainnet_trail_watch_interval_seconds, this loop
        records the peak, arms itself when the peak crosses arm_frac*tp_pct, and
        fires the giveback exit — all on the 2s clock.  The manage-cycle
        _maybe_trailing_exit stays as a backstop; _trail_exiting / _trail_armed /
        _trail_peak are shared so the two paths converge and never double-close.
        """
        run_id = run["run_id"]
        symbol = run["symbol"]
        if not self._settings.mainnet_trail_enabled or tp_pct <= 0:
            return
        interval = max(1, int(self._settings.mainnet_trail_watch_interval_seconds))
        arm_mfe = tp_pct * self._settings.mainnet_trail_arm_frac
        keep = 1.0 - self._settings.mainnet_trail_giveback_frac
        last_entry: float | None = None
        try:
            while True:
                await asyncio.sleep(interval)
                if run_id in self._trail_exiting:
                    # Manage cycle (or a prior iteration) already owns the exit.
                    return
                position = await self._client.get_position(symbol)
                if not position or abs(position.position_amt) < 1e-9:
                    return
                mark = position.mark_price
                entry = position.entry_price  # live cost basis (tracks DCA)
                if entry <= 0 or mark <= 0:
                    continue
                # E1 fast path: a cost-basis change means a DCA layer filled.
                # The old peak vs the NEW (closer-to-market) basis instantly
                # satisfies arm_mfe and fires on the next noise tick — this
                # watcher sees the fill up to 10s before the manage-cycle
                # backstop does, so reset here too: re-accumulate the peak
                # from the current mark and disarm.
                if last_entry is not None and abs(entry - last_entry) / last_entry > 1e-6:
                    self._trail_peak[run_id] = mark
                    self._trail_armed.discard(run_id)
                    logger.info(
                        "trail_reset_on_basis_change",
                        run_id=run_id,
                        old_entry=last_entry,
                        new_entry=entry,
                        mark=mark,
                    )
                    last_entry = entry
                    continue
                last_entry = entry
                peak = self._trail_peak.get(run_id)
                # No-lookahead ordering: check the trigger against the prior peak,
                # then fold in the current mark, then arm if the new peak crossed.
                # Profit floor (same as manage cycle, E2): only lock while at
                # least floor_bp ABOVE cost basis.  Below the floor, leave it to
                # SL/DCA — firing the trail there just stop-losses at maker
                # speed (06-10 −1.40).
                floor_bp = self._settings.mainnet_trail_profit_floor_bp
                if side == "LONG":
                    if run_id in self._trail_armed and peak is not None:
                        trail_stop = entry + (peak - entry) * keep
                        if mark <= trail_stop and mark > entry * (1 + floor_bp / 10_000):
                            if await self._fire_trail_exit(run, close_side, position, peak, mark, trail_stop):
                                return
                            continue
                    new_peak = mark if peak is None else max(peak, mark)
                    self._trail_peak[run_id] = new_peak
                    if run_id not in self._trail_armed and (new_peak - entry) / entry >= arm_mfe:
                        self._trail_armed.add(run_id)
                        logger.info("trail_armed_watcher", run_id=run_id, peak=new_peak, entry=entry)
                else:
                    if run_id in self._trail_armed and peak is not None:
                        trail_stop = entry - (entry - peak) * keep
                        if mark >= trail_stop and mark < entry * (1 - floor_bp / 10_000):
                            if await self._fire_trail_exit(run, close_side, position, peak, mark, trail_stop):
                                return
                            continue
                    new_peak = mark if peak is None else min(peak, mark)
                    self._trail_peak[run_id] = new_peak
                    if run_id not in self._trail_armed and (entry - new_peak) / entry >= arm_mfe:
                        self._trail_armed.add(run_id)
                        logger.info("trail_armed_watcher", run_id=run_id, peak=new_peak, entry=entry)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("trail_watch_failed", run_id=run_id, error=str(exc)[:200])
        finally:
            self._trail_watch_tasks.pop(run_id, None)

    async def _fire_trail_exit(
        self,
        run: dict,
        close_side: str,
        position: PositionInfo,
        peak: float,
        mark: float,
        trail_stop: float,
    ) -> bool:
        """Submit the TRAIL lock-exit from the watcher.  Returns True if the
        close was submitted (caller should stop watching), False if it failed
        and control should fall back to the manage cycle."""
        run_id = run["run_id"]
        self._trail_exiting.add(run_id)
        logger.info("trail_watch_triggered", run_id=run_id, mark=mark, peak=peak, trail_stop=trail_stop)
        try:
            submitted = await self._close_position(
                position.symbol, close_side, abs(position.position_amt), "TRAIL", run
            )
        except Exception:
            # Never strand the run behind the _trail_exiting gate if the close
            # failed — hand control back to the 10s cycle.
            self._trail_exiting.discard(run_id)
            raise
        if not submitted:
            # E3 anchor gate aborted the fire (book already below the profit
            # floor); nothing was cancelled.  Keep watching — SL/DCA still own
            # the downside.
            self._trail_exiting.discard(run_id)
            return False
        return True

    async def _preplace_next_dca(self, run: dict, position: PositionInfo) -> None:
        """Pre-place next DCA as GTC LIMIT immediately after entry/DCA fills.
        The exchange fills it automatically when price reaches the trigger,
        eliminating the 10-second poll lag in fast-moving markets."""
        run_id = run["run_id"]
        if not self._settings.mainnet_recovery_enabled:
            return
        count = self._recovery_counts.get(run_id, 0)
        if count >= self._settings.mainnet_recovery_steps:
            return
        if run_id in self._partial_exits:
            return
        entry_notional = self._settings.mainnet_effective_entry_notional_usdc
        cumulative = float(run.get("cumulative_notional_usdc") or entry_notional)
        if cumulative + entry_notional > self._settings.mainnet_effective_max_cumulative_notional_usdc:
            return
        # Guard: don't pre-place if momentum is already adverse
        if self._settings.mainnet_dca_guard_enabled:
            candles = await self._load_candles(position.symbol)
            allow, guard_reason = evaluate_dca_guard(candles, position.position_direction)
            if not allow:
                logger.info("dca_preplace_skipped_guard", run_id=run_id, reason=guard_reason)
                await self._repo.log_event(run_id, "dca_guard_blocked", {
                    "dca_number": count + 1,
                    "side": position.position_direction,
                    "reason": guard_reason,
                    "mark_price": position.mark_price,
                    "entry_price": position.entry_price,
                    "path": "preplace",
                })
                return
        # Cancel any stale pre-placed order first
        old_oid = self._dca_preloaded.pop(run_id, None)
        if old_oid:
            try:
                await self._client.cancel_order(position.symbol, old_oid)
            except Exception:
                pass
        trigger_pct = self._settings.mainnet_recovery_trigger_pct * (count + 1)
        entry = float(run.get("avg_entry_price") or position.entry_price)
        if position.position_direction == "LONG":
            limit_price = entry * (1 - trigger_pct)
            side = "BUY"
        else:
            limit_price = entry * (1 + trigger_pct)
            side = "SELL"
        try:
            tick = float(await self._client.price_tick_size(position.symbol))
            if tick > 0:
                if side == "BUY":
                    limit_price = math.floor(limit_price / tick) * tick
                else:
                    limit_price = math.ceil(limit_price / tick) * tick
            limit_price = round(limit_price, 8)
        except Exception:
            pass
        qty_str = await self._client.format_quantity(
            position.symbol, entry_notional / max(limit_price, 1e-9)
        )
        client_order_id = f"{run_id}_dca{count + 1}_pre"
        order = None
        try:
            # GTX so the pre-placed DCA never crosses as taker.  A passive limit
            # below market (LONG BUY) is normally accepted; -5022 only fires if
            # price already fell to/through the trigger before the order landed
            # (06-10: cry3mn_1781065747854 / 64156997 each ate 0.080 taker via GTC).
            order = await self._client.create_limit_order_raw(
                symbol=position.symbol,
                side=side,
                quantity=qty_str,
                price=limit_price,
                time_in_force="GTX",
                reduce_only=False,
                client_order_id=client_order_id,
            )
        except BinanceAPIException as exc:
            if exc.code == -5022:
                # Price already at/through the trigger — re-quote GTX at the
                # passive top-of-book so it still fills on the next downtick, but
                # as maker (0 fee) instead of crossing.
                try:
                    book = await self._client.get_book_ticker(position.symbol)
                    passive_price = round(
                        float(book["bidPrice"]) if side == "BUY" else float(book["askPrice"]),
                        8,
                    )
                    order = await self._client.create_limit_order_raw(
                        symbol=position.symbol,
                        side=side,
                        quantity=qty_str,
                        price=passive_price,
                        time_in_force="GTX",
                        reduce_only=False,
                        client_order_id=client_order_id,
                    )
                    logger.info(
                        "dca_preplace_post_only_rejected_requote_book",
                        run_id=run_id, limit=limit_price, book=passive_price, side=side,
                    )
                    limit_price = passive_price
                except Exception as requote_exc:  # noqa: BLE001
                    logger.warning("dca_preload_failed", run_id=run_id, error=str(requote_exc)[:200])
                    return
            else:
                # -2019 margin insufficient (待辦 #12) and other rejects land here.
                logger.warning("dca_preload_failed", run_id=run_id, error=str(exc)[:200])
                return
        except Exception as exc:  # noqa: BLE001
            logger.warning("dca_preload_failed", run_id=run_id, error=str(exc)[:200])
            return
        order_id = int(order.get("orderId", 0))
        self._dca_preloaded[run_id] = order_id
        await self._repo.log_event(
            run_id,
            "dca_preloaded",
            {"order_id": order_id, "price": limit_price, "qty": qty_str, "step": count + 1},
        )
        logger.info("dca_preloaded", run_id=run_id, step=count + 1, price=limit_price, qty=qty_str)

    def _hit_stop(self, side: str, mark: float, sl_price: float) -> bool:
        hit_sl = mark <= sl_price if side == "LONG" else mark >= sl_price
        return sl_price > 0 and hit_sl

    async def _close_position(self, symbol: str, side: str, qty: float, reason: str, run: dict) -> bool:
        """Cancel all open SL/TP orders then market-close the position.
        The STOP_MARKET order armed at entry handles normal SL execution on
        the exchange side; this path is the software backup (ADVERSE_EXIT,
        MAX_HOLD, or _hit_stop fallback).

        Returns True when the close was submitted (or the position is already
        gone).  Returns False ONLY on the TRAIL anchor-gate abort below — in
        that case nothing was cancelled and the caller must keep managing the
        position (no market fallback).
        """
        run_id = run["run_id"]
        if reason == "TRAIL":
            # E3 anchor gate: the trigger checks the MARK price, but the maker
            # exit executes against the BOOK (SELL rests at the bid).  In a
            # fast dump the bid can already sit below cost basis while mark is
            # still a hair above it — proceeding would tear down the real SL
            # and pre-placed DCA, then chase the book into a loss.  Verify the
            # passive anchor clears the profit floor BEFORE cancelling any
            # protection; if it does not, abort the fire entirely and hand the
            # position back to the SL/DCA path (everything is still armed).
            try:
                pos = await self._client.get_position(symbol)
                cost_basis = (
                    float(pos.entry_price)
                    if pos
                    else float(run.get("avg_entry_price") or 0.0)
                )
                book = await self._client.get_book_ticker(symbol)
                anchor = float(book["bidPrice"]) if side == "SELL" else float(book["askPrice"])
                floor_bp = self._settings.mainnet_trail_profit_floor_bp
                floor_price = (
                    cost_basis * (1 + floor_bp / 10_000)
                    if side == "SELL"
                    else cost_basis * (1 - floor_bp / 10_000)
                )
                if cost_basis > 0 and (
                    (side == "SELL" and anchor < floor_price)
                    or (side == "BUY" and anchor > floor_price)
                ):
                    self._trail_exiting.discard(run_id)
                    logger.info(
                        "trail_fire_aborted_anchor_floor",
                        run_id=run_id,
                        anchor=anchor,
                        cost_basis=cost_basis,
                        floor_price=floor_price,
                        side=side,
                    )
                    await self._repo.log_event(
                        run_id,
                        "trail_fire_aborted_anchor_floor",
                        {
                            "anchor": anchor,
                            "cost_basis": cost_basis,
                            "floor_price": floor_price,
                            "side": side,
                        },
                    )
                    return False
            except Exception as exc:  # noqa: BLE001
                # Gate is best-effort: if the book/position fetch fails, the
                # trigger's mark-based floor already passed, so proceed.
                logger.warning("trail_anchor_gate_check_failed", run_id=run_id, error=str(exc)[:200])
        # Cancel any pre-placed DCA limit on EVERY exit path.  A resting GTC/GTX
        # DCA can otherwise fill DURING the close and re-open a position nobody
        # manages — run cry3mn_1781063317906 exited via TRAIL while its DCA
        # preplace was still live, and the manage cycle was short-circuited by
        # _trail_exiting the whole time (no SL re-arm, no fill tracking).
        dca_oid = self._dca_preloaded.pop(run_id, None)
        if dca_oid:
            try:
                await self._client.cancel_order(symbol, dca_oid)
            except Exception:  # noqa: BLE001 — already gone / filled is fine
                pass
        # For TRAIL exits, keep TP orders alive so a price bounce back to the TP
        # level can still capture profit while the maker reprice loop is running.
        # SL / ADVERSE / MAX_HOLD cancel everything first (hard close path).
        if reason != "TRAIL":
            await self._cancel_take_profit_orders(symbol, run_id)
        await self._cancel_stop_loss_order(symbol, run_id)
        qty_str = await self._client.format_quantity(symbol, qty)

        # TRAIL profit-lock: the runner is in profit and not racing a stop, so
        # try a reduce-only POST_ONLY (maker, 0 fee) exit first to save the
        # taker fee.  If it does not fill within the TTL we fall through to the
        # market close below.  SL/ADVERSE/MAX_HOLD always skip this and use the
        # guaranteed market close.
        if reason == "TRAIL" and self._settings.mainnet_trail_exit_use_maker:
            if await self._try_trail_maker_exit(symbol, side, qty_str, run):
                return True

        # Always market-close; STOP_MARKET on the exchange is already cancelled above.
        try:
            order = await self._client.create_market_order(
                symbol,
                side,
                qty_str,
                reduce_only=True,
                client_order_id=f"{run_id}_close",
            )
        except BinanceAPIException as exc:
            if exc.code == -2022:
                # Position already closed by exchange-side SL before software
                # could act. Let the next cycle's 'not position' path handle it.
                logger.info(
                    "market_close_reduce_only_rejected_position_gone",
                    run_id=run_id,
                    reason=reason,
                    code=exc.code,
                )
                return True
            raise
        await self._repo.log_event(run_id, "close_submitted", {"reason": reason, "order": order})
        await self._repo.update_run(run_id, status="CLOSING", exit_reason=reason)
        await self._notify(f"🏁 Mainnet one-run 已送出平倉：<code>{escape(run_id)}</code> reason=<b>{escape(reason)}</b>")
        return True

    async def _try_trail_maker_exit(self, symbol: str, side: str, qty_str: str, run: dict) -> bool:
        """Lock a TRAIL exit at maker fee, falling back to market on timeout.

        Places a reduce-only POST_ONLY limit at the passive top-of-book and,
        for up to mainnet_trail_exit_maker_ttl_seconds, re-prices it every
        mainnet_trail_exit_reprice_seconds to chase the book so a moving market
        does not strand the order at a stale price (Run 61139 ate a taker fee
        because the single static quote never re-anchored after the bid moved).
        Returns True if the position went flat (maker filled — 0 fee).  On
        timeout or placement rejection, cancels the resting order and returns
        False so the caller market-closes whatever remains (reduce_only caps
        the qty, so a partial maker fill is handled safely).
        """
        run_id = run["run_id"]
        client_order_id = f"{run_id}_trail"

        async def _anchor() -> float:
            # A SELL exit rests at/above the best bid, a BUY exit at/below the
            # best ask — always on the passive side of the book.
            book = await self._client.get_book_ticker(symbol)
            return float(book["bidPrice"]) if side == "SELL" else float(book["askPrice"])

        async def _place(price: float) -> dict | None:
            try:
                return await self._place_post_only_with_retry(
                    symbol=symbol,
                    side=side,
                    quantity=qty_str,
                    signal_price=price,
                    client_order_id=client_order_id,
                    slippage_bps=self._settings.mainnet_tp_slippage_bps,
                    fallback_to_gtc=False,
                    reduce_only=True,
                )
            except (GTXSlippageExceeded, BinanceAPIException) as exc:
                logger.warning(
                    "trail_maker_place_failed_fallback_market",
                    run_id=run_id,
                    side=side,
                    error=str(exc)[:200],
                )
                await self._repo.log_event(
                    run_id, "trail_maker_place_failed", {"error": str(exc)[:300]}
                )
                return None

        async def _cancel_resting() -> None:
            try:
                open_orders = await self._client.get_open_orders(symbol)
                for o in open_orders:
                    if str(o.get("clientOrderId") or "") == client_order_id:
                        await self._client.cancel_order(symbol, int(o["orderId"]))
            except BinanceAPIException as exc:
                logger.warning("trail_maker_cancel_failed", run_id=run_id, error=str(exc)[:200])

        async def _flat() -> bool:
            position = await self._client.get_position(symbol)
            return position is None or abs(position.position_amt) < 1e-9

        # Cost basis for the floors below — never place or chase the maker exit
        # through our own entry (+ the E2 epsilon: at zero margin the exit can
        # only break even before fees).
        pos0 = await self._client.get_position(symbol)
        cost_basis = float(pos0.entry_price) if pos0 else float(run.get("avg_entry_price") or 0.0)
        floor_bp = self._settings.mainnet_trail_profit_floor_bp
        floor_price = (
            cost_basis * (1 + floor_bp / 10_000)
            if side == "SELL"
            else cost_basis * (1 - floor_bp / 10_000)
        )

        anchor = await _anchor()
        # E3 (teardown race): the upstream anchor gate in _close_position passed,
        # but SL/TP/DCA teardown takes ~1s and a fast dump can drop the bid
        # through the floor in that window.  The SL is already gone here, so we
        # cannot abort back to managed state — lock what remains at market NOW
        # instead of resting a maker below cost and chasing it down.
        if cost_basis > 0 and (
            (side == "SELL" and anchor < floor_price)
            or (side == "BUY" and anchor > floor_price)
        ):
            logger.info(
                "trail_maker_initial_anchor_floor_market",
                run_id=run_id,
                anchor=anchor,
                cost_basis=cost_basis,
                floor_price=floor_price,
                side=side,
            )
            await self._repo.log_event(
                run_id, "trail_maker_chase_floor",
                {"anchor": anchor, "cost_basis": cost_basis, "floor_price": floor_price, "initial": True},
            )
            return False
        order = await _place(anchor)
        if order is None:
            return False
        logger.info(
            "trail_maker_order_placed",
            run_id=run_id,
            side=side,
            qty=qty_str,
            price=anchor,
            order_id=order.get("orderId"),
        )
        await self._repo.log_event(run_id, "trail_maker_placed", {"order": order, "anchor": anchor})
        await self._notify(
            f"🪝 TRAIL 鎖利改掛 maker（0 手續費）：<code>{escape(run_id)}</code> @ <b>${anchor:.4f}</b>"
        )

        ttl = max(0, int(self._settings.mainnet_trail_exit_maker_ttl_seconds))
        reprice_every = max(1, int(self._settings.mainnet_trail_exit_reprice_seconds))
        # One tick of tolerance so we only re-place when the book has genuinely
        # walked away from our resting quote, not on every micro-jitter.
        try:
            tick = float(await self._client.price_tick_size(symbol))
        except (BinanceAPIException, ValueError, TypeError):
            tick = 0.0
        reprice_threshold = tick if tick > 0 else 0.0
        deadline = time.monotonic() + ttl
        last_reprice = time.monotonic()
        while time.monotonic() < deadline:
            await asyncio.sleep(1.0)
            if await _flat():
                logger.info("trail_maker_filled", run_id=run_id)
                await self._repo.log_event(run_id, "trail_maker_filled", {})
                await self._repo.update_run(run_id, status="CLOSING", exit_reason="TRAIL")
                await self._notify(
                    f"🎯 TRAIL maker 鎖利已成交（省 taker 費）：<code>{escape(run_id)}</code>"
                )
                return True
            # Chase the book: if it has moved past our resting quote by more than
            # a tick, cancel and re-place at the fresh passive anchor.
            if time.monotonic() - last_reprice >= reprice_every:
                last_reprice = time.monotonic()
                new_anchor = await _anchor()
                # Chase floor: if the passive book has fallen through the profit
                # floor (cost basis + E2 epsilon), stop giving up ticks — cancel
                # and market-close now so we lock the remaining gain instead of
                # riding the maker exit into a loss (the SL was already cancelled
                # when this TRAIL close began).
                if cost_basis > 0 and (
                    (side == "SELL" and new_anchor < floor_price)
                    or (side == "BUY" and new_anchor > floor_price)
                ):
                    logger.info(
                        "trail_maker_chase_floor_market",
                        run_id=run_id,
                        anchor=new_anchor,
                        cost_basis=cost_basis,
                        floor_price=floor_price,
                        side=side,
                    )
                    await self._repo.log_event(
                        run_id, "trail_maker_chase_floor",
                        {"anchor": new_anchor, "cost_basis": cost_basis, "floor_price": floor_price},
                    )
                    await _cancel_resting()
                    return False
                if abs(new_anchor - anchor) > reprice_threshold:
                    await _cancel_resting()
                    if await _flat():
                        # Filled in the gap between cancel and re-check.
                        logger.info("trail_maker_filled", run_id=run_id)
                        await self._repo.log_event(run_id, "trail_maker_filled", {})
                        await self._repo.update_run(run_id, status="CLOSING", exit_reason="TRAIL")
                        await self._notify(
                            f"🎯 TRAIL maker 鎖利已成交（省 taker 費）：<code>{escape(run_id)}</code>"
                        )
                        return True
                    reorder = await _place(new_anchor)
                    if reorder is None:
                        # Re-placement rejected — bail to the market fallback.
                        return False
                    anchor = new_anchor
                    await self._repo.log_event(
                        run_id, "trail_maker_repriced", {"anchor": new_anchor}
                    )
        # TTL elapsed without a full fill — cancel the resting maker order and
        # let the caller market-close the remainder (reduce_only caps the qty).
        logger.info("trail_maker_timeout_fallback_market", run_id=run_id, ttl_seconds=ttl)
        await self._repo.log_event(run_id, "trail_maker_timeout", {"ttl_seconds": ttl})
        await _cancel_resting()
        return False

    async def _place_stop_loss_maker(
        self,
        symbol: str,
        side: str,
        qty_str: str,
        sl_price: float,
        run_id: str,
        reason: str,
        run: dict,
    ) -> None:
        """Place a STOP_MARKET algo order as the stop-loss.

        A GTC LIMIT at sl_price (below market for LONG) is a crossing order —
        it fills immediately as taker, not as a resting maker order.  Only
        STOP_MARKET correctly waits for price to reach sl_price before filling.
        """
        try:
            order = await self._client.create_stop_market_sl_order(
                symbol=symbol,
                side=side,
                quantity=qty_str,
                stop_price=sl_price,
            )
        except BinanceAPIException as exc:
            logger.warning("sl_stop_market_place_failed_fallback_market", run_id=run_id, error=str(exc)[:200])
            fallback = await self._client.create_market_order(
                symbol, side, qty_str, reduce_only=True, client_order_id=f"{run_id}_close"
            )
            await self._repo.log_event(run_id, "close_submitted", {"reason": reason, "order": fallback})
            await self._repo.update_run(run_id, status="CLOSING", exit_reason=reason)
            await self._notify(f"🏁 Mainnet one-run 已送出平倉（SL 掛單失敗，市價）：<code>{escape(run_id)}</code> reason=<b>{escape(reason)}</b>")
            return

        await self._repo.log_event(run_id, "sl_placed", {"order": order, "sl_price": sl_price})
        await self._notify(
            f"🛑 <b>Stop-Loss 已掛</b>\n"
            f"Run：<code>{escape(run_id)}</code>\n"
            f"觸發價：<b>${sl_price:.4f}</b> | Qty：<code>{qty_str}</code>"
        )

    async def _cancel_take_profit_orders(self, symbol: str, run_id: str) -> None:
        open_orders = await self._client.get_open_orders(symbol)
        for order in open_orders:
            client_order_id = str(order.get("clientOrderId") or "")
            if client_order_id.startswith(f"{run_id}_tp"):
                await self._client.cancel_order(symbol, int(order["orderId"]))

    async def _cancel_stop_loss_order(self, symbol: str, run_id: str) -> None:
        """Cancel STOP_MARKET algo SL order(s) for this run."""
        self._sl_order_ids.pop(run_id, None)  # clear any stale entry
        try:
            algo_orders = await self._client.get_open_algo_orders(symbol)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cancel_sl_algo_get_failed", run_id=run_id, error=str(exc)[:200])
            return
        for o in (algo_orders or []):
            if not o.get("reduceOnly", True):
                continue
            algo_id = o.get("algoId")
            client_algo_id = o.get("clientAlgoId")
            try:
                await self._client.cancel_algo_order(symbol, algo_id=algo_id, client_algo_id=client_algo_id)
                logger.info("cancel_sl_algo_ok", run_id=run_id, algo_id=algo_id, client_algo_id=client_algo_id)
            except BinanceAPIException as exc:
                logger.warning("cancel_sl_algo_failed", run_id=run_id, algo_id=algo_id, code=exc.code, error=str(exc)[:200])

    async def _cancel_all_run_orders(self, symbol: str, run_id: str) -> None:
        """Cancel every residual order for this run.

        Two distinct surfaces must be swept:
          1. Regular open orders (TP limit orders) — get_open_orders +
             cancel_order, matched by clientOrderId prefix.
          2. Conditional/algo orders (STOP_MARKET SL) — these live in the
             separate openAlgoOrders endpoint and are handled by
             _cancel_stop_loss_order (cancel_algo_order).
        """
        try:
            open_orders = await self._client.get_open_orders(symbol)
        except Exception as exc:
            logger.warning("cancel_all_run_orders_get_failed", run_id=run_id, error=str(exc)[:200])
            open_orders = []
        for order in open_orders:
            cid = str(order.get("clientOrderId") or "")
            if cid.startswith(run_id):
                oid = int(order["orderId"])
                try:
                    await self._client.cancel_order(symbol, oid)
                    logger.info("cancel_all_run_orders_ok", run_id=run_id, client_order_id=cid, order_id=oid)
                except BinanceAPIException as exc:
                    logger.warning("cancel_all_run_orders_failed", run_id=run_id, client_order_id=cid, order_id=oid, code=exc.code)
        # Also sweep the STOP_MARKET SL, which is an algo order on a separate endpoint.
        await self._cancel_stop_loss_order(symbol, run_id)

    async def _finish_flat_run(self, run: dict, reason: str) -> None:
        # Cancel all residual orders (SL STOP_MARKET, TP, etc.) for this run.
        # Use the broad sweep (_cancel_all_run_orders) first so we never leave
        # dangling orders even if the exact clientOrderId suffix matching fails.
        await self._cancel_all_run_orders(run["symbol"], run["run_id"])
        # Drop per-run trailing state so the dicts do not grow unbounded across
        # loop chains.
        dca_pre_oid = self._dca_preloaded.pop(run["run_id"], None)
        if dca_pre_oid:
            try:
                await self._client.cancel_order(run["symbol"], dca_pre_oid)
            except Exception:
                pass
        self._trail_peak.pop(run["run_id"], None)
        self._trail_armed.discard(run["run_id"])
        watch_task = self._trail_watch_tasks.pop(run["run_id"], None)
        if watch_task is not None and not watch_task.done():
            watch_task.cancel()
        self._trail_exiting.discard(run["run_id"])
        self._dca_block_times.pop(run["run_id"], None)
        self._partial_exits.discard(run["run_id"])
        self._final_taken.discard(run["run_id"])
        self._final_order_armed.discard(run["run_id"])
        self._tp_layer_qty.pop(run["run_id"], None)
        summary = await self._build_run_summary(run)
        # Determine exit_reason.  Priority:
        #   1. Explicit reason already written by _close_position (SL/TRAIL/ADVERSE_EXIT/MAX_HOLD_*)
        #   2. If flat_detected (exchange-side close we didn't initiate), check algo
        #      orders to see if STOP_MARKET SL fired; otherwise infer from TP fills.
        #   3. The caller's reason as last resort.
        explicit = run.get("exit_reason")
        if explicit and explicit != "flat_detected":
            exit_reason = explicit
        elif reason != "flat_detected":
            exit_reason = reason
        else:
            inferred = summary["exit_reason"]
            if summary["realized_pnl_usdc"] < -1e-6:
                # flat_detected with a loss is almost always a STOP_MARKET fill
                # (algo order, clientOrderId mismatch) that _infer_flat_exit_reason
                # can't see. Override to SL regardless of inferred value.
                exit_reason = "SL"
            else:
                exit_reason = inferred or reason
        await self._repo.update_run(
            run["run_id"],
            qty=summary["qty"],
            realized_pnl_usdc=summary["realized_pnl_usdc"],
            commission_usdc=summary["commission_usdc"],
        )
        await self._repo.complete_run(run["run_id"], "COMPLETED", exit_reason)
        await self._repo.log_event(run["run_id"], "completed", {"reason": exit_reason})
        # Loop progress: increment completed and compute position label.
        in_loop = self._loop_total > 0
        if in_loop:
            self._loop_completed += 1
        position_label = (
            f" ({self._loop_completed}/{self._loop_total})" if in_loop else ""
        )
        # If SL exit (or flat_detected with a loss) and we are in a loop,
        # set cooldown for this (side, strategy_label) so the chain-arm
        # skips the next same signal.  flat_detected is almost always a
        # STOP_MARKET fill that wasn't captured in time; treating it as SL
        # for cooldown purposes prevents consecutive loss runs.
        from_loop_chain = run.get("params", {}).get("actor") == "telegram_loop" or in_loop
        # Loss is judged on NET PnL (realized − commission), not exit_reason: a
        # TRAIL that fires below cost basis is a real loss too, and tonight those
        # were the bulk of the bleed but never tripped the SL-only cooldown.
        _net_pnl = float(summary["realized_pnl_usdc"]) - float(summary["commission_usdc"])
        _is_loss_exit = _net_pnl < 0
        if from_loop_chain and _is_loss_exit:
            self._loss_streak += 1
            side = str((run.get("params") or {}).get("side") or run.get("side") or "").upper()
            strategy = run.get("strategy_label") or ""
            if side and strategy:
                # Escalate: base + step*(streak-1) → 3, 8, 13… minutes.
                cooldown_minutes = (
                    self._loop_cooldown_minutes
                    + self._settings.mainnet_loop_cooldown_step_minutes * (self._loss_streak - 1)
                )
                cooldown_until = int(time.time() * 1000) + cooldown_minutes * 60_000
                self._loop_cooldowns[(side, strategy)] = cooldown_until
                logger.info(
                    "loop_loss_cooldown_set",
                    run_id=run["run_id"],
                    loss_streak=self._loss_streak,
                    cooldown_minutes=cooldown_minutes,
                    net_pnl=_net_pnl,
                )
        elif from_loop_chain and not _is_loss_exit:
            # Net win — reset the escalation.
            self._loss_streak = 0
        # Loop loss protection: accumulate the chain's NET PnL and break the
        # loop once it reaches the user-set cap (Telegram 🛡 buttons).  Judged
        # on net (realized − commission), same basis as the cooldown above.
        protection_tripped = False
        if in_loop:
            self._loop_net_pnl += _net_pnl
            if (
                self._loop_loss_cap > 0
                and self._loop_completed < self._loop_total
                and self._loop_net_pnl <= -self._loop_loss_cap
            ):
                protection_tripped = True
                logger.info(
                    "loop_loss_protection_tripped",
                    run_id=run["run_id"],
                    loop_net_pnl=self._loop_net_pnl,
                    loss_cap=self._loop_loss_cap,
                    completed=self._loop_completed,
                    total=self._loop_total,
                )
                await self._repo.log_event(
                    run["run_id"],
                    "loop_loss_protection_tripped",
                    {
                        "loop_net_pnl": self._loop_net_pnl,
                        "loss_cap": self._loop_loss_cap,
                        "completed": self._loop_completed,
                        "total": self._loop_total,
                    },
                )
        loop_footer = ""
        if protection_tripped:
            finished_run_ids = list(self._loop_run_ids)
            remaining = self._loop_total - self._loop_completed
            loop_net = self._loop_net_pnl
            self._loop_total = 0
            self._loop_completed = 0
            self._loop_run_ids = []
            self._loop_net_pnl = 0.0
            try:
                loop_runs = await self._repo.get_runs_by_ids(finished_run_ids)
                stats_text = self._build_loop_stats(loop_runs)
                stats_block = f"\n\n📊 <b>Loop 統計 ({len(finished_run_ids)} runs)</b>\n{stats_text}"
            except Exception:  # noqa: BLE001
                stats_block = ""
            loop_footer = (
                f"\n🛡 <b>Loop 虧損保護觸發</b>：累計淨損益 <b>{loop_net:+.4f}</b>"
                f" ≤ −{self._loop_loss_cap:.2f} USDC，已停止剩餘 <b>{remaining}</b> 個 run。"
                f"{stats_block}"
            )
        elif in_loop and self._loop_completed < self._loop_total:
            remaining = self._loop_total - self._loop_completed
            loop_footer = (
                f"\n🔁 還剩 <b>{remaining}</b> 個 run，即將自動 arm 下一個。"
            )
        elif in_loop and self._loop_completed >= self._loop_total:
            finished_run_ids = list(self._loop_run_ids)
            self._loop_total = 0
            self._loop_completed = 0
            self._loop_run_ids = []
            self._loop_net_pnl = 0.0
            try:
                loop_runs = await self._repo.get_runs_by_ids(finished_run_ids)
                stats_text = self._build_loop_stats(loop_runs)
                loop_footer = (
                    f"\n🎯 全部 run 已完成，loop 結束。"
                    f"\n\n📊 <b>Loop 統計 ({len(finished_run_ids)} runs)</b>\n{stats_text}"
                )
            except Exception:  # noqa: BLE001
                loop_footer = "\n🎯 全部 run 已完成，loop 結束。"
        standby_line = (
            "\n🔁 自動 arm 下一個 run。"
            if (in_loop and not protection_tripped and self._loop_completed < self._loop_total)
            else "\n自動交易已回到待命，不會自動開下一單。"
        )
        await self._notify(
            f"🏁 <b>Mainnet one-run 已完成{position_label}</b>\n"
            f"Run：<code>{escape(run['run_id'])}</code>\n"
            f"結果：<code>{escape(str(exit_reason))}</code>\n"
            f"最大倉位：<code>{summary['qty']:.6f}</code>\n"
            f"已實現損益：<b>${summary['realized_pnl_usdc']:.4f}</b>\n"
            f"手續費：<b>${summary['commission_usdc']:.4f}</b>"
            f"{standby_line}"
            f"{loop_footer}"
        )
        # If loop continues, auto-arm the next run.  This must happen AFTER
        # the COMPLETED notification so the user sees the prior run's
        # summary first.  The new run will be ARMED; it will wait for the
        # next wildcat signal.  If the (side, strategy) is still in cooldown,
        # the arm is deferred and resumed by run_cycle once it expires.
        if in_loop and not protection_tripped and self._loop_completed < self._loop_total:
            side = str((run.get("params") or {}).get("side") or run.get("side") or "").upper()
            strategy = run.get("strategy_label") or ""
            await self._try_arm_next_loop_run(side, strategy, run["run_id"])

    @staticmethod
    def _build_loop_stats(runs: list[dict]) -> str:
        import json as _json  # local import to avoid top-level churn
        from collections import defaultdict

        completed = [r for r in runs if r.get("status") == "COMPLETED"]
        entry_failed = [r for r in runs if r.get("status") in {"ENTRY_EXPIRED", "ENTRY_REJECTED"}]

        n = len(completed)
        if n == 0:
            return f"（無已完成的 run，進場失敗 {len(entry_failed)} 次）"

        wins = [r for r in completed if (r.get("realized_pnl_usdc") or 0) >= 0]
        n_wins = len(wins)
        wr_pct = n_wins / n * 100

        total_pnl = sum((r.get("realized_pnl_usdc") or 0) for r in completed)
        total_comm = sum((r.get("commission_usdc") or 0) for r in completed)
        net_pnl = total_pnl - total_comm

        pnl_sign = "+" if net_pnl >= 0 else ""
        gross_sign = "+" if total_pnl >= 0 else ""

        lines = [
            f"{'▲' if net_pnl >= 0 else '▼'} 淨 PnL：<b>{pnl_sign}{net_pnl:.4f}</b>  │  勝率：<b>{n_wins}/{n} ({wr_pct:.0f}%)</b>",
            f"毛利：{gross_sign}{total_pnl:.4f}  │  手續費：{total_comm:.4f}",
        ]
        if entry_failed:
            lines.append(f"進場失敗（未成交）：{len(entry_failed)} 次")

        # Exit-type distribution: e.g. TRAIL 5(+0.180) ｜ TP 3(+0.095) ｜ SL 2(-0.140)
        exit_pnl: dict[str, list[float]] = defaultdict(list)
        for r in completed:
            reason = r.get("exit_reason") or "?"
            exit_pnl[reason].append(r.get("realized_pnl_usdc") or 0)
        if exit_pnl:
            parts = []
            for reason, pnls in sorted(exit_pnl.items(), key=lambda x: -len(x[1])):
                s = sum(pnls)
                sign = "+" if s >= 0 else ""
                parts.append(f"{reason} {len(pnls)}次({sign}{s:.3f})")
            lines.append("出場：" + " ｜ ".join(parts))

        # Strategy distribution: from signal_json.reasons
        strat_counts: dict[str, int] = defaultdict(int)
        for r in completed:
            try:
                sig = _json.loads(r.get("signal_json") or "{}")
                reasons = sig.get("reasons", [])
                if any("rescue" in x for x in reasons):
                    strat_counts["rescue"] += 1
                elif any("catchup" in x for x in reasons):
                    strat_counts["catchup"] += 1
                else:
                    strat = next(
                        (x.split(":")[1] for x in reasons if x.startswith("wildcat:")),
                        "?",
                    )
                    strat = (
                        strat.replace("S1_BB_RSI", "S1")
                        .replace("S5_Stoch", "S5")
                        .replace("S2_SuperTrend", "S2")
                    )
                    strat_counts[strat] += 1
            except Exception:  # noqa: BLE001
                strat_counts["?"] += 1
        if strat_counts:
            strat_parts = [f"{k} {v}" for k, v in sorted(strat_counts.items(), key=lambda x: -x[1])]
            lines.append("策略：" + " ｜ ".join(strat_parts))

        return "\n".join(lines)

    async def _advance_loop_after_entry_failure(self, run: dict, reason: str) -> None:
        """Advance the loop when a run ends during the *entry stage* (no fill).

        Reached from signal_timeout / entry_ttl_expired / entry_not_open /
        slippage_exceeded — paths that go through complete_run and never reach
        _finish_flat_run.  Without this the loop would stall (Bug 9).

        Per product decision: an entry-stage failure *consumes* one loop slot
        and advances to the next run.  There is no position and no PnL, so no
        cooldown is applied (cooldown is only for SL losses).  For a non-loop
        (single) run this is a no-op.
        """
        self._entry_guard_notified.discard(run["run_id"])
        if self._loop_total <= 0:
            return  # single run, nothing to chain
        self._loop_completed += 1
        side = str((run.get("params") or {}).get("side") or run.get("side") or "").upper()
        strategy = run.get("strategy_label") or ""
        if self._loop_completed < self._loop_total:
            remaining = self._loop_total - self._loop_completed
            # entry_ttl_expired means price moved away from our maker bid (likely
            # ran up for LONG).  Impose a 30-second pause before re-arming to
            # prevent immediately chasing the move at a worse price.
            if reason == "entry_ttl_expired" and side and strategy:
                cooldown_until = int(time.time() * 1000) + 30_000
                self._loop_cooldowns[(side, strategy)] = max(
                    self._loop_cooldowns.get((side, strategy), 0), cooldown_until
                )
            await self._notify(
                f"🔁 <b>Loop run entry 階段結束</b>（{escape(reason)}），未成交，消耗 1 次。\n"
                f"進度：<b>{self._loop_completed}/{self._loop_total}</b>，還剩 {remaining} 個，arm 下一個。"
            )
            await self._try_arm_next_loop_run(side, strategy, run["run_id"])
        else:
            finished_run_ids = list(self._loop_run_ids)
            n_total = self._loop_total
            n_done = self._loop_completed
            self._loop_total = 0
            self._loop_completed = 0
            self._loop_run_ids = []
            try:
                loop_runs = await self._repo.get_runs_by_ids(finished_run_ids)
                stats_text = self._build_loop_stats(loop_runs)
                stats_block = f"\n\n📊 <b>Loop 統計 ({len(finished_run_ids)} runs)</b>\n{stats_text}"
            except Exception:  # noqa: BLE001
                stats_block = ""
            await self._notify(
                f"🏁 <b>Loop 全部結束</b>（最後一個 run entry 階段結束：{escape(reason)}）。\n"
                f"進度：<b>{n_done}/{n_total}</b>。自動交易回到待命。"
                f"{stats_block}"
            )

    async def _try_arm_next_loop_run(self, side: str, strategy: str, prev_run_id: str) -> None:
        """Arm the next loop run, honoring the (side, strategy) cooldown.

        If the cooldown is still active, the pending arm is recorded in
        self._loop_resume so run_cycle can resume it once the cooldown expires.
        This is the fix for the loop stalling forever after a cooldown skip.
        """
        if not (self._loop_total > 0 and self._loop_completed < self._loop_total):
            return
        try:
            now_ms = int(time.time() * 1000)
            cooldown_remaining = 0
            cooldown_until = 0
            if side and strategy:
                cooldown_until = self._loop_cooldowns.get((side, strategy), 0)
                if cooldown_until > now_ms:
                    cooldown_remaining = (cooldown_until - now_ms) // 1000
            if cooldown_remaining > 0:
                # Defer: record the pending arm so run_cycle resumes it later.
                self._loop_resume = {
                    "side": side,
                    "strategy": strategy,
                    "prev_run_id": prev_run_id,
                    "resume_at_ms": cooldown_until,
                }
                logger.info(
                    "mainnet_one_run_loop_cooldown_skip",
                    side=side,
                    strategy=strategy,
                    cooldown_remaining_seconds=cooldown_remaining,
                    completed=self._loop_completed,
                    total=self._loop_total,
                )
                await self._notify(
                    f"⏳ <b>Cooldown 中，跳過 arm</b>\n"
                    f"方向：<b>{escape(side)}</b> / 策略：<b>{escape(strategy)}</b>\n"
                    f"冷卻剩 <b>{cooldown_remaining}s</b>，到期後自動 arm 下一 run。\n"
                    f"目前進度：<b>{self._loop_completed}/{self._loop_total}</b>\n"
                    "Loop 保留中，cooldown 到期後會自動繼續。"
                )
                return
            # Cooldown clear — arm the next run now.  Build a new run directly,
            # bypassing the "already have active run" guard.
            preflight_error = await self._preflight()
            if preflight_error:
                await self._notify(
                    f"❌ <b>Loop 自動 arm 失敗</b>\n"
                    f"前一個 run：<code>{escape(prev_run_id)}</code>\n"
                    f"原因：<code>{escape(preflight_error[:300])}</code>\n"
                    "Loop 已中止，請手動確認。"
                )
                self._loop_total = 0
                self._loop_completed = 0
                self._loop_run_ids = []
                self._loop_resume = None
                return
            next_run_id = f"{self._settings.mainnet_client_order_prefix}_{int(time.time() * 1000)}"
            self._loop_run_ids.append(next_run_id)
            next_index = self._loop_completed + 1
            next_params = {
                "actor": "telegram_loop",
                "symbol": self._settings.mainnet_symbol,
                "strategy": self._settings.mainnet_strategy_label,
                "equity_cap_usdc": self._settings.mainnet_equity_cap_usdc,
                "initial_notional_usdc": self._settings.mainnet_effective_entry_notional_usdc,
                "max_cumulative_notional_usdc": self._settings.mainnet_effective_max_cumulative_notional_usdc,
                "leverage": self._settings.mainnet_leverage,
                "maker_first": True,
                "loop_count": self._loop_total,
                "loop_index": next_index,
            }
            await self._repo.create_run(
                {
                    "run_id": next_run_id,
                    "symbol": self._settings.mainnet_symbol,
                    "strategy_label": self._settings.mainnet_strategy_label,
                    "status": "ARMED",
                    "params": next_params,
                }
            )
            await self._repo.log_event(next_run_id, "armed", next_params)
            self._loop_resume = None
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "mainnet_one_run_loop_chain_failed",
                run_id=prev_run_id,
                error=str(exc),
            )
            await self._notify(
                f"❌ <b>Loop 自動 arm 失敗</b>\n"
                f"前一個 run：<code>{escape(prev_run_id)}</code>\n"
                f"錯誤：<code>{escape(str(exc)[:300])}</code>\n"
                "Loop 已中止，請手動確認。"
            )
            self._loop_total = 0
            self._loop_completed = 0
            self._loop_run_ids = []
            self._loop_resume = None
        else:
            # Notify after critical work succeeds; Telegram timeout must NOT abort the loop.
            try:
                await self._notify(
                    f"🔄 <b>Loop 自動 arm 下一個 run</b>\n"
                    f"✅ Mainnet one-run 已啟動 ({next_index}/{self._loop_total})\n"
                    f"Run：<code>{escape(next_run_id)}</code>\n"
                    f"接下來只會等待下一個 wildcat 訊號；沒訊號時不會下單。"
                )
            except Exception as notify_exc:  # noqa: BLE001
                logger.warning(
                    "mainnet_one_run_loop_arm_notify_failed",
                    run_id=next_run_id,
                    error=str(notify_exc),
                )

    async def _maybe_resume_pending_loop(self) -> None:
        """Resume a loop chain-arm previously deferred by a cooldown.

        Called from run_cycle when there is no active run.  Once the recorded
        cooldown expires, arms the next loop run.  This closes the gap where a
        cooldown skip would otherwise leave the loop stalled forever.
        """
        pending = self._loop_resume
        if not pending:
            return
        if not (self._loop_total > 0 and self._loop_completed < self._loop_total):
            self._loop_resume = None
            return
        if int(time.time() * 1000) < int(pending.get("resume_at_ms", 0)):
            return  # cooldown not expired yet
        self._loop_resume = None
        await self._try_arm_next_loop_run(
            pending["side"], pending["strategy"], pending["prev_run_id"]
        )

    async def _build_run_summary(self, run: dict) -> dict[str, float | str | None]:
        orders, trades = await self._load_run_orders_and_trades(run)
        qty = 0.0
        for trade in trades:
            qty = max(qty, float(trade["qty"]))
        for order in orders:
            qty = max(qty, abs(float(order.get("origQty") or 0.0)))
        realized_pnl = sum(float(trade["realized_pnl"]) for trade in trades)
        commission = sum(float(trade["commission"]) for trade in trades)
        # The SL exit is a STOP_MARKET *algo* order whose fill carries an
        # "x-..." clientOrderId (not the "<run_id>_sl" suffix), so the
        # clientOrderId-prefix matching above silently drops the SL close - that
        # is exactly why SL-exit runs reported realized_pnl=0 (A2 fix,
        # 2026-06-08). Add any in-window trades NOT already order-matched;
        # one-run holds a single position per symbol at a time, so an unmatched
        # in-window fill can only be this run's SL close.
        matched_order_ids = {int(order.get("orderId", 0) or 0) for order in orders}
        extra_pnl, extra_comm, extra_qty = await self._window_extra_realized(
            run, matched_order_ids
        )
        realized_pnl += extra_pnl
        commission += extra_comm
        qty = max(qty, extra_qty)
        exit_reason = self._infer_flat_exit_reason(run, orders)
        return {
            "qty": qty,
            "realized_pnl_usdc": realized_pnl,
            "commission_usdc": commission,
            "exit_reason": exit_reason,
        }

    async def _window_extra_realized(
        self, run: dict, matched_order_ids: set[int]
    ) -> tuple[float, float, float]:
        """Sum realized PnL/commission/qty for in-window trades NOT order-matched.

        Captures the algo STOP_MARKET SL fill that clientOrderId matching misses.
        Returns ``(realized_pnl_usdc, commission_usdc, max_qty)``.

        The window starts at ``armed_at_ms`` with NO look-back buffer: in a loop
        the next run can arm ~1s after the previous one completes, so a negative
        buffer would double-count the prior run's SL. Every trade of this run
        (entry, TP, SL) happens strictly after it was armed, so this is exact.
        """
        start_time = int(run.get("armed_at_ms") or 0)
        try:
            trades = await self._client.get_user_trades(
                run["symbol"], start_time=start_time, limit=1000
            )
        except Exception as exc:  # noqa: BLE001 - reporting must not break completion
            logger.warning(
                "window_extra_realized_fetch_failed run=%s error=%s",
                run["run_id"],
                str(exc)[:200],
            )
            return 0.0, 0.0, 0.0
        realized = 0.0
        commission = 0.0
        max_qty = 0.0
        for t in trades:
            if int(t.order_id) in matched_order_ids:
                continue
            realized += float(t.realized_pnl)
            commission += float(t.commission)
            max_qty = max(max_qty, float(t.qty))
        return realized, commission, max_qty

    async def _load_run_orders_and_trades(self, run: dict) -> tuple[list[dict], list[dict[str, float | int | str]]]:
        start_time = max(0, int(run.get("armed_at_ms") or 0) - 60_000)
        orders = await self._client.get_all_orders(run["symbol"], start_time=start_time, limit=1000)
        matching_orders = [
            order for order in orders
            if str(order.get("clientOrderId") or "").startswith(run["run_id"])
        ]
        order_ids = {int(order.get("orderId", 0) or 0) for order in matching_orders}
        api_trades = await self._client.get_user_trades(run["symbol"], start_time=start_time, limit=1000)
        matching_trades = [
            self._normalize_api_trade(trade)
            for trade in api_trades
            if int(trade.order_id) in order_ids
        ]
        if self._trade_repo:
            db_trades = await self._trade_repo.get_trades(run["symbol"], since_ms=start_time, grid_only=False, limit=1000)
            matching_trades.extend(
                self._normalize_db_trade(trade)
                for trade in db_trades
                if int(trade.get("order_id") or 0) in order_ids
            )
        return matching_orders, self._merge_trade_records(matching_trades)

    def _normalize_api_trade(self, trade) -> dict[str, float | int | str]:
        return {
            "trade_id": int(trade.trade_id),
            "order_id": int(trade.order_id),
            "qty": float(trade.qty),
            "realized_pnl": float(trade.realized_pnl),
            "commission": float(trade.commission),
            "time_ms": int(trade.time_ms),
            "commission_asset": str(trade.commission_asset),
        }

    def _normalize_db_trade(self, trade: dict) -> dict[str, float | int | str]:
        return {
            "trade_id": int(trade.get("trade_id") or 0),
            "order_id": int(trade.get("order_id") or 0),
            "qty": float(trade.get("qty") or 0.0),
            "realized_pnl": float(trade.get("realized_pnl") or 0.0),
            "commission": float(trade.get("commission") or 0.0),
            "time_ms": int(trade.get("time_ms") or 0),
            "commission_asset": str(trade.get("commission_asset") or ""),
        }

    def _merge_trade_records(
        self,
        trades: list[dict[str, float | int | str]],
    ) -> list[dict[str, float | int | str]]:
        merged: dict[tuple[int, int], dict[str, float | int | str]] = {}
        for trade in trades:
            key = (int(trade["trade_id"]), int(trade["order_id"]))
            current = merged.get(key)
            if current is None:
                merged[key] = dict(trade)
                continue
            current["qty"] = max(float(current["qty"]), float(trade["qty"]))
            current["time_ms"] = max(int(current["time_ms"]), int(trade["time_ms"]))
            if not float(current["realized_pnl"]) and float(trade["realized_pnl"]):
                current["realized_pnl"] = float(trade["realized_pnl"])
            if not float(current["commission"]) and float(trade["commission"]):
                current["commission"] = float(trade["commission"])
            if not str(current["commission_asset"]) and str(trade["commission_asset"]):
                current["commission_asset"] = str(trade["commission_asset"])
        return sorted(merged.values(), key=lambda trade: (int(trade["time_ms"]), int(trade["trade_id"])))

    def _infer_flat_exit_reason(self, run: dict, orders: list[dict]) -> str | None:
        latest_filled = None
        latest_time = -1
        for order in orders:
            if str(order.get("status", "")).upper() != "FILLED":
                continue
            order_time = int(order.get("updateTime") or order.get("time") or 0)
            if order_time >= latest_time:
                latest_time = order_time
                latest_filled = str(order.get("clientOrderId") or "")
        if not latest_filled:
            return run.get("exit_reason")
        if latest_filled.endswith(FINAL_TP_SUFFIX) or latest_filled.endswith(PARTIAL_TP_SUFFIX) or latest_filled.endswith(MID_TP_SUFFIX):
            return "TP"
        if latest_filled.endswith("_close"):
            return run.get("exit_reason")
        return run.get("exit_reason")

    def _arm_rows(self) -> list[list[InlineKeyboardButton]]:
        """Arm buttons + Telegram-adjustable config rows (💰 notional, 🛡 loss cap).

        The currently selected notional / loss-cap option is marked with ✓.
        """
        notional_now = int(self._settings.mainnet_equity_cap_usdc)
        cap_now = self._loop_loss_cap

        def n_label(v: int) -> str:
            return f"💰${v} ✓" if v == notional_now else f"💰${v}"

        def c_label(v: float) -> str:
            if v <= 0:
                return "🛡關 ✓" if cap_now <= 0 else "🛡關"
            label = f"🛡${v:g}"
            return f"{label} ✓" if abs(cap_now - v) < 1e-9 else label

        return [
            [
                InlineKeyboardButton("啟動 1 run", callback_data="mainnet:arm:1"),
                InlineKeyboardButton("啟動 3 runs", callback_data="mainnet:arm:3"),
            ],
            [
                InlineKeyboardButton("啟動 5 runs", callback_data="mainnet:arm:5"),
                InlineKeyboardButton("啟動 10 runs", callback_data="mainnet:arm:10"),
            ],
            [
                InlineKeyboardButton("啟動 20 runs", callback_data="mainnet:arm:20"),
                InlineKeyboardButton("啟動 30 runs", callback_data="mainnet:arm:30"),
            ],
            [
                InlineKeyboardButton(n_label(v), callback_data=f"mainnet:notional:{v}")
                for v in NOTIONAL_CHOICES
            ],
            [
                InlineKeyboardButton(c_label(v), callback_data=f"mainnet:losscap:{v:g}")
                for v in LOOP_LOSS_CAP_CHOICES
            ],
        ]

    def _buttons(self, active: bool, show_cancel: bool = False) -> InlineKeyboardMarkup:
            logger.info("mainnet_buttons_enter", active=active, loop_total=self._loop_total, show_cancel=show_cancel)
            if active:
                rows = [
                    [InlineKeyboardButton("查詢 one-run 狀態", callback_data="mainnet:status")],
                    [InlineKeyboardButton("取消目前 one-run", callback_data="mainnet:cancel")],
                ]
                if self._loop_total > 0:
                    rows.append(
                        [InlineKeyboardButton("⏹ 停止 loop（不取消目前 run）", callback_data="mainnet:stop_loop")]
                    )
                    markup = InlineKeyboardMarkup(rows)
                    logger.info("mainnet_buttons_exit", active=active, path="early_active_loop", markup=markup is not None)
                    return markup
                rows = self._arm_rows() + [
                    [InlineKeyboardButton("查詢 one-run 狀態", callback_data="mainnet:status")],
                    [InlineKeyboardButton("⏹ 停止 loop", callback_data="mainnet:stop_loop")],
                ]
                markup = InlineKeyboardMarkup(rows)
                logger.info("mainnet_buttons_exit", active=active, path="active_idle", markup=markup is not None)
                return markup
            rows = self._arm_rows() + [
                [InlineKeyboardButton("查詢 one-run 狀態", callback_data="mainnet:status")],
            ]
            if show_cancel:
                rows.append([InlineKeyboardButton("取消目前 one-run", callback_data="mainnet:cancel")])
            rows.append([InlineKeyboardButton("⏹ 停止 loop", callback_data="mainnet:stop_loop")])
            markup = InlineKeyboardMarkup(rows)
            logger.info("mainnet_buttons_exit", active=active, path="idle", markup=markup is not None, show_cancel=show_cancel)
            return markup

    async def _notify(self, text: str) -> None:
        if not self._telegram_app or not self._settings.telegram_chat_id_int:
            return
        # NEVER let a Telegram failure propagate.  _notify is called from inside
        # run_cycle's body, whose exception handler marks the run FAILED and
        # stops managing it.  A telegram.error.TimedOut on a completion notice
        # overwrote a flat +0.163 run as FAILED (cry3mn_1781053774815, 06-10);
        # worse, a timeout on a MID-RUN notice would orphan a live position
        # (manager dead, pre-placed DCA GTC still resting on the exchange).
        # Swallow everything and only log.
        try:
            await self._telegram_app.bot.send_message(
                chat_id=self._settings.telegram_chat_id_int,
                text=text,
                parse_mode="HTML",
            )
        except Exception as exc:
            logger.warning("notify_failed_swallowed", error=str(exc)[:200])
