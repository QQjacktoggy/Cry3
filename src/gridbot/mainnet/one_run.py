"""Telegram-triggered one-run mainnet validation manager."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from config.settings import Settings
from src.gridbot.binance.client import BinanceFuturesClient
from src.gridbot.binance.models import PositionInfo
from src.gridbot.storage.repositories import MainnetRunRepository
from src.gridbot.strategy.long_pullback import Candle
from src.gridbot.strategy.wildcat_live import WildcatLiveDecision, generate_wildcat_v2_adverse_guard_live_decision
from src.gridbot.utils.logging import get_logger

logger = get_logger(__name__)
PARTIAL_TP_SUFFIX = "_tp1"
FINAL_TP_SUFFIX = "_tp2"


TERMINAL_STATUSES = {"COMPLETED", "ENTRY_EXPIRED", "FAILED", "CANCELLED", "EMERGENCY_CLOSED"}


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
    ) -> None:
        self._settings = settings
        self._client = client
        self._repo = repo
        self._telegram_app = telegram_app
        self._protection_sent: set[str] = set()
        self._partial_taken: set[str] = set()
        self._partial_order_armed: set[str] = set()
        self._recovery_counts: dict[str, int] = {}

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
            f"預估保證金：<b>${entry_margin:.4f} USDC</b>",
            "",
            "訊號仍會照常推送；按下啟動後，只會把下一個符合條件的 wildcat 訊號接成一個自動 run。",
        ]
        if active:
            lines.extend(
                [
                    "",
                    f"目前 active run：<code>{escape(active['run_id'])}</code>",
                    f"狀態：<b>{escape(active['status'])}</b>",
                    f"方向：<b>{escape(str(active.get('side') or '-'))}</b>",
                ]
            )
            return RunStatus("\n".join(lines), self._buttons(active=True))
        if latest:
            lines.extend(
                [
                    "",
                    f"最近 run：<code>{escape(latest['run_id'])}</code>",
                    f"狀態：<b>{escape(latest['status'])}</b>",
                    f"結果：<code>{escape(str(latest.get('exit_reason') or '-'))}</code>",
                ]
            )
        return RunStatus("\n".join(lines), self._buttons(active=False))

    async def arm(self, actor: str = "telegram") -> str:
        if not self._settings.mainnet_one_run_enabled:
            return "❌ Mainnet one-run 尚未啟用。請設定 MAINNET_ONE_RUN_ENABLED=true。"
        if not self._settings.mainnet_api_key or not self._settings.mainnet_api_secret:
            return "❌ 尚未設定 MAINNET_API_KEY / MAINNET_API_SECRET。"
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
        params = {
            "actor": actor,
            "symbol": self._settings.mainnet_symbol,
            "strategy": self._settings.mainnet_strategy_label,
            "equity_cap_usdc": self._settings.mainnet_equity_cap_usdc,
            "initial_notional_usdc": self._settings.mainnet_effective_entry_notional_usdc,
            "max_cumulative_notional_usdc": self._settings.mainnet_effective_max_cumulative_notional_usdc,
            "leverage": self._settings.mainnet_leverage,
            "maker_first": True,
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
        return (
            "✅ <b>Mainnet one-run 已啟動</b>\n"
            f"Run：<code>{escape(run_id)}</code>\n"
            "接下來只會等待下一個 wildcat 訊號；沒訊號時不會下單。"
        )

    async def cancel(self) -> str:
        active = await self._repo.get_active_run()
        if not active:
            return "目前沒有 active mainnet run。"
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
        return f"🛑 已取消 run：<code>{escape(active['run_id'])}</code>。"

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
        run_age_bars = max(0, int((int(time.time() * 1000) - int(run["armed_at_ms"])) / 60_000))

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

    async def _place_entry(self, run: dict, decision: WildcatLiveDecision) -> None:
        await self._ensure_fee_guard(run["symbol"])
        await self._client.set_leverage(run["symbol"], self._settings.mainnet_leverage)
        side = "BUY" if decision.side == "LONG" else "SELL"
        entry_notional = self._settings.mainnet_effective_entry_notional_usdc
        qty = await self._client.format_quantity(
            run["symbol"],
            entry_notional / decision.signal.price,
        )
        price = await self._passive_price(run["symbol"], side, decision.signal.price)
        client_order_id = f"{run['run_id']}_entry"
        order = await self._client.create_post_only_limit_order(
            symbol=run["symbol"],
            side=side,
            quantity=qty,
            price=price,
            client_order_id=client_order_id,
        )
        payload = {
            "side": decision.side,
            "strategy": decision.strategy,
            "price": decision.signal.price,
            "entry_price": float(price),
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
            entry_price=float(price),
            cumulative_notional_usdc=entry_notional,
        )
        await self._repo.log_event(run["run_id"], "entry_placed", {"order": order, "signal": payload})
        await self._notify(
            f"{'🟢' if decision.side == 'LONG' else '🔴'} <b>AUTO {('做多' if decision.side == 'LONG' else '做空')} 已掛 maker 單</b>\n"
            f"Run：<code>{escape(run['run_id'])}</code>\n"
            f"策略：<b>{escape(decision.strategy)}</b> | score=<code>{decision.signal.score}</code>\n"
            f"Entry：<b>${float(price):.4f}</b> | Qty：<code>{escape(str(qty))}</code>\n"
            f"Stop：<b>${float(decision.signal.stop_loss or 0):.4f}</b> | TP：<b>${float(decision.signal.take_profits[0] if decision.signal.take_profits else 0):.4f}</b>\n"
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
        if self._take_profit_orders_match(existing_tp, desired):
            return
        for order in existing_tp:
            await self._client.cancel_order(position.symbol, int(order["orderId"]))
        for client_order_id, qty, price in desired:
            await self._client.create_reduce_only_limit_order(
                position.symbol,
                close_side,
                qty,
                price,
                client_order_id=client_order_id,
                post_only=True,
            )
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
    ) -> bool:
        if len(existing_orders) != len(desired_orders):
            return False
        existing_by_id = {
            str(order.get("clientOrderId") or ""): order
            for order in existing_orders
        }
        for client_order_id, qty, price in desired_orders:
            order = existing_by_id.get(client_order_id)
            if order is None:
                return False
            if abs(float(order.get("origQty") or 0.0) - float(qty)) > 1e-9:
                return False
            if abs(float(order.get("price") or 0.0) - float(price)) > 1e-6:
                return False
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
        price = await self._passive_price(position.symbol, side, position.mark_price)
        order = await self._client.create_post_only_limit_order(
            position.symbol,
            side,
            qty,
            float(price),
            client_order_id=f"{run['run_id']}_dca{count + 1}",
        )
        self._recovery_counts[run["run_id"]] = count + 1
        await self._repo.update_run(run["run_id"], cumulative_notional_usdc=cumulative + entry_notional)
        await self._repo.log_event(run["run_id"], "recovery_entry_placed", {"order": order, "signal": signal})
        await self._notify(f"🧩 Mainnet one-run 已掛 DCA maker 單 #{count + 1}：<code>{escape(run['run_id'])}</code>")
        return True

    def _hit_stop(self, side: str, mark: float, sl_price: float) -> bool:
        hit_sl = mark <= sl_price if side == "LONG" else mark >= sl_price
        return sl_price > 0 and hit_sl

    async def _close_position(self, symbol: str, side: str, qty: float, reason: str, run: dict) -> None:
        await self._cancel_take_profit_orders(symbol, run["run_id"])
        qty_str = await self._client.format_quantity(symbol, qty)
        order = await self._client.create_market_order(
            symbol,
            side,
            qty_str,
            reduce_only=True,
            client_order_id=f"{run['run_id']}_close",
        )
        await self._repo.log_event(run["run_id"], "close_submitted", {"reason": reason, "order": order})
        await self._repo.update_run(run["run_id"], status="CLOSING", exit_reason=reason)
        await self._notify(f"🏁 Mainnet one-run 已送出平倉：<code>{escape(run['run_id'])}</code> reason=<b>{escape(reason)}</b>")

    async def _cancel_take_profit_orders(self, symbol: str, run_id: str) -> None:
        open_orders = await self._client.get_open_orders(symbol)
        for order in open_orders:
            client_order_id = str(order.get("clientOrderId") or "")
            if client_order_id.startswith(f"{run_id}_tp"):
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
        await self._notify(
            "🏁 <b>Mainnet one-run 已完成</b>\n"
            f"Run：<code>{escape(run['run_id'])}</code>\n"
            f"結果：<code>{escape(str(exit_reason))}</code>\n"
            f"最大倉位：<code>{summary['qty']:.6f}</code>\n"
            f"已實現損益：<b>${summary['realized_pnl_usdc']:.4f}</b>\n"
            f"手續費：<b>${summary['commission_usdc']:.4f}</b>\n"
            "自動交易已回到待命，不會自動開下一單。"
        )

    async def _build_run_summary(self, run: dict) -> dict[str, float | str | None]:
        orders, trades = await self._load_run_orders_and_trades(run)
        qty = 0.0
        for trade in trades:
            qty = max(qty, float(trade.qty))
        for order in orders:
            qty = max(qty, abs(float(order.get("origQty") or 0.0)))
        realized_pnl = sum(float(trade.realized_pnl) for trade in trades)
        commission = sum(float(trade.commission) for trade in trades)
        exit_reason = self._infer_flat_exit_reason(run, orders)
        return {
            "qty": qty,
            "realized_pnl_usdc": realized_pnl,
            "commission_usdc": commission,
            "exit_reason": exit_reason,
        }

    async def _load_run_orders_and_trades(self, run: dict) -> tuple[list[dict], list]:
        start_time = max(0, int(run.get("armed_at_ms") or 0) - 60_000)
        orders = await self._client.get_all_orders(run["symbol"], start_time=start_time, limit=1000)
        matching_orders = [
            order for order in orders
            if str(order.get("clientOrderId") or "").startswith(run["run_id"])
        ]
        order_ids = {int(order.get("orderId", 0) or 0) for order in matching_orders}
        trades = await self._client.get_user_trades(run["symbol"], start_time=start_time, limit=1000)
        matching_trades = [trade for trade in trades if int(trade.order_id) in order_ids]
        return matching_orders, matching_trades

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
        if active:
            return InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("查詢 one-run 狀態", callback_data="mainnet:status")],
                    [InlineKeyboardButton("取消目前 one-run", callback_data="mainnet:cancel")],
                ]
            )
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("啟動 mainnet one-run", callback_data="mainnet:arm")],
                [InlineKeyboardButton("查詢 one-run 狀態", callback_data="mainnet:status")],
            ]
        )

    async def _notify(self, text: str) -> None:
        if not self._telegram_app or not self._settings.telegram_chat_id_int:
            return
        await self._telegram_app.bot.send_message(
            chat_id=self._settings.telegram_chat_id_int,
            text=text,
            parse_mode="HTML",
        )
