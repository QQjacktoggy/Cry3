"""Telegram-triggered one-run mainnet validation manager."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from html import escape
from pathlib import Path
from typing import Any, Mapping, Sequence

from binance import BinanceAPIException

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from config.settings import Settings
from scripts.backtest_wildcat_s1s5 import build_features
from src.gridbot.binance.client import BinanceFuturesClient
from src.gridbot.binance.models import PositionInfo
from src.gridbot.storage.repositories import FuturesTradeRepository, MainnetRunRepository
from src.gridbot.mainnet.tp_policy_shadow import (
    CODEX_TP_POLICY_VERSION,
    TP_POLICY_PATH_TTL_S,
    baseline_snapshot_from_order_plan as build_tp_policy_baseline_from_order_plan,
    build_active_sample as build_tp_policy_active_sample,
    build_outcomes as build_tp_policy_outcomes,
)
from src.gridbot.strategy.codex_v1_live import (
    CODEX_V1_VERSION,
    CodexV1Decision,
    build_codex_v1_live_features,
    classify_codex_v133_no_lane_candidate,
    codex_v1_feature_gaps,
    format_codex_v1_telegram_report,
    is_hot_up_extension,
    is_mid_up_extension_short_risk,
    is_stale_short_after_upmove,
    live_preflight_rejections,
    select_codex_v1_lane,
)
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
DCA_ENABLED_CONFIG_KEY = "mainnet_dca_enabled"
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

    CODEX_V1_SHADOW_SAMPLE_COOLDOWN_S = 90
    CODEX_V1_SHADOW_ENTRY_REF_MOVE_BP = 5.0
    CODEX_V1_SHADOW_MAX_SAMPLES_PER_RUN = 12
    CODEX_V1_SHADOW_ENTRY_TTL_S = 180
    CODEX_V1_SHADOW_OUTCOME_TTL_S = 300
    CODEX_TP_POLICY_VERSION = CODEX_TP_POLICY_VERSION
    CODEX_TP_POLICY_SHADOW_ENABLED = True
    CODEX_TP_POLICY_LIVE_OVERRIDE_ENABLED = False
    CODEX_TP_POLICY_PATH_TTL_S = TP_POLICY_PATH_TTL_S
    CODEX_V1_SH_WPR_CANARY_ENABLED = False
    CODEX_V1_SH_WPR_CANARY_MAX_NOTIONAL_USDC = 50.0
    CODEX_V1_SH_WPR_CANARY_MAX_ACTIVE_POSITIONS = 1

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
        if (
            CODEX_V1_VERSION.startswith(("_codex_v1.3.7E", "_codex_v1.3.8", "_codex_v1.3.9", "_codex_v1.4"))
            and bool(getattr(self._settings, "mainnet_codex_tp_policy_live_override_enabled", False))
        ):
            try:
                self._settings.mainnet_codex_tp_policy_live_override_enabled = False
            except Exception as exc:  # noqa: BLE001 - startup guard must never block manager construction.
                logger.warning("codex_v13_tp_override_guard_set_failed", error=str(exc)[:200])
            logger.error(
                "codex_v13_tp_override_forced_off",
                version=CODEX_V1_VERSION,
            )
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
        self._dca_enabled: bool = True  # Telegram-toggleable; persisted in app_config
        # Cumulative NET PnL (realized − commission) across the current loop
        # chain; reset when a new loop is armed or the loop ends/stops.
        self._loop_net_pnl: float = 0.0
        self._protection_sent: set[str] = set()
        self._partial_taken: set[str] = set()
        self._partial_order_armed: set[str] = set()
        self._recovery_counts: dict[str, int] = {}
        self._dca_preloaded: dict[str, int] = {}   # run_id -> pre-placed DCA order_id
        # #25: per-run metadata for the live pre-placed DCA order, used to tell a
        # partial fill from a full layer.  {order_id, intended_qty, base_qty}.
        self._dca_preload_meta: dict[str, dict] = {}
        # V6.5 cap-bookkeeping fix: per-run entry sizing scale (rng15 sweet-zone
        # multiplier).  The cumulative-notional cap must scale with the entry,
        # otherwise a 1.2x entry eats the unscaled cap and silently swallows the
        # last DCA layer.  Lost on restart → falls back to 1.0 (conservative:
        # cap unscaled; exact whenever sweet_scale is 1.0/off).
        self._notional_scale: dict[str, float] = {}
        # P0 (2026-06-11): runs whose DCA guard has fired at least once → DCA
        # is banned for the rest of that run.  Live DB 06-10~06-11: 5 runs
        # where a DCA layer STILL filled after a dca_guard_blocked event went
        # 1W/4L, net −5.58 USDC (avg −1.12/run; cry3mn_1781089775237 −1.83,
        # cry3mn_1781132757415 −2.01, cry3mn_1781148031845 −1.62) vs ≈−0.07
        # avg for clean (never-blocked) DCA fills.  The bad fills landed
        # 1.1~2.8 min after the block — past the 60s guard cooldown — so a
        # timed window cannot close the hole: the guard firing once means the
        # regime is already hostile to averaging into this position.
        self._dca_guard_blocked_runs: set[str] = set()
        # De-dupe for the permanent-ban log line so the 10s manage cycle does
        # not emit dca_blocked_guard_permanent every cycle.
        self._dca_guard_blocked_notified: set[str] = set()
        # P1 drift gate: de-dupe DB events per (run_id, dca_number) — the poll
        # path re-evaluates every 10s and would otherwise flood run_events
        # while the drift persists.  logger.info still fires every evaluation.
        self._dca_drift_event_keys: set[tuple[str, int]] = set()
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
        # W6A no-TP1 dynamic exit variables (v1.2.12):
        self._w6a_price_history: dict[str, list[tuple[float, float]]] = {}
        self._w6a_shadow_recorded: set[str] = set()
        self._w6a_stop_tightened_runs: dict[str, float] = {}
        self._w6a_no_bounce_exiting: set[str] = set()
        self._w6a_post_tp_probe_recorded: set[tuple[str, str]] = set()
        # Per-run, per-level placed TP qty (tp1/tp2/tp3) so a fill notification
        # reports that level's qty rather than the whole position drop.
        self._tp_layer_qty: dict[str, dict[str, float]] = {}
        self._tp1_audit_recorded: set[str] = set()
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
        # Track run ids already notified for rng15 range filter skips.
        self._rng15_guard_notified: set[str] = set()
        # Option A direction throttle (V6.8.5): per-direction timestamps of
        # net-loss exits; once >= loss_count losses occur within the window,
        # that direction is blocked for block_minutes.
        self._dir_loss_times: dict[str, list[float]] = {}   # side -> [loss_ms]
        self._dir_throttle_until: dict[str, float] = {}     # side -> block_until_ms
        self._dir_throttle_notified: set[str] = set()       # run_ids already TG-notified
        # Codex v1 live gate: track run ids already notified to avoid per-cycle
        # Telegram spam while an ARMED run waits for an accepted lane.
        self._codex_v1_guard_notified: set[str] = set()
        self._codex_v1_reprice_shadow: dict[str, dict[str, Any]] = {}
        self._codex_v1_shadow_samples: dict[str, dict[str, Any]] = {}
        self._codex_v1_shadow_outcomes_logged: set[str] = set()
        self._codex_v132_tp_policy_samples: dict[str, dict[str, Any]] = {}
        self._codex_v132_tp_policy_outcomes_logged: set[str] = set()
        self._codex_v132_rehydrated_runs: set[str] = set()
        self._codex_v1_shadow_opportunities: dict[str, dict[str, Any]] = {}
        self._codex_v1_shadow_last_sample_by_scope: dict[str, dict[str, Any]] = {}
        self._codex_v1_shadow_sample_counts_by_run: dict[str, int] = {}
        self._codex_survival_watch_notified: set[str] = set()
        # #24: loop-scoped block — set to a wall-clock ms deadline whenever the
        # rescue spike gate skips a cycle.  While now < deadline, NORMAL S1
        # signals are blocked too (rescue keeps re-evaluating per candle).
        self._spike_block_until_ms: float = 0.0
        self._spike_block_notified: set[str] = set()
        # Stale algo-order id cache — populated by deprecated maker SL path, kept for
        # compatibility; cleared harmlessly in _cancel_stop_loss_order.
        self._sl_order_ids: dict[str, int] = {}

    async def status(self) -> RunStatus:
            await self._ensure_runtime_config_loaded()
            latest = await self._repo.get_latest_run()
            active = await self._repo.get_active_run()
            entry_notional = self._settings.mainnet_effective_entry_notional_usdc
            entry_margin = self._settings.mainnet_effective_entry_margin_usdc
            codex_enabled = self._codex_v1_execution_enabled()
            loss_cap_label = (
                f"−${self._loop_loss_cap:.0f} USDC" if self._loop_loss_cap > 0 else "關閉"
            )
            lines = [
                "🧪 <b>Mainnet One-Run 驗證</b>",
                f"狀態：<b>{'已啟用' if self._settings.mainnet_one_run_enabled else '未啟用'}</b>",
                f"交易對：<code>{escape(self._settings.mainnet_symbol)}</code>",
                f"策略：<code>{escape(self._settings.mainnet_strategy_label)}</code>",
                (
                    f"Codex v1 gate：<b>{'ON' if codex_enabled else 'OFF'}</b>"
                    f"（max ${self._settings.mainnet_codex_v1_max_notional_usdc:.2f}）"
                ),
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
        await self._expire_codex_v1_shadow_samples(active, "telegram_cancel")
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
        if active:
            try:
                await self._rehydrate_codex_v132_tp_policy_samples(active)
            except Exception as exc:  # noqa: BLE001 - restore must never interrupt live run management
                logger.warning(
                    "codex_v132_tp_policy_rehydrate_unhandled",
                    run_id=active.get("run_id"),
                    error=str(exc)[:200],
                )
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
        try:
            raw = await self._config_repo.get(DCA_ENABLED_CONFIG_KEY)
            if raw is not None:
                self._dca_enabled = raw.lower() not in ("false", "0", "off")
                logger.info("mainnet_dca_enabled_config_loaded", dca_enabled=self._dca_enabled)
        except Exception as exc:  # noqa: BLE001
            logger.warning("mainnet_dca_enabled_read_failed", error=str(exc))

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

    async def set_dca_enabled(self, enabled: bool) -> str:
        """Telegram DCA 開/關按鈕：即時切換，重啟後仍有效。"""
        await self._ensure_runtime_config_loaded()
        self._dca_enabled = enabled
        if self._config_repo is not None:
            await self._config_repo.set(DCA_ENABLED_CONFIG_KEY, "true" if enabled else "false")
        logger.info("mainnet_dca_enabled_set", dca_enabled=enabled)
        if enabled:
            return f"✅ DCA 已<b>開啟</b>（最多 {self._settings.mainnet_recovery_steps} 層）"
        return "🚫 DCA 已<b>關閉</b>（run 採純進場、等待 TP / SL，不攤平）"

    def _codex_v1_execution_enabled(self) -> bool:
        """Opt-in bridge from the research policy into mainnet one-run entry."""

        label = str(self._settings.mainnet_strategy_label or "")
        return bool(
            self._settings.mainnet_codex_v1_enabled
            or label
            in {
                CODEX_V1_VERSION,
                "_codex_v1.3.3_fee_and_evidence_quality_fix",
                "codex_v1.3.0_w6a_guarded_200cap",
                "_codex_v1.2.12",
                "codex_v1.2.12",
                "codex_v1.2.11",
                "_codex_v1.2.11",
                "codex_v1.2.10",
                "_codex_v1.2.10",
                "codex_v1.2.9",
                "_codex_v1.2.9",
                "codex_v1.2.8",
                "_codex_v1.2.8",
                "codex_v1.2.7",
                "_codex_v1.2.7",
                "codex_v1.2.6",
                "_codex_v1.2.6",
                "codex_v1.2.5",
                "_codex_v1.2.5",
                "codex_v1",
                "codex_v1.0.1",
                "_codex_v1.0.1",
                "codex_v1.0.0",
                "_codex_v1.0.0",
            }
        )

    @staticmethod
    def _codex_v1_raw_candles(candles: list[Candle]) -> list[dict[str, float | int]]:
        return [
            {
                "time_ms": candle.open_time_ms,
                "open": candle.open,
                "high": candle.high,
                "low": candle.low,
                "close": candle.close,
                "volume": candle.volume,
                "quote_volume": candle.quote_volume,
            }
            for candle in candles
        ]

    async def _build_codex_v1_live_features_for_decision(
        self,
        run: dict,
        decision: WildcatLiveDecision,
        candles: list[Candle],
        *,
        rng15: float,
        drift_bp: float | None,
    ) -> dict[str, Any]:
        raw = self._codex_v1_raw_candles(candles)
        feature_series: Mapping[str, Any] | None = None
        try:
            feature_series = build_features(raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("codex_v1_feature_series_failed", run_id=run["run_id"], error=str(exc)[:200])

        features = build_codex_v1_live_features(
            symbol=run["symbol"],
            strategy=decision.strategy,
            side=decision.side,
            score=decision.signal.score,
            rng15=rng15,
            d30=drift_bp,
            signal=decision.signal,
            candles=raw,
            feature_series=feature_series,
        )
        self._populate_codex_v1_reprice_shadow_features(run, decision, candles, features)
        if candles:
            last_close_ms = int(candles[-1].open_time_ms) + 60_000
            features["feature_age_seconds"] = max(0.0, (time.time() * 1000 - last_close_ms) / 1000)
        features["kill_switch"] = not self._settings.mainnet_one_run_enabled

        try:
            book = await self._client.get_book_ticker(run["symbol"])
            bid = float(book["bidPrice"])
            ask = float(book["askPrice"])
            mid = (bid + ask) / 2
            features["spread_bp"] = (ask - bid) / mid * 1e4 if mid > 0 else 999.0
        except Exception as exc:  # noqa: BLE001
            logger.warning("codex_v1_book_preflight_failed", run_id=run["run_id"], error=str(exc)[:200])
            features["spread_bp"] = 999.0
            features["book_preflight_error"] = str(exc)[:160]

        try:
            rate = await self._client.get_commission_rate(run["symbol"])
            maker_rate = float(rate.get("makerCommissionRate", rate.get("makerCommission", 1)))
            features["maker_fee_bp"] = maker_rate * 1e4
        except Exception as exc:  # noqa: BLE001
            logger.warning("codex_v1_fee_preflight_failed", run_id=run["run_id"], error=str(exc)[:200])
            features["maker_fee_bp"] = 999.0
            features["fee_preflight_error"] = str(exc)[:160]

        try:
            position = await self._client.get_position(run["symbol"])
            features["open_position"] = bool(position and abs(position.position_amt) > 0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("codex_v1_position_preflight_failed", run_id=run["run_id"], error=str(exc)[:200])
            features["open_position"] = True
            features["position_preflight_error"] = str(exc)[:160]

        try:
            open_orders = await self._client.get_open_orders(run["symbol"])
            features["open_entry_order"] = any(
                not self._truthy_order_flag(row.get("reduceOnly")) for row in open_orders
            )
            features["open_reduce_order"] = any(
                self._truthy_order_flag(row.get("reduceOnly")) for row in open_orders
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("codex_v1_open_orders_preflight_failed", run_id=run["run_id"], error=str(exc)[:200])
            features["open_entry_order"] = True
            features["open_reduce_order"] = True
            features["open_orders_preflight_error"] = str(exc)[:160]
        return features

    def _populate_codex_v1_reprice_shadow_features(
        self,
        run: dict,
        decision: WildcatLiveDecision,
        candles: list[Candle],
        features: dict[str, Any],
    ) -> None:
        """Populate live-only shadow features for Codex reprice lanes.

        Backtests measured the path for two bars after the candidate signal
        before allowing the reprice overlay.  Live cannot know that path at the
        first tick, so we start a per-run shadow clock when a matching candidate
        first appears, then fill the features only after the wait elapsed.
        """

        strategy = str(features.get("strategy") or decision.strategy or "")
        side = str(features.get("side") or decision.side or "")
        if strategy != "S1_BB_RSI" or side not in {"LONG", "SHORT"}:
            self._codex_v1_reprice_shadow.pop(run["run_id"], None)
            return

        signal_px = float(decision.signal.price or 0.0)
        if signal_px <= 0 or not candles:
            return

        now_ms = int(time.time() * 1000)
        wait_ms = 2 * 60_000
        # Keep the same shadow timer while the run sees the same strategy/side.
        # Including signal price here reset the timer on normal ETH tick noise
        # and prevented the two-minute live shadow from ever maturing.
        signature = f"{strategy}|{side}"
        shadow = self._codex_v1_reprice_shadow.get(run["run_id"])
        if not shadow or shadow.get("signature") != signature:
            shadow = {
                "signature": signature,
                "start_ms": now_ms,
                "signal_px": signal_px,
                "side": side,
            }
            self._codex_v1_reprice_shadow[run["run_id"]] = shadow

        start_ms = int(shadow["start_ms"])
        elapsed_ms = max(0, now_ms - start_ms)
        setup_age_s = round(elapsed_ms / 1000, 1)
        features["setup_age_sec"] = setup_age_s
        features["setup_started_at_ms"] = start_ms
        features["reprice_wait_elapsed_seconds"] = setup_age_s
        if elapsed_ms < wait_ms:
            features["reprice_wait_remaining_seconds"] = round((wait_ms - elapsed_ms) / 1000, 1)
            return

        end_ms = start_ms + wait_ms
        lows: list[float] = []
        highs: list[float] = []
        for candle in candles:
            open_ms = int(candle.open_time_ms)
            close_ms = open_ms + 60_000
            if close_ms < start_ms or open_ms > end_ms:
                continue
            lows.append(float(candle.low))
            highs.append(float(candle.high))
        if not lows or not highs:
            return

        ref_px = float(shadow.get("signal_px") or signal_px)
        if side == "LONG":
            favorable = (max(highs) - ref_px) / ref_px * 1e4
            adverse = (ref_px - min(lows)) / ref_px * 1e4
        else:
            favorable = (ref_px - min(lows)) / ref_px * 1e4
            adverse = (max(highs) - ref_px) / ref_px * 1e4
        features["reprice_favorable_bp"] = round(favorable, 4)
        features["reprice_adverse_bp"] = round(adverse, 4)
        features["reprice_shadow_start_ms"] = start_ms
        features["reprice_shadow_ref_px"] = round(ref_px, 8)
        logger.info(
            "codex_v1_reprice_shadow_ready",
            run_id=run["run_id"],
            side=side,
            elapsed_s=round(elapsed_ms / 1000, 1),
            favorable_bp=features["reprice_favorable_bp"],
            adverse_bp=features["reprice_adverse_bp"],
        )

    async def _apply_codex_v1_gate(
        self,
        run: dict,
        decision: WildcatLiveDecision,
        candles: list[Candle],
        *,
        rng15: float,
        drift_bp: float | None,
    ) -> tuple[WildcatLiveDecision | None, CodexV1Decision, CodexV1Decision, dict[str, Any]]:
        features = await self._build_codex_v1_live_features_for_decision(
            run,
            decision,
            candles,
            rng15=rng15,
            drift_bp=drift_bp,
        )
        raw_codex_decision = select_codex_v1_lane(features)
        codex_decision = await self._codex_v136_maybe_promote_nl_near_w1d_live200(
            raw_codex_decision,
            features,
        )
        codex_decision = await self._codex_v139_maybe_promote_reprice_canary(
            decision,
            raw_codex_decision,
            codex_decision,
            features,
        )
        disabled_lanes = {
            item.strip()
            for item in str(self._settings.mainnet_codex_v1_disabled_lanes or "").split(",")
            if item.strip()
        }
        disabled_lanes_upper = {item.upper() for item in disabled_lanes}
        decision_lane_tokens = {
            str(codex_decision.lane or ""),
            str(codex_decision.lane_code or ""),
        }
        decision_lane_tokens.discard("")
        if codex_decision.accepted and (
            bool(decision_lane_tokens & disabled_lanes)
            or bool({item.upper() for item in decision_lane_tokens} & disabled_lanes_upper)
        ):
            codex_decision = replace(
                codex_decision,
                accepted=False,
                reason="codex_v1_lane_disabled",
                size_mult=0.0,
                notional_mult=0.0,
                requested_notional_usdc=0.0,
                risk_tags=tuple(codex_decision.risk_tags) + ("live_lane_disabled",),
            )
        research_block_reason = self._codex_v1_live_research_block_reason(codex_decision, features)
        if research_block_reason:
            policy_tag = research_block_reason
            shadow_lane = None
            metrics = getattr(codex_decision, "metrics", None)
            if research_block_reason == "v130_w2a_shadow_only":
                shadow_lane = "SH_W2A_SHADOW_ONLY"
                metrics = {
                    **(metrics or {}),
                    "policy_note": "v130_w2a_shadow_only",
                    "policy_tag": "v130_w2a_shadow_only",
                    "shadow_lane": shadow_lane,
                }
            elif research_block_reason in {"codex_v1_w6_weak_drift_block", "codex_v1_w6_deep_pullback_block"}:
                shadow_lane = (
                    "SH_W6A_WEAK_DRIFT_BLOCK"
                    if research_block_reason == "codex_v1_w6_weak_drift_block"
                    else "SH_W6A_DEEP_PULLBACK_VWAP30"
                )
                metrics = {
                    **(metrics or {}),
                    "policy_note": research_block_reason,
                    "policy_tag": research_block_reason,
                    "shadow_lane": shadow_lane,
                }
            codex_decision = replace(
                codex_decision,
                accepted=False,
                reason=research_block_reason,
                size_mult=0.0,
                notional_mult=0.0,
                requested_notional_usdc=0.0,
                risk_tags=tuple(codex_decision.risk_tags) + ("live_research_block",),
                metrics=metrics,
                policy_tag=policy_tag,
                shadow_lane=shadow_lane,
            )

        if codex_decision.accepted and (codex_decision.lane_code == "W6A" or codex_decision.lane == "w6_lane_s1long_rng38_86_range9_15_e0"):
            entry = self._codex_v1_entry_reference_price(
                decision.signal.price,
                decision.side,
                codex_decision.entry_offset_bp or 0.0,
            )
            stop = decision.signal.stop_loss or 0.0
            tp = entry * (1 + decision.tp_pct) if decision.side == "LONG" else entry * (1 - decision.tp_pct)

            base_notional = self._settings.mainnet_effective_entry_notional_usdc
            requested_notional = max(0.0, base_notional * max(0.0, codex_decision.notional_mult))
            max_notional = self._settings.mainnet_effective_max_cumulative_notional_usdc
            if self._settings.mainnet_codex_v1_max_notional_usdc > 0:
                max_notional = min(max_notional, self._settings.mainnet_codex_v1_max_notional_usdc)
            applied_notional = min(requested_notional, max_notional)
            original_qty = applied_notional / entry if entry > 0 else 0.0

            tp_bp = abs(tp - entry) / entry * 10000 if entry > 0 else 0.0
            sl_bp = abs(entry - stop) / entry * 10000 if entry > 0 else 0.0
            planned_rr = tp_bp / sl_bp if sl_bp > 0 else 0.0
            sl_tp_ratio = sl_bp / tp_bp if tp_bp > 0 else math.inf
            raw_requested_notional = max(
                0.0,
                float(codex_decision.requested_notional_usdc or requested_notional),
            )
            base_notional_mult = max(0.0, float(codex_decision.notional_mult or 0.0))

            fee_rate = float(features.get("maker_fee_bp", 2.0)) / 10000
            fee_est_usdc = entry * original_qty * fee_rate
            gross_tp_usdc = abs(tp - entry) * original_qty
            gross_sl_usdc = abs(entry - stop) * original_qty

            w6a_metrics = {
                "entry": entry,
                "stop": stop,
                "tp": tp,
                "tp_bp": round(tp_bp, 4),
                "sl_bp": round(sl_bp, 4),
                "planned_rr": round(planned_rr, 4),
                "sl_tp_ratio": round(sl_tp_ratio, 4) if math.isfinite(sl_tp_ratio) else None,
                "raw_requested_notional_usdc": round(raw_requested_notional, 4),
                "base_notional_mult": round(base_notional_mult, 4),
                "gross_tp_usdc": round(gross_tp_usdc, 4),
                "gross_sl_usdc": round(gross_sl_usdc, 4),
                "fee_est_usdc": round(fee_est_usdc, 4),
            }

            # Part 1 Payoff Geometry Guard
            bad_payoff = planned_rr < 0.85 or sl_bp > 1.35 * tp_bp or gross_tp_usdc < 3.0 * fee_est_usdc

            def _feature_float(name: str, fallback: str | None = None) -> float | None:
                value = features.get(name)
                if value is None and fallback is not None:
                    value = features.get(fallback)
                try:
                    parsed = float(value) if value is not None else None
                except (TypeError, ValueError):
                    return None
                if parsed is None or not math.isfinite(parsed):
                    return None
                return parsed

            # Part 3 Deep-Down / Capitulation Policy
            side_str = str(decision.side).upper()
            d30_value = _feature_float("d30", "drift30")
            vwap_dist_value = _feature_float("vwap_dist_bp")
            pullback_value = _feature_float("pullback_from_recent_high_bp", "pullback")
            rsi_value = _feature_float("rsi", "rsi14")
            adv3_value = _feature_float("adv3", "adv3_bp")
            rng15_value = _feature_float("rng15")
            score_value = _feature_float("score")
            reprice_wait_value = _feature_float("reprice_wait_elapsed_seconds")
            d30 = d30_value if d30_value is not None else 0.0
            vwap_dist_bp = vwap_dist_value if vwap_dist_value is not None else 0.0
            pullback = pullback_value if pullback_value is not None else 0.0
            rsi = rsi_value if rsi_value is not None else 50.0
            adv3 = adv3_value if adv3_value is not None else 0.0
            rng15 = rng15_value if rng15_value is not None else 50.0
            score = score_value if score_value is not None else 70.0

            deep_down_zone = (
                side_str == "LONG"
                and d30 <= -30.0
                and vwap_dist_bp <= -45.0
                and pullback >= 30.0
            )
            capitulation_bounce_ok = (
                vwap_dist_bp <= -100.0
                and pullback >= 40.0
                and rsi <= 37.5
                and adv3 >= 2.0
                and rng15 <= 85.0
            )

            # Part 4 Negative-Drift Range Trap Guard
            negative_drift_range_trap = (
                side_str == "LONG"
                and d30 <= -30.0
                and score >= 78.0
                and rng15 >= 60.0
                and not capitulation_bounce_ok
            )

            skip_reason = None
            regime = None
            risk_tags_add = []
            policy_note = None
            final_qty = original_qty
            risk_cap_original_qty = original_qty
            risk_cap_final_qty = original_qty
            v137_enabled = bool(getattr(self._settings, "mainnet_codex_v137_w6a_risk_shadow_enabled", True))

            if v137_enabled:
                setup_age_value = _feature_float("setup_age_sec", "reprice_wait_elapsed_seconds")
                reclaimed_value = _feature_float("price_above_or_reclaimed_vwap")
                setup_age_sec = setup_age_value if setup_age_value is not None else 0.0
                no_reclaim = reclaimed_value is None or reclaimed_value <= 0.0
                risk_flags = {
                    "no_reclaim": no_reclaim,
                    "vwap_lte_neg45": vwap_dist_bp <= -45.0,
                    "pullback_gte_25": pullback >= 25.0,
                    "setup_age_gte_300": setup_age_sec >= 300.0,
                    "d30_lte_neg30": d30 <= -30.0,
                }
                risk_score = sum(1 for enabled in risk_flags.values() if enabled)
                stale_hard = setup_age_sec >= 600.0 and no_reclaim and vwap_dist_bp <= -30.0
                missing_required = tuple(
                    name
                    for name, value in {
                        "setup_age_sec": setup_age_value,
                        "d30": d30_value,
                        "vwap_dist_bp": vwap_dist_value,
                        "pullback_from_recent_high_bp": pullback_value,
                        "price_above_or_reclaimed_vwap": reclaimed_value,
                    }.items()
                    if value is None
                )
                default_cap = float(getattr(self._settings, "mainnet_codex_v137_w6a_default_cap_usdc", 50.0))
                max_keep_cap = float(getattr(self._settings, "mainnet_codex_v137_w6a_max_keep_notional_usdc", 200.0))
                risk_score_200_max = int(getattr(self._settings, "mainnet_codex_v137_w6a_200_risk_score_max", 2))
                stale_action = str(
                    getattr(self._settings, "mainnet_codex_v137_w6a_stale_hard_action", "cap50") or "cap50"
                ).strip().lower()
                if stale_action not in {"cap50", "shadow_only"}:
                    stale_action = "cap50"
                eligible_200 = bool(
                    not missing_required
                    and setup_age_sec <= 180.0
                    and ((not no_reclaim) or vwap_dist_bp > -30.0)
                    and pullback < 25.0
                    and d30 > -50.0
                    and risk_score <= risk_score_200_max
                )
                requested_above_default = raw_requested_notional > default_cap + 1e-9
                requested_200_or_more = raw_requested_notional >= max_keep_cap - 1e-9

                live_action = "KEEP_REQUESTED_SIZE"
                shadow_action = "keep"
                v137_applied_cap: float | None = None
                v137_shadow_lane = "SH_W6A_RISK_SCORE_V1"

                if risk_score >= 4:
                    skip_reason = "v137_w6a_risk_score_block"
                    regime = "v137_w6a_risk_score_4plus"
                    policy_note = skip_reason
                    live_action = "BLOCK"
                    shadow_action = "would_block"
                elif stale_hard and stale_action == "shadow_only":
                    skip_reason = "v137_w6a_stale_hard_shadow_only"
                    regime = "v137_w6a_stale_hard"
                    policy_note = skip_reason
                    live_action = "SHADOW_ONLY"
                    shadow_action = "would_shadow_only"
                    v137_shadow_lane = "SH_W6A_STALE_HARD_SHADOW_ONLY"
                else:
                    if stale_hard:
                        v137_applied_cap = default_cap
                        policy_note = "v137_w6a_stale_hard_cap50"
                        live_action = "CAP50"
                        shadow_action = "would_force50"
                    elif risk_score == 3 and requested_above_default:
                        v137_applied_cap = default_cap
                        policy_note = "v137_w6a_risk_score_force50"
                        live_action = "FORCE50"
                        shadow_action = "would_force50"
                    elif missing_required and requested_above_default:
                        v137_applied_cap = default_cap
                        policy_note = "v137_w6a_missing_features_50"
                        live_action = "DOWNGRADE50"
                        shadow_action = "would_force50"
                    elif requested_200_or_more and not eligible_200:
                        v137_applied_cap = default_cap
                        policy_note = "v137_w6a_200_promo_downgrade50"
                        live_action = "DOWNGRADE50"
                        shadow_action = "would_force50"
                        v137_shadow_lane = "SH_W6A_200_PROMO_V2"
                    elif raw_requested_notional > max_keep_cap:
                        v137_applied_cap = max_keep_cap
                        policy_note = "v137_w6a_max_200_cap"
                        live_action = "CAP200"
                        shadow_action = "would_cap200"
                        v137_shadow_lane = "SH_W6A_200_PROMO_V2"
                    elif requested_200_or_more:
                        policy_note = "v137_w6a_200_promo_keep"
                        live_action = "KEEP200"
                        shadow_action = "would_keep200"
                        v137_shadow_lane = "SH_W6A_200_PROMO_V2"
                    elif risk_score == 3:
                        policy_note = "v137_w6a_risk_score_keep50"
                        live_action = "KEEP50"
                        shadow_action = "would_keep50"
                    else:
                        policy_note = "v137_w6a_keep_requested"

                risk_tags_add.extend(
                    tag
                    for tag in (
                        "v137_w6a_risk_shadow",
                        policy_note,
                        "v137_w6a_notional_cap_enforced" if v137_applied_cap is not None else None,
                    )
                    if tag
                )
                w6a_metrics.update(
                    {
                        "policy_note": policy_note,
                        "policy_tag": policy_note,
                        "shadow_lane": v137_shadow_lane,
                        "shadow_policy_ids": [
                            "SH_W6A_RISK_SCORE_V1",
                            "SH_W6A_200_PROMO_V2",
                        ],
                        "setup_age_sec": round(setup_age_sec, 4),
                        "reprice_wait_elapsed_seconds": round(reprice_wait_value, 4) if reprice_wait_value is not None else None,
                        "risk_score": risk_score,
                        "risk_flags": risk_flags,
                        "stale_hard": stale_hard,
                        "stale_hard_action": stale_action,
                        "eligible_200": eligible_200,
                        "live_action": live_action,
                        "shadow_action": shadow_action,
                        "would_block": live_action in {"BLOCK", "SHADOW_ONLY"},
                        "would_force50": live_action in {"CAP50", "FORCE50", "DOWNGRADE50"},
                        "would_keep": live_action.startswith("KEEP"),
                        "v137_missing_required_features": list(missing_required),
                        "applied_notional_cap_usdc": round(v137_applied_cap, 4) if v137_applied_cap is not None else None,
                    }
                )
                if skip_reason is None and v137_applied_cap is not None and entry > 0:
                    cap_qty = max(0.0, v137_applied_cap) / entry
                    if 0.0 < cap_qty < final_qty:
                        final_qty = cap_qty
                        risk_cap_final_qty = final_qty
                        w6a_metrics["v137_cap_enforced"] = True
                        w6a_metrics["v137_cap_final_qty"] = round(final_qty, 8)
            else:
                if skip_reason is None and bad_payoff:
                    skip_reason = "w6a_bad_payoff_geometry_blocked"
                    regime = "w6a_bad_payoff_geometry"
                elif skip_reason is None and deep_down_zone and not capitulation_bounce_ok:
                    skip_reason = "w6a_deep_down_extension_long_blocked"
                    regime = "w6a_deep_down"
                elif skip_reason is None and negative_drift_range_trap and bad_payoff:
                    skip_reason = "w6a_negative_drift_range_trap_blocked"
                    regime = "w6a_negative_drift"
                elif skip_reason is None:
                    if deep_down_zone and capitulation_bounce_ok:
                        policy_note = "w6a_deep_down_capitulation_bounce_allowed"
                        risk_tags_add.append("w6a_capitulation_bounce")
                    target_max_loss = float(self._settings.mainnet_codex_v1_w6a_target_max_gross_loss_usdc)
                    risk_per_unit = abs(entry - stop)
                    qty_cap = target_max_loss / risk_per_unit if risk_per_unit > 0 else original_qty
                    if negative_drift_range_trap:
                        qty_cap = min(qty_cap, original_qty * 0.10)
                        policy_note = "w6a_negative_drift_size_reduced"
                        risk_tags_add.append("w6a_negative_drift_reduced")
                    final_qty = min(original_qty, qty_cap)
                    risk_cap_final_qty = final_qty
                    if final_qty < 0.001:
                        skip_reason = "w6a_risk_cap_too_small"
                        regime = "w6a_risk_control"
                    elif final_qty < original_qty:
                        policy_note = policy_note or "w6a_risk_capped"
                        risk_tags_add.append("w6a_risk_capped")
                    if policy_note:
                        w6a_metrics["policy_note"] = policy_note
                        w6a_metrics["policy_tag"] = policy_note

            if skip_reason:
                codex_decision = replace(
                    codex_decision,
                    accepted=False,
                    reason=skip_reason,
                    regime=regime,
                    size_mult=0.0,
                    notional_mult=0.0,
                    requested_notional_usdc=0.0,
                    risk_tags=tuple(codex_decision.risk_tags) + tuple(risk_tags_add) + ("w6a_guard_blocked",),
                    metrics=w6a_metrics,
                    policy_tag=w6a_metrics.get("policy_tag"),
                    shadow_lane=w6a_metrics.get("shadow_lane"),
                )
            else:
                if final_qty < original_qty:
                    ratio = final_qty / original_qty
                    codex_decision = replace(
                        codex_decision,
                        notional_mult=codex_decision.notional_mult * ratio,
                        size_mult=codex_decision.size_mult * ratio,
                        requested_notional_usdc=codex_decision.requested_notional_usdc * ratio,
                        risk_tags=tuple(codex_decision.risk_tags) + tuple(risk_tags_add),
                        metrics=w6a_metrics,
                        policy_tag=w6a_metrics.get("policy_tag"),
                        shadow_lane=w6a_metrics.get("shadow_lane"),
                    )
                else:
                    codex_decision = replace(
                        codex_decision,
                        risk_tags=tuple(codex_decision.risk_tags) + tuple(risk_tags_add),
                        metrics=w6a_metrics,
                        policy_tag=w6a_metrics.get("policy_tag"),
                        shadow_lane=w6a_metrics.get("shadow_lane"),
                    )
                w6a_metrics["policy_note"] = policy_note
                w6a_metrics["risk_cap_original_qty"] = risk_cap_original_qty
                w6a_metrics["risk_cap_final_qty"] = risk_cap_final_qty
        if codex_decision.accepted and codex_decision.policy_tag == "v134_w6a_weak_drift_50_canary":
            daily_limit = int(getattr(self._settings, "mainnet_codex_v134_w6a_weak_drift_50_canary_daily_limit", 0) or 0)
            if daily_limit > 0:
                canary_count = await self._codex_v134_w6a_weak_drift_canary_count_24h()
                metrics = dict(codex_decision.metrics or {})
                metrics["v134_weak_drift_canary_24h_count"] = canary_count
                metrics["v134_weak_drift_canary_daily_limit"] = daily_limit
                if canary_count >= daily_limit:
                    limit_reason = "v134_w6a_weak_drift_daily_limit_block"
                    metrics["policy_note"] = limit_reason
                    metrics["policy_tag"] = limit_reason
                    metrics["shadow_lane"] = "SH_W6A_WEAK_DRIFT_DAILY_LIMIT"
                    codex_decision = replace(
                        codex_decision,
                        accepted=False,
                        reason=limit_reason,
                        regime="v134_w6a_canary_limit",
                        size_mult=0.0,
                        notional_mult=0.0,
                        requested_notional_usdc=0.0,
                        risk_tags=tuple(codex_decision.risk_tags) + ("v134_w6a_canary_daily_limit",),
                        metrics=metrics,
                        policy_tag=limit_reason,
                        shadow_lane="SH_W6A_WEAK_DRIFT_DAILY_LIMIT",
                    )
                else:
                    codex_decision = replace(codex_decision, metrics=metrics)

        gaps = codex_decision.missing_features if codex_decision.accepted else codex_v1_feature_gaps(features)
        preflight = live_preflight_rejections(features)
        raw_snapshot = self._codex_v1_decision_snapshot(raw_codex_decision, features)
        effective_status = "accepted"
        if not codex_decision.accepted or gaps or preflight:
            reason = codex_decision.reason if not codex_decision.accepted else "codex_v1_preflight_blocked"
            if not codex_decision.accepted:
                effective_status = "blocked"
            elif gaps:
                effective_status = "blocked_missing_features"
            else:
                effective_status = "blocked_preflight"
            effective_snapshot = self._codex_v1_decision_snapshot(
                codex_decision,
                features,
                status=effective_status,
                effective_reason=reason,
                gaps=gaps,
                preflight=preflight,
            )
            await self._repo.log_event(
                run["run_id"],
                "entry_codex_v1_skipped",
                {
                    "reason": reason,
                    "gaps": list(gaps),
                    "preflight": list(preflight),
                    "raw_classifier": raw_snapshot,
                    "effective_execution": effective_snapshot,
                    "decision": asdict(codex_decision),
                    "features": self._codex_v1_payload_features(features),
                },
            )
            await self._start_codex_v1_shadow_sample(
                run,
                decision,
                raw_codex_decision,
                codex_decision,
                features,
                reason=reason,
                effective_status=effective_status,
                gaps=gaps,
                preflight=preflight,
            )
            if run["run_id"] not in self._codex_v1_guard_notified:
                self._codex_v1_guard_notified.add(run["run_id"])
                classifier_lane = escape(str(raw_codex_decision.lane_code or raw_codex_decision.lane or "NONE"))
                await self._notify(
                    "🧪 <b>Codex v1 live gate 暫不進場</b>\n"
                    f"Run：<code>{escape(run['run_id'])}</code>\n"
                    f"Classifier：<code>{classifier_lane}</code>\n"
                    f"Effective：<code>{escape(effective_status)}</code>\n"
                    f"原因：<code>{escape(reason)}</code>\n"
                    + format_codex_v1_telegram_report(features, execution_wired=True)
                    + "\n（續等下一根 K / 下一個符合 lane 的訊號，逾時才前進下一個 loop run）"
                )
            else:
                logger.info(
                    "entry_codex_v1_skip",
                    run_id=run["run_id"],
                    reason=reason,
                    gaps=list(gaps),
                    preflight=list(preflight),
                )
            return None, raw_codex_decision, codex_decision, features

        adjusted = self._apply_codex_v1_decision(decision, codex_decision)
        effective_snapshot = self._codex_v1_decision_snapshot(
            codex_decision,
            features,
            status=effective_status,
            effective_reason="accepted",
        )
        await self._repo.log_event(
            run["run_id"],
            "entry_codex_v1_accepted",
            {
                "raw_classifier": raw_snapshot,
                "effective_execution": effective_snapshot,
                "decision": asdict(codex_decision),
                "features": self._codex_v1_payload_features(features),
            },
        )
        return adjusted, raw_codex_decision, codex_decision, features


    async def _codex_v136_nl_near_w1d_live200_count_24h(self) -> int:
        counter = getattr(self._repo, "count_events_since", None)
        if not callable(counter):
            return 0
        since_ms = int(time.time() * 1000) - 24 * 60 * 60 * 1000
        try:
            return int(
                await counter(
                    "entry_codex_v1_accepted",
                    since_ms,
                    "SH_NL_NEAR_W1D_LONG_LIVE200",
                )
            )
        except Exception as exc:  # noqa: BLE001 - fail open; preflight and one-active-run guards still apply.
            logger.warning("v136_nl_near_w1d_live200_count_failed", error=str(exc)[:200])
            return 0

    async def _codex_v136_nl_near_w1d_live200_loss_guard_24h(self) -> dict[str, Any]:
        getter = getattr(self._repo, "get_completed_runs_since", None)
        if not callable(getter):
            return {"completed_count": 0, "sl_count": 0, "net_pnl_usdc": 0.0}
        since_ms = int(time.time() * 1000) - 24 * 60 * 60 * 1000
        try:
            rows = await getter(since_ms, limit=200)
        except TypeError:
            rows = await getter(since_ms)
        except Exception as exc:  # noqa: BLE001 - fail open; preflight and one-active-run guards still apply.
            logger.warning("v136_nl_near_w1d_live200_loss_guard_failed", error=str(exc)[:200])
            return {"completed_count": 0, "sl_count": 0, "net_pnl_usdc": 0.0}

        lane_code = "SH_NL_NEAR_W1D_LONG_LIVE200"
        completed_count = 0
        sl_count = 0
        net_pnl_usdc = 0.0
        for row in rows or []:
            if not isinstance(row, Mapping):
                continue
            signal_raw = row.get("signal_json")
            if isinstance(signal_raw, str):
                try:
                    signal = json.loads(signal_raw) if signal_raw else {}
                except json.JSONDecodeError:
                    signal = {}
            elif isinstance(signal_raw, Mapping):
                signal = signal_raw
            else:
                signal = {}
            codex = signal.get("codex_v1") if isinstance(signal, Mapping) else {}
            if not isinstance(codex, Mapping):
                codex = {}
            row_lane = str(codex.get("lane_code") or codex.get("lane") or "")
            if row_lane != lane_code:
                continue
            try:
                realized = float(row.get("realized_pnl_usdc") or 0.0)
            except (TypeError, ValueError):
                realized = 0.0
            try:
                commission = float(row.get("commission_usdc") or 0.0)
            except (TypeError, ValueError):
                commission = 0.0
            completed_count += 1
            net_pnl_usdc += realized - commission
            if str(row.get("exit_reason") or "").upper() == "SL":
                sl_count += 1
        return {
            "completed_count": completed_count,
            "sl_count": sl_count,
            "net_pnl_usdc": round(net_pnl_usdc, 8),
        }

    async def _codex_v136_maybe_promote_nl_near_w1d_live200(
        self,
        raw_codex_decision: CodexV1Decision,
        features: Mapping[str, Any],
    ) -> CodexV1Decision:
        if raw_codex_decision.accepted or raw_codex_decision.reason != "no_codex_v1_lane_match":
            return raw_codex_decision
        side = str(raw_codex_decision.side or features.get("side") or "").upper()
        strategy = str(raw_codex_decision.strategy or features.get("strategy") or "")
        if side != "LONG" or strategy != "S1_BB_RSI":
            return raw_codex_decision
        candidate = classify_codex_v133_no_lane_candidate(features, reason=raw_codex_decision.reason)
        if not (
            candidate.get("candidate_bucket") == "NL_NEAR_W1D_LONG"
            and candidate.get("nearest_lane_code") == "W1D"
            and int(candidate.get("missing_critical_features") or 0) == 0
        ):
            return raw_codex_decision

        lane_code = "SH_NL_NEAR_W1D_LONG_LIVE200"
        fixed_notional = 200.0
        daily_limit = 3
        d30 = MainnetOneRunManager._codex_v1_shadow_feature_float(features, "d30")
        adv3 = MainnetOneRunManager._codex_v1_shadow_feature_float(features, "adv3")
        vwap_dist_bp = MainnetOneRunManager._codex_v1_shadow_feature_float(features, "vwap_dist_bp")
        reclaimed = MainnetOneRunManager._codex_v1_shadow_feature_float(features, "price_above_or_reclaimed_vwap")
        lacks_vwap_reclaim = reclaimed != 1.0 or (vwap_dist_bp is not None and vwap_dist_bp < 0.0)
        strict_live200_ok = (
            not lacks_vwap_reclaim
            and (d30 is None or d30 > -10.0)
            and (adv3 is None or adv3 <= 8.0)
        )
        count_24h = await self._codex_v136_nl_near_w1d_live200_count_24h()
        loss_guard = await self._codex_v136_nl_near_w1d_live200_loss_guard_24h()
        try:
            base_notional = float(getattr(self._settings, "mainnet_effective_entry_notional_usdc", fixed_notional) or fixed_notional)
        except (TypeError, ValueError):
            base_notional = fixed_notional
        notional_mult = fixed_notional / base_notional if base_notional > 0 else 1.0
        metrics = {
            "promotion_version": "v1.3.6",
            "promotion_source": "no_lane_candidate",
            "candidate_bucket": candidate.get("candidate_bucket"),
            "nearest_lane_code": candidate.get("nearest_lane_code"),
            "nearest_lane_name": candidate.get("nearest_lane_name"),
            "nearest_lane_distance": candidate.get("nearest_lane_distance"),
            "nearest_lane_gaps": candidate.get("nearest_lane_gaps"),
            "promotion_family": candidate.get("promotion_family"),
            "sampling_family": candidate.get("sampling_family"),
            "d30": d30,
            "adv3": adv3,
            "vwap_dist_bp": vwap_dist_bp,
            "price_above_or_reclaimed_vwap": reclaimed,
            "live200_strict_gate_pass": strict_live200_ok,
            "fixed_notional_usdc": fixed_notional,
            "applied_notional_cap_usdc": fixed_notional,
            "entry_ttl_s": 120,
            "entry_model": "post_only_maker_0bp",
            "max_fills_per_day_for_lane": daily_limit,
            "accepted_count_24h": count_24h,
            "loss_guard_completed_count_24h": int(loss_guard.get("completed_count") or 0),
            "loss_guard_sl_count_24h": int(loss_guard.get("sl_count") or 0),
            "loss_guard_net_pnl_24h_usdc": float(loss_guard.get("net_pnl_usdc") or 0.0),
            "loss_guard_net_stop_usdc": -0.45,
        }
        risk_tags = tuple(raw_codex_decision.risk_tags) + (
            "nl_near_w1d_live200",
            "fixed_200_usdc",
            "post_only_maker",
            "no_taker_fallback",
            "no_sizeup",
            "no_dca",
        )
        guard_reason = None
        guard_risk_tag = None
        if lacks_vwap_reclaim:
            guard_reason = "nl_near_w1d_reclaim_guard_shadow"
            guard_risk_tag = "reclaim_guard_shadow"
        elif metrics["loss_guard_sl_count_24h"] >= 1:
            guard_reason = "nl_near_w1d_live200_sl_guard_block"
            guard_risk_tag = "sl_guard_block"
        elif metrics["loss_guard_net_pnl_24h_usdc"] <= -0.45:
            guard_reason = "nl_near_w1d_live200_net_loss_guard_block"
            guard_risk_tag = "net_loss_guard_block"
        elif count_24h >= daily_limit:
            guard_reason = "nl_near_w1d_live200_daily_limit_block"
            guard_risk_tag = "daily_limit_block"
        if guard_reason:
            metrics = {
                **metrics,
                "policy_note": guard_reason,
                "policy_tag": guard_reason,
            }
            return replace(
                raw_codex_decision,
                lane=lane_code,
                lane_code=lane_code,
                strategy=strategy,
                side=side,
                entry_offset_bp=0.0,
                reason=guard_reason,
                regime="nl_near_w1d_live200",
                metrics=metrics,
                policy_tag=guard_reason,
                risk_tags=risk_tags + (guard_risk_tag,),
            )

        metrics = {
            **metrics,
            "policy_note": lane_code if strict_live200_ok else "nl_near_w1d_cap50_reclaim_guard",
            "policy_tag": lane_code if strict_live200_ok else "nl_near_w1d_cap50_reclaim_guard",
            "fixed_notional_usdc": fixed_notional if strict_live200_ok else 50.0,
            "applied_notional_cap_usdc": fixed_notional if strict_live200_ok else 50.0,
        }
        if not strict_live200_ok:
            fixed_notional = 50.0
            notional_mult = fixed_notional / base_notional if base_notional > 0 else 1.0
            risk_tags = tuple(tag for tag in risk_tags if tag != "fixed_200_usdc") + ("fixed_50_usdc", "v139c_reclaim_guard_cap50")
        return replace(
            raw_codex_decision,
            accepted=True,
            lane=lane_code,
            lane_code=lane_code,
            strategy=strategy,
            side=side,
            entry_offset_bp=0.0,
            size_mult=1.0,
            notional_mult=notional_mult,
            requested_notional_usdc=fixed_notional,
            reason="nl_near_w1d_live200_promoted" if strict_live200_ok else "nl_near_w1d_cap50_reclaim_guard_promoted",
            regime="nl_near_w1d_live200" if strict_live200_ok else "nl_near_w1d_cap50_reclaim_guard",
            missing_features=(),
            risk_tags=risk_tags,
            metrics=metrics,
            policy_tag=lane_code,
            shadow_lane=None,
        )

    async def _codex_v139_reprice_canary_count_24h(self) -> int:
        counter = getattr(self._repo, "count_events_since", None)
        if not callable(counter):
            return 0
        since_ms = int(time.time() * 1000) - 24 * 60 * 60 * 1000
        try:
            return int(
                await counter(
                    "entry_codex_v1_accepted",
                    since_ms,
                    "v139_reprice_tiny_canary",
                )
            )
        except Exception as exc:  # noqa: BLE001 - fail open; canary remains tiny and preflight guarded.
            logger.warning("v139_reprice_canary_count_failed", error=str(exc)[:200])
            return 0

    async def _codex_v139_maybe_promote_reprice_canary(
        self,
        decision: WildcatLiveDecision,
        raw_codex_decision: CodexV1Decision,
        codex_decision: CodexV1Decision,
        features: Mapping[str, Any],
    ) -> CodexV1Decision:
        if not bool(getattr(self._settings, "mainnet_codex_v139_reprice_canary_enabled", False)):
            return codex_decision
        if raw_codex_decision.accepted or codex_decision.accepted:
            return codex_decision
        if raw_codex_decision.reason != "no_codex_v1_lane_match" or codex_decision.reason != "no_codex_v1_lane_match":
            return codex_decision

        side = str(codex_decision.side or raw_codex_decision.side or features.get("side") or decision.side or "").upper()
        strategy = str(codex_decision.strategy or raw_codex_decision.strategy or features.get("strategy") or decision.strategy or "")
        if side != "LONG" or strategy != "S1_BB_RSI":
            return codex_decision

        mapping = self._codex_v1_map_block_to_shadow_lane(
            codex_decision.reason,
            decision,
            raw_codex_decision,
            codex_decision,
            features,
        )
        if not mapping:
            return codex_decision
        shadow_lane = str(mapping.get("shadow_lane") or "").upper()
        allowed_lanes = {
            item.strip().upper()
            for item in str(getattr(self._settings, "mainnet_codex_v139_reprice_canary_lanes", "") or "").split(",")
            if item.strip()
        }
        if shadow_lane not in allowed_lanes:
            return codex_decision

        lane_code_by_shadow = {
            "SH_WPR_L_S1": "CNL-WPR-L",
            "SH_L1_ADVERSE_REPRICE_MR_LONG": "CNL-L1MR-L",
        }
        lane_name_by_shadow = {
            "SH_WPR_L_S1": "v139_canary_watch_pre_reprice_long_s1",
            "SH_L1_ADVERSE_REPRICE_MR_LONG": "v139_canary_l1_adverse_reprice_mr_long",
        }
        lane_code = lane_code_by_shadow.get(shadow_lane)
        if not lane_code:
            return codex_decision

        try:
            fixed_notional = float(getattr(self._settings, "mainnet_codex_v139_reprice_canary_notional_usdc", 50.0) or 50.0)
        except (TypeError, ValueError):
            fixed_notional = 50.0
        fixed_notional = max(0.0, fixed_notional)
        if fixed_notional <= 0:
            return codex_decision
        try:
            base_notional = float(getattr(self._settings, "mainnet_effective_entry_notional_usdc", fixed_notional) or fixed_notional)
        except (TypeError, ValueError):
            base_notional = fixed_notional
        notional_mult = fixed_notional / base_notional if base_notional > 0 else 1.0
        try:
            if shadow_lane == "SH_WPR_L_S1":
                entry_offset_bp = float(getattr(self._settings, "mainnet_codex_v139b_wpr_entry_offset_bp", 3.0) or 3.0)
            else:
                entry_offset_bp = float(getattr(self._settings, "mainnet_codex_v139_reprice_canary_entry_offset_bp", 0.0) or 0.0)
        except (TypeError, ValueError):
            entry_offset_bp = 3.0 if shadow_lane == "SH_WPR_L_S1" else 0.0
        daily_cap = int(getattr(self._settings, "mainnet_codex_v139_reprice_canary_daily_cap", 0) or 0)
        count_24h = await self._codex_v139_reprice_canary_count_24h() if daily_cap > 0 else None
        candidate_lane = str(mapping.get("candidate_lane") or "")
        metrics = {
            **(codex_decision.metrics or {}),
            "promotion_version": "v1.3.9C",
            "promotion_source": "no_lane_shadow_reprice_canary",
            "canary_policy": "v139_reprice_tiny_canary",
            "policy_note": "v139_reprice_tiny_canary",
            "policy_tag": "v139_reprice_tiny_canary",
            "shadow_lane": shadow_lane,
            "candidate_lane": candidate_lane,
            "mapping_reason": mapping.get("mapping_reason"),
            "shadow_lane_family": mapping.get("shadow_lane_family"),
            "shadow_lane_reason": mapping.get("shadow_lane_reason"),
            "shadow_reprice_state": mapping.get("shadow_reprice_state"),
            "fixed_notional_usdc": fixed_notional,
            "applied_notional_cap_usdc": fixed_notional,
            "entry_model": f"post_only_maker_{entry_offset_bp:g}bp",
            "canary_lane_code": lane_code,
            "canary_daily_count_24h": count_24h,
            "canary_daily_cap": daily_cap if daily_cap > 0 else None,
            "dca_enabled": bool(getattr(self._settings, "mainnet_codex_v139_reprice_canary_dca_enabled", False)),
            "wpr_profile": "v139b_wpr_waiting_scratch" if shadow_lane == "SH_WPR_L_S1" else None,
            "wpr_partial_tp_pct": (
                float(getattr(self._settings, "mainnet_codex_v139b_wpr_partial_tp_pct", 0.00030) or 0.00030)
                if shadow_lane == "SH_WPR_L_S1"
                else None
            ),
            "wpr_partial_exit_pct": (
                float(getattr(self._settings, "mainnet_codex_v139b_wpr_partial_exit_pct", 0.60) or 0.60)
                if shadow_lane == "SH_WPR_L_S1"
                else None
            ),
            "wpr_max_sl_bp": (
                float(getattr(self._settings, "mainnet_codex_v139b_wpr_max_sl_bp", 8.0) or 8.0)
                if shadow_lane == "SH_WPR_L_S1"
                else None
            ),
        }
        risk_tags = tuple(codex_decision.risk_tags) + (
            "v139_reprice_tiny_canary",
            "shadow_reprice_canary",
            "fixed_50_usdc",
            "post_only_maker",
            "no_taker_fallback",
            "no_dca",
        )
        if shadow_lane == "SH_WPR_L_S1":
            risk_tags = risk_tags + ("v139b_wpr_waiting_scratch", "wpr_discount_entry", "wpr_scratch_exit")
        if shadow_lane == "SH_WPR_L_S1":
            d30 = MainnetOneRunManager._codex_v1_shadow_feature_float(features, "d30")
            vwap_dist_bp = MainnetOneRunManager._codex_v1_shadow_feature_float(features, "vwap_dist_bp")
            reclaimed = MainnetOneRunManager._codex_v1_shadow_feature_float(features, "price_above_or_reclaimed_vwap")
            rsi = MainnetOneRunManager._codex_v1_shadow_feature_float(features, "rsi")
            wpr_block_reason = None
            if vwap_dist_bp is not None and vwap_dist_bp <= -100.0:
                wpr_block_reason = "wpr_extreme_below_vwap_block"
            elif d30 is not None and d30 <= -15.0 and reclaimed != 1.0:
                wpr_block_reason = "wpr_down_tape_block"
            elif (
                vwap_dist_bp is not None
                and vwap_dist_bp <= -5.0
                and rsi is not None
                and rsi < 40.0
            ):
                wpr_block_reason = "wpr_weak_below_vwap_block"
            metrics = {
                **metrics,
                "wpr_guard_d30": d30,
                "wpr_guard_vwap_dist_bp": vwap_dist_bp,
                "wpr_guard_reclaimed": reclaimed,
                "wpr_guard_rsi": rsi,
                "wpr_guard_reason": wpr_block_reason,
            }
            if wpr_block_reason:
                return replace(
                    codex_decision,
                    lane=lane_name_by_shadow.get(shadow_lane, lane_code),
                    lane_code=lane_code,
                    strategy=strategy,
                    side=side,
                    entry_offset_bp=entry_offset_bp,
                    reason=wpr_block_reason,
                    regime="v139c_wpr_admission_guard",
                    metrics={**metrics, "policy_note": wpr_block_reason, "policy_tag": wpr_block_reason},
                    policy_tag=wpr_block_reason,
                    shadow_lane=shadow_lane,
                    risk_tags=risk_tags + (wpr_block_reason,),
                )

        if daily_cap > 0 and count_24h >= daily_cap:
            block_reason = "v139_reprice_canary_daily_cap_block"
            return replace(
                codex_decision,
                lane=lane_name_by_shadow.get(shadow_lane, lane_code),
                lane_code=lane_code,
                strategy=strategy,
                side=side,
                entry_offset_bp=entry_offset_bp,
                reason=block_reason,
                regime="v139_reprice_canary",
                metrics={**metrics, "policy_note": block_reason, "policy_tag": block_reason},
                policy_tag=block_reason,
                shadow_lane=shadow_lane,
                risk_tags=risk_tags + ("daily_limit_block",),
            )

        return replace(
            codex_decision,
            accepted=True,
            lane=lane_name_by_shadow.get(shadow_lane, lane_code),
            lane_code=lane_code,
            strategy=strategy,
            side=side,
            entry_offset_bp=entry_offset_bp,
            size_mult=notional_mult,
            notional_mult=notional_mult,
            requested_notional_usdc=fixed_notional,
            reason="v139_reprice_canary_promoted",
            regime="v139_reprice_canary",
            missing_features=(),
            risk_tags=risk_tags,
            metrics=metrics,
            policy_tag="v139_reprice_tiny_canary",
            shadow_lane=shadow_lane,
        )
    async def _codex_v134_w6a_weak_drift_canary_count_24h(self) -> int:
        counter = getattr(self._repo, "count_events_since", None)
        if not callable(counter):
            return 0
        since_ms = int(time.time() * 1000) - 24 * 60 * 60 * 1000
        try:
            return int(
                await counter(
                    "entry_codex_v1_accepted",
                    since_ms,
                    "v134_w6a_weak_drift_50_canary",
                )
            )
        except Exception as exc:  # noqa: BLE001 - fail open; the $50 cap remains active.
            logger.warning("v134_w6a_canary_count_failed", error=str(exc)[:200])
            return 0

    def _codex_v1_live_research_block_reason(
        self,
        codex_decision: CodexV1Decision,
        features: Mapping[str, Any],
    ) -> str | None:
        if not codex_decision.accepted:
            return None
        if codex_decision.lane == "w6_lane_s1long_rng38_86_range9_15_e0":
            if getattr(self._settings, "mainnet_codex_v137_w6a_risk_shadow_enabled", True):
                return None
            drift30 = features.get("d30", features.get("drift30"))
            try:
                drift30_value = float(drift30) if drift30 is not None else None
            except (TypeError, ValueError):
                drift30_value = None
            weak_drift_blocked = (
                self._settings.mainnet_codex_v1_w6_weak_drift_block_enabled
                and drift30_value is not None
                and drift30_value > float(self._settings.mainnet_codex_v1_w6_weak_drift_threshold_bp)
            )
            if weak_drift_blocked:
                return "codex_v1_w6_weak_drift_block"
            if self._settings.mainnet_codex_v1_w6_deep_pullback_block_enabled:
                adv3 = features.get("adv3", features.get("adv3_bp"))
                rsi = features.get("rsi", features.get("rsi14"))
                vwap_dist = features.get("vwap_dist_bp")
                pullback = features.get("pullback_from_recent_high_bp")
                reclaimed = features.get("price_above_or_reclaimed_vwap")
                try:
                    adv3_value = float(adv3) if adv3 is not None else None
                    rsi_value = float(rsi) if rsi is not None else None
                    vwap_dist_value = float(vwap_dist) if vwap_dist is not None else None
                    pullback_value = float(pullback) if pullback is not None else None
                    reclaimed_value = float(reclaimed) if reclaimed is not None else None
                except (TypeError, ValueError):
                    return None
                vwap_threshold = float(self._settings.mainnet_codex_v1_w6_deep_pullback_vwap_dist_max_bp)
                vwap_or_reclaim_blocked = (
                    (vwap_dist_value is not None and vwap_dist_value <= vwap_threshold)
                    or (reclaimed_value is not None and reclaimed_value <= 0.0)
                )
                if None not in (
                    drift30_value,
                    adv3_value,
                    rsi_value,
                    pullback_value,
                ) and (
                    drift30_value <= float(self._settings.mainnet_codex_v1_w6_deep_pullback_d30_max_bp)
                    and adv3_value >= float(self._settings.mainnet_codex_v1_w6_deep_pullback_adv3_min_bp)
                    and rsi_value <= float(self._settings.mainnet_codex_v1_w6_deep_pullback_rsi_max)
                    and pullback_value >= float(self._settings.mainnet_codex_v1_w6_deep_pullback_pullback_min_bp)
                    and vwap_or_reclaim_blocked
                ):
                    return "codex_v1_w6_deep_pullback_block"
            return None
        if codex_decision.lane == "w2_lane_s1long_score64_74_rng35_55_e0_block":
            if getattr(self._settings, "mainnet_codex_v1_w2a_shadow_only_enabled", True):
                return "v130_w2a_shadow_only"
            if not self._settings.mainnet_codex_v1_w2a_tight_block_enabled:
                return None
            d30 = features.get("d30", features.get("drift30"))
            adv3 = features.get("adv3", features.get("adv3_bp"))
            bb_lower_dist = features.get("bb_lower_dist_bp")
            try:
                d30_value = float(d30) if d30 is not None else None
                adv3_value = float(adv3) if adv3 is not None else None
                bb_lower_dist_value = float(bb_lower_dist) if bb_lower_dist is not None else None
            except (TypeError, ValueError):
                return None
            if d30_value is None or adv3_value is None or bb_lower_dist_value is None:
                return None
            if not (
                float(self._settings.mainnet_codex_v1_w2a_d30_low_bp)
                <= d30_value
                <= float(self._settings.mainnet_codex_v1_w2a_d30_high_bp)
            ):
                return "codex_v1_w2a_tight_block"
            if not (
                float(self._settings.mainnet_codex_v1_w2a_adv3_low_bp)
                <= adv3_value
                <= float(self._settings.mainnet_codex_v1_w2a_adv3_high_bp)
            ):
                return "codex_v1_w2a_tight_block"
            if not (
                float(self._settings.mainnet_codex_v1_w2a_bb_lower_dist_low_bp)
                <= bb_lower_dist_value
                <= float(self._settings.mainnet_codex_v1_w2a_bb_lower_dist_high_bp)
            ):
                return "codex_v1_w2a_tight_block"
        if codex_decision.lane == "w1_lane_s1short_score71_76_range3_9_e0_advopen":
            if not self._settings.mainnet_codex_v1_w1b_tight_block_enabled:
                return None
            d30 = features.get("d30", features.get("drift30"))
            adv3 = features.get("adv3", features.get("adv3_bp"))
            bb_lower_dist = features.get("bb_lower_dist_bp")
            reprice_wait = features.get("reprice_wait_elapsed_seconds")
            try:
                d30_value = float(d30) if d30 is not None else None
                adv3_value = float(adv3) if adv3 is not None else None
                bb_lower_dist_value = float(bb_lower_dist) if bb_lower_dist is not None else None
                reprice_wait_value = float(reprice_wait) if reprice_wait is not None else None
            except (TypeError, ValueError):
                return None
            if (
                d30_value is None
                or adv3_value is None
                or bb_lower_dist_value is None
                or reprice_wait_value is None
            ):
                return None
            if not (
                float(self._settings.mainnet_codex_v1_w1b_d30_low_bp)
                <= d30_value
                <= float(self._settings.mainnet_codex_v1_w1b_d30_high_bp)
            ):
                return "codex_v1_w1b_tight_block"
            if adv3_value > float(self._settings.mainnet_codex_v1_w1b_adv3_high_bp):
                return "codex_v1_w1b_tight_block"
            if bb_lower_dist_value > float(self._settings.mainnet_codex_v1_w1b_bb_lower_dist_high_bp):
                return "codex_v1_w1b_tight_block"
            if reprice_wait_value > float(self._settings.mainnet_codex_v1_w1b_reprice_wait_max_seconds):
                return "codex_v1_w1b_tight_block"
        return None

    def _apply_codex_v1_decision(
        self,
        decision: WildcatLiveDecision,
        codex_decision: CodexV1Decision,
    ) -> WildcatLiveDecision:
        base_notional = self._settings.mainnet_effective_entry_notional_usdc
        requested_notional = max(0.0, base_notional * max(0.0, codex_decision.notional_mult))
        max_notional = self._settings.mainnet_effective_max_cumulative_notional_usdc
        if self._settings.mainnet_codex_v1_max_notional_usdc > 0:
            max_notional = min(max_notional, self._settings.mainnet_codex_v1_max_notional_usdc)
        metrics = getattr(codex_decision, "metrics", None) or {}
        applied_cap = metrics.get("applied_notional_cap_usdc") if isinstance(metrics, Mapping) else None
        try:
            applied_cap_value = float(applied_cap) if applied_cap is not None else None
        except (TypeError, ValueError):
            applied_cap_value = None
        if applied_cap_value is not None and math.isfinite(applied_cap_value) and applied_cap_value > 0:
            max_notional = min(max_notional, applied_cap_value)
        applied_notional = min(requested_notional, max_notional)
        lane_code = str(codex_decision.lane_code or "").upper()
        wpr_profile = lane_code == "CNL-WPR-L"
        partial_tp_pct = decision.partial_tp_pct
        partial_exit_pct = decision.partial_exit_pct
        if self._use_codex_v138_w6a_exit_policy(codex_decision.lane_code):
            partial_tp_pct = self._codex_v138_w6a_partial_tp_pct()
        if wpr_profile:
            partial_tp_pct = self._codex_v139b_wpr_partial_tp_pct()
            partial_exit_pct = self._codex_v139b_wpr_partial_exit_pct()

        entry_ref = self._codex_v1_entry_reference_price(
            decision.signal.price,
            decision.side,
            codex_decision.entry_offset_bp or 0.0,
        )
        stop_loss = decision.signal.stop_loss
        if wpr_profile and entry_ref > 0:
            max_sl_bp = self._codex_v139b_wpr_max_sl_bp()
            if decision.side == "LONG":
                sl_floor = entry_ref * (1 - max_sl_bp / 10_000.0)
                if stop_loss is None or stop_loss <= 0 or stop_loss < sl_floor:
                    stop_loss = sl_floor
            elif decision.side == "SHORT":
                sl_ceiling = entry_ref * (1 + max_sl_bp / 10_000.0)
                if stop_loss is None or stop_loss <= 0 or stop_loss > sl_ceiling:
                    stop_loss = sl_ceiling
        effective_sl_pct = decision.sl_pct
        if wpr_profile and entry_ref > 0 and stop_loss is not None and stop_loss > 0:
            effective_sl_pct = abs(entry_ref - stop_loss) / entry_ref
        if decision.side == "LONG":
            take_profit = entry_ref * (1 + decision.tp_pct)
        else:
            take_profit = entry_ref * (1 - decision.tp_pct)
        leverage = max(1, int(self._settings.mainnet_leverage))
        reasons = list(decision.signal.reasons or [])
        reasons.extend(
            [
                f"codex_v1:{CODEX_V1_VERSION}",
                f"codex_v1_lane_code:{codex_decision.lane_code}",
                f"codex_v1_lane:{codex_decision.lane}",
                f"codex_v1_entry_bp:{codex_decision.entry_offset_bp:g}",
                f"codex_v1_notional_mult:{codex_decision.notional_mult:.2f}",
            ]
        )
        if getattr(codex_decision, "metrics", None) and codex_decision.metrics.get("policy_note"):
            reasons.append(f"codex_v1_policy_note:{codex_decision.metrics['policy_note']}")
        if self._use_codex_v138_w6a_exit_policy(codex_decision.lane_code):
            reasons.append(f"codex_v138_w6a_partial_tp_pct:{partial_tp_pct:g}")
        if wpr_profile:
            reasons.append(f"codex_v139b_wpr_partial_tp_pct:{partial_tp_pct:g}")
            reasons.append(f"codex_v139b_wpr_partial_exit_pct:{partial_exit_pct:g}")
            reasons.append(f"codex_v139b_wpr_max_sl_bp:{self._codex_v139b_wpr_max_sl_bp():g}")
        risk_notes = list(decision.signal.risk_notes or [])
        risk_notes.append("codex_v1_execution_gate")
        if applied_notional < requested_notional:
            risk_notes.append("codex_v1_notional_capped")

        new_signal = replace(
            decision.signal,
            entries=[entry_ref],
            stop_loss=stop_loss,
            take_profits=[take_profit],
            planned_notional_usdc=applied_notional,
            planned_margin_usdc=applied_notional / leverage,
            planned_qty=applied_notional / entry_ref if entry_ref > 0 else 0.0,
            reasons=reasons,
            risk_notes=risk_notes,
        )
        return replace(
            decision,
            signal=new_signal,
            partial_tp_pct=partial_tp_pct,
            partial_exit_pct=partial_exit_pct,
            sl_pct=effective_sl_pct,
        )

    def _codex_v139b_wpr_partial_tp_pct(self) -> float:
        try:
            pct = float(getattr(self._settings, "mainnet_codex_v139b_wpr_partial_tp_pct", 0.00030) or 0.00030)
        except (TypeError, ValueError):
            pct = 0.00030
        if not math.isfinite(pct) or pct <= 0:
            return 0.00030
        return pct

    def _codex_v139b_wpr_partial_exit_pct(self) -> float:
        try:
            pct = float(getattr(self._settings, "mainnet_codex_v139b_wpr_partial_exit_pct", 0.60) or 0.60)
        except (TypeError, ValueError):
            pct = 0.60
        if not math.isfinite(pct) or pct <= 0 or pct >= 1:
            return 0.60
        return pct

    def _codex_v139b_wpr_max_sl_bp(self) -> float:
        try:
            bp = float(getattr(self._settings, "mainnet_codex_v139b_wpr_max_sl_bp", 8.0) or 8.0)
        except (TypeError, ValueError):
            bp = 8.0
        if not math.isfinite(bp) or bp <= 0:
            return 8.0
        return bp

    def _use_codex_v138_w6a_exit_policy(self, lane_code: Any) -> bool:
        return (
            CODEX_V1_VERSION.startswith(("_codex_v1.3.8", "_codex_v1.3.9", "_codex_v1.4"))
            and str(lane_code or "").upper() == "W6A"
        )

    def _codex_v138_w6a_partial_tp_pct(self) -> float:
        try:
            pct = float(getattr(self._settings, "mainnet_codex_v138_w6a_partial_tp_pct", 0.0006))
        except (TypeError, ValueError):
            pct = 0.0006
        if not math.isfinite(pct) or pct <= 0:
            return 0.0006
        return pct

    def _signal_partial_tp_pct(self, signal: Mapping[str, Any]) -> float:
        wildcat = signal.get("wildcat") if isinstance(signal, Mapping) else None
        value = wildcat.get("partial_tp_pct") if isinstance(wildcat, Mapping) else None
        try:
            pct = float(value) if value is not None else None
        except (TypeError, ValueError):
            pct = None
        if pct is not None and math.isfinite(pct) and pct > 0:
            return pct

        codex = signal.get("codex_v1") if isinstance(signal, Mapping) else None
        lane_code = codex.get("lane_code") if isinstance(codex, Mapping) else None
        if self._use_codex_v138_w6a_exit_policy(lane_code):
            return self._codex_v138_w6a_partial_tp_pct()

        try:
            fallback = float(getattr(self._settings, "mainnet_partial_tp_pct", 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0
        return fallback if math.isfinite(fallback) and fallback > 0 else 0.0

    def _signal_partial_exit_pct(self, signal: Mapping[str, Any]) -> float:
        wildcat = signal.get("wildcat") if isinstance(signal, Mapping) else None
        value = wildcat.get("partial_exit_pct") if isinstance(wildcat, Mapping) else None
        try:
            pct = float(value) if value is not None else None
        except (TypeError, ValueError):
            pct = None
        if pct is not None and math.isfinite(pct) and pct > 0:
            return min(1.0, pct)

        try:
            fallback = float(getattr(self._settings, "mainnet_partial_exit_pct", 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0
        if not math.isfinite(fallback) or fallback <= 0:
            return 0.0
        return min(1.0, fallback)

    @staticmethod
    def _codex_v1_entry_reference_price(price: float, side: str, entry_offset_bp: float) -> float:
        if price <= 0:
            return price
        offset = entry_offset_bp / 10_000
        return price * (1 - offset) if side == "LONG" else price * (1 + offset)

    @staticmethod
    def _codex_v1_shadow_price(value: Any) -> float | None:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(parsed) or parsed <= 0:
            return None
        return parsed

    def _codex_v1_shadow_sample_prices(
        self,
        decision: WildcatLiveDecision,
        raw_codex_decision: CodexV1Decision,
        codex_decision: CodexV1Decision,
    ) -> tuple[float, float, float] | None:
        metrics = codex_decision.metrics if isinstance(codex_decision.metrics, Mapping) else {}
        entry = self._codex_v1_shadow_price(metrics.get("entry"))
        stop = self._codex_v1_shadow_price(metrics.get("stop"))
        tp = self._codex_v1_shadow_price(metrics.get("tp"))

        if entry is None:
            entry = self._codex_v1_entry_reference_price(
                float(decision.signal.price or 0.0),
                decision.side,
                raw_codex_decision.entry_offset_bp or codex_decision.entry_offset_bp or 0.0,
            )
            entry = self._codex_v1_shadow_price(entry)
        if stop is None:
            stop = self._codex_v1_shadow_price(decision.signal.stop_loss)
        if tp is None and decision.signal.take_profits:
            tp = self._codex_v1_shadow_price(decision.signal.take_profits[0])
        if tp is None and entry is not None:
            if decision.side == "LONG":
                tp = entry * (1 + decision.tp_pct)
            else:
                tp = entry * (1 - decision.tp_pct)
            tp = self._codex_v1_shadow_price(tp)
        if entry is None or stop is None or tp is None:
            return None
        return entry, stop, tp


    @staticmethod
    def _codex_v1_shadow_stable_hash(*parts: object) -> str:
        payload = json.dumps(parts, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:18]

    @staticmethod
    def _codex_v1_shadow_strategy_bucket(strategy: str) -> str:
        if strategy == "S1_BB_RSI":
            return "S1"
        return strategy or "UNKNOWN"

    @staticmethod
    def _codex_v1_shadow_feature_float(features: Mapping[str, Any], key: str) -> float | None:
        value = features.get(key)
        if value in (None, ""):
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) else None


    def _codex_v133_fee_audit_payload(
        self,
        features: Mapping[str, Any],
        *,
        entry_price: float,
        target_price: float,
    ) -> dict[str, Any]:
        maker_fee_bp = self._codex_v1_shadow_feature_float(features, "maker_fee_bp")
        taker_fee_bp = self._codex_v1_shadow_feature_float(features, "taker_fee_bp")
        maker_fee_bp = max(0.0, maker_fee_bp or 0.0)
        taker_fee_bp = max(0.0, taker_fee_bp if taker_fee_bp is not None else maker_fee_bp)
        expected_capture_bp = 0.0
        if entry_price > 0 and target_price > 0:
            expected_capture_bp = abs(target_price - entry_price) / entry_price * 10000.0
        estimated_slippage_bp = float(getattr(self._settings, "mainnet_codex_v133_estimated_slippage_bp", 0.4) or 0.0)
        min_net_buffer_bp = float(getattr(self._settings, "mainnet_codex_v133_min_net_buffer_bp", 1.5) or 0.0)
        estimated_roundtrip_fee_bp = maker_fee_bp + maker_fee_bp
        required_capture_bp = estimated_roundtrip_fee_bp + estimated_slippage_bp + min_net_buffer_bp
        expected_net_buffer_bp = expected_capture_bp - required_capture_bp
        return {
            "fee_audit_version": "v1.3.3",
            "fee_gate_mode": "audit_only" if getattr(self._settings, "mainnet_codex_v133_fee_gate_audit_only", True) else "off",
            "fee_gate_enforce": bool(getattr(self._settings, "mainnet_codex_v133_fee_gate_enforce", False)),
            "expected_capture_source": "target_distance",
            "expected_capture_bp": round(expected_capture_bp, 4),
            "estimated_roundtrip_fee_bp": round(estimated_roundtrip_fee_bp, 4),
            "estimated_slippage_bp": round(estimated_slippage_bp, 4),
            "min_net_buffer_bp": round(min_net_buffer_bp, 4),
            "expected_net_buffer_bp": round(expected_net_buffer_bp, 4),
            "fee_buffer_pass": expected_net_buffer_bp >= 0.0,
            "maker_fee_bp": round(maker_fee_bp, 4),
            "taker_fee_bp": round(taker_fee_bp, 4),
        }
    @staticmethod
    def _codex_v1_shadow_reprice_state(features: Mapping[str, Any]) -> str:
        wait_s = MainnetOneRunManager._codex_v1_shadow_feature_float(features, "reprice_wait_elapsed_seconds")
        favorable_bp = MainnetOneRunManager._codex_v1_shadow_feature_float(features, "reprice_favorable_bp")
        adverse_bp = MainnetOneRunManager._codex_v1_shadow_feature_float(features, "reprice_adverse_bp")
        if wait_s is None:
            return "missing"
        if wait_s < 120.0 or favorable_bp is None or adverse_bp is None:
            return "waiting"
        return "ready"

    @staticmethod
    def _codex_v1_shadow_sample_key(run_id: str, shadow_lane: str, lane_code: str, side: str) -> str:
        parts = [run_id, shadow_lane or "SH_UNKNOWN", lane_code or "NONE", side or "UNKNOWN"]
        return ":".join(part.replace(":", "_") for part in parts)

    @staticmethod
    def _codex_v1_no_lane_shadow_lane(
        reason: str,
        decision: WildcatLiveDecision,
        raw_codex_decision: CodexV1Decision,
        features: Mapping[str, Any],
    ) -> str | None:
        lane_code = str(raw_codex_decision.lane_code or "")
        if lane_code:
            return None
        side = str(raw_codex_decision.side or decision.side or "").upper()
        strategy = str(raw_codex_decision.strategy or decision.strategy or "")
        if reason != "no_codex_v1_lane_match":
            return None
        if side == "SHORT":
            return "NL-UNCLASSIFIED"
        if side != "LONG":
            return None
        if strategy != "S1_BB_RSI":
            return "NL-UNCLASSIFIED"
        wait_s = MainnetOneRunManager._codex_v1_shadow_feature_float(features, "reprice_wait_elapsed_seconds")
        favorable_bp = MainnetOneRunManager._codex_v1_shadow_feature_float(features, "reprice_favorable_bp")
        adverse_bp = MainnetOneRunManager._codex_v1_shadow_feature_float(features, "reprice_adverse_bp")
        vwap_dist_bp = MainnetOneRunManager._codex_v1_shadow_feature_float(features, "vwap_dist_bp")
        pullback_bp = MainnetOneRunManager._codex_v1_shadow_feature_float(features, "pullback_from_recent_high_bp")
        if wait_s is not None and (wait_s < 120.0 or favorable_bp is None or adverse_bp is None):
            return "NL-WATCH_PRE_REPRICE"
        if (
            favorable_bp is not None
            and adverse_bp is not None
            and favorable_bp >= 5.0
            and adverse_bp >= 5.0
            and (vwap_dist_bp is None or vwap_dist_bp <= -15.0)
            and (pullback_bp is None or pullback_bp >= 20.0)
        ):
            return "NL-L1_ADVERSE_REPRICE_MR_LONG"
        return "NL-UNCLASSIFIED"

    @staticmethod
    def _codex_v1_shadow_short_veto_matches(features: Mapping[str, Any]) -> list[str]:
        matches: list[str] = []
        try:
            if is_hot_up_extension(features):
                matches.append("hot_up_extension")
            if is_mid_up_extension_short_risk(features):
                matches.append("mid_up_extension")
            if is_stale_short_after_upmove(features):
                matches.append("stale_upmove")
        except Exception:
            logger.exception("codex_v1_shadow_short_veto_match_failed")
        return matches

    @staticmethod
    def _codex_v1_shadow_lane_metadata(
        shadow_lane: str,
        side: str,
        strategy: str,
        features: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        reprice_state = MainnetOneRunManager._codex_v1_shadow_reprice_state(features)
        if shadow_lane == "SH_WPR_L_S1":
            return {
                "shadow_lane_family": "NL",
                "shadow_lane_reason": "pre_reprice_timing_discovery_only",
                "shadow_reprice_state": "waiting",
                "candidate_lane": "NL-WATCH_PRE_REPRICE",
            }
        if shadow_lane == "SH_L1_ADVERSE_REPRICE_MR_LONG":
            return {
                "shadow_lane_family": "NL",
                "shadow_lane_reason": "adverse_reprice_mean_reversion_candidate",
                "shadow_reprice_state": reprice_state,
                "candidate_lane": "NL-L1_ADVERSE_REPRICE_MR_LONG",
            }
        if shadow_lane in {"SH_UNC_L_S1", "SH_UNC_S_S1"}:
            return {
                "shadow_lane_family": "NL",
                "shadow_lane_reason": "no_lane_unclassified_shadow_sample",
                "shadow_reprice_state": reprice_state,
                "candidate_lane": "NL-UNCLASSIFIED",
            }
        if shadow_lane == "SH_ANCHOR_S_SAFE":
            return {
                "shadow_lane_family": "ANCHOR_S",
                "shadow_lane_reason": "disabled_anchor_s_safe_shadow_sample",
                "shadow_reprice_state": reprice_state,
                "candidate_lane": "ANCHOR-S",
            }
        if shadow_lane.startswith("SH_DISABLED"):
            return {
                "shadow_lane_family": "DISABLED",
                "shadow_lane_reason": "disabled_lane_shadow_sample",
                "shadow_reprice_state": reprice_state,
            }
        if shadow_lane.startswith("SH_SHORT"):
            return {
                "shadow_lane_family": "SHORT_VETO",
                "shadow_lane_reason": "short_veto_shadow_sample",
                "shadow_reprice_state": reprice_state,
            }
        if shadow_lane.startswith("SH_W2A"):
            return {
                "shadow_lane_family": "W2A",
                "shadow_lane_reason": "w2a_shadow_only",
                "shadow_reprice_state": reprice_state,
                "candidate_lane": "W2A",
            }
        if shadow_lane.startswith("SH_S1P_L"):
            return {
                "shadow_lane_family": "S1P-L",
                "shadow_lane_reason": "s1p_l_wait_gt180_block_shadow_sample",
                "shadow_reprice_state": reprice_state,
                "candidate_lane": "S1P-L",
            }
        if shadow_lane.startswith("SH_W6A"):
            return {
                "shadow_lane_family": "W6A",
                "shadow_lane_reason": "w6a_guarded_block_shadow_sample",
                "shadow_reprice_state": reprice_state,
                "candidate_lane": "W6A",
            }
        return None

    def _codex_v1_map_block_to_shadow_lane(
        self,
        reason: str,
        decision: WildcatLiveDecision,
        raw_codex_decision: CodexV1Decision,
        codex_decision: CodexV1Decision,
        features: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        metrics = codex_decision.metrics if isinstance(codex_decision.metrics, Mapping) else {}
        raw_shadow_lane = str(codex_decision.shadow_lane or metrics.get("shadow_lane") or "")
        lane_code = str(raw_codex_decision.lane_code or codex_decision.lane_code or "")
        side = str(codex_decision.side or raw_codex_decision.side or decision.side or "").upper()
        strategy = str(codex_decision.strategy or raw_codex_decision.strategy or decision.strategy or "")
        strategy_bucket = self._codex_v1_shadow_strategy_bucket(strategy)
        reprice_state = self._codex_v1_shadow_reprice_state(features)

        if raw_shadow_lane.startswith("SH_S1P_L") or (lane_code == "S1P-L" and reason == "s1p_l_wait_gt180_block"):
            shadow_lane = raw_shadow_lane or "SH_S1P_L_WAIT_GT180"
            meta = self._codex_v1_shadow_lane_metadata(shadow_lane, side, strategy, features) or {}
            return {
                "shadow_lane": shadow_lane,
                "candidate_lane": meta.get("candidate_lane") or "S1P-L",
                "mapping_reason": reason or "s1p_l_wait_gt180_block",
                "secondary_reasons": [],
                "fill_model": "limit_touch",
                **meta,
            }

        if raw_shadow_lane.startswith("SH_W2A") or lane_code == "W2A":
            shadow_lane = raw_shadow_lane or "SH_W2A_SHADOW_ONLY"
            meta = self._codex_v1_shadow_lane_metadata(shadow_lane, side, strategy, features) or {}
            return {
                "shadow_lane": shadow_lane,
                "candidate_lane": meta.get("candidate_lane") or "W2A",
                "mapping_reason": reason or "w2a_shadow_only",
                "secondary_reasons": [],
                "fill_model": "limit_touch",
                **meta,
            }

        if raw_shadow_lane.startswith("SH_W6A") or lane_code == "W6A":
            shadow_lane = raw_shadow_lane or "SH_W6A_GUARDED_BLOCK"
            meta = self._codex_v1_shadow_lane_metadata(shadow_lane, side, strategy, features) or {}
            fill_model = "immediate_shadow" if shadow_lane in {"SH_W6A_RAW240_IMMEDIATE", "SH_W6A_BAD_RR_EARLY"} else "limit_touch"
            return {
                "shadow_lane": shadow_lane,
                "candidate_lane": meta.get("candidate_lane") or "W6A",
                "mapping_reason": reason or "w6a_guarded_block",
                "secondary_reasons": [],
                "fill_model": fill_model,
                **meta,
            }

        if reason == "codex_v1_lane_disabled":
            raw_lane_name = str(raw_codex_decision.lane or codex_decision.lane or "")
            is_anchor_s = side == "SHORT" and (
                lane_code == "ANCHOR-S" or raw_lane_name == "anchor_s1_preblock_broad_su6_exitA"
            )
            shadow_lane = (
                "SH_ANCHOR_S_SAFE"
                if is_anchor_s
                else ("SH_DISABLED_SHORT_S1" if side == "SHORT" else "SH_DISABLED_LONG_S1")
            )
            meta = self._codex_v1_shadow_lane_metadata(shadow_lane, side, strategy, features) or {}
            return {
                "shadow_lane": shadow_lane,
                "candidate_lane": lane_code or str(raw_codex_decision.lane or codex_decision.lane or reason),
                "mapping_reason": reason,
                "secondary_reasons": ["disabled_anchor_s_specialized_shadow"] if is_anchor_s else [],
                "fill_model": "limit_touch",
                **meta,
            }

        short_veto_map = {
            "hot_up_extension_short_blocked": ("SH_SHORT_HOT_UP_EXTENSION_S1", "hot_up_extension"),
            "mid_up_extension_short_blocked": ("SH_SHORT_MID_UP_EXTENSION_S1", "mid_up_extension"),
            "stale_short_after_upmove_blocked": ("SH_SHORT_STALE_UPMOVE_S1", "stale_upmove"),
        }
        if reason in short_veto_map:
            matched = self._codex_v1_shadow_short_veto_matches(features)
            priority = [
                ("hot_up_extension", "SH_SHORT_HOT_UP_EXTENSION_S1", "hot_up_extension_short_blocked"),
                ("mid_up_extension", "SH_SHORT_MID_UP_EXTENSION_S1", "mid_up_extension_short_blocked"),
                ("stale_upmove", "SH_SHORT_STALE_UPMOVE_S1", "stale_short_after_upmove_blocked"),
            ]
            primary, shadow_lane, candidate_lane = next(
                ((name, lane, canonical) for name, lane, canonical in priority if name in matched),
                (short_veto_map[reason][1], short_veto_map[reason][0], reason),
            )
            secondary = [item for item in matched if item != primary]
            if reason != candidate_lane and reason not in secondary:
                secondary.append(reason)
            meta = self._codex_v1_shadow_lane_metadata(shadow_lane, side, strategy, features) or {}
            return {
                "shadow_lane": shadow_lane,
                "candidate_lane": candidate_lane,
                "mapping_reason": primary,
                "secondary_reasons": secondary,
                "fill_model": "limit_touch",
                **meta,
            }

        raw_nl = self._codex_v1_no_lane_shadow_lane(reason, decision, raw_codex_decision, features)
        if raw_nl:
            fallback_reason = None
            if raw_nl == "NL-WATCH_PRE_REPRICE" and not (side == "LONG" and strategy_bucket == "S1"):
                shadow_lane = "SH_UNC_S_S1" if side == "SHORT" else "SH_UNC_L_S1"
                fallback_reason = "watch_pre_reprice_side_strategy_fallback"
            elif raw_nl == "NL-WATCH_PRE_REPRICE":
                shadow_lane = "SH_WPR_L_S1"
            elif raw_nl == "NL-L1_ADVERSE_REPRICE_MR_LONG":
                shadow_lane = "SH_L1_ADVERSE_REPRICE_MR_LONG"
            elif side == "SHORT":
                shadow_lane = "SH_UNC_S_S1"
            else:
                shadow_lane = "SH_UNC_L_S1"
            meta = self._codex_v1_shadow_lane_metadata(shadow_lane, side, strategy, features) or {}
            return {
                "shadow_lane": shadow_lane,
                "candidate_lane": raw_nl,
                "mapping_reason": fallback_reason or raw_nl,
                "secondary_reasons": [],
                "fill_model": "limit_touch",
                "shadow_lane_family": meta.get("shadow_lane_family", "NL"),
                "shadow_lane_reason": meta.get("shadow_lane_reason", "no_lane_shadow_sample"),
                "shadow_reprice_state": meta.get("shadow_reprice_state", reprice_state),
            }

        return None

    @staticmethod
    def _codex_v1_shadow_priority(shadow_lane: str) -> int:
        if shadow_lane == "SH_WPR_L_S1":
            return 1
        if shadow_lane.startswith("SH_S1P_L"):
            return 2
        if shadow_lane.startswith("SH_W6A"):
            return 2
        if shadow_lane.startswith("SH_W2A"):
            return 3
        if shadow_lane == "SH_ANCHOR_S_SAFE":
            return 4
        if shadow_lane == "SH_L1_ADVERSE_REPRICE_MR_LONG":
            return 4
        if shadow_lane.startswith("SH_DISABLED"):
            return 5
        if shadow_lane.startswith("SH_SHORT"):
            return 6
        return 7

    @staticmethod
    def _codex_v1_shadow_reference_price(features: Mapping[str, Any], decision: WildcatLiveDecision, entry_price: float) -> float:
        for key in ("mid_price", "mark_price", "last_price", "close", "price"):
            value = MainnetOneRunManager._codex_v1_shadow_feature_float(features, key)
            if value is not None and value > 0:
                return value
        signal_price = MainnetOneRunManager._codex_v1_shadow_price(getattr(decision.signal, "price", None))
        return signal_price or entry_price

    @staticmethod
    def _codex_v1_shadow_entry_bucket(entry_price: float, reference_price: float) -> int:
        if entry_price <= 0 or reference_price <= 0:
            return 0
        bp = (entry_price - reference_price) / reference_price * 10000.0
        return int(math.floor(bp / 5.0) * 5)

    @staticmethod
    def _codex_v1_shadow_2min_bucket(start_ms: int) -> int:
        return int(start_ms // 120000)

    def _codex_v1_shadow_opportunity_id(
        self,
        symbol: str,
        shadow_lane: str,
        candidate_lane: str,
        side: str,
        strategy: str,
        reprice_state: str,
        entry_price_bucket: int,
        bucket_2min: int,
        tp_price_bucket: int = 0,
        sl_price_bucket: int = 0,
        version_family: str = "codex_v1",
    ) -> str:
        return "opp_" + self._codex_v1_shadow_stable_hash(
            symbol,
            shadow_lane,
            candidate_lane,
            side,
            strategy,
            reprice_state,
            entry_price_bucket,
            tp_price_bucket,
            sl_price_bucket,
            bucket_2min,
            version_family,
        )

    def _codex_v1_shadow_sample_id(
        self,
        run_id: str,
        start_ms: int,
        opportunity_id: str,
        entry_price: float,
        tp_price: float,
        sl_price: float,
    ) -> str:
        return "sh_" + self._codex_v1_shadow_stable_hash(
            run_id,
            start_ms,
            opportunity_id,
            round(entry_price, 8),
            round(tp_price, 8),
            round(sl_price, 8),
        )

    def _codex_v1_touch_shadow_opportunity(self, opportunity_id: str, run_id: str, start_ms: int) -> dict[str, Any]:
        state = self._codex_v1_shadow_opportunities.setdefault(
            opportunity_id,
            {
                "first_seen_run_id": run_id,
                "first_seen_ms": start_ms,
                "last_seen_run_id": run_id,
                "last_seen_ms": start_ms,
                "raw_block_rows_count": 0,
            },
        )
        state["last_seen_run_id"] = run_id
        state["last_seen_ms"] = start_ms
        state["raw_block_rows_count"] = int(state.get("raw_block_rows_count") or 0) + 1
        return state

    async def _codex_v1_try_replace_lower_priority_shadow_sample(self, sample: Mapping[str, Any]) -> bool:
        run_id = str(sample.get("run_id") or "")
        if self._codex_v1_shadow_sample_counts_by_run.get(run_id, 0) < self.CODEX_V1_SHADOW_MAX_SAMPLES_PER_RUN:
            return False
        new_priority = int(sample.get("sample_priority") or 99)
        replaceable: list[tuple[int, int, str, Mapping[str, Any]]] = []
        for key, active in self._codex_v1_shadow_samples.items():
            if str(active.get("run_id") or "") != run_id:
                continue
            active_priority = int(active.get("sample_priority") or 99)
            if active_priority > new_priority:
                replaceable.append((active_priority, int(active.get("start_ms") or 0), key, active))
        if not replaceable:
            return False
        _priority, _start_ms, old_key, old_sample = sorted(replaceable, key=lambda item: (-item[0], item[1]))[0]
        self._codex_v1_shadow_samples.pop(old_key, None)
        self._codex_v1_shadow_sample_counts_by_run[run_id] = max(
            0,
            self._codex_v1_shadow_sample_counts_by_run.get(run_id, 0) - 1,
        )
        drop = {
            key: old_sample.get(key)
            for key in (
                "sample_id",
                "opportunity_id",
                "first_seen_run_id",
                "last_seen_run_id",
                "raw_block_rows_count",
                "run_id",
                "symbol",
                "version",
                "classifier_version",
                "shadow_lane",
                "candidate_lane",
                "candidate_bucket",
                "nearest_lane_code",
                "nearest_lane_distance",
                "promotion_family",
                "sampling_family",
                "sampling_quota_key",
                "promotion_eligible",
                "diagnostic_fill_model",
                "sample_group_id",
                "fee_buffer_pass",
                "expected_net_buffer_bp",
                "shadow_lane_family",
                "side",
                "strategy",
                "entry_price",
                "entry_reference_price",
                "entry_price_bucket",
                "opportunity_2min_bucket",
                "sample_priority",
                "fill_model",
                "reason",
                "policy_tag",
            )
        }
        drop.update(
            {
                "event_type": "shadow_sample_dropped",
                "drop_reason": "replaced_by_higher_priority",
                "replacement_sample_id": sample.get("sample_id"),
                "replacement_shadow_lane": sample.get("shadow_lane"),
                "replacement_priority": sample.get("sample_priority"),
                "cooldown_s": self.CODEX_V1_SHADOW_SAMPLE_COOLDOWN_S,
                "entry_ref_move_override_bp": self.CODEX_V1_SHADOW_ENTRY_REF_MOVE_BP,
                "max_shadow_samples_per_run": self.CODEX_V1_SHADOW_MAX_SAMPLES_PER_RUN,
            }
        )
        await self._repo.log_event(run_id, "entry_codex_v1_shadow_sample_dropped", drop)
        return True

    def _codex_v1_should_start_shadow_sample(self, sample: Mapping[str, Any]) -> tuple[bool, str | None]:
        run_id = str(sample.get("run_id") or "")
        if self._codex_v1_shadow_sample_counts_by_run.get(run_id, 0) >= self.CODEX_V1_SHADOW_MAX_SAMPLES_PER_RUN:
            return False, "per_run_cap"
        opportunity_id = str(sample.get("opportunity_id") or "")
        if any(str(active.get("opportunity_id") or "") == opportunity_id for active in self._codex_v1_shadow_samples.values()):
            return False, "active_opportunity_pending"
        scope_key = str(sample.get("sample_scope_key") or "")
        last = self._codex_v1_shadow_last_sample_by_scope.get(scope_key)
        if not last:
            return True, None
        now_ms = int(sample.get("start_ms") or 0)
        last_ms = int(last.get("start_ms") or 0)
        elapsed_s = (now_ms - last_ms) / 1000.0
        entry = self._codex_v1_shadow_price(sample.get("entry_price"))
        last_entry = self._codex_v1_shadow_price(last.get("entry_price"))
        entry_move_bp = 0.0
        if entry is not None and last_entry is not None and last_entry > 0:
            entry_move_bp = abs(entry - last_entry) / last_entry * 10000.0
        if elapsed_s < self.CODEX_V1_SHADOW_SAMPLE_COOLDOWN_S:
            if entry_move_bp < self.CODEX_V1_SHADOW_ENTRY_REF_MOVE_BP:
                return False, "cooldown"
            family = str(sample.get("sampling_family") or sample.get("shadow_lane_family") or "OTHER")
            active_family = sum(
                1
                for active in self._codex_v1_shadow_samples.values()
                if str(active.get("run_id") or "") == run_id
                and not active.get("diagnostic_only")
                and str(active.get("sampling_family") or active.get("shadow_lane_family") or "OTHER") == family
            )
            family_active_cap = int(getattr(self._settings, "mainnet_codex_v133_shadow_family_active_cap", 3) or 3)
            if active_family >= max(1, family_active_cap):
                return False, "family_active_cap"
            if int(sample.get("entry_price_bucket") or 0) == int(last.get("entry_price_bucket") or 0):
                return False, "cooldown"
        return True, None


    async def _start_codex_v1_shadow_sample(
        self,
        run: dict,
        decision: WildcatLiveDecision,
        raw_codex_decision: CodexV1Decision,
        codex_decision: CodexV1Decision,
        features: Mapping[str, Any],
        *,
        reason: str,
        effective_status: str,
        gaps: Sequence[str] = (),
        preflight: Sequence[str] = (),
    ) -> None:
        mapping = self._codex_v1_map_block_to_shadow_lane(
            reason,
            decision,
            raw_codex_decision,
            codex_decision,
            features,
        )
        if not mapping:
            return
        prices = self._codex_v1_shadow_sample_prices(decision, raw_codex_decision, codex_decision)
        shadow_lane = str(mapping.get("shadow_lane") or "")
        lane_code = str(raw_codex_decision.lane_code or codex_decision.lane_code or "")
        if prices is None:
            logger.warning(
                "codex_v1_shadow_sample_missing_prices",
                run_id=run["run_id"],
                shadow_lane=shadow_lane,
                lane_code=lane_code,
            )
            return

        start_ms = int(time.time() * 1000)
        entry, stop, tp = prices
        side = str(codex_decision.side or raw_codex_decision.side or decision.side or "").upper()
        strategy = str(codex_decision.strategy or raw_codex_decision.strategy or decision.strategy or "")
        symbol = str(run.get("symbol") or features.get("symbol") or getattr(decision.signal, "symbol", "") or "UNKNOWN")
        candidate_lane = str(mapping.get("candidate_lane") or lane_code or raw_codex_decision.lane or "UNKNOWN")
        candidate_meta: dict[str, Any] = {}
        if (
            getattr(self._settings, "mainnet_codex_v133_no_lane_miner_enabled", True)
            and (reason == "no_codex_v1_lane_match" or mapping.get("shadow_lane_family") == "NL")
        ):
            candidate_meta = classify_codex_v133_no_lane_candidate(features, reason=reason)
        candidate_bucket = str(candidate_meta.get("candidate_bucket") or "")
        opportunity_candidate = candidate_bucket or candidate_lane
        reprice_state = str(mapping.get("shadow_reprice_state") or self._codex_v1_shadow_reprice_state(features))
        entry_reference = self._codex_v1_shadow_reference_price(features, decision, entry)
        entry_bucket = self._codex_v1_shadow_entry_bucket(entry, entry_reference)
        tp_bucket = self._codex_v1_shadow_entry_bucket(tp, entry)
        sl_bucket = self._codex_v1_shadow_entry_bucket(stop, entry)
        bucket_2min = self._codex_v1_shadow_2min_bucket(start_ms)
        opportunity_id = self._codex_v1_shadow_opportunity_id(
            symbol,
            shadow_lane,
            opportunity_candidate,
            side,
            strategy,
            reprice_state,
            entry_bucket,
            bucket_2min,
            tp_price_bucket=tp_bucket,
            sl_price_bucket=sl_bucket,
            version_family="codex_v1",
        )
        opportunity_state = self._codex_v1_touch_shadow_opportunity(opportunity_id, str(run["run_id"]), start_ms)
        sample_id = self._codex_v1_shadow_sample_id(str(run["run_id"]), start_ms, opportunity_id, entry, tp, stop)
        sample_scope_key = f"{symbol}:{shadow_lane}:{side}"
        metrics = codex_decision.metrics if isinstance(codex_decision.metrics, Mapping) else {}
        sample_priority = self._codex_v1_shadow_priority(shadow_lane)
        if bool(getattr(self._settings, "mainnet_codex_v134_nl_near_long_priority_enabled", True)):
            if candidate_bucket in {"NL_NEAR_RP1_LONG", "NL_NEAR_S1P_L_LONG", "NL_NEAR_W1D_LONG"}:
                sample_priority = min(sample_priority, 3)
            elif candidate_bucket == "NL_NEAR_W6A_LONG":
                sample_priority = min(sample_priority, 4)
        fill_model = str(mapping.get("fill_model") or "limit_touch")
        fee_audit = self._codex_v133_fee_audit_payload(features, entry_price=entry, target_price=tp)
        sample_group_id = "grp_" + self._codex_v1_shadow_stable_hash(opportunity_id, shadow_lane, side, strategy)
        diagnostic_fill_model = (
            "immediate_shadow"
            if getattr(self._settings, "mainnet_codex_v133_diagnostic_fill_enabled", True)
            and mapping.get("shadow_lane_family") in {"NL", "SHORT_VETO", "ANCHOR_S"}
            and fill_model == "limit_touch"
            else None
        )
        sample = {
            "event_type": "shadow_sample_started",
            "sample_id": sample_id,
            "opportunity_id": opportunity_id,
            "first_seen_run_id": opportunity_state.get("first_seen_run_id"),
            "last_seen_run_id": opportunity_state.get("last_seen_run_id"),
            "raw_block_rows_count": opportunity_state.get("raw_block_rows_count"),
            "run_id": run["run_id"],
            "symbol": symbol,
            "version": CODEX_V1_VERSION,
            "classifier_version": codex_decision.version,
            "baseline": codex_decision.baseline,
            "shadow_lane": shadow_lane,
            "candidate_lane": candidate_lane,
            "candidate_bucket": candidate_bucket or None,
            "nearest_lane_code": candidate_meta.get("nearest_lane_code"),
            "nearest_lane_name": candidate_meta.get("nearest_lane_name"),
            "nearest_lane_distance": candidate_meta.get("nearest_lane_distance"),
            "nearest_lane_gaps": candidate_meta.get("nearest_lane_gaps") or {},
            "missing_critical_features": candidate_meta.get("missing_critical_features"),
            "failed_threshold_count": candidate_meta.get("failed_threshold_count"),
            "candidate_classified_at_ms": start_ms if candidate_meta else None,
            "candidate_reason": candidate_meta.get("candidate_reason"),
            "promotion_family": candidate_meta.get("promotion_family") or mapping.get("shadow_lane_family") or shadow_lane,
            "sampling_family": candidate_meta.get("sampling_family") or mapping.get("shadow_lane_family") or "OTHER",
            "sampling_quota_key": candidate_bucket or shadow_lane,
            "shadow_lane_family": mapping.get("shadow_lane_family"),
            "shadow_lane_reason": mapping.get("shadow_lane_reason"),
            "shadow_reprice_state": reprice_state,
            "mapping_reason": mapping.get("mapping_reason"),
            "secondary_reasons": list(mapping.get("secondary_reasons") or []),
            "lane_code": lane_code or codex_decision.lane_code,
            "lane": raw_codex_decision.lane or codex_decision.lane,
            "strategy": strategy,
            "side": side,
            "start_ms": start_ms,
            "entry_price": round(entry, 8),
            "tp_price": round(tp, 8),
            "sl_price": round(stop, 8),
            "entry_reference_price": round(entry_reference, 8),
            "entry_price_bucket": entry_bucket,
            "tp_price_bucket": tp_bucket,
            "sl_price_bucket": sl_bucket,
            "opportunity_2min_bucket": bucket_2min,
            "version_family": "codex_v1",
            "sample_scope_key": sample_scope_key,
            "sample_priority": sample_priority,
            "fill_model": fill_model,
            "promotion_eligible": fill_model == "limit_touch",
            "diagnostic_fill_model": diagnostic_fill_model,
            "diagnostic_only": False,
            "sample_group_id": sample_group_id,
            "entry_ttl_s": self.CODEX_V1_SHADOW_ENTRY_TTL_S,
            "outcome_ttl_s": self.CODEX_V1_SHADOW_OUTCOME_TTL_S,
            "reason": reason,
            "policy_tag": codex_decision.policy_tag or metrics.get("policy_tag") or metrics.get("policy_note"),
            "requested_notional_usdc": raw_codex_decision.requested_notional_usdc,
            "raw_requested_notional_usdc": metrics.get("raw_requested_notional_usdc"),
            "fee_audit": fee_audit,
            "expected_capture_bp": fee_audit.get("expected_capture_bp"),
            "estimated_roundtrip_fee_bp": fee_audit.get("estimated_roundtrip_fee_bp"),
            "estimated_slippage_bp": fee_audit.get("estimated_slippage_bp"),
            "min_net_buffer_bp": fee_audit.get("min_net_buffer_bp"),
            "expected_net_buffer_bp": fee_audit.get("expected_net_buffer_bp"),
            "fee_buffer_pass": fee_audit.get("fee_buffer_pass"),
            "effective_status": effective_status,
            "cooldown_s": self.CODEX_V1_SHADOW_SAMPLE_COOLDOWN_S,
            "entry_ref_move_override_bp": self.CODEX_V1_SHADOW_ENTRY_REF_MOVE_BP,
            "max_shadow_samples_per_run": self.CODEX_V1_SHADOW_MAX_SAMPLES_PER_RUN,
            "raw_classifier": self._codex_v1_decision_snapshot(raw_codex_decision, features),
            "effective_execution": self._codex_v1_decision_snapshot(
                codex_decision,
                features,
                status="shadow_sample",
                effective_reason=reason,
                gaps=gaps,
                preflight=preflight,
            ),
            "decision": asdict(codex_decision),
            "features": self._codex_v1_payload_features(features),
        }
        if candidate_meta:
            await self._repo.log_event(
                run["run_id"],
                "entry_codex_v1_no_lane_candidate",
                {
                    "event_type": "no_lane_candidate_classified",
                    "version": CODEX_V1_VERSION,
                    "run_id": run["run_id"],
                    "symbol": symbol,
                    "reason": reason,
                    "side": side,
                    "strategy": strategy,
                    "shadow_lane": shadow_lane,
                    "candidate_lane": candidate_lane,
                    "candidate_bucket": candidate_bucket or None,
                    "nearest_lane_code": candidate_meta.get("nearest_lane_code"),
                    "nearest_lane_name": candidate_meta.get("nearest_lane_name"),
                    "nearest_lane_distance": candidate_meta.get("nearest_lane_distance"),
                    "nearest_lane_gaps": candidate_meta.get("nearest_lane_gaps") or {},
                    "missing_critical_features": candidate_meta.get("missing_critical_features"),
                    "failed_threshold_count": candidate_meta.get("failed_threshold_count"),
                    "candidate_classified_at_ms": start_ms,
                    "promotion_family": sample.get("promotion_family"),
                    "sampling_family": sample.get("sampling_family"),
                    "opportunity_id": opportunity_id,
                    "sample_id": sample_id,
                },
            )
        should_start, drop_reason = self._codex_v1_should_start_shadow_sample(sample)
        if not should_start and drop_reason == "per_run_cap":
            if await self._codex_v1_try_replace_lower_priority_shadow_sample(sample):
                should_start, drop_reason = self._codex_v1_should_start_shadow_sample(sample)
        if not should_start:
            drop = {
                key: sample.get(key)
                for key in (
                    "sample_id",
                    "opportunity_id",
                    "first_seen_run_id",
                    "last_seen_run_id",
                    "raw_block_rows_count",
                    "run_id",
                    "symbol",
                    "version",
                    "classifier_version",
                    "shadow_lane",
                    "candidate_lane",
                    "candidate_bucket",
                    "nearest_lane_code",
                    "nearest_lane_distance",
                    "promotion_family",
                    "sampling_family",
                    "sampling_quota_key",
                    "promotion_eligible",
                    "diagnostic_fill_model",
                    "sample_group_id",
                    "fee_buffer_pass",
                    "expected_net_buffer_bp",
                    "shadow_lane_family",
                    "side",
                    "strategy",
                    "entry_price",
                    "entry_reference_price",
                    "entry_price_bucket",
                    "tp_price_bucket",
                    "sl_price_bucket",
                    "opportunity_2min_bucket",
                    "version_family",
                    "sample_priority",
                    "fill_model",
                    "reason",
                    "policy_tag",
                )
            }
            drop.update(
                {
                    "event_type": "shadow_sample_dropped",
                    "drop_reason": drop_reason,
                    "cooldown_s": self.CODEX_V1_SHADOW_SAMPLE_COOLDOWN_S,
                    "entry_ref_move_override_bp": self.CODEX_V1_SHADOW_ENTRY_REF_MOVE_BP,
                    "max_shadow_samples_per_run": self.CODEX_V1_SHADOW_MAX_SAMPLES_PER_RUN,
                }
            )
            await self._repo.log_event(run["run_id"], "entry_codex_v1_shadow_sample_dropped", drop)
            return

        sample["strict_sample_id"] = sample_id
        diagnostic_sample: dict[str, Any] | None = None
        if diagnostic_fill_model:
            diagnostic_sample_id = "diag_" + self._codex_v1_shadow_stable_hash(sample_id, diagnostic_fill_model)
            sample["diagnostic_sample_id"] = diagnostic_sample_id
            diagnostic_sample = dict(sample)
            diagnostic_sample.update(
                {
                    "sample_id": diagnostic_sample_id,
                    "fill_model": diagnostic_fill_model,
                    "promotion_eligible": False,
                    "diagnostic_only": True,
                    "strict_sample_id": sample_id,
                    "diagnostic_sample_id": diagnostic_sample_id,
                    "promotion_block_reason": "diagnostic_fill_model",
                    "sampling_quota_key": f"{sample.get('sampling_quota_key')}:diagnostic",
                }
            )

        self._codex_v1_shadow_samples[sample_id] = sample
        self._codex_v1_shadow_sample_counts_by_run[str(run["run_id"])] = (
            self._codex_v1_shadow_sample_counts_by_run.get(str(run["run_id"]), 0) + 1
        )
        self._codex_v1_shadow_last_sample_by_scope[sample_scope_key] = {
            "start_ms": start_ms,
            "entry_price": round(entry, 8),
            "entry_price_bucket": entry_bucket,
            "sample_id": sample_id,
            "opportunity_id": opportunity_id,
            "shadow_lane": shadow_lane,
            "side": side,
            "sampling_family": sample.get("sampling_family"),
        }
        await self._repo.log_event(run["run_id"], "entry_codex_v1_shadow_sample_started", sample)
        if diagnostic_sample is not None:
            self._codex_v1_shadow_samples[str(diagnostic_sample["sample_id"])] = diagnostic_sample
            await self._repo.log_event(run["run_id"], "entry_codex_v1_shadow_sample_started", diagnostic_sample)
        await self._start_codex_v132_tp_policy_sample(sample, source_type="shadow_sample")

    @staticmethod
    def _codex_v1_shadow_path_bp(
        side: str,
        entry: float,
        observed_high: float,
        observed_low: float,
    ) -> tuple[float, float]:
        if entry <= 0:
            return 0.0, 0.0
        if side == "LONG":
            return (
                max(0.0, (observed_high - entry) / entry * 10_000.0),
                max(0.0, (entry - observed_low) / entry * 10_000.0),
            )
        return (
            max(0.0, (entry - observed_low) / entry * 10_000.0),
            max(0.0, (observed_high - entry) / entry * 10_000.0),
        )


    @staticmethod
    def _codex_v1_shadow_gross_pnl_bp(side: str, entry: float, exit_price: float) -> float:
        if entry <= 0 or exit_price <= 0:
            return 0.0
        if side == "LONG":
            return (exit_price - entry) / entry * 10000.0
        return (entry - exit_price) / entry * 10000.0

    def _codex_v1_shadow_paper_pnl(self, sample: Mapping[str, Any], outcome: Mapping[str, Any]) -> dict[str, Any]:
        shadow_outcome = str(outcome.get("shadow_outcome") or "none")
        if shadow_outcome == "no_fill":
            return {
                "paper_pnl_bp_before_fee": 0.0,
                "paper_pnl_bp_after_fee": 0.0,
                "paper_pnl_usdc_after_fee": 0.0,
                "fee_model": "maker_taker_estimate",
                "estimated_fee_bp": 0.0,
                "conservative_slippage_buffer_bp": 0.0,
            }
        entry = self._codex_v1_shadow_price(sample.get("entry_price")) or 0.0
        side = str(sample.get("side") or "").upper()
        tp = self._codex_v1_shadow_price(sample.get("tp_price"))
        sl = self._codex_v1_shadow_price(sample.get("sl_price"))
        exit_price = self._codex_v1_shadow_price(outcome.get("exit_reference_price"))
        if exit_price is None:
            if shadow_outcome == "tp1_first" and tp is not None:
                exit_price = tp
            elif shadow_outcome in {"sl_first", "ambiguous_both"} and sl is not None:
                exit_price = sl
            else:
                exit_price = entry
        gross_bp = self._codex_v1_shadow_gross_pnl_bp(side, entry, exit_price)
        features = sample.get("features") if isinstance(sample.get("features"), Mapping) else {}
        maker_fee_bp = self._codex_v1_shadow_feature_float(features, "maker_fee_bp") or 0.0
        fee_bp = max(0.0, maker_fee_bp) * 2.0
        slippage_buffer_bp = 0.0
        after_fee_bp = gross_bp - fee_bp - slippage_buffer_bp
        notional = sample.get("requested_notional_usdc") or sample.get("raw_requested_notional_usdc") or 0.0
        try:
            notional_value = float(notional)
        except (TypeError, ValueError):
            notional_value = 0.0
        return {
            "paper_pnl_bp_before_fee": round(gross_bp, 4),
            "paper_pnl_bp_after_fee": round(after_fee_bp, 4),
            "paper_pnl_usdc_after_fee": round(after_fee_bp / 10000.0 * max(0.0, notional_value), 6),
            "fee_model": "maker_taker_estimate",
            "estimated_fee_bp": round(fee_bp, 4),
            "conservative_slippage_buffer_bp": round(slippage_buffer_bp, 4),
            "exit_reference_price": round(exit_price, 8) if exit_price else None,
        }

    def _codex_v1_shadow_first_touch(
        self,
        sample: Mapping[str, Any],
        candles: Sequence[Candle],
    ) -> dict[str, Any] | None:
        entry = self._codex_v1_shadow_price(sample.get("entry_price"))
        tp = self._codex_v1_shadow_price(sample.get("tp_price"))
        sl = self._codex_v1_shadow_price(sample.get("sl_price"))
        side = str(sample.get("side") or "").upper()
        if entry is None or tp is None or sl is None or side not in {"LONG", "SHORT"}:
            return None
        start_ms = int(sample.get("start_ms") or 0)
        entry_ttl_s = int(sample.get("entry_ttl_s") or self.CODEX_V1_SHADOW_ENTRY_TTL_S)
        outcome_ttl_s = int(sample.get("outcome_ttl_s") or self.CODEX_V1_SHADOW_OUTCOME_TTL_S)
        entry_expiry_ms = start_ms + entry_ttl_s * 1000
        outcome_expiry_ms = start_ms + outcome_ttl_s * 1000
        fill_model = str(sample.get("fill_model") or "limit_touch")
        filled = fill_model == "immediate_shadow"
        filled_ms = start_ms if filled else None
        observed_high: float | None = None
        observed_low: float | None = None
        last_close_ms: int | None = None
        last_close: float | None = None

        def _fill_age_s(value_ms: int | None) -> float | None:
            if value_ms is None:
                return None
            return round(max(0, int(value_ms) - start_ms) / 1000.0, 1)

        def _fill_age_bucket(value_ms: int | None) -> str:
            return self._entry_fill_age_bucket(_fill_age_s(value_ms))

        for candle in sorted(candles, key=lambda item: int(item.open_time_ms)):
            open_ms = int(candle.open_time_ms)
            close_ms = open_ms + 60_000
            if close_ms <= start_ms:
                continue
            # With 1m bars we cannot safely split a candle around sample time.
            # Use only full candles opened at/after the sample to avoid using
            # pre-sample highs/lows as fake fills or TP/SL touches.
            if open_ms < start_ms:
                continue
            if open_ms >= outcome_expiry_ms:
                break
            high = float(candle.high)
            low = float(candle.low)
            last_close_ms = close_ms
            last_close = float(candle.close)

            if not filled:
                if close_ms > entry_expiry_ms:
                    return {
                        "shadow_outcome": "no_fill",
                        "filled": False,
                        "status": "resolved",
                        "fill_model": fill_model,
                        "filled_ts": None,
                        "entry_fill_age_s": None,
                        "entry_fill_age_bucket": "no_fill",
                        "resolved_ts": entry_expiry_ms,
                        "hit_time_ms": entry_expiry_ms,
                        "elapsed_s": round(max(0, entry_expiry_ms - start_ms) / 1000.0, 1),
                        "tp_hit": False,
                        "sl_hit": False,
                        "mfe_bp": 0.0,
                        "mae_bp": 0.0,
                        "ambiguity_flag": False,
                        "bar_resolution_note": "entry_ttl_boundary_conservative_no_fill",
                    }
                if side == "LONG":
                    filled = low <= entry
                else:
                    filled = high >= entry
                if filled:
                    filled_ms = close_ms
                    # The fill candle's high/low has unknown order relative to
                    # the fill, so first-touch evaluation starts next candle.
                    continue
                continue

            if close_ms > outcome_expiry_ms:
                break
            observed_high = high if observed_high is None else max(observed_high, high)
            observed_low = low if observed_low is None else min(observed_low, low)
            if side == "LONG":
                tp_hit = high >= tp
                sl_hit = low <= sl
                tp_exit = tp
                sl_exit = sl
            else:
                tp_hit = low <= tp
                sl_hit = high >= sl
                tp_exit = tp
                sl_exit = sl
            if not tp_hit and not sl_hit:
                continue
            if tp_hit and sl_hit:
                shadow_outcome = "ambiguous_both"
                exit_price = sl_exit
            elif tp_hit:
                shadow_outcome = "tp1_first"
                exit_price = tp_exit
            else:
                shadow_outcome = "sl_first"
                exit_price = sl_exit
            mfe_bp, mae_bp = self._codex_v1_shadow_path_bp(side, entry, observed_high, observed_low)
            return {
                "shadow_outcome": shadow_outcome,
                "filled": True,
                "status": "resolved",
                "fill_model": fill_model,
                "filled_ts": filled_ms,
                "entry_fill_age_s": _fill_age_s(filled_ms),
                "entry_fill_age_bucket": _fill_age_bucket(filled_ms),
                "resolved_ts": close_ms,
                "hit_time_ms": close_ms,
                "hit_candle_open_ms": open_ms,
                "elapsed_s": round(max(0, close_ms - start_ms) / 1000.0, 1),
                "hit_high": round(high, 8),
                "hit_low": round(low, 8),
                "tp_hit": tp_hit,
                "sl_hit": sl_hit,
                "mfe_bp": round(mfe_bp, 4),
                "mae_bp": round(mae_bp, 4),
                "exit_reference_price": round(exit_price, 8),
                "ambiguity_flag": bool(tp_hit and sl_hit),
            }

        if not filled and last_close_ms is not None and last_close_ms >= entry_expiry_ms:
            return {
                "shadow_outcome": "no_fill",
                "filled": False,
                "status": "resolved",
                "fill_model": fill_model,
                "filled_ts": None,
                "entry_fill_age_s": None,
                "entry_fill_age_bucket": "no_fill",
                "resolved_ts": entry_expiry_ms,
                "hit_time_ms": entry_expiry_ms,
                "elapsed_s": round(max(0, entry_expiry_ms - start_ms) / 1000.0, 1),
                "tp_hit": False,
                "sl_hit": False,
                "mfe_bp": 0.0,
                "mae_bp": 0.0,
                "ambiguity_flag": False,
            }
        if filled and last_close_ms is not None and last_close_ms >= outcome_expiry_ms:
            observed_high = observed_high if observed_high is not None else entry
            observed_low = observed_low if observed_low is not None else entry
            mfe_bp, mae_bp = self._codex_v1_shadow_path_bp(side, entry, observed_high, observed_low)
            exit_price = last_close if last_close is not None else entry
            return {
                "shadow_outcome": "none",
                "filled": True,
                "status": "resolved",
                "fill_model": fill_model,
                "filled_ts": filled_ms,
                "entry_fill_age_s": _fill_age_s(filled_ms),
                "entry_fill_age_bucket": _fill_age_bucket(filled_ms),
                "resolved_ts": outcome_expiry_ms,
                "hit_time_ms": outcome_expiry_ms,
                "elapsed_s": round(outcome_ttl_s, 1),
                "tp_hit": False,
                "sl_hit": False,
                "mfe_bp": round(mfe_bp, 4),
                "mae_bp": round(mae_bp, 4),
                "exit_reference_price": round(exit_price, 8),
                "ambiguity_flag": False,
            }
        return None


    async def _log_codex_v1_shadow_outcome(
        self,
        key: str,
        sample: Mapping[str, Any],
        outcome: Mapping[str, Any],
        *,
        terminal_reason: str | None = None,
    ) -> None:
        if key in self._codex_v1_shadow_outcomes_logged:
            return
        shadow_outcome = str(outcome.get("shadow_outcome") or "none")
        details = {
            "event_type": "shadow_outcome",
            "sample_id": sample.get("sample_id"),
            "opportunity_id": sample.get("opportunity_id"),
            "first_seen_run_id": sample.get("first_seen_run_id"),
            "last_seen_run_id": sample.get("last_seen_run_id"),
            "raw_block_rows_count": sample.get("raw_block_rows_count"),
            "run_id": sample.get("run_id"),
            "symbol": sample.get("symbol"),
            "version": sample.get("version") or CODEX_V1_VERSION,
            "classifier_version": sample.get("classifier_version"),
            "baseline": sample.get("baseline"),
            "shadow_lane": sample.get("shadow_lane"),
            "shadow_lane_family": sample.get("shadow_lane_family"),
            "shadow_lane_reason": sample.get("shadow_lane_reason"),
            "shadow_reprice_state": sample.get("shadow_reprice_state"),
            "candidate_lane": sample.get("candidate_lane"),
            "candidate_bucket": sample.get("candidate_bucket"),
            "nearest_lane_code": sample.get("nearest_lane_code"),
            "nearest_lane_name": sample.get("nearest_lane_name"),
            "nearest_lane_distance": sample.get("nearest_lane_distance"),
            "nearest_lane_gaps": sample.get("nearest_lane_gaps") or {},
            "missing_critical_features": sample.get("missing_critical_features"),
            "failed_threshold_count": sample.get("failed_threshold_count"),
            "candidate_classified_at_ms": sample.get("candidate_classified_at_ms"),
            "candidate_reason": sample.get("candidate_reason"),
            "promotion_family": sample.get("promotion_family"),
            "sampling_family": sample.get("sampling_family"),
            "sampling_quota_key": sample.get("sampling_quota_key"),
            "promotion_eligible": sample.get("promotion_eligible"),
            "diagnostic_fill_model": sample.get("diagnostic_fill_model"),
            "diagnostic_only": sample.get("diagnostic_only"),
            "sample_group_id": sample.get("sample_group_id"),
            "strict_sample_id": sample.get("strict_sample_id"),
            "diagnostic_sample_id": sample.get("diagnostic_sample_id"),
            "promotion_block_reason": sample.get("promotion_block_reason"),
            "fee_audit": sample.get("fee_audit"),
            "expected_capture_bp": sample.get("expected_capture_bp"),
            "estimated_roundtrip_fee_bp": sample.get("estimated_roundtrip_fee_bp"),
            "estimated_slippage_bp": sample.get("estimated_slippage_bp"),
            "min_net_buffer_bp": sample.get("min_net_buffer_bp"),
            "expected_net_buffer_bp": sample.get("expected_net_buffer_bp"),
            "fee_buffer_pass": sample.get("fee_buffer_pass"),
            "mapping_reason": sample.get("mapping_reason"),
            "secondary_reasons": sample.get("secondary_reasons") or [],
            "lane_code": sample.get("lane_code"),
            "lane": sample.get("lane"),
            "strategy": sample.get("strategy"),
            "side": sample.get("side"),
            "start_ms": sample.get("start_ms"),
            "entry_price": sample.get("entry_price"),
            "tp_price": sample.get("tp_price"),
            "sl_price": sample.get("sl_price"),
            "entry_reference_price": sample.get("entry_reference_price"),
            "entry_price_bucket": sample.get("entry_price_bucket"),
            "tp_price_bucket": sample.get("tp_price_bucket"),
            "sl_price_bucket": sample.get("sl_price_bucket"),
            "opportunity_2min_bucket": sample.get("opportunity_2min_bucket"),
            "version_family": sample.get("version_family"),
            "sample_priority": sample.get("sample_priority"),
            "fill_model": sample.get("fill_model"),
            "entry_ttl_s": sample.get("entry_ttl_s"),
            "outcome_ttl_s": sample.get("outcome_ttl_s"),
            "reason": sample.get("reason"),
            "policy_tag": sample.get("policy_tag"),
            "requested_notional_usdc": sample.get("requested_notional_usdc"),
            "raw_requested_notional_usdc": sample.get("raw_requested_notional_usdc"),
            "shadow_outcome": shadow_outcome,
            "outcome": shadow_outcome,
            "terminal_reason": terminal_reason,
            "raw_classifier": sample.get("raw_classifier"),
            "effective_execution": sample.get("effective_execution"),
            "decision": sample.get("decision"),
            "features": sample.get("features"),
        }
        details.update(dict(outcome))
        if details.get("entry_fill_age_bucket") is None:
            if shadow_outcome == "no_fill" or details.get("filled") is False:
                details["entry_fill_age_s"] = None
                details["entry_fill_age_bucket"] = "no_fill"
            else:
                try:
                    filled_ts = details.get("filled_ts")
                    start_ms = details.get("start_ms")
                    age_s = (
                        round(max(0, int(filled_ts) - int(start_ms)) / 1000.0, 1)
                        if filled_ts is not None and start_ms is not None
                        else None
                    )
                except (TypeError, ValueError):
                    age_s = None
                details["entry_fill_age_s"] = age_s
                details["entry_fill_age_bucket"] = self._entry_fill_age_bucket(age_s)
        details.update(self._codex_v1_shadow_paper_pnl(sample, details))
        if details.get("diagnostic_only") or details.get("promotion_eligible") is False or str(details.get("fill_model") or "") != "limit_touch":
            details["promotion_counts_as"] = "diagnostic_only"
        elif terminal_reason == "live_entry_submitted" or details.get("shadow_outcome") == "terminated":
            details["promotion_counts_as"] = "excluded_terminal"
        elif details.get("shadow_outcome") == "ambiguous_both":
            details["promotion_counts_as"] = "sl_failure"
        elif details.get("shadow_outcome") == "sl_first":
            details["promotion_counts_as"] = "sl_failure"
        elif details.get("shadow_outcome") == "tp1_first":
            details["promotion_counts_as"] = "tp_success"
        else:
            details["promotion_counts_as"] = details.get("shadow_outcome")
        self._codex_v1_shadow_outcomes_logged.add(key)
        self._codex_v1_shadow_samples.pop(key, None)
        await self._repo.log_event(str(sample.get("run_id")), "entry_codex_v1_shadow_outcome", details)

    async def _update_codex_v1_shadow_outcomes(self, run: Mapping[str, Any], candles: Sequence[Candle]) -> None:
        if not candles:
            return
        run_id = str(run.get("run_id") or "")
        for key, sample in list(self._codex_v1_shadow_samples.items()):
            if str(sample.get("run_id") or "") != run_id:
                continue
            outcome = self._codex_v1_shadow_first_touch(sample, candles)
            if outcome is not None:
                await self._log_codex_v1_shadow_outcome(key, sample, outcome)
        await self._update_codex_v132_tp_policy_outcomes(run, candles)

    async def _expire_codex_v1_shadow_samples(
        self,
        run: Mapping[str, Any],
        reason: str,
        candles: Sequence[Candle] | None = None,
    ) -> None:
        if candles:
            await self._update_codex_v1_shadow_outcomes(run, candles)
        run_id = str(run.get("run_id") or "")
        now_ms = int(time.time() * 1000)
        for key, sample in list(self._codex_v1_shadow_samples.items()):
            if str(sample.get("run_id") or "") != run_id:
                continue
            start_ms = int(sample.get("start_ms") or now_ms)
            terminal_outcome = "terminated" if reason == "live_entry_submitted" else "none"
            await self._log_codex_v1_shadow_outcome(
                key,
                sample,
                {
                    "shadow_outcome": terminal_outcome,
                    "hit_time_ms": now_ms,
                    "elapsed_s": round(max(0, now_ms - start_ms) / 1000.0, 1),
                    "excluded_from_promotion": reason == "live_entry_submitted",
                },
                terminal_reason=reason,
            )

    def _clear_codex_v1_shadow_samples(self, run_id: str) -> None:
        self._codex_v1_shadow_samples = {
            key: value
            for key, value in self._codex_v1_shadow_samples.items()
            if str(value.get("run_id") or "") != str(run_id)
        }
        self._codex_v1_shadow_sample_counts_by_run.pop(str(run_id), None)

    def _clear_codex_v132_tp_policy_samples(self, run_id: str) -> None:
        self._codex_v132_tp_policy_samples = {
            key: value
            for key, value in self._codex_v132_tp_policy_samples.items()
            if str(value.get("run_id") or "") != str(run_id)
        }

    async def _drop_codex_v132_tp_policy_samples(self, run_id: str, reason: str) -> None:
        now_ms = int(time.time() * 1000)
        for paired_sample_id, active in list(self._codex_v132_tp_policy_samples.items()):
            if str(active.get("run_id") or "") != str(run_id):
                continue
            start_ms = int(active.get("start_ms") or now_ms)
            details = {
                **dict(active),
                "event_type": "tp_policy_shadow_dropped",
                "drop_reason": reason,
                "dropped_at_ms": now_ms,
                "elapsed_s": round(max(0, now_ms - start_ms) / 1000.0, 1),
            }
            outcome_id = f"tpdrop_{paired_sample_id}_{reason}"
            if outcome_id not in self._codex_v132_tp_policy_outcomes_logged:
                self._codex_v132_tp_policy_outcomes_logged.add(outcome_id)
                await self._repo.log_event(run_id, "entry_codex_v1_tp_policy_shadow_dropped", details)
            self._codex_v132_tp_policy_samples.pop(paired_sample_id, None)

    def _codex_v132_enabled(self) -> bool:
        return bool(
            self.CODEX_TP_POLICY_SHADOW_ENABLED
            and getattr(self._settings, "mainnet_codex_tp_policy_shadow_enabled", True)
        )

    @staticmethod
    def _codex_v132_event_details(event: Mapping[str, Any]) -> dict[str, Any]:
        details = event.get("details")
        if isinstance(details, Mapping):
            return dict(details)
        raw = event.get("details_json")
        if isinstance(raw, Mapping):
            return dict(raw)
        if isinstance(raw, str) and raw:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                return {}
            if isinstance(parsed, Mapping):
                return dict(parsed)
        return {}

    async def _rehydrate_codex_v132_tp_policy_samples(self, run: Mapping[str, Any]) -> None:
        if not self._codex_v132_enabled():
            return
        run_id = str(run.get("run_id") or "")
        if not run_id or run_id in self._codex_v132_rehydrated_runs:
            return
        event_types = (
            "entry_codex_v1_tp_policy_shadow_started",
            "entry_codex_v1_tp_policy_shadow_outcome",
            "entry_codex_v1_tp_policy_shadow_dropped",
        )
        get_events_by_types = getattr(self._repo, "get_events_by_types", None)
        get_events = getattr(self._repo, "get_events", None)
        if not callable(get_events_by_types) and not callable(get_events):
            self._codex_v132_rehydrated_runs.add(run_id)
            return
        try:
            if callable(get_events_by_types):
                events = await get_events_by_types(run_id, event_types, limit=5000)
            else:
                events = await get_events(run_id, limit=5000)
                events = [event for event in events if str(event.get("event_type") or "") in event_types]
        except Exception as exc:  # noqa: BLE001
            logger.warning("codex_v132_tp_policy_rehydrate_events_failed", run_id=run_id, error=str(exc)[:200])
            return
        self._codex_v132_rehydrated_runs.add(run_id)

        terminal_pairs: set[str] = set()
        started_by_pair: dict[str, dict[str, Any]] = {}
        for event in events:
            event_type = str(event.get("event_type") or "")
            details = self._codex_v132_event_details(event)
            paired_id = str(details.get("paired_sample_id") or "")
            if not paired_id:
                continue
            if event_type in {"entry_codex_v1_tp_policy_shadow_outcome", "entry_codex_v1_tp_policy_shadow_dropped"}:
                terminal_pairs.add(paired_id)

        for event in events:
            event_type = str(event.get("event_type") or "")
            if event_type != "entry_codex_v1_tp_policy_shadow_started":
                continue
            details = self._codex_v132_event_details(event)
            paired_id = str(details.get("paired_sample_id") or "")
            if not paired_id or paired_id in terminal_pairs or paired_id in started_by_pair:
                continue
            started_by_pair[paired_id] = details

        restored: list[str] = []
        for paired_id, active in started_by_pair.items():
            if paired_id in self._codex_v132_tp_policy_samples:
                continue
            active = dict(active)
            active["rehydrated_from_event_log"] = True
            self._codex_v132_tp_policy_samples[paired_id] = active
            restored.append(paired_id)

        if restored:
            logger.info("codex_v132_tp_policy_rehydrated", run_id=run_id, samples=len(restored))
            try:
                await self._repo.log_event(
                    run_id,
                    "entry_codex_v1_tp_policy_shadow_rehydrated",
                    {
                        "event_type": "tp_policy_shadow_rehydrated",
                        "run_id": run_id,
                        "restored_count": len(restored),
                        "paired_sample_ids": restored[:20],
                    },
                )
            except Exception as exc:  # noqa: BLE001 - audit write must not affect run management
                logger.warning("codex_v132_tp_policy_rehydrated_log_failed", run_id=run_id, error=str(exc)[:200])
    async def _start_codex_v132_tp_policy_sample(
        self,
        sample: Mapping[str, Any],
        *,
        source_type: str,
        actual_live_pnl_bp_after_fee: float | None = None,
    ) -> None:
        if not self._codex_v132_enabled():
            return
        active = build_tp_policy_active_sample(
            self._settings,
            sample,
            source_type=source_type,
            actual_live_pnl_bp_after_fee=actual_live_pnl_bp_after_fee,
            baseline_override=sample.get("baseline_override") if isinstance(sample.get("baseline_override"), Mapping) else None,
        )
        if not active:
            return
        paired_sample_id = str(active.get("paired_sample_id") or "")
        if not paired_sample_id or paired_sample_id in self._codex_v132_tp_policy_samples:
            return
        self._codex_v132_tp_policy_samples[paired_sample_id] = active
        await self._repo.log_event(str(active.get("run_id")), "entry_codex_v1_tp_policy_shadow_started", active)

    async def _start_codex_v132_live_tp_policy_sample(
        self,
        run: Mapping[str, Any],
        position: PositionInfo,
        signal: Mapping[str, Any],
        tp_orders: Sequence[tuple[str, str, float]] | None = None,
        *,
        fill_detected_ms: int | None = None,
    ) -> None:
        side = "LONG" if position.position_amt > 0 else "SHORT"
        entry = float(position.entry_price or 0.0)
        full_tp = self._codex_v1_shadow_price(signal.get("take_profit"))
        tp_pct = float((signal.get("wildcat") or {}).get("tp_pct") or 0.0)
        if tp_pct > 0 and entry > 0:
            full_tp = entry * (1 + tp_pct) if side == "LONG" else entry * (1 - tp_pct)
        sl = self._codex_v1_shadow_price(signal.get("stop_loss"))
        if entry <= 0 or not full_tp or not sl:
            return
        codex = signal.get("codex_v1") if isinstance(signal.get("codex_v1"), Mapping) else {}
        run_id = str(run.get("run_id") or "")
        sample = {
            "sample_id": f"live_{run_id}",
            "run_id": run_id,
            "opportunity_id": f"live_{run_id}",
            "first_seen_run_id": run_id,
            "symbol": run.get("symbol") or position.symbol,
            "version": self.CODEX_TP_POLICY_VERSION,
            "classifier_version": CODEX_V1_VERSION,
            "shadow_lane": None,
            "candidate_lane": codex.get("lane_code") or run.get("strategy_label") or "LIVE",
            "shadow_lane_family": codex.get("lane_code") or "LIVE",
            "side": side,
            "strategy": codex.get("strategy") or signal.get("strategy") or run.get("strategy_label") or "LIVE",
            "start_ms": int(fill_detected_ms or time.time() * 1000),
            "entry_price": round(entry, 8),
            "tp_price": round(float(full_tp), 8),
            "sl_price": round(float(sl), 8),
            "fill_model": "immediate_shadow",
            "entry_ttl_s": 0,
            "outcome_ttl_s": self.CODEX_TP_POLICY_PATH_TTL_S,
            "reason": "live_entry_filled_tp_policy_shadow",
            "requested_notional_usdc": float(run.get("cumulative_notional_usdc") or 0.0),
            "features": {},
        }
        baseline_override = build_tp_policy_baseline_from_order_plan(
            self._settings,
            sample,
            current_qty=abs(position.position_amt),
            orders=tp_orders or [],
        )
        if baseline_override:
            sample["baseline_override"] = baseline_override
            sample["baseline_source"] = "actual_tp_order_plan"
        else:
            sample["baseline_source"] = "settings_fallback"
        await self._start_codex_v132_tp_policy_sample(sample, source_type="live_trade")

    def _codex_v132_has_active_tp_policy_sample(self, run_id: str) -> bool:
        return any(str(sample.get("run_id") or "") == str(run_id) for sample in self._codex_v132_tp_policy_samples.values())

    async def _update_codex_v132_tp_policy_outcomes(
        self,
        run: Mapping[str, Any],
        candles: Sequence[Candle],
        *,
        force_terminal: bool = False,
        terminal_reason: str | None = None,
    ) -> None:
        if not candles or not self._codex_v132_enabled():
            return
        run_id = str(run.get("run_id") or "")
        for paired_sample_id, active in list(self._codex_v132_tp_policy_samples.items()):
            if str(active.get("run_id") or "") != run_id:
                continue
            outcomes = build_tp_policy_outcomes(
                self._settings,
                active,
                candles,
                force_terminal=force_terminal,
                terminal_reason=terminal_reason,
            )
            if outcomes is None:
                continue
            if not outcomes:
                self._codex_v132_tp_policy_samples.pop(paired_sample_id, None)
                continue
            for details in outcomes:
                outcome_id = str(details.get("tp_policy_outcome_id") or "")
                if outcome_id and outcome_id in self._codex_v132_tp_policy_outcomes_logged:
                    continue
                if outcome_id:
                    self._codex_v132_tp_policy_outcomes_logged.add(outcome_id)
                await self._repo.log_event(run_id, "entry_codex_v1_tp_policy_shadow_outcome", details)
            self._codex_v132_tp_policy_samples.pop(paired_sample_id, None)

    async def _terminalize_codex_v132_tp_policy_samples(
        self,
        run: Mapping[str, Any],
        summary: Mapping[str, Any],
        candles: Sequence[Candle],
    ) -> None:
        if not getattr(self._settings, "mainnet_codex_v133_tp_terminalization_enabled", True):
            await self._drop_codex_v132_tp_policy_samples(str(run.get("run_id") or ""), "run_finished")
            return
        run_id = str(run.get("run_id") or "")
        if not run_id or not self._codex_v132_has_active_tp_policy_sample(run_id):
            return
        try:
            notional = float(run.get("cumulative_notional_usdc") or 0.0)
        except (TypeError, ValueError):
            notional = 0.0
        realized = float(summary.get("realized_pnl_usdc") or 0.0)
        commission = float(summary.get("commission_usdc") or 0.0)
        net = realized - commission
        actual_live_pnl_bp_after_fee = (net / notional * 10000.0) if notional > 0 else None
        for active in self._codex_v132_tp_policy_samples.values():
            if str(active.get("run_id") or "") != run_id:
                continue
            if active.get("source_type") != "live_trade":
                continue
            active["actual_live_pnl_usdc_after_fee"] = round(net, 8)
            active["actual_live_realized_pnl_usdc"] = round(realized, 8)
            active["actual_live_commission_usdc"] = round(commission, 8)
            active["actual_live_pnl_bp_after_fee"] = (
                round(actual_live_pnl_bp_after_fee, 4)
                if actual_live_pnl_bp_after_fee is not None
                else None
            )
            active["terminalization_version"] = "v1.3.3"
            active["terminalization_trigger"] = "run_finished"
        if candles:
            await self._update_codex_v132_tp_policy_outcomes(
                run,
                candles,
                force_terminal=True,
                terminal_reason="terminalized_from_live_run",
            )
        if self._codex_v132_has_active_tp_policy_sample(run_id):
            await self._drop_codex_v132_tp_policy_samples(run_id, "invalid_missing_live_path")

    @staticmethod
    def _truthy_order_flag(value: Any) -> bool:
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @staticmethod
    def _codex_v1_payload_features(features: Mapping[str, Any]) -> dict[str, Any]:
        keys = (
            "symbol",
            "strategy",
            "side",
            "score",
            "rng15",
            "d30",
            "adv3",
            "d3",
            "d5",
            "range_bp",
            "ret3_bp",
            "close_pos",
            "range_pos_5",
            "range_pos_15",
            "range_pos_30",
            "rsi",
            "bb_lower_dist_bp",
            "vwap_dist_bp",
            "pullback_from_recent_high_bp",
            "price_above_or_reclaimed_vwap",
            "setup_age_sec",
            "setup_started_at_ms",
            "reprice_wait_elapsed_seconds",
            "reprice_wait_remaining_seconds",
            "reprice_favorable_bp",
            "reprice_adverse_bp",
            "reprice_shadow_ref_px",
            "spread_bp",
            "feature_age_seconds",
            "maker_fee_bp",
            "open_position",
            "open_entry_order",
            "open_reduce_order",
            "kill_switch",
        )
        payload: dict[str, Any] = {}
        for key in keys:
            if key not in features:
                continue
            value = features[key]
            if isinstance(value, float):
                payload[key] = round(value, 6)
            else:
                payload[key] = value
        return payload

    @classmethod
    def _codex_v1_decision_snapshot(
        cls,
        decision: CodexV1Decision,
        features: Mapping[str, Any] | None = None,
        *,
        status: str | None = None,
        effective_reason: str | None = None,
        gaps: Sequence[str] = (),
        preflight: Sequence[str] = (),
    ) -> dict[str, Any]:
        snapshot = {
            "accepted": bool(decision.accepted),
            "version": decision.version,
            "baseline": decision.baseline,
            "lane_code": decision.lane_code,
            "lane": decision.lane,
            "strategy": decision.strategy,
            "side": decision.side,
            "entry_offset_bp": decision.entry_offset_bp,
            "size_mult": decision.size_mult,
            "notional_mult": decision.notional_mult,
            "requested_notional_usdc": decision.requested_notional_usdc,
            "reason": decision.reason,
            "regime": decision.regime,
            "missing_features": list(decision.missing_features),
            "risk_tags": list(decision.risk_tags),
            "metrics": getattr(decision, "metrics", None),
            "policy_tag": getattr(decision, "policy_tag", None),
            "shadow_lane": getattr(decision, "shadow_lane", None),
        }
        if status is not None:
            snapshot["status"] = status
        if effective_reason is not None:
            snapshot["effective_reason"] = effective_reason
        if gaps:
            snapshot["gaps"] = list(gaps)
        if preflight:
            snapshot["preflight"] = list(preflight)
        if features is not None:
            snapshot["features"] = cls._codex_v1_payload_features(features)
        return snapshot

    @staticmethod
    def _codex_v1_signal_meta(signal_or_run: Mapping[str, Any] | dict | None) -> dict[str, Any]:
        if not signal_or_run:
            return {}
        if "codex_v1" in signal_or_run:
            codex = signal_or_run.get("codex_v1") or {}
            return codex if isinstance(codex, dict) else {}
        signal_json = signal_or_run.get("signal_json") if isinstance(signal_or_run, Mapping) else None
        if not signal_json:
            return {}
        try:
            parsed = json.loads(signal_json)
        except Exception:
            return {}
        codex = parsed.get("codex_v1") or {}
        return codex if isinstance(codex, dict) else {}

    @classmethod
    def _codex_v1_telegram_note(cls, signal_or_run: Mapping[str, Any] | dict | None) -> str:
        codex = cls._codex_v1_signal_meta(signal_or_run)
        if not (
            codex.get("enabled")
            or codex.get("lane_code")
            or codex.get("lane")
            or codex.get("raw_classifier")
            or codex.get("effective_execution")
        ):
            return ""
        version = escape(str(codex.get("version") or CODEX_V1_VERSION))
        lane_code = escape(str(codex.get("lane_code") or codex.get("lane") or "UNKNOWN"))
        lane_rule = escape(str(codex.get("lane") or "UNKNOWN"))
        raw = codex.get("raw_classifier") or {}
        effective = codex.get("effective_execution") or {}
        raw_lane = escape(str(raw.get("lane_code") or raw.get("lane") or lane_code))
        raw_rule = escape(str(raw.get("lane") or lane_rule))
        effective_status = escape(str(effective.get("status") or "unknown"))
        effective_reason = escape(str(effective.get("effective_reason") or effective.get("reason") or "accepted"))
        metrics = codex.get("metrics") if isinstance(codex.get("metrics"), Mapping) else {}
        policy_note = codex.get("policy_tag") or codex.get("policy_note") or metrics.get("policy_tag") or metrics.get("policy_note")
        applied_notional = codex.get("applied_notional_usdc")
        requested_notional = codex.get("requested_notional_usdc")
        sl_tp_ratio = metrics.get("sl_tp_ratio")
        extra_lines = []
        if policy_note:
            extra_lines.append(f"Policy Tag：<code>{escape(str(policy_note))}</code>")
        try:
            applied_value = float(applied_notional) if applied_notional is not None else None
        except (TypeError, ValueError):
            applied_value = None
        try:
            requested_value = float(requested_notional) if requested_notional is not None else None
        except (TypeError, ValueError):
            requested_value = None
        if applied_value is not None and math.isfinite(applied_value):
            if requested_value is not None and math.isfinite(requested_value) and requested_value > 0:
                if applied_value < requested_value - 1e-9:
                    extra_lines.append(
                        "Effective Notional："
                        f"<code>${applied_value:.2f}</code> / raw <code>${requested_value:.2f}</code>"
                    )
                else:
                    extra_lines.append(f"Effective Notional：<code>${applied_value:.2f}</code>")
            else:
                extra_lines.append(f"Effective Notional：<code>${applied_value:.2f}</code>")
        if applied_value is not None and requested_value not in (None, 0, 0.0):
            try:
                final_size = applied_value / float(requested_value)
            except (TypeError, ValueError, ZeroDivisionError):
                final_size = None
            if final_size is not None and math.isfinite(final_size):
                label = "Cap Ratio" if applied_value < float(requested_value) - 1e-9 else "Final Size"
                extra_lines.append(f"{label}：<code>{final_size:.2f}x</code>")
        if sl_tp_ratio is not None:
            extra_lines.append(f"Payoff：<code>sl_tp_ratio={escape(str(sl_tp_ratio))}</code>")
        extra = "\n" + "\n".join(extra_lines) if extra_lines else ""
        return (
            "\n"
            f"版本：<code>{version}</code>\n"
            f"Lane Code：<code>{lane_code}</code>\n"
            f"Full Lane：<code>{lane_rule}</code>\n"
            f"Raw Classifier：<code>{raw_lane}</code>\n"
            f"Raw Rule：<code>{raw_rule}</code>\n"
            f"Effective Execution：<code>{lane_code}</code> / <code>{effective_status}</code>\n"
            f"Live Reason：<code>{effective_reason}</code>"
            f"{extra}"
        )

    @staticmethod
    def _entry_fill_age_bucket(age_s: Any) -> str:
        if age_s is None:
            return "unknown"
        try:
            age = float(age_s)
        except (TypeError, ValueError):
            return "unknown"
        if not math.isfinite(age) or age < 0:
            return "unknown"
        if age <= 45:
            return "0-45s"
        if age <= 90:
            return "45-90s"
        if age <= 180:
            return "90-180s"
        return "180s+"

    @staticmethod
    def _codex_v1_signal_payload(run: Mapping[str, Any]) -> dict[str, Any]:
        raw = run.get("signal_json")
        if isinstance(raw, Mapping):
            return dict(raw)
        if not raw:
            return {}
        try:
            parsed = json.loads(str(raw))
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}

    @staticmethod
    def _codex_v1_signal_lane_code(signal: Mapping[str, Any]) -> str:
        codex = signal.get("codex_v1") if isinstance(signal, Mapping) else None
        if not isinstance(codex, Mapping):
            return ""
        candidates: list[Any] = [codex.get("lane_code")]
        effective_execution = codex.get("effective_execution")
        if isinstance(effective_execution, Mapping):
            candidates.append(effective_execution.get("lane_code"))
        raw_classifier = codex.get("raw_classifier")
        if isinstance(raw_classifier, Mapping):
            candidates.append(raw_classifier.get("lane_code"))
        for value in candidates:
            text = str(value or "").strip().upper()
            if text:
                return text
        return ""

    def _codex_v1_entry_ttl_overrides(self) -> dict[str, int]:
        raw = str(getattr(self._settings, "mainnet_codex_v135_entry_ttl_seconds_by_lane", "") or "")
        overrides: dict[str, int] = {}
        for chunk in raw.split(","):
            item = chunk.strip()
            if not item:
                continue
            sep = ":" if ":" in item else "=" if "=" in item else None
            if sep is None:
                continue
            lane, seconds = item.split(sep, 1)
            lane_key = lane.strip().upper()
            if not lane_key:
                continue
            try:
                ttl_s = int(float(seconds.strip()))
            except (TypeError, ValueError):
                continue
            if ttl_s > 0:
                overrides[lane_key] = ttl_s
        return overrides

    def _codex_v1_live_entry_ttl_policy(self, run: Mapping[str, Any]) -> dict[str, Any]:
        default_ttl = max(1, int(getattr(self._settings, "mainnet_entry_order_ttl_seconds", 45) or 45))
        signal = self._codex_v1_signal_payload(run)
        codex = signal.get("codex_v1") if isinstance(signal, Mapping) else None
        lane_code = self._codex_v1_signal_lane_code(signal)
        policy = {
            "ttl_seconds": default_ttl,
            "ttl_source": "global_entry_order_ttl",
            "lane_code": lane_code or None,
        }
        if not getattr(self._settings, "mainnet_codex_v135_entry_ttl_by_lane_enabled", True):
            return policy
        if not isinstance(codex, Mapping) or not codex.get("enabled"):
            return policy
        overrides = self._codex_v1_entry_ttl_overrides()
        ttl_s = overrides.get(lane_code)
        if ttl_s is None:
            return policy
        policy["ttl_seconds"] = ttl_s
        policy["ttl_source"] = "codex_v135_lane_override"
        return policy

    @staticmethod
    def _run_uses_codex_v1(run: Mapping[str, Any]) -> bool:
        signal = MainnetOneRunManager._codex_v1_signal_payload(run)
        codex = signal.get("codex_v1") or {}
        return isinstance(codex, Mapping) and bool(codex.get("enabled"))

    async def _run_armed(self, run: dict) -> None:
        if int(time.time() * 1000) - int(run["armed_at_ms"]) > self._settings.mainnet_one_run_signal_timeout_minutes * 60_000:
            await self._expire_codex_v1_shadow_samples(run, "signal_timeout")
            await self._repo.complete_run(run["run_id"], "ENTRY_EXPIRED", "signal_timeout")
            await self._notify(f"⌛ Mainnet one-run 等待訊號逾時，已停止：<code>{escape(run['run_id'])}</code>")
            await self._advance_loop_after_entry_failure(run, "signal_timeout")
            return
        candles = await self._load_candles(run["symbol"])
        await self._update_codex_v1_shadow_outcomes(run, candles)

        # Calculate pre-entry 15m price range (rng15) in bp (excluding current bar)
        rng15 = 0.0
        if len(candles) >= 16:
            window = candles[-16:-1]
            hi = max(c.high for c in window)
            lo = min(c.low for c in window)
            px = candles[-1].close
            rng15 = (hi - lo) / px * 1e4 if px > 0 else 0.0

        # Sizing scale inside the sweet zone (default scale 1.0 = off).
        s = self._settings
        notional_scale = (
            s.mainnet_rng15_sweet_scale
            if s.mainnet_rng15_sweet_low_bp <= rng15 < s.mainnet_rng15_sweet_high_bp
            else 1.0
        )
        # Signed net drift over the range window (bp).  Persisted per entry as
        # "drift30" for offline regime bucketing — golden-window forensics
        # (06-10) showed drift, not WR, separates the +2.67 range segment
        # (+6bp/5.1h) from the −183bp-downtrend losing segment — and it
        # optionally boosts sizing in confirmed-range tape (default OFF).
        drift_bp = self._signed_drift_bp(candles, s.mainnet_range_drift_window_bars)
        if (
            s.mainnet_range_scale != 1.0
            and s.mainnet_range_drift_max_bp > 0
            and drift_bp is not None
            and abs(drift_bp) <= s.mainnet_range_drift_max_bp
        ):
            # Range regime confirmed → compose with the rng15 sweet-zone
            # multiplier.  The V6.5 scale bookkeeping in _place_entry records
            # the combined entry/base ratio, so the DCA cumulative cap follows
            # this boost automatically — do NOT touch the cap logic here.
            notional_scale *= s.mainnet_range_scale

        decision = generate_wildcat_v2_adverse_guard_live_decision(
            candles,
            target_daily_usdc=self._settings.mainnet_equity_cap_usdc * 0.03,
            notional_usdc=self._settings.mainnet_effective_entry_notional_usdc * notional_scale,
            leverage=self._settings.mainnet_leverage,
            rescue_enabled=await self._is_rescue_enabled(),
        )
        if decision is None:
            return

        # Option A: direction consecutive-loss throttle (V6.8.5).
        now_ms = time.time() * 1000
        _dir_block_until = self._dir_throttle_until.get(decision.side, 0.0)
        if _dir_block_until > now_ms:
            _remaining_s = int((_dir_block_until - now_ms) / 1000)
            _dir_count_cfg = int(getattr(self._settings, "mainnet_dir_throttle_loss_count", 2) or 2)
            if run["run_id"] not in self._dir_throttle_notified:
                self._dir_throttle_notified.add(run["run_id"])
                await self._repo.log_event(
                    run["run_id"],
                    "direction_throttled",
                    {
                        "side": decision.side,
                        "strategy": decision.strategy,
                        "remaining_s": _remaining_s,
                    },
                )
                await self._notify(
                    f"⛔ <b>方向節流：暫停 {decision.side} 進場</b>\n"
                    f"Run：<code>{escape(run['run_id'])}</code>\n"
                    f"原因：60min 內同方向連續 ≥{_dir_count_cfg} 筆淨虧\n"
                    f"剩餘：<b>{_remaining_s // 60}m{_remaining_s % 60:02d}s</b>"
                )
            else:
                logger.info(
                    "direction_throttle_skip",
                    run_id=run["run_id"],
                    side=decision.side,
                    remaining_s=_remaining_s,
                )
            return  # stay ARMED, retry next cycle

        # Codex v1 canary owns entry qualification end-to-end.  The legacy
        # rng15/trend/rescue/spike guards remain active for non-Codex runs, but
        # must not pre-filter Codex samples or the live validation is biased by
        # the old strategy.
        raw_codex_decision: CodexV1Decision | None = None
        codex_decision: CodexV1Decision | None = None
        codex_features: dict[str, Any] | None = None
        if self._codex_v1_execution_enabled():
            adjusted_decision, raw_codex_decision, codex_decision, codex_features = await self._apply_codex_v1_gate(
                run,
                decision,
                candles,
                rng15=rng15,
                drift_bp=drift_bp,
            )
            if adjusted_decision is None:
                return
            await self._place_entry(
                run,
                adjusted_decision,
                rng15=rng15,
                drift_bp=drift_bp,
                raw_codex_decision=raw_codex_decision,
                codex_decision=codex_decision,
                codex_features=codex_features,
            )
            return

        # Volatility Range Filter
        if len(candles) >= 16:
            if s.mainnet_rng15_gate_high_bp > 0 and rng15 > s.mainnet_rng15_gate_high_bp:  # High Volatility Risk Zone
                if run["run_id"] not in self._rng15_guard_notified:
                    self._rng15_guard_notified.add(run["run_id"])
                    await self._repo.log_event(
                        run["run_id"],
                        "entry_rng15_high_skipped",
                        {"side": decision.side, "strategy": decision.strategy, "rng15": round(rng15, 2)},
                    )
                    await self._notify(
                        "🛡️ <b>進場守門：跳過高險區</b>\n"
                        f"Run：<code>{escape(run['run_id'])}</code>\n"
                        f"方向：{escape(decision.side)}｜策略：{escape(decision.strategy)}\n"
                        f"原因：15m 波動波幅為 <b>{rng15:.1f} bp</b>（&gt; {s.mainnet_rng15_gate_high_bp:g} bp，市場波動過大，暫避）\n"
                        "（續等波幅冷卻，逾時則前進下一個 loop run）"
                    )
                else:
                    logger.info(
                        "entry_rng15_high_skip run=%s side=%s rng15=%.2f",
                        run["run_id"],
                        decision.side,
                        rng15,
                    )
                return  # stay ARMED, retry next cycle
            elif s.mainnet_rng15_gate_low_bp > 0 and rng15 < s.mainnet_rng15_gate_low_bp:  # Low Volatility Non-Sweet Zone
                if run["run_id"] not in self._rng15_guard_notified:
                    self._rng15_guard_notified.add(run["run_id"])
                    await self._repo.log_event(
                        run["run_id"],
                        "entry_rng15_low_skipped",
                        {"side": decision.side, "strategy": decision.strategy, "rng15": round(rng15, 2)},
                    )
                    await self._notify(
                        "🛡️ <b>進場守門：跳過低波幅區</b>\n"
                        f"Run：<code>{escape(run['run_id'])}</code>\n"
                        f"方向：{escape(decision.side)}｜策略：{escape(decision.strategy)}\n"
                        f"原因：15m 波動波幅為 <b>{rng15:.1f} bp</b>（&lt; {s.mainnet_rng15_gate_low_bp:g} bp，非甜蜜區，不予開單）\n"
                        "（續等波幅回溫，逾時則前進下一個 loop run）"
                    )
                else:
                    logger.info(
                        "entry_rng15_low_skip run=%s side=%s rng15=%.2f",
                        run["run_id"],
                        decision.side,
                        rng15,
                    )
                return  # stay ARMED, retry next cycle

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
        now_ms = time.time() * 1000
        if is_rescue and candles:
            last = candles[-1]
            if last.open > 0:
                candle_move_pct = (last.close - last.open) / last.open
                spike_adverse = (
                    (decision.side == "SHORT" and candle_move_pct > 0.0012) or
                    (decision.side == "LONG" and candle_move_pct < -0.0012)
                )
                if spike_adverse:
                    # #24: arm the loop-scoped block so a NORMAL S1 signal can't
                    # blindly enter the same post-spike regime in the next ~120s
                    # (rescue itself keeps re-evaluating each candle below).
                    block_secs = self._settings.mainnet_spike_block_seconds
                    if block_secs > 0:
                        self._spike_block_until_ms = now_ms + block_secs * 1000
                    if run["run_id"] not in self._rescue_spike_notified:
                        self._rescue_spike_notified.add(run["run_id"])
                        await self._repo.log_event(
                            run["run_id"],
                            "rescue_spike_skip",
                            {
                                "side": decision.side,
                                "candle_move_pct": round(candle_move_pct * 100, 3),
                                "s1_block_secs": block_secs,
                            },
                        )
                        await self._notify(
                            f"⚡ Rescue {decision.side} 跳過（急速波動 {candle_move_pct*100:+.2f}%）："
                            f"<code>{escape(run['run_id'])}</code>"
                        )
                    return  # stay ARMED, retry next cycle
        # #24: a recent rescue spike skip blocks NORMAL S1 entries too, so we
        # don't catch the falling knife 110s later (cry3mn_1781088625968, -0.71).
        # Time-boxed: once the window lapses S1 re-evaluates, preserving the
        # post-spike V-bounce that fuels most TRAIL wins.
        if not is_rescue and now_ms < self._spike_block_until_ms:
            remaining = int((self._spike_block_until_ms - now_ms) / 1000)
            if run["run_id"] not in self._spike_block_notified:
                self._spike_block_notified.add(run["run_id"])
                await self._repo.log_event(
                    run["run_id"],
                    "s1_spike_block_skip",
                    {"side": decision.side, "strategy": decision.strategy, "remaining_s": remaining},
                )
                await self._notify(
                    f"⚡ S1 {decision.side} 暫緩進場（急速波動冷卻中，剩 {remaining}s）："
                    f"<code>{escape(run['run_id'])}</code>"
                )
            else:
                logger.info(
                    "s1_spike_block_skip", run_id=run["run_id"], side=decision.side, remaining_s=remaining,
                )
            return  # stay ARMED, retry next cycle
        await self._place_entry(
            run,
            decision,
            rng15=rng15,
            drift_bp=drift_bp,
        )

    async def _run_entry_pending(self, run: dict) -> None:
        symbol = run["symbol"]
        open_orders = await self._client.get_open_orders(symbol)
        order_id = int(run["entry_order_id"]) if run.get("entry_order_id") else None
        still_open = any(int(row.get("orderId", 0)) == order_id for row in open_orders)
        position = await self._client.get_position(symbol)
        if position:
            fill_detected_ms = int(time.time() * 1000)
            ttl_policy = self._codex_v1_live_entry_ttl_policy(run)
            entry_anchor_ms = int(run.get("updated_at_ms") or fill_detected_ms)
            entry_fill_age_s = round(max(0, fill_detected_ms - entry_anchor_ms) / 1000.0, 1)
            await self._repo.update_run(
                run["run_id"],
                status="RUNNING",
                avg_entry_price=position.entry_price,
                qty=abs(position.position_amt),
            )
            await self._repo.log_event(
                run["run_id"],
                "entry_filled",
                {
                    "entry_price": position.entry_price,
                    "qty": abs(position.position_amt),
                    "entry_fill_age_s": entry_fill_age_s,
                    "entry_fill_age_bucket": self._entry_fill_age_bucket(entry_fill_age_s),
                    "entry_ttl_s": ttl_policy["ttl_seconds"],
                    "entry_ttl_source": ttl_policy["ttl_source"],
                    "lane_code": ttl_policy.get("lane_code"),
                },
            )
            signal = self._codex_v1_signal_payload(run)
            tp_orders = await self._sync_take_profit_orders(run, position, signal)
            await self._start_codex_v132_live_tp_policy_sample(
                run,
                position,
                signal,
                tp_orders,
                fill_detected_ms=fill_detected_ms,
            )
            # Place initial stop-loss maker order if enabled
            if self._settings.mainnet_sl_use_maker:
                signal = self._codex_v1_signal_payload(run)
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
            trail_sig = self._codex_v1_signal_payload(run)
            trail_tp_pct = float(trail_sig.get("wildcat", {}).get("tp_pct") or 0.0)
            trail_side = "LONG" if position.position_amt > 0 else "SHORT"
            trail_close_side = "SELL" if position.position_amt > 0 else "BUY"
            self._start_trail_watch(run, trail_side, trail_close_side, trail_tp_pct)
            codex_note = self._codex_v1_telegram_note(trail_sig)
            await self._notify(
                "✅ <b>Mainnet one-run 已成交</b>\n"
                f"Run：<code>{escape(run['run_id'])}</code>\n"
                f"方向：<b>{escape(str(run.get('side') or ''))}</b>\n"
                f"均價：<b>${position.entry_price:.4f}</b>\n"
                f"數量：<code>{abs(position.position_amt):.6f}</code>"
                f"{codex_note}"
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
        ttl_policy = self._codex_v1_live_entry_ttl_policy(run)
        age_ms = int(time.time() * 1000) - int(run["updated_at_ms"])
        ttl_seconds = max(1, int(ttl_policy["ttl_seconds"]))
        if age_ms >= ttl_seconds * 1000:
            if order_id:
                await self._client.cancel_order(symbol, order_id)
            entry_age_s = round(max(0, age_ms) / 1000.0, 1)
            await self._repo.log_event(
                run["run_id"],
                "entry_ttl_expired",
                {
                    "entry_age_s": entry_age_s,
                    "entry_ttl_s": ttl_seconds,
                    "entry_ttl_source": ttl_policy["ttl_source"],
                    "lane_code": ttl_policy.get("lane_code"),
                },
            )
            await self._repo.complete_run(run["run_id"], "ENTRY_EXPIRED", "entry_ttl_expired")
            await self._notify(
                f"⌛ Entry maker 掛單逾時（TTL {ttl_seconds}s）未成交，已取消：<code>{escape(run['run_id'])}</code>"
            )
            await self._advance_loop_after_entry_failure(run, "entry_ttl_expired")

    async def _run_running(self, run: dict) -> None:
        symbol = run["symbol"]
        position = await self._client.get_position(symbol)
        if not position:
            await self._finish_flat_run(run, "flat_detected")
            return
        if run["run_id"] in self._trail_exiting or run["run_id"] in self._w6a_no_bounce_exiting:
            # A software exit is already managing a reduce-only order; skip this
            # cycle so SL/TRAIL/no-bounce paths cannot double-submit.
            return
        current_qty = abs(position.position_amt)
        prev_qty = float(run.get("qty") or 0.0)
        if current_qty > prev_qty + 1e-9:
            # #25: distinguish a PARTIAL fill of the resting GTX DCA from a full
            # layer.  Counting a tiny partial (0.001 of an intended 0.124) as a
            # full layer widened the SL, bumped the layer count, inflated the
            # cumulative notional, and pre-placed the NEXT layer immediately —
            # the 21-second double-layer cascade in cry3mn_1781089775237.  We
            # gate the full-layer bookkeeping on the cumulative fill reaching a
            # fraction of the pre-placed order's intended qty; the order_id match
            # ensures the growth actually came from the live pre-placed order
            # (poll-path fills have no live pre-placed slot → treated as full).
            meta = self._dca_preload_meta.get(run["run_id"])
            intended_qty = float(meta.get("intended_qty") or 0.0) if meta else 0.0
            base_qty = float(meta.get("base_qty") or 0.0) if meta else 0.0
            is_partial_fill = (
                meta is not None
                and meta.get("order_id") == self._dca_preloaded.get(run["run_id"])
                and intended_qty > 0
                and (current_qty - base_qty)
                < intended_qty * self._settings.mainnet_dca_min_fill_ratio
            )
            if is_partial_fill:
                # Leave the layer OPEN: only sync qty tracking, keep the resting
                # order so its remainder keeps filling, and leave SL / TRAIL /
                # layer count / cumulative notional untouched.  The pre-DCA SL
                # anchor stays (the added size is small by definition); the layer
                # settles on the cycle the fill finally crosses the threshold, or
                # the residual order is cancelled on exit.
                await self._repo.log_event(
                    run["run_id"],
                    "recovery_partial_fill",
                    {
                        "qty": current_qty,
                        "added_qty": current_qty - prev_qty,
                        "filled_this_layer": current_qty - base_qty,
                        "intended_qty": intended_qty,
                        "fill_ratio": round((current_qty - base_qty) / intended_qty, 4),
                        "preloaded_order_id": meta.get("order_id"),
                    },
                )
                logger.info(
                    "recovery_partial_fill",
                    run_id=run["run_id"],
                    filled_this_layer=current_qty - base_qty,
                    intended_qty=intended_qty,
                )
                await self._repo.update_run(run["run_id"], qty=current_qty)
                run["qty"] = current_qty
                # fall through to normal exit management below
            else:
                await self._consume_dca_layer(run, position, current_qty, prev_qty, symbol)
        elif abs(current_qty - prev_qty) > 1e-9:
            # Qty shrank (TP partial fills) — sync tracking only, do NOT touch SL
            self._partial_exits.add(run["run_id"])
            # TP partial fill — cancel any pre-placed DCA (no averaging into a winner)
            if run["run_id"] in self._dca_preloaded:
                try:
                    await self._client.cancel_order(symbol, self._dca_preloaded.pop(run["run_id"]))
                except Exception:
                    self._dca_preloaded.pop(run["run_id"], None)
            self._dca_preload_meta.pop(run["run_id"], None)
            await self._repo.update_run(run["run_id"], qty=current_qty)
            run["qty"] = current_qty

        await self._run_running_manage(run, position, symbol, current_qty, prev_qty)

    async def _consume_dca_layer(
        self,
        run: dict,
        position: "PositionInfo",
        current_qty: float,
        prev_qty: float,
        symbol: str,
    ) -> None:
        """A full DCA layer filled (qty grew past the partial-fill threshold).
        Record the fill, reset the TRAIL baseline, count the layer + added
        notional, and re-arm the SL at the new (widened) average entry price."""
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
            self._dca_preload_meta.pop(run["run_id"], None)
            self._recovery_counts[run["run_id"]] = self._recovery_counts.get(run["run_id"], 0) + 1
            entry_notional = self._settings.mainnet_effective_entry_notional_usdc
            new_cumulative = float(run.get("cumulative_notional_usdc") or entry_notional) + entry_notional
            await self._repo.update_run(run["run_id"], cumulative_notional_usdc=new_cumulative)
            run["cumulative_notional_usdc"] = new_cumulative
        await self._cancel_stop_loss_order(symbol, run["run_id"])
        signal = json.loads(run.get("signal_json") or "{}")
        sl_pct = self._effective_sl_pct(signal)
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

    def _w6a_post_tp_probe_thresholds_bp(self) -> list[float]:
        raw = getattr(
            self._settings,
            "mainnet_codex_v137_w6a_post_tp_probe_giveback_bp",
            "1.5,2.0,2.5",
        )
        if isinstance(raw, (list, tuple, set)):
            parts = list(raw)
        else:
            parts = str(raw or "").replace(";", ",").split(",")
        thresholds: list[float] = []
        for part in parts:
            try:
                value = float(part)
            except (TypeError, ValueError):
                continue
            if math.isfinite(value) and value > 0:
                thresholds.append(value)
        return sorted(set(thresholds)) or [1.5, 2.0, 2.5]

    async def _maybe_log_w6a_post_tp_probe_shadow(
        self,
        *,
        run: dict[str, Any],
        signal: dict[str, Any],
        position: PositionInfo,
        side: str,
        mark: float,
        entry: float,
        qty: float,
        peak: float,
        mfe_r: float,
        unrealized_r: float,
    ) -> None:
        if not getattr(self._settings, "mainnet_codex_v137_w6a_post_tp_probe_shadow", True):
            return
        codex = signal.get("codex_v1") or {}
        if (codex.get("lane_code") or "").upper() != "W6A":
            return
        if qty <= 0 or entry <= 0 or mark <= 0:
            return
        side_upper = str(side or "").upper()
        if side_upper not in {"LONG", "SHORT"}:
            return
        run_id = str(run["run_id"])
        thresholds = self._w6a_post_tp_probe_thresholds_bp()
        if side_upper == "LONG":
            favorable_peak = max(float(peak or mark), mark)
            peak_bp = (favorable_peak - entry) / entry * 10_000.0
            current_bp = (mark - entry) / entry * 10_000.0
            partial_tp_pct = self._signal_partial_tp_pct(signal)
            tp1_price = entry * (1.0 + partial_tp_pct)
        else:
            favorable_peak = min(float(peak or mark), mark)
            peak_bp = (entry - favorable_peak) / entry * 10_000.0
            current_bp = (entry - mark) / entry * 10_000.0
            partial_tp_pct = self._signal_partial_tp_pct(signal)
            tp1_price = entry * (1.0 - partial_tp_pct)
        giveback_bp = max(0.0, peak_bp - current_bp)
        base_payload = {
            "shadow_policy": "SH_W6A_POST_TP_PROBE_V1",
            "trade_id": run_id,
            "run_id": run_id,
            "symbol": run.get("symbol") or position.symbol,
            "strategy": run.get("strategy") or codex.get("policy_tag"),
            "policy_version": CODEX_V1_VERSION,
            "side": side_upper,
            "position_qty": qty,
            "runner_qty": qty,
            "entry_price": entry,
            "tp1_price": tp1_price,
            "current_price": mark,
            "mfe_r": mfe_r,
            "unrealized_r": unrealized_r,
            "giveback_bp": giveback_bp,
            "peak_bp": peak_bp,
            "current_bp": current_bp,
            "trail_level": getattr(self._settings, "mainnet_trail_offset_pct", None),
            "order_id": None,
            "reason": "post_tp_runner_giveback_shadow",
            "thresholds_bp": thresholds,
        }
        eval_key = (run_id, "post_tp_probe_eval")
        if eval_key not in self._w6a_post_tp_probe_recorded:
            await self._repo.log_event(
                run_id,
                "post_tp_probe_eval",
                {**base_payload, "shadow_action": "eval"},
            )
            self._w6a_post_tp_probe_recorded.add(eval_key)

        max_threshold = max(thresholds) if thresholds else 0.0
        for threshold in thresholds:
            threshold_label = f"{threshold:.1f}"
            if giveback_bp >= threshold:
                event_type = "post_tp_probe_exit" if threshold == max_threshold else "post_tp_probe_reduce"
                shadow_action = "would_exit" if event_type == "post_tp_probe_exit" else "would_reduce"
            else:
                event_type = "post_tp_probe_hold"
                shadow_action = "would_hold"
            key = (run_id, f"{event_type}:{threshold_label}")
            if key in self._w6a_post_tp_probe_recorded:
                continue
            await self._repo.log_event(
                run_id,
                event_type,
                {
                    **base_payload,
                    "threshold_bp": threshold,
                    "shadow_action": shadow_action,
                },
            )
            self._w6a_post_tp_probe_recorded.add(key)

    async def _run_running_manage(
        self,
        run: dict,
        position: "PositionInfo",
        symbol: str,
        current_qty: float,
        prev_qty: float,
    ) -> None:
        """Post-fill management for a RUNNING position: CLOSING wait, residual
        dust cleanup, partial-fill state, TP sync, DCA poll, TRAIL watch/exit,
        software SL backstop, adverse + max-hold exits."""
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
        if self._codex_v132_has_active_tp_policy_sample(str(run.get("run_id") or "")):
            try:
                await self._update_codex_v132_tp_policy_outcomes(run, await self._load_candles(symbol))
            except Exception as exc:  # noqa: BLE001
                logger.warning("codex_v132_tp_policy_update_failed", run_id=run.get("run_id"), error=str(exc)[:200])
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

        # --- W6A No-TP1 Dynamic Exit Guard (v1.2.12) ---
        lane_code = (signal.get("codex_v1") or {}).get("lane_code")
        if lane_code == "W6A" and side == "LONG":
            tp1_filled = run["run_id"] in self._partial_exits
            if not tp1_filled:
                tp1_filled = await self._repo.get_first_event_time(run["run_id"], "partial_exit") is not None

            # Track micro momentum and retain enough path for the v1.3.7E no-bounce low break.
            now_t = time.time()
            no_bounce_after_s = float(getattr(self._settings, "mainnet_codex_v137_w6a_no_bounce_after_seconds", 60.0))
            history = self._w6a_price_history.setdefault(run["run_id"], [])
            history.append((now_t, mark))
            history[:] = [(t, p) for t, p in history if now_t - t <= max(120.0, no_bounce_after_s + 30.0)]
            recent_history = [(t, p) for t, p in history if now_t - t <= 15]
            prior_prices = [p for _, p in history[:-1]]
            local_low_break = bool(prior_prices and mark <= min(prior_prices))

            old_price = None
            for t, p in recent_history:
                if 8 <= now_t - t <= 15:
                    old_price = p
                    break
            if old_price is None and recent_history:
                old_price = recent_history[0][1]
            micro_return_10s = (mark - old_price) / old_price if old_price and old_price > 0.0 else 0.0

            # Compute R metrics
            planned_stop_risk = abs(entry - sl_price)
            unrealized_r = (mark - entry) / planned_stop_risk if planned_stop_risk > 0.0 else 0.0

            peak = self._trail_peak.get(run["run_id"], mark)
            max_unrealized_profit = max(peak - entry, 0.0)
            mfe_r = max_unrealized_profit / planned_stop_risk if planned_stop_risk > 0.0 else 0.0

            seconds_since_fill = max(0.0, (int(time.time() * 1000) - hold_start_ms) / 1000.0)
            policy_note = (signal.get("codex_v1") or {}).get("policy_note")
            if tp1_filled:
                await self._maybe_log_w6a_post_tp_probe_shadow(
                    run=run,
                    signal=signal,
                    position=position,
                    side=side,
                    mark=mark,
                    entry=entry,
                    qty=qty,
                    peak=peak,
                    mfe_r=mfe_r,
                    unrealized_r=unrealized_r,
                )

            # A. 25-second early failure check
            if not tp1_filled and seconds_since_fill >= 25.0 and policy_note != "w6a_deep_down_capitulation_bounce_allowed":
                weak_no_bounce = unrealized_r <= -0.35 and mfe_r < 0.15 and mark <= entry and micro_return_10s <= 0.0
                if weak_no_bounce:
                    if self._settings.mainnet_codex_v1_w6a_no_tp1_exit_shadow:
                        shadow_key = f"{run['run_id']}:weak_no_bounce_exit"
                        if shadow_key not in self._w6a_shadow_recorded:
                            self._w6a_shadow_recorded.add(shadow_key)
                            await self._repo.log_event(
                                run["run_id"],
                                "w6a_exit_policy_shadow",
                                {
                                    "decision": {
                                        "version": CODEX_V1_VERSION,
                                        "lane_code": "W6A",
                                        "reason": "w6a_no_tp1_weak_no_bounce_early_exit_shadow",
                                    },
                                    "state": {
                                        "seconds_since_fill": round(seconds_since_fill, 1),
                                        "unrealized_r": round(unrealized_r, 4),
                                        "mfe_r": round(mfe_r, 4),
                                        "tp1_filled": False,
                                        "current_price_below_entry": mark <= entry,
                                    },
                                }
                            )
                    if self._settings.mainnet_codex_v1_w6a_no_tp1_early_exit_live:
                        await self._repo.log_event(
                            run["run_id"],
                            "w6a_early_exit_sent",
                            {
                                "decision": {
                                    "version": CODEX_V1_VERSION,
                                    "lane_code": "W6A",
                                    "reason": "w6a_no_tp1_weak_no_bounce_early_exit",
                                }
                            }
                        )
                        await self._close_position(symbol, close_side, qty, "w6a_no_tp1_weak_no_bounce_early_exit", run)
                        return

            # A2. V1.3.7E no-bounce damage reducer: one-shot soft maker exit after a stale fill fails to bounce.
            distance_to_sl_r = abs(mark - sl_price) / planned_stop_risk if planned_stop_risk > 0.0 and sl_price > 0.0 else None
            no_bounce_v2_signal = (
                not tp1_filled
                and seconds_since_fill >= no_bounce_after_s
                and mfe_r <= 0.05
                and unrealized_r <= -0.45
                and local_low_break
            )
            if no_bounce_v2_signal:
                no_bounce_state = {
                    "version": CODEX_V1_VERSION,
                    "lane_code": "W6A",
                    "seconds_since_fill": round(seconds_since_fill, 1),
                    "unrealized_r": round(unrealized_r, 4),
                    "mfe_r": round(mfe_r, 4),
                    "tp1_filled": False,
                    "local_low_break": bool(local_low_break),
                    "distance_to_sl_r": round(distance_to_sl_r, 4) if distance_to_sl_r is not None else None,
                    "mark_price": mark,
                    "entry_price": entry,
                    "stop_loss": sl_price,
                }
                if getattr(self._settings, "mainnet_codex_v137_w6a_no_bounce_exit_shadow", True):
                    shadow_key = f"{run['run_id']}:no_bounce_exit_v2"
                    if shadow_key not in self._w6a_shadow_recorded:
                        self._w6a_shadow_recorded.add(shadow_key)
                        await self._repo.log_event(
                            run["run_id"],
                            "w6a_exit_policy_shadow",
                            {
                                "shadow_policy": "SH_W6A_NO_BOUNCE_EXIT_V2",
                                "variants": ["45s_soft_exit", "60s_soft_exit", "90s_soft_exit", "tight_stop", "hold_baseline"],
                                "state": no_bounce_state,
                            },
                        )
                if getattr(self._settings, "mainnet_codex_v137_w6a_no_bounce_exit_live", True):
                    if run["run_id"] in self._w6a_no_bounce_exiting:
                        return
                    fallback_unrealized_r = float(
                        getattr(self._settings, "mainnet_codex_v137_w6a_no_bounce_market_fallback_unrealized_r", -0.55)
                    )
                    fallback_distance_r = float(
                        getattr(self._settings, "mainnet_codex_v137_w6a_no_bounce_market_fallback_distance_to_sl_r", 0.10)
                    )
                    market_fallback_now = unrealized_r <= fallback_unrealized_r or (
                        distance_to_sl_r is not None and distance_to_sl_r <= fallback_distance_r
                    )
                    exit_reason = "w6a_no_bounce_market_fallback" if market_fallback_now else "w6a_no_bounce_soft_exit_v2"
                    self._w6a_no_bounce_exiting.add(run["run_id"])
                    await self._repo.log_event(
                        run["run_id"],
                        "no_bounce_exit_signal",
                        {
                            **no_bounce_state,
                            "exit_reason": exit_reason,
                            "market_fallback_now": market_fallback_now,
                        },
                    )
                    submitted = await self._close_position(symbol, close_side, qty, exit_reason, run)
                    if not submitted:
                        self._w6a_no_bounce_exiting.discard(run["run_id"])
                    return
            # B. 60/90-second stop tightening check
            tighten_threshold = 90.0 if policy_note == "w6a_deep_down_capitulation_bounce_allowed" else 60.0
            if not tp1_filled and seconds_since_fill >= tighten_threshold and run["run_id"] not in self._w6a_stop_tightened_runs:
                new_stop_r = None
                rule_reason = None
                if unrealized_r <= -0.20 and mfe_r < 0.20:
                    new_stop_r = -0.55
                    rule_reason = "w6a_no_tp1_weak_progress_stop_tightened"
                elif unrealized_r <= 0.0 and mfe_r >= 0.25:
                    new_stop_r = -0.70
                    rule_reason = "w6a_no_tp1_weak_progress_stop_tightened"

                if new_stop_r is not None:
                    if self._settings.mainnet_codex_v1_w6a_no_tp1_exit_shadow:
                        shadow_key = f"{run['run_id']}:stop_tighten"
                        if shadow_key not in self._w6a_shadow_recorded:
                            self._w6a_shadow_recorded.add(shadow_key)
                            await self._repo.log_event(
                                run["run_id"],
                                "w6a_exit_policy_shadow",
                                {
                                    "decision": {
                                        "version": CODEX_V1_VERSION,
                                        "lane_code": "W6A",
                                        "reason": "w6a_no_tp1_weak_progress_stop_tightened_shadow",
                                        "new_stop_r": new_stop_r,
                                    },
                                    "state": {
                                        "seconds_since_fill": round(seconds_since_fill, 1),
                                        "unrealized_r": round(unrealized_r, 4),
                                        "mfe_r": round(mfe_r, 4),
                                        "tp1_filled": False,
                                        "current_price_below_entry": mark <= entry,
                                    },
                                }
                            )
                    if self._settings.mainnet_codex_v1_w6a_no_tp1_stop_tighten_live:
                        new_sl = entry + new_stop_r * planned_stop_risk
                        signal["stop_loss"] = new_sl
                        new_signal_json = json.dumps(signal)
                        await self._repo.update_run(run["run_id"], signal_json=new_signal_json)
                        run["signal_json"] = new_signal_json

                        await self._cancel_stop_loss_order(symbol, run["run_id"])
                        await self._place_stop_loss_maker(
                            symbol=symbol,
                            side=close_side,
                            qty_str=await self._client.format_quantity(symbol, qty),
                            sl_price=new_sl,
                            run_id=run["run_id"],
                            reason="SL",
                            run=run,
                        )
                        self._w6a_stop_tightened_runs[run["run_id"]] = new_stop_r
                        await self._repo.log_event(
                            run["run_id"],
                            "w6a_stop_tightened",
                            {
                                "decision": {
                                    "version": CODEX_V1_VERSION,
                                    "lane_code": "W6A",
                                    "reason": rule_reason,
                                    "new_stop_r": new_stop_r,
                                },
                                "state": {
                                    "unrealized_r": round(unrealized_r, 4),
                                    "mfe_r": round(mfe_r, 4),
                                }
                            }
                        )

        if await self._maybe_codex_survival_exit(
            run,
            signal,
            position,
            side,
            mark,
            entry,
            qty,
            close_side,
            hold_start_ms,
        ):
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

    async def _place_entry(
        self,
        run: dict,
        decision: WildcatLiveDecision,
        rng15: float = 0.0,
        drift_bp: float | None = None,
        raw_codex_decision: CodexV1Decision | None = None,
        codex_decision: CodexV1Decision | None = None,
        codex_features: Mapping[str, Any] | None = None,
    ) -> None:
        if self._codex_v1_execution_enabled() and codex_decision is None:
            await self._repo.log_event(
                run["run_id"],
                "entry_codex_v1_hard_blocked",
                {
                    "reason": "codex_v1_enabled_without_accepted_lane",
                    "side": decision.side,
                    "strategy": decision.strategy,
                    "score": decision.signal.score,
                    "rng15": round(rng15, 2),
                    "drift30": round(drift_bp, 2) if drift_bp is not None else None,
                },
            )
            if run["run_id"] not in self._codex_v1_guard_notified:
                self._codex_v1_guard_notified.add(run["run_id"])
                await self._notify(
                    "🛑 <b>Codex v1 hard gate 擋單</b>\n"
                    f"Run：<code>{escape(run['run_id'])}</code>\n"
                    f"版本：<code>{CODEX_V1_VERSION}</code>\n"
                    "Lane Code：<code>NONE</code>\n"
                    "Full Lane：<code>NONE</code>\n"
                    "Raw Classifier：<code>NONE</code>\n"
                    "Raw Rule：<code>NONE</code>\n"
                    "Effective Execution：<code>NONE</code> / <code>blocked_no_accepted_lane</code>\n"
                    "Live Reason：<code>codex_v1_enabled_without_accepted_lane</code>\n"
                    "原因：Codex v1 已啟用，但這個候選沒有 accepted lane；禁止 fallback 舊規則下單。\n"
                    f"候選：<code>{escape(decision.strategy)}</code> / <code>{escape(decision.side)}</code> / "
                    f"score=<code>{decision.signal.score}</code>"
                )
            return
        await self._ensure_fee_guard(run["symbol"])
        await self._client.set_leverage(run["symbol"], self._settings.mainnet_leverage)
        side = "BUY" if decision.side == "LONG" else "SELL"
        entry_notional = decision.signal.planned_notional_usdc
        entry_signal_price = self._codex_v1_entry_reference_price(
            decision.signal.price,
            decision.side,
            codex_decision.entry_offset_bp if codex_decision else 0.0,
        )
        # V6.5: remember the actual sizing scale so DCA cumulative-cap checks
        # can scale the cap with the entry (fixes the 1.2x bookkeeping bug).
        base_notional = self._settings.mainnet_effective_entry_notional_usdc
        if base_notional > 0 and entry_notional > 0:
            self._notional_scale[run["run_id"]] = entry_notional / base_notional
        qty = await self._client.format_quantity(
            run["symbol"],
            entry_notional / entry_signal_price,
        )
        client_order_id = f"{run['run_id']}_entry"
        ladder_offset = 0.0 if codex_decision is not None else self._settings.mainnet_entry_limit_offset
        ladder_deadline_ms: int | None = None
        entry_note = ""
        s = self._settings
        if (
            s.mainnet_rng15_sweet_scale != 1.0
            and s.mainnet_rng15_sweet_low_bp <= rng15 < s.mainnet_rng15_sweet_high_bp
        ):
            entry_note += f"\n🔥 <b>甜蜜區進場，資金已自動放大 {s.mainnet_rng15_sweet_scale:g} 倍！</b>"
        if ladder_offset > 0.0:
            # Ladder entry: place GTC LIMIT at a better price and wait up to TTL bars
            tick = float(await self._client.price_tick_size(run["symbol"]))
            raw_limit = entry_signal_price * (1 - ladder_offset if side == "BUY" else 1 + ladder_offset)
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
            # Display-only sign fix (06-11): the ladder offset is SUBTRACTED for
            # BUY (wait for a dip) and ADDED for SELL (wait for a pop) — see
            # raw_limit above.  The old hardcoded "-" mislabeled SHORT ladders;
            # order prices themselves were always correct.
            offset_sign = "-" if side == "BUY" else "+"
            entry_note = (
                f"\n🪜 Ladder：${limit_price:.4f}（訊號 ${entry_signal_price:.4f} {offset_sign} {ladder_offset*10000:.0f}bp）"
                f"｜TTL {self._settings.mainnet_entry_limit_ttl_bars} 根 K 棒"
            )
        else:
            try:
                order = await self._place_post_only_with_retry(
                    symbol=run["symbol"],
                    side=side,
                    quantity=qty,
                    signal_price=entry_signal_price,
                    client_order_id=client_order_id,
                    slippage_bps=self._settings.mainnet_entry_slippage_bps,
                    fallback_to_gtc=False if codex_decision is not None else self._settings.mainnet_entry_fallback_to_gtc,
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
            final_price = float(order.get("price", 0) or entry_signal_price)
            used_gtc = order.get("timeInForce") != "GTX"
            entry_note = "\n⚠️ 使用 GTC 限價單進場（maker 保護已關閉）" if used_gtc else ""
        sl_pct = self._effective_sl_pct({"wildcat": {"sl_pct": decision.sl_pct}})
        stop_loss = self._sl_price_from_pct(final_price, decision.side, sl_pct)
        payload = {
            "side": decision.side,
            "strategy": decision.strategy,
            "price": decision.signal.price,
            "entry_reference_price": entry_signal_price,
            "entry_price": final_price,
            "stop_loss": stop_loss,
            "take_profits": decision.signal.take_profits,
            "take_profit": decision.signal.take_profits[0] if decision.signal.take_profits else None,
            "score": decision.signal.score,
            "reasons": decision.signal.reasons,
            "wildcat": {
                "tp_pct": decision.tp_pct,
                "sl_pct": sl_pct,
                "partial_exit_pct": decision.partial_exit_pct,
                "partial_tp_pct": decision.partial_tp_pct,
                # 06-11: post-hoc analysis reads signal_json/entry_placed as
                # ground truth for what the run actually traded with, but the
                # decision object carries the BACKTEST PRESET recovery values
                # (e.g. steps=3) while live execution reads runtime settings
                # (V6.5: steps=1) — the stale preset polluted the 06-10/06-11
                # layer-count analysis.  Persist the live runtime values here;
                # tp/sl/partial/adverse keys stay decision-driven because the
                # executor genuinely uses those from the decision.
                "recovery_steps": self._settings.mainnet_recovery_steps,
                "recovery_trigger_pct": self._settings.mainnet_recovery_trigger_pct,
                "recovery_tp_shrink": self._settings.mainnet_recovery_tp_shrink,
                "recovery_sl_widen_per_layer": self._settings.mainnet_recovery_sl_widen_per_layer,
                "dca_enabled": self._dca_enabled,
                "adverse_exit_bars": decision.adverse_exit_bars,
                "adverse_exit_loss_pct": decision.adverse_exit_loss_pct,
                "max_holding_bars": decision.max_holding_bars,
                "trail_require_partial_fill": self._settings.mainnet_trail_require_partial_fill,
                "trail_disable_final_tp": self._settings.mainnet_trail_disable_final_tp,
            },
            "entry_ladder_deadline_ms": ladder_deadline_ms,
            # V6.5: per-entry volatility context for offline stats (WR by rng15
            # bucket, sweet-zone tuning).  Persisted in signal_json + entry_placed.
            "rng15": round(rng15, 2),
            # 06-11: signed net drift (bp) over mainnet_range_drift_window_bars
            # at entry — None when candle history was too thin.  Feeds the
            # drift-bucket analysis that will pick the DCA drift gate and
            # range-scale thresholds (see golden-window vs V5.5 forensics).
            "drift30": round(drift_bp, 2) if drift_bp is not None else None,
            "notional_scale": round(self._notional_scale.get(run["run_id"], 1.0), 4),
        }
        if codex_decision is not None:
            raw_snapshot = self._codex_v1_decision_snapshot(raw_codex_decision or codex_decision, codex_features)
            effective_snapshot = self._codex_v1_decision_snapshot(
                codex_decision,
                codex_features,
                status="submitted",
                effective_reason="accepted",
            )
            codex_target_price = self._codex_v1_shadow_price(payload.get("take_profit"))
            if codex_target_price is None and decision.signal.take_profits:
                codex_target_price = self._codex_v1_shadow_price(decision.signal.take_profits[0])
            codex_fee_audit = self._codex_v133_fee_audit_payload(
                codex_features or {},
                entry_price=float(final_price),
                target_price=float(codex_target_price or final_price),
            )
            live_ttl_policy = self._codex_v1_live_entry_ttl_policy(
                {"signal_json": {"codex_v1": {"enabled": True, "lane_code": codex_decision.lane_code}}}
            )
            entry_note += f"\n⏱ Codex Entry TTL：{live_ttl_policy['ttl_seconds']}s"
            payload["codex_v1"] = {
                "enabled": True,
                "version": codex_decision.version,
                "baseline": codex_decision.baseline,
                "lane_code": codex_decision.lane_code,
                "lane": codex_decision.lane,
                "entry_offset_bp": codex_decision.entry_offset_bp,
                "size_mult": codex_decision.size_mult,
                "notional_mult": codex_decision.notional_mult,
                "requested_notional_usdc": codex_decision.requested_notional_usdc,
                "applied_notional_usdc": entry_notional,
                "live_entry_ttl_s": live_ttl_policy["ttl_seconds"],
                "live_entry_ttl_source": live_ttl_policy["ttl_source"],
                "live_entry_ttl_lane_code": live_ttl_policy.get("lane_code"),
                "risk_tags": list(codex_decision.risk_tags),
                "policy_tag": (
                    codex_decision.policy_tag
                    or (
                        getattr(codex_decision, "metrics", {}).get("policy_tag")
                        if getattr(codex_decision, "metrics", None)
                        else None
                    )
                ),
                "policy_note": (
                    codex_decision.policy_tag
                    or (
                        getattr(codex_decision, "metrics", {}).get("policy_note")
                        if getattr(codex_decision, "metrics", None)
                        else None
                    )
                ),
                "shadow_lane": (
                    codex_decision.shadow_lane
                    or (
                        getattr(codex_decision, "metrics", {}).get("shadow_lane")
                        if getattr(codex_decision, "metrics", None)
                        else None
                    )
                ),
                "metrics": getattr(codex_decision, "metrics", None),
                "fee_audit": codex_fee_audit,
                "fee_buffer_pass": codex_fee_audit.get("fee_buffer_pass"),
                "expected_net_buffer_bp": codex_fee_audit.get("expected_net_buffer_bp"),
                "features": self._codex_v1_payload_features(codex_features or {}),
                "raw_classifier": raw_snapshot,
                "effective_execution": effective_snapshot,
            }
        codex_note = self._codex_v1_telegram_note(payload)
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
        await self._expire_codex_v1_shadow_samples(run, "live_entry_submitted")
        drift_note = f"{drift_bp:.1f}bp" if drift_bp is not None else "n/a"
        await self._notify(
            f"{'🟢' if decision.side == 'LONG' else '🔴'} <b>AUTO {('做多' if decision.side == 'LONG' else '做空')} 已掛 maker 單</b>\n"
            f"Run：<code>{escape(run['run_id'])}</code>\n"
            f"策略：<b>{escape(decision.strategy)}</b> | score=<code>{decision.signal.score}</code> | rng15=<code>{rng15:.1f}bp</code> | drift=<code>{drift_note}</code>\n"
            f"Entry：<b>${final_price:.4f}</b> | Qty：<code>{escape(str(qty))}</code>\n"
            f"Stop：<b>${float(stop_loss or 0):.4f}</b> | TP：<b>${float(decision.signal.take_profits[0] if decision.signal.take_profits else 0):.4f}</b>{entry_note}{codex_note}\n"
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
                self._partial_taken.add(run_id)
                self._partial_exits.add(run_id)
                await self._repo.log_event(
                    run_id,
                    "partial_exit",
                    {
                        "exit_event_type": "partial_exit",
                        "exit_reason": "TP1",
                        "qty_requested": qty_text,
                        "qty_filled": qty_text,
                        "position_qty_before": ref_qty,
                        "position_qty_after": current_qty,
                        "position_qty": current_qty,
                    },
                )
                await self._repo.log_event(run_id, "trail_runner_activated", {"mode": "tp1_then_trail"})
                codex_note = self._codex_v1_telegram_note(run)
                await self._notify(
                    f"✅ Mainnet one-run 已部分獲利了結：<code>{escape(run_id)}</code> qty=<code>{escape(str(qty_text))}</code>{codex_note}"
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
                await self._repo.log_event(
                    run_id,
                    "mid_exit",
                    {
                        "exit_event_type": "partial_exit",
                        "exit_reason": "TP2",
                        "qty_requested": qty_text,
                        "qty_filled": qty_text,
                        "position_qty_before": ref_qty,
                        "position_qty_after": current_qty,
                        "position_qty": current_qty,
                    },
                )
                codex_note = self._codex_v1_telegram_note(run)
                await self._notify(
                    f"✅ Mainnet one-run TP2 +{self._settings.mainnet_mid_tp_pct*100:.2f}% 已出場：<code>{escape(run_id)}</code> qty=<code>{escape(str(qty_text))}</code>{codex_note}"
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
                await self._repo.log_event(
                    run_id,
                    "final_exit",
                    {
                        "exit_event_type": "final_exit",
                        "exit_reason": "TP3",
                        "qty_requested": qty_text,
                        "qty_filled": qty_text,
                        "position_qty_before": ref_qty,
                        "position_qty_after": current_qty,
                        "position_qty": current_qty,
                    },
                )
                codex_note = self._codex_v1_telegram_note(run)
                await self._notify(
                    f"✅ Mainnet one-run TP3 (signal) 已出場：<code>{escape(run_id)}</code> qty=<code>{escape(str(qty_text))}</code>{codex_note}"
                )


    async def _audit_tp1_touch_no_fill(
        self,
        run: dict,
        position: PositionInfo,
        desired: list[tuple[str, str, float]],
        existing_tp: list[dict],
        signal: dict,
    ) -> None:
        run_id = run["run_id"]
        if run_id in self._tp1_audit_recorded:
            return
        if await self._repo.get_first_event_time(run_id, "partial_exit") is not None:
            return
        tp1 = next((order for order in desired if str(order[0]).endswith(PARTIAL_TP_SUFFIX)), None)
        if tp1 is None:
            return
        tp1_client_order_id, order_qty, tp1_price = tp1
        try:
            book = await self._client.get_book_ticker(position.symbol)
            best_bid = float(book.get("bidPrice") or 0.0)
            best_ask = float(book.get("askPrice") or 0.0)
            tick = float(await self._client.price_tick_size(position.symbol))
        except Exception as exc:  # noqa: BLE001 - audit must not block TP sync.
            logger.warning("tp1_touch_audit_book_failed", run_id=run_id, error=str(exc)[:200])
            return
        if tp1_price <= 0 or best_bid <= 0 or best_ask <= 0:
            return
        side = str(run.get("side") or signal.get("side") or position.position_direction or "").upper()
        last_price = float(getattr(position, "mark_price", 0.0) or 0.0)
        if side == "LONG":
            last_touch = last_price >= tp1_price
            executable_touch = best_bid >= tp1_price
            crossed_ticks = (best_bid - tp1_price) / tick if tick > 0 else 0.0
        elif side == "SHORT":
            last_touch = last_price <= tp1_price
            executable_touch = best_ask <= tp1_price
            crossed_ticks = (tp1_price - best_ask) / tick if tick > 0 else 0.0
        else:
            return
        crossed = executable_touch and crossed_ticks >= 1.0
        if not executable_touch:
            return
        tp1_order = next(
            (order for order in existing_tp if str(order.get("clientOrderId") or "") == tp1_client_order_id),
            None,
        )
        order_status = str(tp1_order.get("status") or "open") if tp1_order else "missing"
        order_id = tp1_order.get("orderId") if tp1_order else None
        try:
            orig_qty = float(tp1_order.get("origQty") or order_qty) if tp1_order else float(order_qty)
        except (TypeError, ValueError):
            orig_qty = None
        try:
            executed_qty = float(tp1_order.get("executedQty") or 0.0) if tp1_order else 0.0
        except (TypeError, ValueError):
            executed_qty = 0.0
        remaining_qty = max(0.0, orig_qty - executed_qty) if orig_qty is not None else None
        if tp1_order is None:
            event_type = "tp1_order_missing_at_touch"
        elif crossed:
            event_type = "tp1_cross_no_fill_audit"
        else:
            event_type = "tp1_touch_no_fill_audit"
        self._tp1_audit_recorded.add(run_id)
        await self._repo.log_event(
            run_id,
            event_type,
            {
                "tp1_price": tp1_price,
                "last_price": last_price,
                "best_bid": best_bid,
                "best_ask": best_ask,
                "tp1_order_id": order_id,
                "tp1_client_order_id": tp1_client_order_id,
                "order_status": order_status,
                "order_qty": orig_qty,
                "remaining_qty": remaining_qty,
                "order_created_ts": tp1_order.get("time") or tp1_order.get("updateTime") if tp1_order else None,
                "touch_ts": int(time.time() * 1000),
                "touch_duration_ms": None,
                "crossed_ticks": round(max(0.0, crossed_ticks), 4),
                "last_touch": bool(last_touch),
                "executable_touch": bool(executable_touch),
                "partial_exit_seen": False,
                "side": side,
            },
        )

    async def _sync_take_profit_orders(self, run: dict, position: PositionInfo, signal: dict) -> list[tuple[str, str, float]]:
        run_id = run["run_id"]
        side = str(run.get("side") or signal.get("side") or position.position_direction).upper()
        if side not in {"LONG", "SHORT"}:
            return []
        current_qty = abs(position.position_amt)
        if current_qty <= 0:
            return []
        close_side = "SELL" if position.position_direction == "LONG" else "BUY"
        desired = await self._desired_take_profit_orders(run, position, signal, close_side)
        existing_orders = await self._client.get_open_orders(position.symbol)
        existing_tp = [
            order for order in existing_orders
            if str(order.get("clientOrderId") or "").startswith(f"{run_id}_tp")
        ]
        await self._audit_tp1_touch_no_fill(run, position, desired, existing_tp, signal)
        current_qty = abs(position.position_amt)
        if self._take_profit_orders_match(
            existing_tp, desired, current_qty, close_side, position.mark_price
        ):
            return desired
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
        actual_orders: list[tuple[str, str, float]] = []
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
                actual_orders.append((client_order_id, qty, price))
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
                    return actual_orders
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
                        actual_orders.append((client_order_id, qty, book_price))
                    except BinanceAPIException as requote_exc:
                        if requote_exc.code == -2022:
                            logger.info(
                                "tp_requote_reduce_only_rejected_position_gone",
                                run_id=run_id,
                                client_order_id=client_order_id,
                                code=requote_exc.code,
                            )
                            return actual_orders
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
        for client_order_id, qty, _ in actual_orders:
            if client_order_id.endswith(PARTIAL_TP_SUFFIX):
                layer_qty["tp1"] = float(qty)
            elif client_order_id.endswith(MID_TP_SUFFIX):
                layer_qty["tp2"] = float(qty)
            elif client_order_id.endswith(FINAL_TP_SUFFIX):
                layer_qty["tp3"] = float(qty)
        if any(client_order_id.endswith(PARTIAL_TP_SUFFIX) for client_order_id, _, _ in actual_orders):
            self._partial_order_armed.add(run_id)
        if any(client_order_id.endswith(MID_TP_SUFFIX) for client_order_id, _, _ in actual_orders):
            self._mid_order_armed.add(run_id)
        if any(client_order_id.endswith(FINAL_TP_SUFFIX) for client_order_id, _, _ in actual_orders):
            self._final_order_armed.add(run_id)
        await self._repo.log_event(
            run_id,
            "take_profit_synced",
            {
                "orders": [
                    {"client_order_id": client_order_id, "qty": qty, "price": price}
                    for client_order_id, qty, price in actual_orders
                ]
            },
        )
        return actual_orders

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
        partial_price = self._partial_take_profit_price(position, shrink, signal=signal)
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
        partial_exit_pct = self._signal_partial_exit_pct(signal)
        if (
            partial_exit_pct > 0
            and partial_price > 0
            and abs(partial_price - full_tp_price) > 0.01
        ):
            partial_qty_raw = current_qty * partial_exit_pct
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
        if (
            full_tp_price > 0
            and remaining_qty > 0
            and not self._settings.mainnet_trail_disable_final_tp
        ):
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
    def _trail_ready(self, run_id: str) -> bool:
        if not self._settings.mainnet_trail_require_partial_fill:
            return True
        return run_id in self._partial_exits

    def _effective_sl_pct(self, signal: dict) -> float:
        override = float(getattr(self._settings, "mainnet_hard_sl_pct_override", 0.0) or 0.0)
        if override > 0:
            return override
        return float(signal.get("wildcat", {}).get("sl_pct") or 0.0)

    @staticmethod
    def _sl_price_from_pct(entry_price: float, side: str, sl_pct: float) -> float:
        if entry_price <= 0 or sl_pct <= 0:
            return 0.0
        if side == "LONG":
            return entry_price * (1 - sl_pct)
        if side == "SHORT":
            return entry_price * (1 + sl_pct)
        return 0.0

    def _partial_take_profit_price(
        self,
        position: PositionInfo,
        shrink: float = 1.0,
        signal: Mapping[str, Any] | None = None,
    ) -> float:
        pct = self._signal_partial_tp_pct(signal or {}) * shrink
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
        close_side: str = "",
        mark_price: float = 0.0,
    ) -> bool:
        """Check if existing TP orders match the desired set (price+qty per level).

        Compares each desired order against existing orders by price within a
        tolerance.  If every desired level either (a) has a matching existing
        order with the same qty, or (b) has qty==0 (already filled), the set
        matches and no cancel/rebuild is needed.

        #29 (2026-06-11): when the market has spiked PAST a TP level, the
        -5022 fallback re-quotes that level at the passive top-of-book — a
        much better price than the cost-basis TP.  The old price-equality
        check then saw that re-quote as a mismatch on the next sync cycle and
        cancel/re-placed it at the stale TP price (rejected again → re-quoted
        again → cancelled again, ~10s per lap; 110 such laps in the live log,
        run cry3mn_1781147531799 chased 1635.95→1635.04 in 30s without ever
        resting).  The TP ladder therefore never filled during favourable
        spikes and only re-armed after price fell back — exactly the window
        where the windfall had already evaporated.  Fix: an existing order
        resting at a BETTER-than-desired price also matches, but only while
        the mark is still beyond the desired level (the desired GTX would be
        rejected again anyway).  Once price retraces inside the level the
        normal rebuild — including the post-DCA re-peg — takes over.
        """
        if not desired_orders:
            return len(existing_orders) == 0

        def _better_and_beyond(existing_price: float, desired_price: float) -> bool:
            if not close_side or mark_price <= 0:
                return False
            if close_side == "BUY":
                return existing_price < desired_price and mark_price < desired_price
            return existing_price > desired_price and mark_price > desired_price

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
                price_ok = abs(ep - desired_price) < 0.005 or _better_and_beyond(ep, desired_price)
                if price_ok and abs(eq - desired_qty) < 1e-9:
                    matched = True
                    break
            if not matched:
                return False

        for o in existing_orders:
            p = float(o.get("price", 0) or 0)
            covered = any(
                abs(dp - p) < 0.005 or _better_and_beyond(p, dp)
                for _, _, dp in desired_orders
            )
            if not covered:
                return False

        return True

    @staticmethod
    def _signed_drift_bp(candles: list[Candle], window_bars: int) -> float | None:
        """Signed close-to-close net drift of the last `window_bars` 1m bars, in bp.

        Positive = up-drift, negative = down-drift.  Returns None when there is
        not enough candle history — callers must fail OPEN (treat unknown drift
        as no-block / no-boost) so a thin candle cache cannot silently disable
        DCA or distort sizing.
        """
        window = max(1, int(window_bars))
        if len(candles) < window + 1:
            return None
        last_close = candles[-1].close
        if last_close <= 0:
            return None
        return (last_close - candles[-(window + 1)].close) / last_close * 1e4

    def _dca_drift_blocked(self, candles: list[Candle]) -> float | None:
        """Return the offending drift (bp) when the DCA drift gate blocks, else None.

        P1 (2026-06-11, Codex proposal): block ONLY DCA — never the entry —
        when the market shows sustained directional drift.  Golden-window
        forensics (06-10): the 93%-WR segment (13:00-18:50 TW) drifted just
        +6bp over 5.1h and DCA was a profit assist; the V5.5 losing segment
        was a −183bp/6.8h downtrend where DCA amplified every loss (both −2.0
        tails were DCA layer fills mid-dump).  Mean-reversion entries are fine
        in both regimes — averaging INTO a drift is what bleeds.  This gate is
        re-evaluated on every DCA attempt by design: once the drift fades the
        gate re-opens (unlike the permanent per-run momentum-guard ban).
        """
        gate = self._settings.mainnet_dca_drift_gate_bp
        if gate <= 0:
            return None
        drift_bp = self._signed_drift_bp(candles, self._settings.mainnet_dca_drift_window_bars)
        if drift_bp is None:
            return None
        return drift_bp if abs(drift_bp) > gate else None

    async def _maybe_recovery(self, run: dict, signal: dict, position: PositionInfo) -> bool:
        if self._run_uses_codex_v1(run):
            return False
        if not self._settings.mainnet_recovery_enabled:
            return False
        if not self._dca_enabled:
            return False
        count = self._recovery_counts.get(run["run_id"], 0)
        if count >= self._settings.mainnet_recovery_steps:
            return False
        # Block DCA after TP1 partial fill: never average into a runner that
        # already booked partial profit.
        if run["run_id"] in self._partial_exits:
            logger.info("dca_blocked_partial_exit", run_id=run["run_id"])
            return False
        # P0 (06-11): one guard block = permanent DCA ban for this run.  The
        # 60s cooldown alone re-opened the door: all 4 losing guarded-DCA
        # fills (net −5.58 USDC) landed 1.1~2.8 min after the block, well past
        # the window.  See __init__ (_dca_guard_blocked_runs) for the data.
        if run["run_id"] in self._dca_guard_blocked_runs:
            if run["run_id"] not in self._dca_guard_blocked_notified:
                self._dca_guard_blocked_notified.add(run["run_id"])
                logger.info("dca_blocked_guard_permanent", run_id=run["run_id"], path="poll")
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
        # V6.5: scale both the layer notional and the cumulative cap by the
        # run's actual entry scale (rng15 sweet-zone sizing).  Without this a
        # scaled entry eats the unscaled cap and silently swallows the last
        # DCA layer (the original 1.2x bookkeeping bug).
        scale = self._notional_scale.get(run["run_id"], 1.0)
        entry_notional = self._settings.mainnet_effective_entry_notional_usdc * scale
        cumulative = float(run.get("cumulative_notional_usdc") or entry_notional)
        if cumulative + entry_notional > self._settings.mainnet_effective_max_cumulative_notional_usdc * scale:
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
        # Load candles once for both DCA gates (Stoch momentum guard + drift gate).
        candles: list[Candle] | None = None
        if self._settings.mainnet_dca_guard_enabled or self._settings.mainnet_dca_drift_gate_bp > 0:
            candles = await self._load_candles(position.symbol)
        if self._settings.mainnet_dca_guard_enabled:
            allow_dca, guard_reason = evaluate_dca_guard(candles, position.position_direction)
            if not allow_dca:
                self._dca_block_times[run["run_id"]] = int(time.time() * 1000)
                # P0 (06-11): arm the permanent per-run DCA ban — see __init__
                # for the live loss data (net −5.58 over 5 post-block fills).
                self._dca_guard_blocked_runs.add(run["run_id"])
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
                    f"{self._codex_v1_telegram_note(run)}"
                )
                return False
        # P1 (06-11): DCA-only drift gate — checked after the momentum guard so
        # a momentum block still arms the permanent ban first.  Dynamic by
        # design: NOT added to _dca_guard_blocked_runs, so DCA resumes once
        # the drift fades (see _dca_drift_blocked for the regime rationale).
        if candles is not None:
            drift_bp = self._dca_drift_blocked(candles)
            if drift_bp is not None:
                logger.info(
                    "dca_drift_blocked",
                    run_id=run["run_id"],
                    dca_number=count + 1,
                    drift_bp=round(drift_bp, 2),
                    gate_bp=self._settings.mainnet_dca_drift_gate_bp,
                    window_bars=self._settings.mainnet_dca_drift_window_bars,
                    path="poll",
                )
                event_key = (run["run_id"], count + 1)
                if event_key not in self._dca_drift_event_keys:
                    self._dca_drift_event_keys.add(event_key)
                    await self._repo.log_event(run["run_id"], "dca_drift_blocked", {
                        "dca_number": count + 1,
                        "drift_bp": round(drift_bp, 2),
                        "gate_bp": self._settings.mainnet_dca_drift_gate_bp,
                        "window_bars": self._settings.mainnet_dca_drift_window_bars,
                        "mark_price": position.mark_price,
                        "path": "poll",
                    })
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
                f"{self._codex_v1_telegram_note(run)}"
            )
            return False
        self._recovery_counts[run["run_id"]] = count + 1
        await self._repo.update_run(run["run_id"], cumulative_notional_usdc=cumulative + entry_notional)
        await self._repo.log_event(run["run_id"], "recovery_entry_placed", {"order": order, "signal": signal})
        await self._notify(
            f"🧩 Mainnet one-run 已掛 DCA maker 單 #{count + 1}：<code>{escape(run['run_id'])}</code>"
            f"{self._codex_v1_telegram_note(run)}"
        )
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
        if not self._trail_ready(run_id):
            return False
        if run_id in self._trail_exiting:
            # The fast watcher already owns the lock-exit; report handled so
            # the manage cycle skips its SL/ADVERSE/MAX_HOLD close paths.
            return True
        peak = self._trail_peak.get(run_id)
        arm_mfe = self._trail_arm_mfe(run, tp_pct)
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
                await self._repo.log_event(
                    run_id,
                    "trail_armed",
                    {
                        "symbol": position.symbol,
                        "side": side,
                        "position_qty": qty,
                        "entry_price": entry,
                        "current_price": mark,
                        "mfe_r": round((new_peak - entry) / entry, 8),
                        "trail_level": None,
                        "reason": "manage_cycle",
                    },
                )
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
                await self._repo.log_event(
                    run_id,
                    "trail_armed",
                    {
                        "symbol": position.symbol,
                        "side": side,
                        "position_qty": qty,
                        "entry_price": entry,
                        "current_price": mark,
                        "mfe_r": round((entry - new_peak) / entry, 8),
                        "trail_level": None,
                        "reason": "manage_cycle",
                    },
                )
                self._start_trail_watch(run, side, close_side, tp_pct)
        return False

    def _is_w6a_run(self, run: Mapping[str, Any]) -> bool:
        signal = self._codex_v1_signal_payload(run)
        return self._codex_v1_signal_lane_code(signal) == "W6A"

    def _w6a_fast_trail_enabled(self) -> bool:
        if CODEX_V1_VERSION.startswith(("_codex_v1.3.8", "_codex_v1.3.9", "_codex_v1.4")):
            return bool(getattr(self._settings, "mainnet_codex_v138_w6a_fast_trail_enabled", False))
        return bool(getattr(self._settings, "mainnet_codex_v137_w6a_fast_trail_enabled", True))

    def _w6a_fast_trail_watch_interval_seconds(self) -> int:
        if CODEX_V1_VERSION.startswith(("_codex_v1.3.8", "_codex_v1.3.9", "_codex_v1.4")):
            return max(1, int(getattr(self._settings, "mainnet_codex_v138_w6a_trail_watch_interval_seconds", 1) or 1))
        return max(1, int(getattr(self._settings, "mainnet_codex_v137_w6a_trail_watch_interval_seconds", 1) or 1))

    def _w6a_fast_trail_arm_cap_bp(self) -> float:
        if CODEX_V1_VERSION.startswith(("_codex_v1.3.8", "_codex_v1.3.9", "_codex_v1.4")):
            return float(getattr(self._settings, "mainnet_codex_v138_w6a_trail_arm_cap_bp", 3.5) or 0.0)
        return float(getattr(self._settings, "mainnet_codex_v137_w6a_trail_arm_cap_bp", 3.5) or 0.0)

    def _trail_watch_interval_seconds(self, run: Mapping[str, Any]) -> int:
        if self._is_w6a_run(run) and self._w6a_fast_trail_enabled():
            return self._w6a_fast_trail_watch_interval_seconds()
        return max(1, int(self._settings.mainnet_trail_watch_interval_seconds))

    def _trail_arm_mfe(self, run: Mapping[str, Any], tp_pct: float) -> float:
        arm_mfe = tp_pct * self._settings.mainnet_trail_arm_frac
        if self._is_w6a_run(run) and self._w6a_fast_trail_enabled():
            cap_bp = self._w6a_fast_trail_arm_cap_bp()
            if cap_bp > 0:
                arm_mfe = min(arm_mfe, cap_bp / 10_000.0)
        return arm_mfe
    def _start_trail_watch(self, run: dict, side: str, close_side: str, tp_pct: float) -> None:
        """Spawn the fast trail watcher for a run (idempotent).

        Started at ENTRY FILL (not at arming): the watcher tracks the peak and
        arms itself in the 2s loop, so a sub-cycle spike is recorded even before
        the 10s manage cycle wakes.
        """
        run_id = run["run_id"]
        if (
            not self._settings.mainnet_trail_enabled
            or tp_pct <= 0
            or not self._trail_ready(run_id)
        ):
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
        interval = self._trail_watch_interval_seconds(run)
        arm_mfe = self._trail_arm_mfe(run, tp_pct)
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
                        await self._repo.log_event(
                            run_id,
                            "trail_armed",
                            {
                                "symbol": symbol,
                                "side": side,
                                "position_qty": abs(position.position_amt),
                                "entry_price": entry,
                                "current_price": mark,
                                "mfe_r": round((new_peak - entry) / entry, 8),
                                "trail_level": None,
                                "reason": "watcher",
                            },
                        )
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
                        await self._repo.log_event(
                            run_id,
                            "trail_armed",
                            {
                                "symbol": symbol,
                                "side": side,
                                "position_qty": abs(position.position_amt),
                                "entry_price": entry,
                                "current_price": mark,
                                "mfe_r": round((entry - new_peak) / entry, 8),
                                "trail_level": None,
                                "reason": "watcher",
                            },
                        )
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
        await self._repo.log_event(
            run_id,
            "trail_fire_signal",
            {
                "symbol": position.symbol,
                "side": close_side,
                "position_qty": abs(position.position_amt),
                "entry_price": position.entry_price,
                "current_price": mark,
                "mfe_r": round(abs(peak - position.entry_price) / position.entry_price, 8) if position.entry_price > 0 else None,
                "trail_level": trail_stop,
                "reason": "watcher",
            },
        )
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
        if self._run_uses_codex_v1(run):
            return
        if not self._settings.mainnet_recovery_enabled:
            return
        if not self._dca_enabled:
            return
        count = self._recovery_counts.get(run_id, 0)
        if count >= self._settings.mainnet_recovery_steps:
            return
        if run_id in self._partial_exits:
            return
        # P0 (06-11): guard fired earlier in this run → DCA banned for the
        # run's whole life (see __init__: 1W/4L net −5.58 USDC across the 5
        # post-block fills the 60s cooldown failed to stop).
        if run_id in self._dca_guard_blocked_runs:
            if run_id not in self._dca_guard_blocked_notified:
                self._dca_guard_blocked_notified.add(run_id)
                logger.info("dca_blocked_guard_permanent", run_id=run_id, path="preplace")
            return
        # V6.5: scale-aware cap (see _maybe_recovery for rationale).
        scale = self._notional_scale.get(run_id, 1.0)
        entry_notional = self._settings.mainnet_effective_entry_notional_usdc * scale
        cumulative = float(run.get("cumulative_notional_usdc") or entry_notional)
        if cumulative + entry_notional > self._settings.mainnet_effective_max_cumulative_notional_usdc * scale:
            return
        # Load candles once for both DCA gates (Stoch momentum guard + drift gate).
        candles: list[Candle] | None = None
        if self._settings.mainnet_dca_guard_enabled or self._settings.mainnet_dca_drift_gate_bp > 0:
            candles = await self._load_candles(position.symbol)
        # Guard: don't pre-place if momentum is already adverse
        if self._settings.mainnet_dca_guard_enabled:
            allow, guard_reason = evaluate_dca_guard(candles, position.position_direction)
            if not allow:
                # P0 (06-11): arm the permanent per-run DCA ban — see __init__.
                self._dca_guard_blocked_runs.add(run_id)
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
        # P1 (06-11): DCA-only drift gate — dynamic, so the next preplace
        # attempt (after a later fill or guard pass) re-evaluates and may pass
        # once the drift fades.  Never feeds the permanent ban set.
        if candles is not None:
            drift_bp = self._dca_drift_blocked(candles)
            if drift_bp is not None:
                logger.info(
                    "dca_drift_blocked",
                    run_id=run_id,
                    dca_number=count + 1,
                    drift_bp=round(drift_bp, 2),
                    gate_bp=self._settings.mainnet_dca_drift_gate_bp,
                    window_bars=self._settings.mainnet_dca_drift_window_bars,
                    path="preplace",
                )
                event_key = (run_id, count + 1)
                if event_key not in self._dca_drift_event_keys:
                    self._dca_drift_event_keys.add(event_key)
                    await self._repo.log_event(run_id, "dca_drift_blocked", {
                        "dca_number": count + 1,
                        "drift_bp": round(drift_bp, 2),
                        "gate_bp": self._settings.mainnet_dca_drift_gate_bp,
                        "window_bars": self._settings.mainnet_dca_drift_window_bars,
                        "mark_price": position.mark_price,
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
        # #25: remember what a FULL fill of this layer looks like so the qty-grew
        # detector in _run_running can distinguish a partial fill (sync only) from
        # a complete layer (widen SL, +1 layer, +notional, pre-place next).
        try:
            intended_qty = abs(float(qty_str))
        except (TypeError, ValueError):
            intended_qty = 0.0
        self._dca_preload_meta[run_id] = {
            "order_id": order_id,
            "intended_qty": intended_qty,
            "base_qty": abs(position.position_amt),
        }
        await self._repo.log_event(
            run_id,
            "dca_preloaded",
            {"order_id": order_id, "price": limit_price, "qty": qty_str, "step": count + 1},
        )
        logger.info("dca_preloaded", run_id=run_id, step=count + 1, price=limit_price, qty=qty_str)

    def _hit_stop(self, side: str, mark: float, sl_price: float) -> bool:
        hit_sl = mark <= sl_price if side == "LONG" else mark >= sl_price
        return sl_price > 0 and hit_sl

    async def _maybe_codex_survival_exit(
        self,
        run: dict,
        signal: dict,
        position: "PositionInfo",
        side: str,
        mark: float,
        entry: float,
        qty: float,
        close_side: str,
        hold_start_ms: int,
    ) -> bool:
        """Codex-only 5m+ survival manager for weak trades.

        Recent live Codex losses hit SL around 6-8 minutes, while the generic
        adverse exit waits about 10 minutes. This guard starts observing after
        five minutes and only closes after the trade has failed to develop into
        TP/TRAIL territory.
        """
        if not self._settings.mainnet_codex_survival_enabled:
            return False
        if not self._run_uses_codex_v1(run):
            return False
        if side not in {"LONG", "SHORT"} or entry <= 0 or mark <= 0 or qty <= 0:
            return False

        now_ms = int(time.time() * 1000)
        age_seconds = max(0.0, (now_ms - int(hold_start_ms)) / 1000.0)
        codex_payload = signal.get("codex_v1") if isinstance(signal, Mapping) else {}
        if not isinstance(codex_payload, Mapping):
            codex_payload = {}
        lane = codex_payload.get("lane")
        lane_code = str(codex_payload.get("lane_code") or "").upper()
        survival_profile = None
        if CODEX_V1_VERSION.startswith(("_codex_v1.3.9", "_codex_v1.4")) and lane_code == "CNL-WPR-L":
            survival_profile = "v139b_wpr_waiting_scratch"
        elif (
            CODEX_V1_VERSION.startswith(("_codex_v1.3.9", "_codex_v1.4"))
            and bool(getattr(self._settings, "mainnet_codex_v139_w1b_survival_enabled", False))
            and lane_code == "W1B"
        ):
            survival_profile = "v139_w1b_delayed"

        watch_after = float(self._settings.mainnet_codex_survival_watch_after_seconds)
        if survival_profile == "v139b_wpr_waiting_scratch":
            watch_after = 0.0
        if age_seconds < watch_after:
            return False

        current_bp = self._side_pnl_bp(side, entry, mark)
        peak = self._trail_peak.get(run["run_id"], mark)
        mfe_bp = max(0.0, self._side_pnl_bp(side, entry, peak))

        if run["run_id"] not in self._codex_survival_watch_notified:
            self._codex_survival_watch_notified.add(run["run_id"])
            await self._repo.log_event(
                run["run_id"],
                "codex_survival_watch",
                {
                    "age_seconds": round(age_seconds, 1),
                    "side": side,
                    "entry": entry,
                    "mark": mark,
                    "mfe_bp": round(mfe_bp, 4),
                    "current_bp": round(current_bp, 4),
                    "lane": lane,
                    "lane_code": lane_code or None,
                    "survival_profile": survival_profile,
                },
            )

        min_mfe = float(self._settings.mainnet_codex_survival_min_mfe_bp)
        micro_floor = float(self._settings.mainnet_codex_survival_micro_trail_floor_bp)
        early_fail_loss = float(self._settings.mainnet_codex_survival_early_fail_loss_bp)
        damage_loss = float(self._settings.mainnet_codex_survival_damage_loss_bp)
        exit_after = float(self._settings.mainnet_codex_survival_exit_after_seconds)
        force_after = float(self._settings.mainnet_codex_survival_force_after_seconds)
        if survival_profile == "v139_w1b_delayed":
            exit_after = float(getattr(self._settings, "mainnet_codex_v139_w1b_survival_exit_after_seconds", 900) or 900)
            force_after = float(getattr(self._settings, "mainnet_codex_v139_w1b_survival_force_after_seconds", 900) or 900)
            early_fail_loss = float(getattr(self._settings, "mainnet_codex_v139_w1b_survival_early_fail_loss_bp", 14.0) or 14.0)
            damage_loss = float(getattr(self._settings, "mainnet_codex_v139_w1b_survival_damage_loss_bp", 14.0) or 14.0)
        elif survival_profile == "v139b_wpr_waiting_scratch":
            min_mfe = float(getattr(self._settings, "mainnet_codex_v139b_wpr_scratch_mfe_bp", 3.0) or 3.0)
            micro_floor = float(getattr(self._settings, "mainnet_codex_v139b_wpr_scratch_floor_bp", 0.5) or 0.5)
            force_after = float(getattr(self._settings, "mainnet_codex_v139b_wpr_force_after_seconds", 240) or 240)
            damage_loss = float(getattr(self._settings, "mainnet_codex_v139b_wpr_damage_loss_bp", 5.0) or 5.0)

        reason: str | None = None
        if survival_profile == "v139b_wpr_waiting_scratch":
            if age_seconds >= force_after and current_bp <= -damage_loss:
                reason = "CNL_WPR_DAMAGE_CONTROL"
            elif mfe_bp >= min_mfe and current_bp <= micro_floor:
                reason = "CNL_WPR_SCRATCH"
        elif (
            age_seconds >= exit_after
            and mfe_bp >= min_mfe
            and 0.0 <= current_bp <= micro_floor
        ):
            reason = "CODEX_MICRO_TRAIL"
        elif age_seconds >= exit_after and mfe_bp < min_mfe and current_bp <= -early_fail_loss:
            reason = "CODEX_EARLY_FAIL"
        elif age_seconds >= force_after and current_bp <= -damage_loss:
            reason = "CODEX_DAMAGE_CONTROL"

        if not reason:
            return False

        await self._repo.log_event(
            run["run_id"],
            "codex_survival_exit",
            {
                "reason": reason,
                "age_seconds": round(age_seconds, 1),
                "side": side,
                "entry": entry,
                "mark": mark,
                "mfe_bp": round(mfe_bp, 4),
                "current_bp": round(current_bp, 4),
                "qty": qty,
                "lane": lane,
                "lane_code": lane_code or None,
                "survival_profile": survival_profile,
            },
        )
        await self._close_position(position.symbol, close_side, qty, reason, run)
        return True

    @staticmethod
    def _side_pnl_bp(side: str, entry: float, price: float) -> float:
        if entry <= 0 or price <= 0:
            return 0.0
        if side == "LONG":
            return (price - entry) / entry * 10_000.0
        if side == "SHORT":
            return (entry - price) / entry * 10_000.0
        return 0.0

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
        try:
            current_position = await self._client.get_position(symbol)
            if current_position is None or abs(float(current_position.position_amt)) < 1e-9:
                logger.info("close_skipped_position_flat", run_id=run_id, reason=reason)
                await self._repo.log_event(
                    run_id,
                    "close_skipped_position_flat",
                    {"reason": reason, "side": side},
                )
                if reason.startswith("CODEX_") or reason in {"CNL_WPR_SCRATCH", "CNL_WPR_DAMAGE_CONTROL"}:
                    await self._repo.log_event(
                        run_id,
                        "survival_exit_done",
                        {"reason": reason, "mode": "already_flat"},
                    )
                return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("close_position_flat_check_failed", run_id=run_id, reason=reason, error=str(exc)[:200])
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

        # Maker-first exits are allowed only for timed/profit-lock software
        # exits. Emergency stop-style exits still go straight to market.
        survival_maker_reasons = {"CODEX_MICRO_TRAIL", "CODEX_EARLY_FAIL", "CODEX_DAMAGE_CONTROL", "CNL_WPR_SCRATCH", "CNL_WPR_DAMAGE_CONTROL"}
        no_bounce_maker_reason = "w6a_no_bounce_soft_exit_v2"
        no_bounce_reasons = {no_bounce_maker_reason, "w6a_no_bounce_market_fallback"}
        use_maker_exit = (
            reason == "TRAIL" and self._settings.mainnet_trail_exit_use_maker
        ) or (
            reason in survival_maker_reasons
            and self._settings.mainnet_codex_survival_exit_use_maker
        ) or (
            reason == no_bounce_maker_reason
            and getattr(self._settings, "mainnet_codex_v137_w6a_no_bounce_exit_live", True)
        )
        if use_maker_exit:
            if reason in survival_maker_reasons:
                maker_ttl = self._settings.mainnet_codex_survival_exit_maker_ttl_seconds
            elif reason == no_bounce_maker_reason:
                maker_ttl = getattr(self._settings, "mainnet_codex_v137_w6a_no_bounce_maker_ttl_seconds", 5)
            else:
                maker_ttl = None
            enforce_profit_floor = reason in {"TRAIL", "CODEX_MICRO_TRAIL"}
            if reason in survival_maker_reasons or reason == no_bounce_maker_reason:
                adverse_break_bp = self._settings.mainnet_codex_survival_exit_adverse_break_bp
            else:
                adverse_break_bp = None
            if await self._try_trail_maker_exit(
                symbol,
                side,
                qty_str,
                run,
                reason=reason,
                ttl_seconds=maker_ttl,
                enforce_profit_floor=enforce_profit_floor,
                adverse_break_bp=adverse_break_bp,
            ):
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
                if reason in survival_maker_reasons:
                    await self._repo.log_event(
                        run_id,
                        "survival_exit_done",
                        {"reason": reason, "mode": "already_flat", "code": exc.code},
                    )
                return True
            raise
        await self._repo.log_event(run_id, "close_submitted", {"reason": reason, "order": order})
        if reason == "TRAIL":
            await self._repo.log_event(run_id, "trail_fire_order_submitted", {"reason": reason, "order": order})
        if reason in no_bounce_reasons:
            await self._repo.log_event(
                run_id,
                "no_bounce_market_fallback",
                {"reason": reason, "mode": "market", "order": order},
            )
        if reason in survival_maker_reasons:
            await self._repo.log_event(
                run_id,
                "survival_exit_done",
                {"reason": reason, "mode": "market", "order": order},
            )
        await self._repo.update_run(run_id, status="CLOSING", exit_reason=reason)
        codex_note = self._codex_v1_telegram_note(run)
        await self._notify(
            f"🏁 Mainnet one-run 已送出平倉：<code>{escape(run_id)}</code> reason=<b>{escape(reason)}</b>{codex_note}"
        )
        return True

    async def _try_trail_maker_exit(
        self,
        symbol: str,
        side: str,
        qty_str: str,
        run: dict,
        *,
        reason: str = "TRAIL",
        ttl_seconds: int | None = None,
        enforce_profit_floor: bool = True,
        adverse_break_bp: float | None = None,
    ) -> bool:
        """Lock a trailing/survival exit at maker fee, falling back to market on timeout.

        Places a reduce-only POST_ONLY limit at the passive top-of-book and,
        for up to mainnet_trail_exit_maker_ttl_seconds, re-prices it every
        mainnet_trail_exit_reprice_seconds to chase the book so a moving market
        does not strand the order at a stale price (Run 61139 ate a taker fee
        because the single static quote never re-anchored after the bid moved).
        Returns True if the position went flat (maker filled — 0 fee).  On
        timeout or placement rejection, cancels the resting order and returns
        False so the caller market-closes whatever remains (reduce_only caps
        the qty, so a partial maker fill is handled safely). Profit-floor
        enforcement stays on for profit-lock exits, but is disabled for damage
        control exits that are already intentionally accepting a small loss.
        """
        run_id = run["run_id"]
        is_no_bounce_exit = reason == "w6a_no_bounce_soft_exit_v2"
        client_order_id = f"{run_id}_no_bounce" if is_no_bounce_exit else f"{run_id}_trail"
        is_survival_exit = reason.startswith("CODEX_") or reason in {"CNL_WPR_SCRATCH", "CNL_WPR_DAMAGE_CONTROL"}

        async def _log_survival_fallback(fallback_reason: str, details: dict | None = None) -> None:
            if not is_survival_exit:
                return
            payload = {"reason": reason, "fallback_reason": fallback_reason}
            if details:
                payload.update(details)
            await self._repo.log_event(run_id, "survival_maker_fallback_market", payload)

        async def _log_no_bounce_fallback(fallback_reason: str, details: dict | None = None) -> None:
            if not is_no_bounce_exit:
                return
            payload = {"reason": reason, "fallback_reason": fallback_reason}
            if details:
                payload.update(details)
            await self._repo.log_event(run_id, "no_bounce_market_fallback", payload)

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
                    run_id, "trail_maker_place_failed", {"error": str(exc)[:300], "reason": reason}
                )
                await _log_survival_fallback("place_failed", {"error": str(exc)[:300]})
                await _log_no_bounce_fallback("place_failed", {"error": str(exc)[:300]})
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

        async def _current_pnl_bp() -> float | None:
            if cost_basis <= 0:
                return None
            try:
                position = await self._client.get_position(symbol)
                mark_price = float(getattr(position, "mark_price", 0.0) or 0.0) if position else 0.0
                if mark_price <= 0:
                    mark_price = await _anchor()
            except Exception as exc:  # noqa: BLE001
                logger.warning("survival_maker_adverse_check_failed", run_id=run_id, error=str(exc)[:200])
                return None
            if side == "SELL":
                return (mark_price - cost_basis) / cost_basis * 10_000.0
            return (cost_basis - mark_price) / cost_basis * 10_000.0

        floor_price: float | None = None
        if enforce_profit_floor:
            floor_bp = self._settings.mainnet_trail_profit_floor_bp
            floor_price = (
                cost_basis * (1 + floor_bp / 10_000)
                if side == "SELL"
                else cost_basis * (1 - floor_bp / 10_000)
            )

        ttl_source = (
            self._settings.mainnet_trail_exit_maker_ttl_seconds
            if ttl_seconds is None
            else ttl_seconds
        )
        ttl = max(0, int(ttl_source))
        anchor = await _anchor()
        # E3 (teardown race): the upstream anchor gate in _close_position passed,
        # but SL/TP/DCA teardown takes ~1s and a fast dump can drop the bid
        # through the floor in that window.  The SL is already gone here, so we
        # cannot abort back to managed state — lock what remains at market NOW
        # instead of resting a maker below cost and chasing it down.
        adverse_break_base_bp: float | None = None
        if adverse_break_bp is not None:
            adverse_break_base_bp = await _current_pnl_bp()

        if enforce_profit_floor and cost_basis > 0 and floor_price is not None and (
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
            await _log_survival_fallback(
                "profit_floor_break",
                {"anchor": anchor, "cost_basis": cost_basis, "floor_price": floor_price, "initial": True},
            )
            return False
        if is_survival_exit:
            await self._repo.log_event(
                run_id,
                "survival_maker_attempt",
                {
                    "reason": reason,
                    "side": side,
                    "qty": qty_str,
                    "anchor": anchor,
                    "ttl_seconds": ttl,
                    "adverse_break_bp": adverse_break_bp,
                    "adverse_break_base_bp": adverse_break_base_bp,
                    "enforce_profit_floor": enforce_profit_floor,
                },
            )
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
        await self._repo.log_event(run_id, "trail_maker_placed", {"order": order, "anchor": anchor, "reason": reason})
        if reason == "TRAIL":
            await self._repo.log_event(run_id, "trail_exit_order_submitted", {"order": order, "anchor": anchor, "reason": reason})
        if is_no_bounce_exit:
            await self._repo.log_event(
                run_id,
                "no_bounce_maker_order_submitted",
                {"order": order, "anchor": anchor, "reason": reason, "ttl_seconds": ttl},
            )
        codex_note = self._codex_v1_telegram_note(run)
        await self._notify(
            f"🪝 {escape(reason)} 改掛 maker（0 手續費）：<code>{escape(run_id)}</code> @ <b>${anchor:.4f}</b>{codex_note}"
        )

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
                if reason == "TRAIL":
                    await self._repo.log_event(run_id, "trail_exit_filled", {"reason": reason})
                if is_no_bounce_exit:
                    await self._repo.log_event(run_id, "no_bounce_maker_filled", {"reason": reason})
                await self._repo.update_run(run_id, status="CLOSING", exit_reason=reason)
                if is_survival_exit:
                    await self._repo.log_event(
                        run_id,
                        "survival_exit_done",
                        {"reason": reason, "mode": "maker"},
                    )
                codex_note = self._codex_v1_telegram_note(run)
                await self._notify(
                    f"🎯 {escape(reason)} maker 出場已成交（省 taker 費）：<code>{escape(run_id)}</code>{codex_note}"
                )
                return True
            current_bp = await _current_pnl_bp()
            adverse_break_threshold_bp = (
                adverse_break_base_bp - abs(float(adverse_break_bp))
                if adverse_break_bp is not None and adverse_break_base_bp is not None
                else (-abs(float(adverse_break_bp)) if adverse_break_bp is not None else None)
            )
            if (
                adverse_break_threshold_bp is not None
                and current_bp is not None
                and current_bp <= adverse_break_threshold_bp
            ):
                logger.info(
                    "survival_maker_adverse_break_market",
                    run_id=run_id,
                    reason=reason,
                    current_bp=current_bp,
                    adverse_break_bp=adverse_break_bp,
                    adverse_break_base_bp=adverse_break_base_bp,
                    adverse_break_threshold_bp=adverse_break_threshold_bp,
                )
                await self._repo.log_event(
                    run_id,
                    "survival_maker_adverse_break",
                    {
                        "reason": reason,
                        "current_bp": current_bp,
                        "adverse_break_bp": adverse_break_bp,
                        "adverse_break_base_bp": adverse_break_base_bp,
                        "adverse_break_threshold_bp": adverse_break_threshold_bp,
                    },
                )
                await _cancel_resting()
                await _log_survival_fallback(
                    "adverse_break",
                    {
                        "current_bp": current_bp,
                        "adverse_break_bp": adverse_break_bp,
                        "adverse_break_base_bp": adverse_break_base_bp,
                        "adverse_break_threshold_bp": adverse_break_threshold_bp,
                    },
                )
                await _log_no_bounce_fallback(
                    "adverse_break",
                    {
                        "current_bp": current_bp,
                        "adverse_break_bp": adverse_break_bp,
                        "adverse_break_base_bp": adverse_break_base_bp,
                        "adverse_break_threshold_bp": adverse_break_threshold_bp,
                    },
                )
                return False
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
                if enforce_profit_floor and cost_basis > 0 and floor_price is not None and (
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
                    await _log_survival_fallback(
                        "profit_floor_break",
                        {"anchor": new_anchor, "cost_basis": cost_basis, "floor_price": floor_price},
                    )
                    return False
                if abs(new_anchor - anchor) > reprice_threshold:
                    await _cancel_resting()
                    if await _flat():
                        # Filled in the gap between cancel and re-check.
                        logger.info("trail_maker_filled", run_id=run_id)
                        await self._repo.log_event(run_id, "trail_maker_filled", {})
                        if reason == "TRAIL":
                            await self._repo.log_event(run_id, "trail_exit_filled", {"reason": reason})
                        if is_no_bounce_exit:
                            await self._repo.log_event(run_id, "no_bounce_maker_filled", {"reason": reason})
                        await self._repo.update_run(run_id, status="CLOSING", exit_reason=reason)
                        if is_survival_exit:
                            await self._repo.log_event(
                                run_id,
                                "survival_exit_done",
                                {"reason": reason, "mode": "maker"},
                            )
                        codex_note = self._codex_v1_telegram_note(run)
                        await self._notify(
                            f"🎯 {escape(reason)} maker 出場已成交（省 taker 費）：<code>{escape(run_id)}</code>{codex_note}"
                        )
                        return True
                    reorder = await _place(new_anchor)
                    if reorder is None:
                        # Re-placement rejected — bail to the market fallback.
                        return False
                    anchor = new_anchor
                    await self._repo.log_event(
                        run_id, "trail_maker_repriced", {"anchor": new_anchor, "reason": reason}
                    )
        # TTL elapsed without a full fill — cancel the resting maker order and
        # let the caller market-close the remainder (reduce_only caps the qty).
        logger.info("trail_maker_timeout_fallback_market", run_id=run_id, ttl_seconds=ttl)
        await self._repo.log_event(run_id, "trail_maker_timeout", {"ttl_seconds": ttl, "reason": reason})
        await _log_survival_fallback("timeout", {"ttl_seconds": ttl})
        await _log_no_bounce_fallback("timeout", {"ttl_seconds": ttl})
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
            codex_note = self._codex_v1_telegram_note(run)
            await self._notify(
                f"🏁 Mainnet one-run 已送出平倉（SL 掛單失敗，市價）：<code>{escape(run_id)}</code> reason=<b>{escape(reason)}</b>{codex_note}"
            )
            return

        await self._repo.log_event(run_id, "sl_placed", {"order": order, "sl_price": sl_price})
        codex_note = self._codex_v1_telegram_note(run)
        await self._notify(
            f"🛑 <b>Stop-Loss 已掛</b>\n"
            f"Run：<code>{escape(run_id)}</code>\n"
            f"觸發價：<b>${sl_price:.4f}</b> | Qty：<code>{qty_str}</code>{codex_note}"
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
        self._w6a_no_bounce_exiting.discard(run["run_id"])
        self._w6a_price_history.pop(run["run_id"], None)
        self._w6a_stop_tightened_runs.pop(run["run_id"], None)
        self._tp1_audit_recorded.discard(run["run_id"])
        self._w6a_post_tp_probe_recorded = {
            key for key in self._w6a_post_tp_probe_recorded if key[0] != run["run_id"]
        }
        self._dca_block_times.pop(run["run_id"], None)
        self._dca_preload_meta.pop(run["run_id"], None)
        self._rescue_spike_notified.discard(run["run_id"])
        self._spike_block_notified.discard(run["run_id"])
        self._codex_v1_guard_notified.discard(run["run_id"])
        self._codex_v1_reprice_shadow.pop(run["run_id"], None)
        self._clear_codex_v1_shadow_samples(run["run_id"])
        self._codex_survival_watch_notified.discard(run["run_id"])
        self._partial_taken.discard(run["run_id"])
        self._partial_exits.discard(run["run_id"])
        self._final_taken.discard(run["run_id"])
        self._final_order_armed.discard(run["run_id"])
        self._tp_layer_qty.pop(run["run_id"], None)
        self._rng15_guard_notified.discard(run["run_id"])
        self._notional_scale.pop(run["run_id"], None)
        self._dca_guard_blocked_runs.discard(run["run_id"])
        self._dca_guard_blocked_notified.discard(run["run_id"])
        self._dca_drift_event_keys = {k for k in self._dca_drift_event_keys if k[0] != run["run_id"]}
        summary = await self._build_run_summary(run)
        terminal_candles: list[Candle] = []
        try:
            terminal_candles = await self._load_candles(run["symbol"])
        except Exception as exc:  # noqa: BLE001 - terminalization is audit-only
            logger.warning("codex_v133_tp_terminalization_candles_failed", run_id=run["run_id"], error=str(exc)[:200])
        await self._terminalize_codex_v132_tp_policy_samples(run, summary, terminal_candles)
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
        await self._repo.log_event(
            run["run_id"],
            "completed",
            {
                "exit_event_type": "completed",
                "reason": exit_reason,
                "exit_reason_final": exit_reason,
                "total_qty": summary["qty"],
                "gross_pnl": summary["realized_pnl_usdc"],
                "total_commission": summary["commission_usdc"],
                "net_pnl": float(summary["realized_pnl_usdc"]) - float(summary["commission_usdc"]),
                "has_tp1": await self._repo.get_first_event_time(run["run_id"], "partial_exit") is not None,
                "has_trail": exit_reason == "TRAIL",
                "has_soft_exit": str(exit_reason).startswith("w6a_no_bounce"),
            },
        )
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

        # Option A: direction consecutive-loss throttle.  Runs always (not
        # just in loops) because regime state is market-wide, not loop-scoped.
        _dir_side = str((run.get("params") or {}).get("side") or run.get("side") or "").upper()
        _dir_block_min = float(getattr(self._settings, "mainnet_dir_throttle_block_minutes", 30.0) or 30.0)
        _dir_window_sec = float(getattr(self._settings, "mainnet_dir_throttle_window_seconds", 3600.0) or 3600.0)
        _dir_count = int(getattr(self._settings, "mainnet_dir_throttle_loss_count", 2) or 2)
        if _dir_side and _dir_block_min > 0 and _dir_count > 0:
            _now_ms = time.time() * 1000
            if _is_loss_exit:
                _losses = self._dir_loss_times.get(_dir_side, [])
                _losses.append(_now_ms)
                _losses = [t for t in _losses if _now_ms - t <= _dir_window_sec * 1000]
                self._dir_loss_times[_dir_side] = _losses
                if len(_losses) >= _dir_count:
                    self._dir_throttle_until[_dir_side] = _now_ms + _dir_block_min * 60_000
                    logger.info(
                        "direction_throttle_armed",
                        run_id=run["run_id"],
                        side=_dir_side,
                        loss_count=len(_losses),
                        block_minutes=_dir_block_min,
                    )
            else:
                # Net win: reset loss counter so it does not compound across
                # winning periods.  Active block expires on its own timer.
                self._dir_loss_times.pop(_dir_side, None)
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
        codex_note = self._codex_v1_telegram_note(run)
        await self._notify(
            f"🏁 <b>Mainnet one-run 已完成{position_label}</b>\n"
            f"Run：<code>{escape(run['run_id'])}</code>\n"
            f"結果：<code>{escape(str(exit_reason))}</code>\n"
            f"最大倉位：<code>{summary['qty']:.6f}</code>\n"
            f"已實現損益：<b>${summary['realized_pnl_usdc']:.4f}</b>\n"
            f"手續費：<b>${summary['commission_usdc']:.4f}</b>"
            f"{codex_note}"
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
        self._rescue_spike_notified.discard(run["run_id"])
        self._spike_block_notified.discard(run["run_id"])
        self._rng15_guard_notified.discard(run["run_id"])
        self._codex_v1_guard_notified.discard(run["run_id"])
        self._codex_v1_reprice_shadow.pop(run["run_id"], None)
        self._clear_codex_v1_shadow_samples(run["run_id"])
        await self._drop_codex_v132_tp_policy_samples(run["run_id"], "entry_failure")
        self._codex_survival_watch_notified.discard(run["run_id"])
        self._w6a_no_bounce_exiting.discard(run["run_id"])
        self._w6a_price_history.pop(run["run_id"], None)
        self._w6a_stop_tightened_runs.pop(run["run_id"], None)
        self._tp1_audit_recorded.discard(run["run_id"])
        self._w6a_post_tp_probe_recorded = {
            key for key in self._w6a_post_tp_probe_recorded if key[0] != run["run_id"]
        }
        self._notional_scale.pop(run["run_id"], None)
        # Entry-stage failures never reach a position, so these should already
        # be empty — discard defensively to keep the sets bounded across loops.
        self._dca_guard_blocked_runs.discard(run["run_id"])
        self._dca_guard_blocked_notified.discard(run["run_id"])
        self._dca_drift_event_keys = {k for k in self._dca_drift_event_keys if k[0] != run["run_id"]}
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
        """Arm buttons + Telegram-adjustable config rows (💰 notional, 🛡 loss cap, DCA).

        The currently selected notional / loss-cap option is marked with ✓.
        """
        notional_now = int(self._settings.mainnet_equity_cap_usdc)
        cap_now = self._loop_loss_cap
        dca_on = self._dca_enabled

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
            [
                InlineKeyboardButton(
                    f"🔄 DCA {'開 ✓' if dca_on else '開'}",
                    callback_data="mainnet:dca:on",
                ),
                InlineKeyboardButton(
                    f"🚫 DCA {'關' if dca_on else '關 ✓'}",
                    callback_data="mainnet:dca:off",
                ),
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
        event_time_ms = int(time.time() * 1000)
        run_id = self._extract_run_id_from_text(text)
        notice = {
            "event_time_ms": event_time_ms,
            "run_id": run_id,
            "chat_id": self._settings.telegram_chat_id_int or None,
            "delivery_status": "skipped_not_configured",
            "text": text,
        }
        # NEVER let a Telegram failure propagate.  _notify is called from inside
        # run_cycle's body, whose exception handler marks the run FAILED and
        # stops managing it.  A telegram.error.TimedOut on a completion notice
        # overwrote a flat +0.163 run as FAILED (cry3mn_1781053774815, 06-10);
        # worse, a timeout on a MID-RUN notice would orphan a live position
        # (manager dead, pre-placed DCA GTC still resting on the exchange).
        # Swallow everything and only log.
        try:
            if self._telegram_app and self._settings.telegram_chat_id_int:
                msg = await self._telegram_app.bot.send_message(
                    chat_id=self._settings.telegram_chat_id_int,
                    text=text,
                    parse_mode="HTML",
                )
                notice["delivery_status"] = "sent"
                message_id = getattr(msg, "message_id", None)
                if message_id is not None:
                    notice["telegram_message_id"] = message_id
        except Exception as exc:
            notice["delivery_status"] = "failed"
            notice["error"] = str(exc)[:200]
            logger.warning("notify_failed_swallowed", error=str(exc)[:200])
        finally:
            await self._persist_telegram_notice(notice)

    @staticmethod
    def _strip_html_tags(text: str) -> str:
        return re.sub(r"<[^>]+>", "", text)

    def _extract_run_id_from_text(self, text: str) -> str | None:
        prefix = re.escape(self._settings.mainnet_client_order_prefix)
        match = re.search(rf"{prefix}_[A-Za-z0-9_]+", text)
        return match.group(0) if match else None

    @staticmethod
    def _extract_notice_metadata_from_text(text: str) -> dict[str, Any]:
        metadata: dict[str, Any] = {}
        patterns = {
            "notice_version": r"版本：<code>([^<]+)</code>",
            "notice_lane_code": r"Lane Code：(?:<code>)?([^<\n]+)(?:</code>)?",
            "notice_full_lane": r"Full Lane：(?:<code>)?([^<\n]+)(?:</code>)?",
            "notice_raw_classifier": r"Raw Classifier：(?:<code>)?([^<\n]+)(?:</code>)?",
            "notice_raw_rule": r"Raw Rule：(?:<code>)?([^<\n]+)(?:</code>)?",
            "notice_effective_execution": r"Effective Execution：(?:<code>)?([^<\n]+)(?:</code>)?",
            "notice_effective_reason": r"Live Reason：(?:<code>)?([^<\n]+)(?:</code>)?",
        }
        for key, pattern in patterns.items():
            match = re.search(pattern, text)
            if match:
                metadata[key] = match.group(1).strip()
        if text:
            first_line = re.sub(r"<[^>]+>", "", text.splitlines()[0]).strip()
            if first_line:
                metadata["notice_title"] = first_line
        return metadata

    @staticmethod
    def _extract_codex_metadata_from_signal_payload(signal_payload: Mapping[str, Any]) -> dict[str, Any]:
        codex = signal_payload.get("codex_v1") if isinstance(signal_payload, Mapping) else None
        if not isinstance(codex, Mapping):
            return {}
        metadata: dict[str, Any] = {
            "codex_version": codex.get("version"),
            "codex_baseline": codex.get("baseline"),
            "lane_code": codex.get("lane_code"),
            "full_lane": codex.get("lane"),
        }
        raw_classifier = codex.get("raw_classifier")
        if isinstance(raw_classifier, Mapping):
            metadata["raw_classifier"] = raw_classifier.get("lane_code")
            metadata["raw_rule"] = raw_classifier.get("lane")
            metadata["raw_reason"] = raw_classifier.get("reason")
        effective_execution = codex.get("effective_execution")
        if isinstance(effective_execution, Mapping):
            lane_code = effective_execution.get("lane_code")
            status = effective_execution.get("status")
            if lane_code or status:
                metadata["effective_execution"] = " / ".join(
                    part for part in (str(lane_code or "").strip(), str(status or "").strip()) if part
                )
            metadata["effective_reason"] = effective_execution.get("effective_reason") or effective_execution.get("reason")
        return {key: value for key, value in metadata.items() if value not in (None, "")}

    async def _enrich_notice_from_run(self, record: dict[str, Any]) -> None:
        run_id = record.get("run_id")
        if not run_id:
            return
        try:
            rows = await self._repo.get_runs_by_ids([str(run_id)])
        except Exception as exc:  # noqa: BLE001
            logger.warning("telegram_notice_run_lookup_failed", run_id=run_id, error=str(exc)[:200])
            return
        if not rows:
            return
        run = rows[0]
        record["run_status"] = run.get("status")
        record["run_exit_reason"] = run.get("exit_reason")
        record["run_updated_at_ms"] = run.get("updated_at_ms")
        record["run_completed_at_ms"] = run.get("completed_at_ms")
        signal_json = run.get("signal_json")
        if not signal_json:
            return
        try:
            signal_payload = json.loads(str(signal_json))
        except Exception as exc:  # noqa: BLE001
            logger.warning("telegram_notice_signal_json_parse_failed", run_id=run_id, error=str(exc)[:200])
            return
        for key, value in self._extract_codex_metadata_from_signal_payload(signal_payload).items():
            record.setdefault(key, value)

    async def _persist_telegram_notice(self, notice: dict[str, Any]) -> None:
        record = dict(notice)
        text = str(record.get("text") or "")
        record["text_plain"] = self._strip_html_tags(text)
        record.update(self._extract_notice_metadata_from_text(text))
        await self._enrich_notice_from_run(record)
        try:
            path = Path(self._settings.mainnet_telegram_notice_log_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as exc:  # noqa: BLE001 - audit persistence must not break trading
            logger.warning("telegram_notice_file_write_failed", error=str(exc)[:200])

        run_id = record.get("run_id")
        if not run_id:
            return
        try:
            await self._repo.log_event(run_id, "telegram_notice", record)
        except Exception as exc:  # noqa: BLE001 - audit persistence must not break trading
            logger.warning("telegram_notice_db_log_failed", run_id=run_id, error=str(exc)[:200])
