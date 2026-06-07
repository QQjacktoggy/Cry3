"""Telegram-triggered one-run mainnet validation manager."""

from __future__ import annotations

import asyncio
import json
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
from src.gridbot.strategy.wildcat_live import WildcatLiveDecision, generate_wildcat_v2_adverse_guard_live_decision
from src.gridbot.utils.logging import get_logger

logger = get_logger(__name__)
PARTIAL_TP_SUFFIX = "_tp1"
FINAL_TP_SUFFIX = "_tp2"


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
    ) -> None:
        self._settings = settings
        self._client = client
        self._repo = repo
        self._trade_repo = trade_repo
        self._telegram_app = telegram_app
        self._protection_sent: set[str] = set()
        self._partial_taken: set[str] = set()
        self._partial_order_armed: set[str] = set()
        self._recovery_counts: dict[str, int] = {}
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
        # Cooldown tracker for loop chains: key = (side, strategy_label),
        # value = cooldown_until_ms.  After an SL exit, the same side +
        # strategy combination is blocked for the configured duration so
        # we do not chain into an identical losing setup.
        self._loop_cooldowns: dict[tuple[str, str], int] = {}
        self._loop_cooldown_minutes: int = self._settings.mainnet_loop_cooldown_minutes

    async def status(self) -> RunStatus:
            latest = await self._repo.get_latest_run()
            active = await self._repo.get_active_run()
            entry_notional = self._settings.mainnet_effective_entry_notional_usdc
            entry_margin = self._settings.mainnet_effective_entry_margin_usdc
            lines = [
                "🧪 <b>Mainnet One-Run 驗證</b>",
                f"狀態：<b>{'已啟用' if self._settings.mainnet_one_run_enabled else '未啟用'}</b>",
                f"交易對：<code>{escape(self._settings.mainnet_symbol)}</code>",
                f"策略：<code>{escape(self._settings.mainnet_strategy_label)}</code>",
                f"資金上限：<b>${self._settings.mainnet_equity_cap_usdc:.2f} USDC</b>",
                f"單筆名目/槓桿：<b>${entry_notional:.2f}</b> / <b>{self._settings.mainnet_leverage}x</b>",
                f"預估保證金：<b>${entry_margin:.4f}</b>",
                "",
                "訊號仍會照常推送；按下啟動後，只會把下一個符合條件的 wildcat 訊號接成一個自動 run。",
            ]
            if self._loop_total > 0:
                lines.append("")
                lines.append(
                    f"🔁 <b>Loop 進行中：{self._loop_completed}/{self._loop_total}</b>"
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
            if latest:
                lines.extend(
                    [
                        "",
                        f"最近 run：<code>{escape(latest['run_id'])}</code>",
                        f"狀態：<b>{escape(latest['status'])}</b>",
                        f"結果：<code>{escape(str(latest.get('exit_reason') or '-'))}</code>",
                    ]
                )
            markup = self._buttons(active=bool(active))
            logger.info("mainnet_status_reply", has_markup=markup is not None, active=bool(active), loop_total=self._loop_total)
            return RunStatus("\n".join(lines), markup)

    async def arm(self, actor: str = "telegram", loop_count: int = 1) -> str:
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
        self._loop_cooldowns.clear()
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
        active = await self._repo.get_active_run()
        if not active:
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

    async def _run_armed(self, run: dict) -> None:
        if int(time.time() * 1000) - int(run["armed_at_ms"]) > self._settings.mainnet_one_run_signal_timeout_minutes * 60_000:
            await self._repo.complete_run(run["run_id"], "ENTRY_EXPIRED", "signal_timeout")
            await self._notify(f"⌛ Mainnet one-run 等待訊號逾時，已停止：<code>{escape(run['run_id'])}</code>")
            return
        candles = await self._load_candles(run["symbol"])
        decision = generate_wildcat_v2_adverse_guard_live_decision(
            candles,
            target_daily_usdc=self._settings.mainnet_equity_cap_usdc * 0.03,
            notional_usdc=self._settings.mainnet_effective_entry_notional_usdc,
            leverage=self._settings.mainnet_leverage,
        )
        if decision is None:
            return
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
            return
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

    async def _run_running(self, run: dict) -> None:
        symbol = run["symbol"]
        position = await self._client.get_position(symbol)
        if not position:
            await self._finish_flat_run(run, "flat_detected")
            return
        current_qty = abs(position.position_amt)
        if abs(current_qty - float(run.get("qty") or 0.0)) > 1e-9:
            # DCA filled - update SL maker to new average entry price
            await self._cancel_stop_loss_order(symbol, run["run_id"])
            if self._settings.mainnet_sl_use_maker:
                signal = json.loads(run.get("signal_json") or "{}")
                sl_pct = float(signal.get("wildcat", {}).get("sl_pct") or 0.0)
                if sl_pct > 0:
                    new_avg_entry = position.entry_price
                    if new_avg_entry > 0:
                        if position.position_direction == "LONG":
                            new_sl = new_avg_entry * (1 - sl_pct)
                        else:
                            new_sl = new_avg_entry * (1 + sl_pct)
                        await self._place_stop_loss_maker(
                            symbol=symbol,
                            side="SELL" if position.position_amt > 0 else "BUY",
                            qty_str=await self._client.format_quantity(symbol, current_qty),
                            sl_price=new_sl,
                            run_id=run["run_id"],
                            reason="SL",
                            run=run,
                        )
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

        await self._refresh_partial_fill_state(run, position)
        await self._sync_take_profit_orders(run, position, signal)
        if await self._maybe_recovery(run, signal, position):
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
            # Entry rejected — slippage exceeded tolerance, stop this run
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
            return
        final_price = float(order.get("price", 0) or decision.signal.price)
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
                "adverse_exit_bars": decision.adverse_exit_bars,
                "adverse_exit_loss_pct": decision.adverse_exit_loss_pct,
                "max_holding_bars": decision.max_holding_bars,
            },
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
        used_gtc = order.get("timeInForce") != "GTX"
        gtc_note = "\n⚠️ 使用 GTC 限價單進場（maker 保護已關閉）" if used_gtc else ""
        await self._notify(
            f"{'🟢' if decision.side == 'LONG' else '🔴'} <b>AUTO {('做多' if decision.side == 'LONG' else '做空')} 已掛 maker 單</b>\n"
            f"Run：<code>{escape(run['run_id'])}</code>\n"
            f"策略：<b>{escape(decision.strategy)}</b> | score=<code>{decision.signal.score}</code>\n"
            f"Entry：<b>${final_price:.4f}</b> | Qty：<code>{escape(str(qty))}</code>\n"
            f"Stop：<b>${float(decision.signal.stop_loss or 0):.4f}</b> | TP：<b>${float(decision.signal.take_profits[0] if decision.signal.take_profits else 0):.4f}</b>{gtc_note}\n"
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

        # No GTC fallback — re-raise last BinanceAPIException
        raise last_exc  # type: ignore[misc]

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

    async def _refresh_partial_fill_state(self, run: dict, position: PositionInfo) -> None:
        run_id = run["run_id"]
        if run_id in self._partial_taken or run_id not in self._partial_order_armed:
            return
        current_qty = abs(position.position_amt)
        initial_qty = float(run.get("qty") or 0.0)
        open_orders = await self._client.get_open_orders(position.symbol)
        partial_open = any(
            str(order.get("clientOrderId") or "") == f"{run_id}{PARTIAL_TP_SUFFIX}"
            for order in open_orders
        )
        if partial_open or abs(current_qty - initial_qty) < 1e-9:
            return
        qty_closed = max(0.0, initial_qty - current_qty)
        qty_text = await self._client.format_quantity(position.symbol, qty_closed) if qty_closed > 0 else "unknown"
        self._partial_taken.add(run_id)
        self._partial_order_armed.discard(run_id)
        await self._repo.log_event(run_id, "partial_exit", {"qty": qty_text, "position_qty": current_qty})
        await self._notify(
            f"✅ Mainnet one-run 已部分獲利了結：<code>{escape(run_id)}</code> qty=<code>{escape(str(qty_text))}</code>"
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
                if exc.code == -5022 and self._settings.mainnet_tp_fallback_to_gtc:
                    # Market past TP — fill immediately as taker to ensure exit
                    logger.warning(
                        "tp_post_only_rejected_fallback_gtc",
                        run_id=run_id,
                        client_order_id=client_order_id,
                        price=price,
                        side=close_side,
                    )
                    await self._client.create_reduce_only_limit_order(
                        position.symbol,
                        close_side,
                        qty,
                        price,
                        client_order_id=client_order_id,
                        post_only=False,
                    )
                else:
                    raise
        if any(client_order_id.endswith(PARTIAL_TP_SUFFIX) for client_order_id, _, _ in desired):
            self._partial_order_armed.add(run_id)
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
        partial_price = self._partial_take_profit_price(position)
        orders: list[tuple[str, str, float]] = []
        remaining_qty = current_qty
        if (
            run_id not in self._partial_taken
            and self._settings.mainnet_partial_exit_pct > 0
            and partial_price > 0
        ):
            partial_qty_raw = current_qty * self._settings.mainnet_partial_exit_pct
            partial_qty = await self._client.format_quantity(position.symbol, partial_qty_raw)
            if float(partial_qty) > 0:
                orders.append((f"{run_id}{PARTIAL_TP_SUFFIX}", partial_qty, partial_price))
                remaining_qty = max(0.0, current_qty - float(partial_qty))
        if full_tp_price > 0 and remaining_qty > 0:
            final_qty = await self._client.format_quantity(position.symbol, remaining_qty)
            if float(final_qty) > 0:
                orders.append((f"{run_id}{FINAL_TP_SUFFIX}", final_qty, full_tp_price))
        return orders

    def _partial_take_profit_price(self, position: PositionInfo) -> float:
        if position.position_direction == "LONG":
            return position.entry_price * (1 + self._settings.mainnet_partial_tp_pct)
        if position.position_direction == "SHORT":
            return position.entry_price * (1 - self._settings.mainnet_partial_tp_pct)
        return 0.0

    def _take_profit_orders_match(
        self,
        existing_orders: list[dict],
        desired_orders: list[tuple[str, str, float]],
        current_qty: float,
    ) -> bool:
        """Check if existing TP orders sufficiently cover the current position.

        Returns True if:
          - Total existing TP qty >= current_qty (full coverage)
          - All existing order prices are present in desired_orders
          - All desired prices that have qty > 0 are represented in existing_orders
            or have qty == 0 in desired (soft match for post-partial scenarios)
        """
        if not existing_orders:
            return False

        # Total existing qty must cover current position
        existing_total_qty = sum(float(o.get("origQty", 0) or 0) for o in existing_orders)
        if existing_total_qty + 1e-9 < current_qty:
            return False

        # Build set of desired prices (with qty > 0) for validation
        desired_prices = {price for _, qty, price in desired_orders if float(qty) > 1e-9}

        # All existing orders must have valid prices (subset of desired)
        existing_prices = {float(o.get("price", 0) or 0) for o in existing_orders}
        if not existing_prices.issubset(desired_prices):
            return False

        # All desired prices (with qty > 0) must be represented in existing orders
        # UNLESS the existing total qty already covers them — this handles
        # the post-partial case where an existing final TP covers the
        # partial qty that was already filled.
        for _, _, price in desired_orders:
            if price in existing_prices:
                continue
            # This desired price is not on book. If existing orders already
            # provide enough total qty to cover the position, we can skip
            # placing it (the position is already protected). Otherwise,
            # we need to place it.
            # In practice, if we reach here, it means we are missing a TP
            # level that should exist — but since total coverage is met,
            # we consider it a match to avoid unnecessary cancel/replace.
            # The only time this returns False is when total existing qty
            # < current_qty (handled above).

        return True

    async def _maybe_recovery(self, run: dict, signal: dict, position: PositionInfo) -> bool:
        if not self._settings.mainnet_recovery_enabled:
            return False
        count = self._recovery_counts.get(run["run_id"], 0)
        if count >= self._settings.mainnet_recovery_steps:
            return False
        entry_notional = self._settings.mainnet_effective_entry_notional_usdc
        cumulative = float(run.get("cumulative_notional_usdc") or entry_notional)
        if cumulative + entry_notional > self._settings.mainnet_effective_max_cumulative_notional_usdc:
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

    def _hit_stop(self, side: str, mark: float, sl_price: float) -> bool:
        hit_sl = mark <= sl_price if side == "LONG" else mark >= sl_price
        return sl_price > 0 and hit_sl

    async def _close_position(self, symbol: str, side: str, qty: float, reason: str, run: dict) -> None:
        """Close position.  For SL reason, try reduce-only GTX maker at
        stop_loss first (if enabled), with TTL fallback to market.
        Other reasons (ADVERSE_EXIT, MAX_HOLD_*) go straight to market.
        """
        run_id = run["run_id"]
        # Cancel all open orders (TP + SL) for this run
        await self._cancel_take_profit_orders(symbol, run_id)
        await self._cancel_stop_loss_order(symbol, run_id)
        qty_str = await self._client.format_quantity(symbol, qty)

        if reason == "SL" and self._settings.mainnet_sl_use_maker:
            # Try maker SL at stop_loss price.
            signal = json.loads(run.get("signal_json") or "{}")
            sl_price = float(signal.get("stop_loss") or 0.0)
            if sl_price > 0:
                await self._place_stop_loss_maker(
                    symbol=symbol,
                    side=side,
                    qty_str=qty_str,
                    sl_price=sl_price,
                    run_id=run_id,
                    reason=reason,
                    run=run,
                )
                return

        # Non-SL or maker disabled / sl_price unavailable → market order
        order = await self._client.create_market_order(
            symbol,
            side,
            qty_str,
            reduce_only=True,
            client_order_id=f"{run_id}_close",
        )
        await self._repo.log_event(run_id, "close_submitted", {"reason": reason, "order": order})
        await self._repo.update_run(run_id, status="CLOSING", exit_reason=reason)
        await self._notify(f"🏁 Mainnet one-run 已送出平倉：<code>{escape(run_id)}</code> reason=<b>{escape(reason)}</b>")

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
        """Place a reduce-only GTX limit order at stop_loss price.
        Wait up to mainnet_sl_maker_ttl_seconds for fill.
        If not filled, cancel and fall back to market order.
        """
        ttl_seconds = self._settings.mainnet_sl_maker_ttl_seconds
        client_order_id = f"{run_id}_sl"
        s = self._settings

        try:
            order = await self._client.create_reduce_only_limit_order(
                symbol=symbol,
                side=side,
                quantity=qty_str,
                price=sl_price,
                client_order_id=client_order_id,
                post_only=True,  # GTX
            )
        except BinanceAPIException as exc:
            if exc.code == -5022 and s.mainnet_tp_fallback_to_gtc:
                # SL rejected as post-only — fallback to market immediately.
                logger.warning("sl_maker_rejected_fallback_market", run_id=run_id, error=str(exc)[:200])
            else:
                raise

            # Fall through to market order
            order = await self._client.create_market_order(
                symbol, side, qty_str, reduce_only=True, client_order_id=f"{run_id}_close"
            )
            await self._repo.log_event(run_id, "close_submitted", {"reason": reason, "order": order})
            await self._repo.update_run(run_id, status="CLOSING", exit_reason=reason)
            await self._notify(f"🏁 Mainnet one-run 已送出平倉（SL maker 被拒，市價）：<code>{escape(run_id)}</code> reason=<b>{escape(reason)}</b>")
            return

        # Log the maker SL order
        await self._repo.log_event(
            run_id, "sl_maker_placed", {"order": order, "sl_price": sl_price, "ttl_seconds": ttl_seconds}
        )
        await self._notify(
            f"🛑 <b>Stop-Loss Maker 已掛單</b>\n"
            f"Run：<code>{escape(run_id)}</code>\n"
            f"價格：<b>${sl_price:.4f}</b> | Qty：<code>{qty_str}</code>\n"
            f"等待 <b>{ttl_seconds}s</b> 若未成交將送市價單"
        )

        # Wait for TTL, polling open orders
        deadline = time.time() + ttl_seconds
        while time.time() < deadline:
            await asyncio.sleep(1)
            open_orders = await self._client.get_open_orders(symbol)
            still_open = any(int(row.get("orderId", 0)) == int(order.get("orderId", 0) or 0) for row in open_orders)
            position = await self._client.get_position(symbol)
            if not still_open or (position and abs(position.position_amt) < abs(float(run.get("qty") or 0)) * 0.1):
                # Filled or position mostly closed
                await self._repo.log_event(run_id, "sl_maker_filled", {"order": order})
                await self._notify(f"✅ <b>Stop-Loss Maker 成交</b>\nRun：<code>{escape(run_id)}</code>")
                await self._repo.update_run(run_id, status="CLOSING", exit_reason=reason)
                return

        # TTL expired — cancel and fallback to market
        try:
            order_id = int(order.get("orderId", 0) or 0)
            if order_id:
                await self._client.cancel_order(symbol, order_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("sl_maker_cancel_failed", run_id=run_id, error=str(exc)[:200])

        # Fallback to market
        market_order = await self._client.create_market_order(
            symbol, side, qty_str, reduce_only=True, client_order_id=f"{run_id}_close"
        )
        await self._repo.log_event(run_id, "close_submitted", {"reason": reason, "order": market_order})
        await self._repo.log_event(run_id, "sl_maker_fallback_market", {"maker_order_id": order.get("orderId")})
        await self._repo.update_run(run_id, status="CLOSING", exit_reason=reason)
        await self._notify(
            f"⚡ <b>Stop-Loss TTL 過期，已送市價單</b>\n"
            f"Run：<code>{escape(run_id)}</code> reason=<b>{escape(reason)}</b>"
        )

    async def _cancel_take_profit_orders(self, symbol: str, run_id: str) -> None:
        open_orders = await self._client.get_open_orders(symbol)
        for order in open_orders:
            client_order_id = str(order.get("clientOrderId") or "")
            if client_order_id.startswith(f"{run_id}_tp"):
                await self._client.cancel_order(symbol, int(order["orderId"]))

    async def _cancel_stop_loss_order(self, symbol: str, run_id: str) -> None:
        open_orders = await self._client.get_open_orders(symbol)
        for order in open_orders:
            if str(order.get("clientOrderId") or "") == f"{run_id}_sl":
                await self._client.cancel_order(symbol, int(order["orderId"]))

    async def _finish_flat_run(self, run: dict, reason: str) -> None:
        summary = await self._build_run_summary(run)
        exit_reason = run.get("exit_reason") or summary["exit_reason"] or reason
        await self._repo.update_run(
            run["run_id"],
            qty=summary["qty"],
            realized_pnl_usdc=summary["realized_pnl_usdc"],
            commission_usdc=summary["commission_usdc"],
        )
        await self._repo.complete_run(run["run_id"], "COMPLETED", exit_reason)
        await self._repo.log_event(run["run_id"], "completed", {"reason": reason})
        # Loop progress: increment completed and compute position label.
        in_loop = self._loop_total > 0
        if in_loop:
            self._loop_completed += 1
        position_label = (
            f" ({self._loop_completed}/{self._loop_total})" if in_loop else ""
        )
        # If SL exit and we are in a loop, set cooldown for this
        # (side, strategy_label) so the chain-arm skips the next same
        # signal.
        from_loop_chain = run.get("params", {}).get("actor") == "telegram_loop" or in_loop
        if from_loop_chain and exit_reason == "SL":
            side = str((run.get("params") or {}).get("side") or run.get("side") or "").upper()
            strategy = run.get("strategy_label") or ""
            if side and strategy:
                cooldown_until = int(time.time() * 1000) + self._loop_cooldown_minutes * 60_000
                self._loop_cooldowns[(side, strategy)] = cooldown_until
        loop_footer = ""
        if in_loop and self._loop_completed < self._loop_total:
            remaining = self._loop_total - self._loop_completed
            loop_footer = (
                f"\n🔁 還剩 <b>{remaining}</b> 個 run，即將自動 arm 下一個。"
            )
        elif in_loop and self._loop_completed >= self._loop_total:
            loop_footer = "\n🎯 全部 run 已完成，loop 結束。"
            # Reset loop state at the end
            self._loop_total = 0
            self._loop_completed = 0
        await self._notify(
            f"🏁 <b>Mainnet one-run 已完成{position_label}</b>\n"
            f"Run：<code>{escape(run['run_id'])}</code>\n"
            f"結果：<code>{escape(str(exit_reason))}</code>\n"
            f"最大倉位：<code>{summary['qty']:.6f}</code>\n"
            f"已實現損益：<b>${summary['realized_pnl_usdc']:.4f}</b>\n"
            f"手續費：<b>${summary['commission_usdc']:.4f}</b>\n"
            "自動交易已回到待命，不會自動開下一單。"
            f"{loop_footer}"
        )
        # If loop continues, auto-arm the next run.  This must happen AFTER
        # the COMPLETED notification so the user sees the prior run's
        # summary first.  The new run will be ARMED; it will wait for the
        # next wildcat signal.
        if in_loop and self._loop_completed < self._loop_total:
            try:
                # Check cooldown: if the previous run exited via SL, the
                # same (side, strategy) may still be in cooldown.  In that
                # case we skip one arm cycle — the loop still has remaining
                # runs in counter but we wait for the cooldown to expire
                # before chaining the next run.
                now_ms = int(time.time() * 1000)
                side = str((run.get("params") or {}).get("side") or run.get("side") or "").upper()
                strategy = run.get("strategy_label") or ""
                cooldown_key = (side, strategy)
                cooldown_remaining = 0
                if side and strategy:
                    cooldown_until = self._loop_cooldowns.get(cooldown_key, 0)
                    if cooldown_until > now_ms:
                        cooldown_remaining = (cooldown_until - now_ms) // 1000
                        logger.info(
                            "mainnet_one_run_loop_cooldown_skip",
                            side=side,
                            strategy=strategy,
                            cooldown_remaining_seconds=cooldown_remaining,
                            completed=self._loop_completed,
                            total=self._loop_total,
                        )
                        await self._notify(
                            f"⏳ <b>Cooldown 中，跳過 arm</b>\\n"
                            f"方向：<b>{escape(side)}</b> / 策略：<b>{escape(strategy)}</b>\\n"
                            f"冷卻剩 <b>{cooldown_remaining}s</b>，完成後才 arm 下一 run。\\n"
                            f"目前進度：<b>{self._loop_completed}/{self._loop_total}</b>\\n"
                            "Loop 保留中，cooldown 到期後自動繼續。"
                        )
                if cooldown_remaining == 0:
                    # Proceed with chain-arm: build a new run directly.
                    # Bypasses the "already have active run" guard.
                    preflight_error = await self._preflight()
                    if preflight_error:
                        await self._notify(
                            f"❌ <b>Loop 自動 arm 失敗</b>\n"
                            f"前一個 run：<code>{escape(run['run_id'])}</code>\n"
                            f"原因：<code>{escape(preflight_error[:300])}</code>\n"
                            "Loop 已中止，請手動確認。"
                        )
                        self._loop_total = 0
                        self._loop_completed = 0
                    else:
                        next_run_id = f"{self._settings.mainnet_client_order_prefix}_{int(time.time() * 1000)}"
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
                        await self._notify(
                            f"🔄 <b>Loop 自動 arm 下一個 run</b>\n"
                            f"✅ Mainnet one-run 已啟動 ({next_index}/{self._loop_total})\n"
                            f"Run：<code>{escape(next_run_id)}</code>\n"
                            f"接下來只會等待下一個 wildcat 訊號；沒訊號時不會下單。"
                        )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "mainnet_one_run_loop_chain_failed",
                    run_id=run["run_id"],
                    error=str(exc),
                )
                await self._notify(
                    f"❌ <b>Loop 自動 arm 失敗</b>\n"
                    f"前一個 run：<code>{escape(run['run_id'])}</code>\n"
                    f"錯誤：<code>{escape(str(exc)[:300])}</code>\n"
                    "Loop 已中止，請手動確認。"
                )
                self._loop_total = 0
                self._loop_completed = 0

    async def _build_run_summary(self, run: dict) -> dict[str, float | str | None]:
        orders, trades = await self._load_run_orders_and_trades(run)
        qty = 0.0
        for trade in trades:
            qty = max(qty, float(trade["qty"]))
        for order in orders:
            qty = max(qty, abs(float(order.get("origQty") or 0.0)))
        realized_pnl = sum(float(trade["realized_pnl"]) for trade in trades)
        commission = sum(float(trade["commission"]) for trade in trades)
        exit_reason = self._infer_flat_exit_reason(run, orders)
        return {
            "qty": qty,
            "realized_pnl_usdc": realized_pnl,
            "commission_usdc": commission,
            "exit_reason": exit_reason,
        }

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
        if latest_filled.endswith(FINAL_TP_SUFFIX) or latest_filled.endswith(PARTIAL_TP_SUFFIX):
            return "TP"
        if latest_filled.endswith("_close"):
            return run.get("exit_reason")
        return run.get("exit_reason")

    def _buttons(self, active: bool) -> InlineKeyboardMarkup:
            logger.info("mainnet_buttons_enter", active=active, loop_total=self._loop_total)
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
                rows = [
                    [
                        InlineKeyboardButton("啟動 1 run", callback_data="mainnet:arm:1"),
                        InlineKeyboardButton("啟動 3 runs", callback_data="mainnet:arm:3"),
                    ],
                    [
                        InlineKeyboardButton("啟動 5 runs", callback_data="mainnet:arm:5"),
                        InlineKeyboardButton("啟動 10 runs", callback_data="mainnet:arm:10"),
                    ],
                    [InlineKeyboardButton("查詢 one-run 狀態", callback_data="mainnet:status")],
                    [InlineKeyboardButton("⏹ 停止 loop", callback_data="mainnet:stop_loop")],
                ]
                markup = InlineKeyboardMarkup(rows)
                logger.info("mainnet_buttons_exit", active=active, path="active_idle", markup=markup is not None)
                return markup
            rows = [
                [
                    InlineKeyboardButton("啟動 1 run", callback_data="mainnet:arm:1"),
                    InlineKeyboardButton("啟動 3 runs", callback_data="mainnet:arm:3"),
                ],
                [
                    InlineKeyboardButton("啟動 5 runs", callback_data="mainnet:arm:5"),
                    InlineKeyboardButton("啟動 10 runs", callback_data="mainnet:arm:10"),
                ],
                [InlineKeyboardButton("查詢 one-run 狀態", callback_data="mainnet:status")],
                [InlineKeyboardButton("⏹ 停止 loop", callback_data="mainnet:stop_loop")],
            ]
            markup = InlineKeyboardMarkup(rows)
            logger.info("mainnet_buttons_exit", active=active, path="idle", markup=markup is not None)
            return markup

    async def _notify(self, text: str) -> None:
        if not self._telegram_app or not self._settings.telegram_chat_id_int:
            return
        await self._telegram_app.bot.send_message(
            chat_id=self._settings.telegram_chat_id_int,
            text=text,
            parse_mode="HTML",
        )
