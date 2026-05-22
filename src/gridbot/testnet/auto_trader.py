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
from src.gridbot.strategy.signal_journal import generate_router_allocator_v13_trend350_live_decision
from src.gridbot.testnet.trader import TestnetOrderResult, TestnetTrader
from src.gridbot.utils.logging import get_logger

logger = get_logger(__name__)
STOP_ORDER_PREFIX = "cry3sl_"
TAKE_PROFIT_ORDER_PREFIX = "cry3tp_"

SIDE_LABELS = {
    "BUY": "買入 / 回補",
    "SELL": "賣出 / 放空",
    "long": "做多",
    "short": "做空",
    "LONG": "做多",
    "SHORT": "做空",
}

REGIME_LABELS = {
    "trend_up": "上升趨勢",
    "trend_down": "下降趨勢",
    "high_volatility": "高波動",
    "range": "區間盤整",
    "chop": "震盪雜訊",
    "low_liquidity": "低流動性",
    "blocked": "已封鎖",
    "ntrend": "N 字趨勢",
}

RISK_MODE_LABELS = {
    "aggressive": "積極",
    "normal": "正常",
    "small": "縮倉",
    "off": "保守/停用",
    "blocked": "已封鎖",
    "ntrend": "N 字模式",
}

PLAYBOOK_LABELS = {
    "breakout": "突破延續",
    "breakdown": "跌破延續",
    "no_trade": "觀望",
    "vwap_reversion": "VWAP 均值回歸",
    "blocked": "已封鎖",
    "ntrend": "N 字劇本",
}

ALLOCATOR_PROFILE_LABELS = {
    "trend_aggressive": "趨勢強攻",
    "trend_normal": "趨勢標準",
    "short_breakdown": "空方跌破",
    "short": "空方標準",
    "ntrend": "N 字配置",
    "blocked": "已封鎖",
    "base": "基礎配置",
}

ALLOCATOR_STATE_LABELS = {
    "active": "啟用",
    "normal": "正常",
    "base": "基礎",
    "blocked": "已封鎖",
}

REASON_LABELS = {
    "daily loss stop": "觸發單日虧損上限",
    "strategy max holding": "達到最長持有時間",
    "strategy stop loss": "觸發策略停損",
    "strategy take profit": "觸發策略停利",
    "entry exit level breached": "開倉後已觸發策略出場線",
}


@dataclass
class ActivePlan:
    symbol: str
    side: str
    strategy: str
    regime: str
    risk_mode: str
    market_playbook: str
    allocator_state: str
    allocator_profile: str
    allocator_scale: float
    opened_at_ms: int
    entry_price: float
    stop_loss: float
    take_profit: float
    max_holding_bars: int
    score: int
    reasons: list[str]


@dataclass(frozen=True)
class LiveDecisionContext:
    signal: SignalPlan
    strategy: str
    regime: str
    risk_mode: str
    market_playbook: str
    allocator_state: str
    allocator_profile: str
    allocator_scale: float
    max_holding_bars: int


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
        await self.run_manage_cycle()
        await self.run_entry_cycle()

    async def run_entry_cycle(self) -> None:
        if self._settings.trading_mode != "testnet_live":
            logger.info("testnet_auto_trade_skipped", mode=self._settings.trading_mode)
            return
        if not self._settings.binance_testnet:
            raise RuntimeError("Refusing auto trading unless BINANCE_TESTNET=true.")

        for symbol in self._settings.symbols_list:
            await self._run_symbol(symbol, allow_new_entries=True)

    async def run_manage_cycle(self) -> None:
        if self._settings.trading_mode != "testnet_live":
            return
        if not self._settings.binance_testnet:
            raise RuntimeError("Refusing auto trading unless BINANCE_TESTNET=true.")
        for symbol in self._settings.symbols_list:
            await self._run_symbol(symbol, allow_new_entries=False)

    async def _run_symbol(self, symbol: str, allow_new_entries: bool) -> None:
        today_net = await self._today_net_pnl(symbol)
        loss_stop = -self._settings.testnet_equity_usdc * self._settings.max_daily_loss_pct / 100
        target_stop = self._settings.testnet_equity_usdc * self._settings.testnet_daily_target_pct / 100
        position = await self._client.get_position(symbol)

        if today_net <= loss_stop:
            if position:
                plan = self._plans.get(symbol)
                if plan is None:
                    plan = await self._recover_plan(symbol, position, today_net)
                result = await self._trader.close_position(symbol)
                if result:
                    await self._cleanup_stale_protection_orders(symbol)
                    self._plans.pop(symbol, None)
                    if plan is not None:
                        await self._notify_order("daily loss stop", result, position, plan, today_net=today_net)
                    else:
                        await self._notify_text(
                            "🏁 <b>Testnet 自動平倉</b>\n"
                            "原因：觸發單日虧損上限\n"
                            f"交易對：<code>{escape(result.symbol)}</code>\n"
                            f"方向：<b>{escape(_label_side(result.side))}</b>\n"
                            "策略：無法復原既有 plan\n"
                            f"標記價：${position.mark_price:.4f} | 未實現損益：${position.unrealized_pnl:.4f}\n"
                            f"平倉前今日淨損益：${today_net:.4f}\n"
                            f"訂單 ID：<code>{escape(str(result.order.get('orderId', 'N/A')))}</code>"
                        )
            if not self._loss_stop_notified:
                self._loss_stop_notified = True
                await self._notify_text(
                    "🛑 <b>Testnet 自動交易已暫停</b>\n"
                    f"原因：觸發單日虧損上限\n交易對：<code>{escape(symbol)}</code>\n"
                    f"今日淨損益：${today_net:.4f} / 停止線：${loss_stop:.4f}"
                )
            return

        if position:
            if symbol not in self._plans:
                recovered = await self._recover_plan(symbol, position, today_net)
                if recovered is not None:
                    self._plans[symbol] = recovered
                    self._notified_unmanaged.discard(symbol)
                    await self._sync_protection_orders(symbol, recovered, quantity=self._position_quantity(position))
                    await self._notify_text(
                        "🔁 <b>已重新接管既有持倉</b>\n"
                        f"交易對：<code>{escape(symbol)}</code>\n"
                        f"持倉：{_label_side(position.position_direction)} {abs(position.position_amt):.6f}\n"
                        f"策略：<b>{escape(recovered.strategy)}</b>\n"
                        f"趨勢判定：<b>{escape(_label_regime(recovered.regime))}</b> | 風險模式：<b>{escape(_label_risk_mode(recovered.risk_mode))}</b>\n"
                        f"停損：${recovered.stop_loss:.4f} | TP1：${recovered.take_profit:.4f}"
                    )
                else:
                    await self._close_unmanaged_position(symbol, position, today_net)
                    return
            await self._manage_position(symbol, position, today_net)
            return

        plan = self._plans.pop(symbol, None)
        if plan is not None:
            exit_order = await self._recent_exit_order(symbol, plan)
            await self._cleanup_stale_protection_orders(symbol)
            await self._notify_exchange_exit(symbol, plan, exit_order, today_net=today_net)
        else:
            await self._cleanup_stale_protection_orders(symbol)

        if not allow_new_entries:
            return

        if today_net >= target_stop:
            if not self._target_stop_notified:
                self._target_stop_notified = True
                await self._notify_text(
                    "🎯 <b>已達成今日目標</b>\n"
                    f"交易對：<code>{escape(symbol)}</code>\n"
                    f"今日淨損益：${today_net:.4f} / 目標：${target_stop:.4f}\n"
                    "今天不再開新倉。"
                )
            return

        candles = await self._load_candles(symbol)
        decision = self._live_signal_decision(symbol, candles, today_net)
        signal = decision.signal
        if signal.action not in {"PLAN_LONG", "PLAN_SHORT"} or signal.score < self._settings.testnet_min_signal_score:
            logger.info(
                "testnet_signal_wait",
                symbol=symbol,
                strategy=decision.strategy,
                regime=decision.regime,
                risk_mode=decision.risk_mode,
                market_playbook=decision.market_playbook,
                allocator_profile=decision.allocator_profile,
                allocator_state=decision.allocator_state,
                allocator_scale=decision.allocator_scale,
                action=signal.action,
                score=signal.score,
                reasons=signal.reasons[:3],
            )
            return

        notional = self._bounded_signal_notional(signal)
        leverage = self._bounded_signal_leverage(signal)
        direction = "short" if signal.action == "PLAN_SHORT" else "long"
        reward_pct = self._planned_reward_pct(signal, direction)
        if reward_pct < self._settings.testnet_min_reward_pct:
            logger.info(
                "testnet_signal_wait_fee_buffer",
                symbol=symbol,
                strategy=decision.strategy,
                regime=decision.regime,
                risk_mode=decision.risk_mode,
                market_playbook=decision.market_playbook,
                allocator_profile=decision.allocator_profile,
                allocator_state=decision.allocator_state,
                allocator_scale=decision.allocator_scale,
                action=signal.action,
                score=signal.score,
                reward_pct=reward_pct,
                min_reward_pct=self._settings.testnet_min_reward_pct,
                reasons=signal.reasons[:3],
            )
            return
        planned_entry = self._planned_entry_price(signal)
        planned_stop, planned_take_profit = self._live_exit_levels(signal, direction, planned_entry)
        mark_price = await self._current_mark_price(symbol)
        preflight_reason = self._exit_level_reason(direction, mark_price, planned_stop, planned_take_profit)
        if preflight_reason is not None:
            logger.info(
                "testnet_signal_skip_entry_exit_level",
                symbol=symbol,
                strategy=decision.strategy,
                regime=decision.regime,
                risk_mode=decision.risk_mode,
                market_playbook=decision.market_playbook,
                allocator_profile=decision.allocator_profile,
                allocator_state=decision.allocator_state,
                allocator_scale=decision.allocator_scale,
                action=signal.action,
                score=signal.score,
                mark=mark_price,
                planned_entry=planned_entry,
                stop=planned_stop,
                take_profit=planned_take_profit,
                reason=preflight_reason,
                reasons=signal.reasons[:3],
            )
            return
        result = await self._trader.open_position(symbol, direction, notional, leverage=leverage)
        executed_entry = self._executed_entry_price(result, signal)
        stop_loss, take_profit = self._live_exit_levels(signal, direction, executed_entry)
        plan = ActivePlan(
            symbol=symbol,
            side=direction,
            strategy=decision.strategy,
            regime=decision.regime,
            risk_mode=decision.risk_mode,
            market_playbook=decision.market_playbook,
            allocator_state=decision.allocator_state,
            allocator_profile=decision.allocator_profile,
            allocator_scale=decision.allocator_scale,
            opened_at_ms=self._order_time_ms(result.order) or self._now_ms(),
            entry_price=executed_entry,
            stop_loss=stop_loss,
            take_profit=take_profit,
            max_holding_bars=decision.max_holding_bars,
            score=signal.score,
            reasons=[f"strategy={decision.strategy}"] + signal.reasons,
        )
        self._plans[symbol] = plan
        current_position = await self._client.get_position(symbol)
        if current_position:
            immediate_reason = self._exit_reason(plan, current_position.mark_price)
            if immediate_reason is not None:
                close_result = await self._trader.close_position(symbol)
                if close_result:
                    await self._cleanup_stale_protection_orders(symbol)
                    self._plans.pop(symbol, None)
                    await self._notify_order("entry exit level breached", close_result, current_position, plan, today_net=today_net)
                return
        await self._sync_protection_orders(symbol, plan, quantity=result.quantity)
        await self._notify_entry(result, decision)

    async def _manage_position(self, symbol: str, position: PositionInfo, today_net: float) -> None:
        plan = self._plans.get(symbol)
        if plan is None:
            await self._close_unmanaged_position(symbol, position, today_net)
            return

        price = position.mark_price
        reason = self._exit_reason(plan, price)

        if reason is None:
            await self._sync_protection_orders(symbol, plan, quantity=self._position_quantity(position))
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
            await self._cleanup_stale_protection_orders(symbol)
            self._plans.pop(symbol, None)
            await self._notify_order(reason, result, position, plan, today_net=today_net)

    async def _close_unmanaged_position(self, symbol: str, position: PositionInfo, today_net: float) -> None:
        result = await self._trader.close_position(symbol)
        if not result:
            return
        await self._cleanup_stale_protection_orders(symbol)
        self._plans.pop(symbol, None)
        self._notified_unmanaged.add(symbol)
        await self._notify_text(
            "🏁 <b>Testnet 保護性平倉</b>\n"
            "原因：無法復原策略 plan，避免 unmanaged 持倉失控\n"
            f"交易對：<code>{escape(result.symbol)}</code>\n"
            f"方向：<b>{escape(_label_side(result.side))}</b>\n"
            f"原持倉：{escape(_label_side(position.position_direction))} {abs(position.position_amt):.6f}\n"
            f"標記價：${position.mark_price:.4f} | 未實現損益：${position.unrealized_pnl:.4f}\n"
            f"平倉前今日淨損益：${today_net:.4f}\n"
            f"訂單 ID：<code>{escape(str(result.order.get('orderId', 'N/A')))}</code>"
        )

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

    def _live_signal_decision(
        self,
        symbol: str,
        candles: list[Candle],
        today_net: float,
    ) -> LiveDecisionContext:
        if self._settings.testnet_strategy_label.startswith("router_allocator_v13_trend350"):
            base = StrategyConfig(
                symbol=symbol,
                equity_usdc=self._settings.testnet_equity_usdc,
                compounding_enabled=True,
                daily_target_min_pct=self._settings.testnet_daily_target_pct,
                daily_target_max_pct=self._settings.testnet_daily_target_pct,
                risk_per_trade_pct=100.0,
                min_score=60,
                max_effective_leverage=self._settings.max_effective_leverage,
                maker_fee_rate=0.0,
                taker_fee_rate=0.0004,
                daily_soft_loss_pct=self._settings.daily_soft_loss_pct,
                daily_max_loss_pct=self._settings.max_daily_loss_pct,
                daily_loss_risk_scale=0.55,
                daily_target_stop_pct=10.0,
                max_open_positions=1,
                max_position_margin_pct=100.0,
                cooldown_bars=4,
                max_consecutive_losses_before_cooldown=3,
                consecutive_loss_cooldown_bars=18,
                max_holding_bars=48,
                take_profit_r=(0.55, 1.1, 2.2),
                exit_weights=(0.25, 0.35, 0.40),
            )
            decision = generate_router_allocator_v13_trend350_live_decision(candles, base, today_net)
            if decision is None:
                return LiveDecisionContext(
                    signal=SignalPlan(
                        action="WAIT",
                        confidence=0,
                        score=0,
                        symbol=symbol,
                        price=candles[-1].close,
                        rsi=None,
                        atr=None,
                        support=None,
                        vwap=None,
                        reasons=["router live decision blocked"],
                    ),
                    strategy="router_wait",
                    regime="blocked",
                    risk_mode="blocked",
                    market_playbook="blocked",
                    allocator_state="blocked",
                    allocator_profile="blocked",
                    allocator_scale=0.0,
                    max_holding_bars=0,
                )
            return LiveDecisionContext(
                signal=decision.signal,
                strategy=decision.strategy,
                regime=decision.regime,
                risk_mode=decision.risk_mode,
                market_playbook=decision.market_playbook,
                allocator_state=decision.allocator_state,
                allocator_profile=decision.allocator_profile,
                allocator_scale=decision.allocator_scale,
                max_holding_bars=decision.max_holding_bars,
            )

        signal = generate_ntrend_signal(candles, self._ntrend_config(symbol))
        return LiveDecisionContext(
            signal=signal,
            strategy="ntrend_ma20",
            regime="ntrend",
            risk_mode="ntrend",
            market_playbook="ntrend",
            allocator_state="base",
            allocator_profile="ntrend",
            allocator_scale=1.0,
            max_holding_bars=48,
        )

    def _bounded_signal_notional(self, signal: SignalPlan) -> float:
        leverage = self._bounded_signal_leverage(signal)
        margin_cap = self._settings.testnet_equity_usdc * self._settings.testnet_max_position_margin_pct / 100
        notional_cap = margin_cap * leverage
        requested = signal.planned_notional_usdc or self._settings.testnet_order_notional_usdc
        return max(5.0, min(requested, notional_cap, self._settings.testnet_max_order_notional_usdc))

    def _bounded_signal_leverage(self, signal: SignalPlan) -> int:
        leverage = signal.leverage_cap or self._settings.testnet_order_leverage
        return max(1, min(int(leverage), int(self._settings.max_effective_leverage)))

    def _executed_entry_price(self, result: TestnetOrderResult, signal: SignalPlan) -> float:
        order_avg = self._order_float(result.order, "avgPrice", "averagePrice")
        if order_avg is not None and order_avg > 0:
            return order_avg
        executed_qty = self._order_float(result.order, "executedQty", "origQty")
        cumulative_quote = self._order_float(result.order, "cumQuote", "cumQuoteQty", "cummulativeQuoteQty")
        if executed_qty is not None and executed_qty > 0 and cumulative_quote is not None and cumulative_quote > 0:
            return cumulative_quote / executed_qty
        if signal.entries:
            return float(signal.entries[0])
        return float(signal.price)

    def _live_exit_levels(self, signal: SignalPlan, direction: str, executed_entry: float) -> tuple[float, float]:
        planned_entry = self._planned_entry_price(signal)
        planned_stop = float(signal.stop_loss) if signal.stop_loss is not None else 0.0
        planned_tp = float(signal.take_profits[0]) if signal.take_profits else 0.0

        if direction == "short":
            risk_distance = max(planned_stop - planned_entry, 0.0)
            reward_distance = max(planned_entry - planned_tp, 0.0)
            stop_loss = executed_entry + risk_distance if risk_distance > 0 else executed_entry * 1.015
            take_profit = executed_entry - reward_distance if reward_distance > 0 else executed_entry * 0.99
        else:
            risk_distance = max(planned_entry - planned_stop, 0.0)
            reward_distance = max(planned_tp - planned_entry, 0.0)
            stop_loss = executed_entry - risk_distance if risk_distance > 0 else executed_entry * 0.985
            take_profit = executed_entry + reward_distance if reward_distance > 0 else executed_entry * 1.01

        return round(stop_loss, 4), round(take_profit, 4)

    def _planned_entry_price(self, signal: SignalPlan) -> float:
        return float(signal.entries[0]) if signal.entries else float(signal.price)

    def _planned_reward_pct(self, signal: SignalPlan, direction: str) -> float:
        planned_entry = self._planned_entry_price(signal)
        planned_tp = float(signal.take_profits[0]) if signal.take_profits else 0.0
        if planned_entry <= 0 or planned_tp <= 0:
            return 0.0
        if direction == "short":
            reward_distance = planned_entry - planned_tp
        else:
            reward_distance = planned_tp - planned_entry
        return max(reward_distance, 0.0) / planned_entry * 100

    async def _sync_protection_orders(self, symbol: str, plan: ActivePlan, quantity: str | None = None) -> None:
        if not self._settings.testnet_exchange_protection_enabled:
            return
        open_algo_orders = await self._client.get_open_algo_orders(symbol)
        open_orders = await self._client.get_open_orders(symbol)
        has_stop = False
        has_tp = False
        exit_side = "BUY" if plan.side == "short" else "SELL"
        for order in open_algo_orders:
            client_order_id = self._protection_client_order_id(order)
            stop_matches = self._protection_order_matches(order, exit_side, "STOP_MARKET", plan.stop_loss)
            if client_order_id.startswith(STOP_ORDER_PREFIX) or stop_matches:
                if self._protection_order_matches(order, exit_side, "STOP_MARKET", plan.stop_loss):
                    has_stop = True
                elif client_order_id.startswith(STOP_ORDER_PREFIX):
                    await self._cancel_algo_order(symbol, order)
            elif client_order_id.startswith(TAKE_PROFIT_ORDER_PREFIX):
                await self._cancel_algo_order(symbol, order)
        for order in open_orders:
            client_order_id = self._protection_client_order_id(order)
            tp_matches = self._limit_order_matches(order, exit_side, plan.take_profit)
            if client_order_id.startswith(TAKE_PROFIT_ORDER_PREFIX) or tp_matches:
                if tp_matches:
                    has_tp = True
                elif client_order_id.startswith(TAKE_PROFIT_ORDER_PREFIX):
                    await self._cancel_plain_order(symbol, order)
            elif client_order_id.startswith(STOP_ORDER_PREFIX):
                await self._cancel_plain_order(symbol, order)

        if not has_stop:
            await self._client.create_conditional_close_order(
                symbol=symbol,
                side=exit_side,
                order_type="STOP_MARKET",
                trigger_price=plan.stop_loss,
                quantity=quantity,
                client_algo_id=f"{STOP_ORDER_PREFIX}{self._now_ms()}",
            )
        if not has_tp:
            if not quantity:
                logger.warning("testnet_tp_limit_missing_quantity", symbol=symbol, strategy=plan.strategy)
                return
            await self._client.create_reduce_only_limit_order(
                symbol=symbol,
                side=exit_side,
                quantity=quantity,
                price=plan.take_profit,
                client_order_id=f"{TAKE_PROFIT_ORDER_PREFIX}{self._now_ms()}",
            )

    async def _cleanup_stale_protection_orders(self, symbol: str) -> None:
        open_algo_orders = await self._client.get_open_algo_orders(symbol)
        for order in open_algo_orders:
            client_order_id = self._protection_client_order_id(order)
            if not (
                client_order_id.startswith(STOP_ORDER_PREFIX)
                or client_order_id.startswith(TAKE_PROFIT_ORDER_PREFIX)
            ):
                continue
            await self._cancel_algo_order(symbol, order)
        open_orders = await self._client.get_open_orders(symbol)
        for order in open_orders:
            client_order_id = self._protection_client_order_id(order)
            if not (
                client_order_id.startswith(STOP_ORDER_PREFIX)
                or client_order_id.startswith(TAKE_PROFIT_ORDER_PREFIX)
            ):
                continue
            await self._cancel_plain_order(symbol, order)

    async def _cancel_algo_order(self, symbol: str, order: dict) -> None:
        algo_id = order.get("algoId")
        if algo_id is None:
            algo_id = order.get("orderId")
        if algo_id is not None:
            await self._client.cancel_algo_order(symbol, algo_id=int(algo_id))
            return
        client_algo_id = order.get("clientAlgoId")
        if client_algo_id is None:
            client_algo_id = order.get("clientOrderId")
        if client_algo_id:
            await self._client.cancel_algo_order(symbol, client_algo_id=str(client_algo_id))

    async def _cancel_plain_order(self, symbol: str, order: dict) -> None:
        order_id = order.get("orderId")
        if order_id is not None:
            await self._client.cancel_order(symbol, order_id=int(order_id))

    @staticmethod
    def _protection_client_order_id(order: dict) -> str:
        return str(order.get("clientAlgoId") or order.get("clientOrderId") or "")

    @staticmethod
    def _position_quantity(position: PositionInfo) -> str:
        return f"{abs(position.position_amt):.8f}".rstrip("0").rstrip(".")

    def _protection_order_matches(
        self,
        order: dict,
        expected_side: str,
        expected_type: str,
        expected_trigger_price: float,
    ) -> bool:
        side = self._order_value(order, "side")
        order_type = self._order_value(order, "orderType", "type", "origType", "order_type")
        trigger_price = self._order_float(order, "triggerPrice", "stopPrice", "trigger_price")
        if side is None or str(side).upper() != expected_side:
            return False
        if order_type is None or str(order_type).upper() != expected_type:
            return False
        if trigger_price is None:
            return False
        return abs(trigger_price - expected_trigger_price) <= 0.0001

    def _limit_order_matches(
        self,
        order: dict,
        expected_side: str,
        expected_price: float,
    ) -> bool:
        side = self._order_value(order, "side")
        order_type = self._order_value(order, "orderType", "type", "origType", "order_type")
        price = self._order_float(order, "price", "stopPrice", "triggerPrice")
        if side is None or str(side).upper() != expected_side:
            return False
        if order_type is None or str(order_type).upper() != "LIMIT":
            return False
        if price is None:
            return False
        return abs(price - expected_price) <= 0.0001

    @staticmethod
    def _order_value(order: dict, *keys: str):
        for key in keys:
            if key in order and order[key] not in (None, ""):
                return order[key]
        return None

    @classmethod
    def _order_float(cls, order: dict, *keys: str) -> float | None:
        value = cls._order_value(order, *keys)
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    async def _recover_plan(self, symbol: str, position: PositionInfo, today_net: float) -> ActivePlan | None:
        direction = "short" if position.position_amt < 0 else "long"
        entry_side = "SELL" if direction == "short" else "BUY"
        orders = await self._client.get_all_orders(symbol, limit=100)
        entry_order = None
        for order in sorted(orders, key=lambda x: x.get("time", 0), reverse=True):
            client_order_id = str(order.get("clientOrderId", ""))
            if not client_order_id.startswith("cry3tn_"):
                continue
            if order.get("status") != "FILLED":
                continue
            if order.get("side") != entry_side:
                continue
            entry_order = order
            break
        if entry_order is None:
            logger.warning("testnet_plan_recovery_missing_entry", symbol=symbol, direction=direction)
            return None

        candles = await self._load_candles(symbol)
        entry_time = int(entry_order.get("time", 0))
        prior_candles = [c for c in candles if c.open_time_ms <= entry_time]
        if not prior_candles:
            logger.warning("testnet_plan_recovery_missing_candles", symbol=symbol, entry_time=entry_time)
            return None

        decision = self._live_signal_decision(symbol, prior_candles, 0.0)
        signal = decision.signal
        if signal.action not in {"PLAN_LONG", "PLAN_SHORT"}:
            logger.warning(
                "testnet_plan_recovery_invalid_decision",
                symbol=symbol,
                action=signal.action,
                strategy=decision.strategy,
                reasons=signal.reasons[:3],
            )
            return None

        fallback_entry = float(signal.entries[0] if signal.entries else signal.price)
        order_avg = self._order_float(entry_order, "avgPrice", "averagePrice")
        executed_entry = order_avg if order_avg is not None and order_avg > 0 else fallback_entry
        stop_loss, take_profit = self._live_exit_levels(signal, direction, executed_entry)
        return ActivePlan(
            symbol=symbol,
            side=direction,
            strategy=decision.strategy,
            regime=decision.regime,
            risk_mode=decision.risk_mode,
            market_playbook=decision.market_playbook,
            allocator_state=decision.allocator_state,
            allocator_profile=decision.allocator_profile,
            allocator_scale=decision.allocator_scale,
            opened_at_ms=entry_time,
            entry_price=executed_entry,
            stop_loss=stop_loss,
            take_profit=take_profit,
            max_holding_bars=decision.max_holding_bars,
            score=signal.score,
            reasons=[f"strategy={decision.strategy}"] + signal.reasons,
        )

    async def _recent_exit_order(self, symbol: str, plan: ActivePlan) -> dict | None:
        exit_side = "BUY" if plan.side == "short" else "SELL"
        candidates = []
        for order in await self._client.get_all_orders(symbol, limit=100):
            client_order_id = self._protection_client_order_id(order)
            if not (
                client_order_id.startswith(STOP_ORDER_PREFIX)
                or client_order_id.startswith(TAKE_PROFIT_ORDER_PREFIX)
                or client_order_id.startswith("cry3close_")
            ):
                continue
            if str(order.get("status", "")).upper() != "FILLED":
                continue
            if str(order.get("side", "")).upper() != exit_side:
                continue
            order_time = self._order_time_ms(order) or 0
            if order_time and order_time < plan.opened_at_ms - 1_000:
                continue
            candidates.append((order_time, order))
        if not candidates:
            return None
        return max(candidates, key=lambda item: item[0])[1]

    def _exit_reason(self, plan: ActivePlan, price: float) -> str | None:
        max_hold_ms = plan.max_holding_bars * self._interval_ms()
        if max_hold_ms > 0 and self._now_ms() - plan.opened_at_ms >= max_hold_ms:
            return "strategy max holding"
        return self._exit_level_reason(plan.side, price, plan.stop_loss, plan.take_profit)

    def _exit_level_reason(self, side: str, price: float, stop_loss: float, take_profit: float) -> str | None:
        if side == "short":
            if price >= stop_loss:
                return "strategy stop loss"
            if price <= take_profit:
                return "strategy take profit"
        else:
            if price <= stop_loss:
                return "strategy stop loss"
            if price >= take_profit:
                return "strategy take profit"
        return None

    async def _current_mark_price(self, symbol: str) -> float:
        mark = await self._client.get_mark_price(symbol)
        price = float(mark.get("markPrice") or 0)
        if price <= 0:
            raise RuntimeError(f"Invalid mark price for {symbol}.")
        return price

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

    async def _notify_entry(self, result: TestnetOrderResult, decision: LiveDecisionContext) -> None:
        signal = decision.signal
        reasons = "\n".join(f"- {escape(reason)}" for reason in signal.reasons[:4])
        entry = signal.entries[0] if signal.entries else signal.price
        stop = signal.stop_loss if signal.stop_loss is not None else 0.0
        take_profit = signal.take_profits[0] if signal.take_profits else 0.0
        logger.info(
            "testnet_position_opened",
            symbol=result.symbol,
            side=result.side,
            strategy=decision.strategy,
            regime=decision.regime,
            risk_mode=decision.risk_mode,
            market_playbook=decision.market_playbook,
            allocator_profile=decision.allocator_profile,
            allocator_state=decision.allocator_state,
            allocator_scale=decision.allocator_scale,
            quantity=result.quantity,
            stop=stop,
            take_profit=take_profit,
            order_id=result.order.get("orderId"),
        )
        await self._notify_text(
            "🚀 <b>Testnet 自動開倉</b>\n"
            f"交易對：<code>{escape(result.symbol)}</code>\n"
            f"方向：<b>{escape(_label_side(result.side))}</b>\n"
            f"數量：<code>{escape(result.quantity)}</code>\n"
            f"名目金額：${result.notional_usdc:.2f} | 槓桿：{result.leverage}x\n"
            f"策略：<b>{escape(decision.strategy)}</b>\n"
            f"趨勢判定：<b>{escape(_label_regime(decision.regime))}</b> | 風險模式：<b>{escape(_label_risk_mode(decision.risk_mode))}</b>\n"
            f"市況劇本：<b>{escape(_label_playbook(decision.market_playbook))}</b>\n"
            f"資金配置：<b>{escape(_label_allocator_profile(decision.allocator_profile))}</b> ({escape(_label_allocator_state(decision.allocator_state))}) x{decision.allocator_scale:.2f}\n"
            f"訊號分數：{signal.score} | 信心度：{signal.confidence}\n"
            f"參考進場：${entry:.4f}\n"
            f"停損：${stop:.4f} | TP1：${take_profit:.4f}\n"
            f"訂單 ID：<code>{escape(str(result.order.get('orderId', 'N/A')))}</code>\n"
            f"{reasons}"
        )

    async def _notify_order(
        self,
        reason: str,
        result: TestnetOrderResult,
        position: PositionInfo,
        plan: ActivePlan,
        today_net: float,
    ) -> None:
        logger.info(
            "testnet_position_closed",
            symbol=result.symbol,
            side=result.side,
            reason=reason,
            strategy=plan.strategy,
            regime=plan.regime,
            risk_mode=plan.risk_mode,
            market_playbook=plan.market_playbook,
            allocator_profile=plan.allocator_profile,
            allocator_state=plan.allocator_state,
            allocator_scale=plan.allocator_scale,
            quantity=result.quantity,
            mark=position.mark_price,
            stop=plan.stop_loss,
            take_profit=plan.take_profit,
            today_net=today_net,
            order_id=result.order.get("orderId"),
        )
        await self._notify_text(
            "🏁 <b>Testnet 自動平倉</b>\n"
            f"原因：{escape(_label_reason(reason))}\n"
            f"交易對：<code>{escape(result.symbol)}</code>\n"
            f"方向：<b>{escape(_label_side(result.side))}</b>\n"
            f"策略：<b>{escape(plan.strategy)}</b>\n"
            f"趨勢判定：<b>{escape(_label_regime(plan.regime))}</b> | 風險模式：<b>{escape(_label_risk_mode(plan.risk_mode))}</b>\n"
            f"市況劇本：<b>{escape(_label_playbook(plan.market_playbook))}</b>\n"
            f"資金配置：<b>{escape(_label_allocator_profile(plan.allocator_profile))}</b> ({escape(_label_allocator_state(plan.allocator_state))}) x{plan.allocator_scale:.2f}\n"
            f"數量：<code>{escape(result.quantity)}</code>\n"
            f"標記價：${position.mark_price:.4f} | 未實現損益：${position.unrealized_pnl:.4f}\n"
            f"平倉前今日淨損益：${today_net:.4f}\n"
            f"訂單 ID：<code>{escape(str(result.order.get('orderId', 'N/A')))}</code>"
        )

    async def _notify_exchange_exit(
        self,
        symbol: str,
        plan: ActivePlan,
        exit_order: dict | None,
        today_net: float,
    ) -> None:
        client_order_id = self._protection_client_order_id(exit_order or {})
        reason = "交易所端已無持倉"
        if client_order_id.startswith(TAKE_PROFIT_ORDER_PREFIX):
            reason = "交易所停利單成交"
        elif client_order_id.startswith(STOP_ORDER_PREFIX):
            reason = "交易所停損單成交"
        elif client_order_id.startswith("cry3close_"):
            reason = "市價平倉單成交"

        avg_price = self._order_float(exit_order or {}, "avgPrice", "price")
        qty = self._order_float(exit_order or {}, "executedQty", "origQty")
        gross_pnl = None
        if avg_price is not None and qty is not None:
            if plan.side == "short":
                gross_pnl = (plan.entry_price - avg_price) * qty
            else:
                gross_pnl = (avg_price - plan.entry_price) * qty

        logger.info(
            "testnet_exchange_position_closed",
            symbol=symbol,
            reason=reason,
            client_order_id=client_order_id,
            strategy=plan.strategy,
            side=plan.side,
            entry_price=plan.entry_price,
            exit_price=avg_price,
            quantity=qty,
            gross_pnl=gross_pnl,
            today_net=today_net,
            order_id=(exit_order or {}).get("orderId"),
        )
        exit_line = (
            f"進場：${plan.entry_price:.4f} | 出場：${avg_price:.4f}\n"
            if avg_price is not None
            else f"進場：${plan.entry_price:.4f} | 出場：未知\n"
        )
        gross_line = f"粗估損益：${gross_pnl:.4f}（未扣手續費）\n" if gross_pnl is not None else ""
        await self._notify_text(
            "🏁 <b>Testnet 交易所平倉</b>\n"
            f"原因：{escape(reason)}\n"
            f"交易對：<code>{escape(symbol)}</code>\n"
            f"方向：<b>{escape(_label_side(plan.side))}</b>\n"
            f"策略：<b>{escape(plan.strategy)}</b>\n"
            f"{exit_line}"
            f"{gross_line}"
            f"停損：${plan.stop_loss:.4f} | TP1：${plan.take_profit:.4f}\n"
            f"目前今日淨損益：${today_net:.4f}\n"
            f"訂單 ID：<code>{escape(str((exit_order or {}).get('orderId', 'N/A')))}</code>"
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

    def _interval_ms(self) -> int:
        interval = self._settings.testnet_kline_interval
        if interval.endswith("m"):
            return int(interval[:-1]) * 60_000
        if interval.endswith("h"):
            return int(interval[:-1]) * 3_600_000
        return 300_000

    def _now_ms(self) -> int:
        return int(datetime.now(timezone.utc).timestamp() * 1000)

    @staticmethod
    def _order_time_ms(order: dict) -> int | None:
        for key in ("updateTime", "time", "transactTime"):
            value = order.get(key)
            if value in (None, ""):
                continue
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                continue
            if parsed > 0:
                return parsed
        return None


def _label_side(value: str) -> str:
    return SIDE_LABELS.get(value, value)


def _label_regime(value: str) -> str:
    return REGIME_LABELS.get(value, value)


def _label_risk_mode(value: str) -> str:
    return RISK_MODE_LABELS.get(value, value)


def _label_playbook(value: str) -> str:
    return PLAYBOOK_LABELS.get(value, value)


def _label_allocator_profile(value: str) -> str:
    return ALLOCATOR_PROFILE_LABELS.get(value, value)


def _label_allocator_state(value: str) -> str:
    return ALLOCATOR_STATE_LABELS.get(value, value)


def _label_reason(value: str) -> str:
    return REASON_LABELS.get(value, value)
