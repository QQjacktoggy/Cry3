"""Strategy-driven Binance Futures testnet execution loop."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape

from config.settings import Settings
from src.gridbot.binance.client import BinanceFuturesClient
from src.gridbot.binance.models import IncomeRecord, PositionInfo
from src.gridbot.strategy.long_ntrend import NTrendConfig, generate_ntrend_signal
from src.gridbot.strategy.long_pullback import Candle, SignalPlan, StrategyConfig
from src.gridbot.testnet.trader import TestnetOrderResult, TestnetTrader
from src.gridbot.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class ActivePlan:
    symbol: str
    opened_at_ms: int
    entry_price: float
    stop_loss: float
    take_profit: float
    score: int
    reasons: list[str]


class TestnetAutoTrader:
    """Run the live testnet signal loop and emit Telegram event reports."""

    def __init__(
        self,
        settings: Settings,
        client: BinanceFuturesClient,
        telegram_app=None,
    ) -> None:
        self._settings = settings
        self._client = client
        self._trader = TestnetTrader(settings, client)
        self._telegram_app = telegram_app
        self._plans: dict[str, ActivePlan] = {}
        self._notified_unmanaged: set[str] = set()
        self._target_stop_notified = False
        self._loss_stop_notified = False

    async def run_cycle(self) -> None:
        if self._settings.trading_mode != "testnet_live":
            logger.info("testnet_auto_trade_skipped", mode=self._settings.trading_mode)
            return
        if not self._settings.binance_testnet:
            raise RuntimeError("Refusing auto trading unless BINANCE_TESTNET=true.")

        for symbol in self._settings.symbols_list:
            await self._run_symbol(symbol)

    async def _run_symbol(self, symbol: str) -> None:
        today_net = await self._today_net_pnl(symbol)
        loss_stop = -self._settings.testnet_equity_usdc * self._settings.max_daily_loss_pct / 100
        target_stop = self._settings.testnet_equity_usdc * self._settings.testnet_daily_target_pct / 100
        position = await self._client.get_position(symbol)

        if today_net <= loss_stop:
            if position:
                result = await self._trader.close_position(symbol)
                if result:
                    await self._notify_order("daily loss stop", result, position, today_net=today_net)
            if not self._loss_stop_notified:
                self._loss_stop_notified = True
                await self._notify_text(
                    "🛑 <b>Testnet auto trade paused</b>\n"
                    f"Reason: daily loss stop\nSymbol: <code>{escape(symbol)}</code>\n"
                    f"Today net: ${today_net:.4f} / stop ${loss_stop:.4f}"
                )
            return

        if position:
            await self._manage_position(symbol, position, today_net)
            return

        if today_net >= target_stop:
            if not self._target_stop_notified:
                self._target_stop_notified = True
                await self._notify_text(
                    "🎯 <b>Daily target reached</b>\n"
                    f"Symbol: <code>{escape(symbol)}</code>\n"
                    f"Today net: ${today_net:.4f} / target ${target_stop:.4f}\n"
                    "No new position will be opened today."
                )
            return

        candles = await self._load_candles(symbol)
        signal = generate_ntrend_signal(candles, self._ntrend_config(symbol))
        if signal.action != "PLAN_LONG" or signal.score < self._settings.testnet_min_signal_score:
            logger.info(
                "testnet_signal_wait",
                symbol=symbol,
                action=signal.action,
                score=signal.score,
                reasons=signal.reasons[:3],
            )
            return

        notional = self._bounded_signal_notional(signal)
        leverage = self._bounded_signal_leverage(signal)
        result = await self._trader.open_position(symbol, "long", notional, leverage=leverage)
        self._plans[symbol] = ActivePlan(
            symbol=symbol,
            opened_at_ms=candles[-1].open_time_ms,
            entry_price=signal.entries[0] if signal.entries else signal.price,
            stop_loss=signal.stop_loss or signal.price * 0.985,
            take_profit=signal.take_profits[0] if signal.take_profits else signal.price * 1.01,
            score=signal.score,
            reasons=signal.reasons,
        )
        await self._notify_entry(result, signal)

    async def _manage_position(self, symbol: str, position: PositionInfo, today_net: float) -> None:
        plan = self._plans.get(symbol)
        if plan is None:
            if symbol not in self._notified_unmanaged:
                self._notified_unmanaged.add(symbol)
                await self._notify_text(
                    "ℹ️ <b>Existing testnet position detected</b>\n"
                    f"Symbol: <code>{escape(symbol)}</code>\n"
                    f"Position: {position.position_direction} {abs(position.position_amt):.6f}\n"
                    "No local strategy plan is attached after restart, so auto close is skipped."
                )
            return

        price = position.mark_price
        reason = None
        if price <= plan.stop_loss:
            reason = "strategy stop loss"
        elif price >= plan.take_profit:
            reason = "strategy take profit"

        if reason is None:
            logger.info(
                "testnet_position_held",
                symbol=symbol,
                mark=price,
                stop=plan.stop_loss,
                take_profit=plan.take_profit,
                today_net=today_net,
            )
            return

        result = await self._trader.close_position(symbol)
        if result:
            self._plans.pop(symbol, None)
            await self._notify_order(reason, result, position, today_net=today_net)

    async def _load_candles(self, symbol: str) -> list[Candle]:
        rows = await self._client.get_klines(
            symbol=symbol,
            interval=self._settings.testnet_kline_interval,
            limit=self._settings.testnet_kline_limit,
        )
        return [Candle.from_binance_kline(row) for row in rows]

    def _ntrend_config(self, symbol: str) -> NTrendConfig:
        base = StrategyConfig(
            symbol=symbol,
            equity_usdc=self._settings.testnet_equity_usdc,
            daily_target_min_pct=self._settings.testnet_daily_target_pct,
            daily_target_max_pct=self._settings.testnet_daily_target_pct,
            max_effective_leverage=self._settings.max_effective_leverage,
            daily_soft_loss_pct=self._settings.daily_soft_loss_pct,
            daily_max_loss_pct=self._settings.max_daily_loss_pct,
            risk_per_trade_pct=0.9,
            min_score=self._settings.testnet_min_signal_score,
            max_position_margin_pct=self._settings.testnet_max_position_margin_pct,
            maker_fee_rate=0.0,
            taker_fee_rate=0.0004,
        )
        return NTrendConfig(base=base)

    def _bounded_signal_notional(self, signal: SignalPlan) -> float:
        leverage = self._bounded_signal_leverage(signal)
        margin_cap = self._settings.testnet_equity_usdc * self._settings.testnet_max_position_margin_pct / 100
        notional_cap = margin_cap * leverage
        requested = signal.planned_notional_usdc or self._settings.testnet_order_notional_usdc
        return max(5.0, min(requested, notional_cap, self._settings.testnet_max_order_notional_usdc))

    def _bounded_signal_leverage(self, signal: SignalPlan) -> int:
        leverage = signal.leverage_cap or self._settings.testnet_order_leverage
        return max(1, min(int(leverage), int(self._settings.max_effective_leverage)))

    async def _today_net_pnl(self, symbol: str) -> float:
        day_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        day_start_ms = int(day_start.timestamp() * 1000)
        records: list[IncomeRecord] = []
        for income_type in ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE"):
            records.extend(
                await self._client.get_income_history(
                    income_type=income_type,
                    symbol=symbol,
                    start_time=day_start_ms,
                    limit=100,
                )
            )
        return sum(item.income for item in records)

    async def _notify_entry(self, result: TestnetOrderResult, signal: SignalPlan) -> None:
        reasons = "\n".join(f"- {escape(reason)}" for reason in signal.reasons[:4])
        entry = signal.entries[0] if signal.entries else signal.price
        stop = signal.stop_loss if signal.stop_loss is not None else 0.0
        take_profit = signal.take_profits[0] if signal.take_profits else 0.0
        await self._notify_text(
            "🚀 <b>Testnet auto entry</b>\n"
            f"Symbol: <code>{escape(result.symbol)}</code>\n"
            f"Side: <b>{result.side}</b>\n"
            f"Qty: <code>{escape(result.quantity)}</code>\n"
            f"Notional: ${result.notional_usdc:.2f} | Leverage: {result.leverage}x\n"
            f"Signal score: {signal.score} | confidence: {signal.confidence}\n"
            f"Entry ref: ${entry:.4f}\n"
            f"Stop: ${stop:.4f} | TP1: ${take_profit:.4f}\n"
            f"Order ID: <code>{escape(str(result.order.get('orderId', 'N/A')))}</code>\n"
            f"{reasons}"
        )

    async def _notify_order(
        self,
        reason: str,
        result: TestnetOrderResult,
        position: PositionInfo,
        today_net: float,
    ) -> None:
        await self._notify_text(
            "🏁 <b>Testnet auto exit</b>\n"
            f"Reason: {escape(reason)}\n"
            f"Symbol: <code>{escape(result.symbol)}</code>\n"
            f"Side: <b>{result.side}</b>\n"
            f"Qty: <code>{escape(result.quantity)}</code>\n"
            f"Mark: ${position.mark_price:.4f} | U-PnL: ${position.unrealized_pnl:.4f}\n"
            f"Today net before close: ${today_net:.4f}\n"
            f"Order ID: <code>{escape(str(result.order.get('orderId', 'N/A')))}</code>"
        )

    async def _notify_text(self, text: str) -> None:
        if not self._telegram_app or not self._settings.telegram_chat_id_int:
            logger.info("telegram_trade_notice_skipped", msg=text[:80])
            return
        await self._telegram_app.bot.send_message(
            chat_id=self._settings.telegram_chat_id_int,
            text=text,
            parse_mode="HTML",
        )
