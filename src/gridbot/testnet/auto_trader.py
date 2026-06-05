"""Strategy-driven Binance Futures testnet execution loop."""

from __future__ import annotations

import time
from dataclasses import replace
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from config.settings import Settings
from src.gridbot.binance.client import BinanceFuturesClient
from src.gridbot.binance.models import IncomeRecord, PositionInfo
from src.gridbot.strategy.long_ntrend import NTrendConfig, generate_ntrend_signal
from src.gridbot.strategy.long_pullback import Candle, SignalPlan, StrategyConfig
from src.gridbot.strategy.signal_journal import (
    explain_router_allocator_high_return_live_block,
    explain_router_allocator_v13_trend350_live_block,
    generate_router_allocator_high_return_live_decision,
    generate_router_allocator_v13_trend350_live_decision,
)
from src.gridbot.testnet.fill_policy import (
    effective_entry_tolerance_bps,
    entry_limit_price,
    normalize_entry_fill_policy,
    reward_pct_for_entry,
)
from src.gridbot.testnet.trader import TestnetOrderResult, TestnetTrader
from src.gridbot.utils.logging import get_logger

logger = get_logger(__name__)
ENTRY_ORDER_PREFIX = "cry3en_"
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

ENTRY_MODE_LABELS = {
    "router_limit": "高報酬 Router 原始 Entry 掛單",
    "router_filled": "高報酬 Router Entry 成交",
    "trend350_limit": "Trend350 原始 Entry 掛單",
    "trend350_filled": "Trend350 Entry 成交",
}

FILL_POLICY_LABELS = {
    "strict": "嚴格原始 Entry",
    "limit_tolerance": "限價容忍補單",
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


@dataclass(frozen=True)
class PendingEntry:
    symbol: str
    decision: LiveDecisionContext
    direction: str
    notional: float
    leverage: int
    planned_entry: float
    order_entry_price: float
    planned_stop: float
    planned_take_profit: float
    fill_policy: str
    tolerance_bps: float
    order_id: int | None
    client_order_id: str
    quantity: str
    created_at_ms: int
    last_update_ms: int
    update_count: int
    expires_at_ms: int
    reason: str


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
        self._pending_entries: dict[str, PendingEntry] = {}
        self._notified_unmanaged: set[str] = set()
        self._target_stop_notified = False
        self._loss_stop_notified = False
        self._last_flat_manage_check_ms: dict[str, int] = {}
        self._cooldown_until: dict[str, float] = {}
        self._manual_signal_messages: dict[int, dict] = {}
        self._latest_manual_signal: dict | None = None

    def get_manual_signal_message(self, message_id: int | None) -> dict | None:
        if message_id is None:
            return None
        return self._manual_signal_messages.get(int(message_id))

    def get_latest_manual_signal(self) -> dict | None:
        return self._latest_manual_signal

    async def run_cycle(self) -> None:
        await self.run_manage_cycle()
        await self.run_entry_cycle()

    async def run_entry_cycle(self) -> None:
        if self._settings.trading_mode != "testnet_live":
            logger.info("testnet_auto_trade_skipped", mode=self._settings.trading_mode)
            return
        if not self._settings.binance_testnet and not self._settings.testnet_telegram_signal_only:
            raise RuntimeError("Refusing auto trading unless BINANCE_TESTNET=true.")

        for symbol in self._settings.symbols_list:
            await self._run_symbol(symbol, allow_new_entries=True)

    async def run_manage_cycle(self) -> None:
        if self._settings.trading_mode != "testnet_live":
            return
        if self._settings.testnet_telegram_signal_only:
            logger.info("testnet_manage_cycle_skipped", reason="telegram_signal_only")
            return
        if not self._settings.binance_testnet:
            raise RuntimeError("Refusing auto trading unless BINANCE_TESTNET=true.")
        for symbol in self._settings.symbols_list:
            if self._should_throttle_flat_manage(symbol):
                logger.info(
                    "testnet_manage_cycle_throttled",
                    symbol=symbol,
                    flat_interval_seconds=self._settings.testnet_manage_flat_interval_seconds,
                )
                continue
            await self._run_symbol(symbol, allow_new_entries=False)

    async def _run_symbol(self, symbol: str, allow_new_entries: bool) -> None:
        if self._settings.testnet_telegram_signal_only:
            await self._run_signal_only_symbol(symbol, allow_new_entries=allow_new_entries)
            return

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
            pending = self._pending_entries.get(symbol)
            if pending is not None:
                await self._cancel_pending_entry(symbol, pending, reason="daily loss stop")
            return

        if position:
            self._last_flat_manage_check_ms.pop(symbol, None)
            pending = self._pending_entries.pop(symbol, None)
            if pending is not None:
                plan = self._plan_from_pending_fill(symbol, pending, position)
                self._plans[symbol] = plan
                self._notified_unmanaged.discard(symbol)
                await self._sync_protection_orders(symbol, plan, quantity=self._position_quantity(position))
                result = TestnetOrderResult(
                    symbol=symbol,
                    side="SELL" if pending.direction == "short" else "BUY",
                    quantity=pending.quantity,
                    notional_usdc=pending.notional,
                    leverage=pending.leverage,
                    reduce_only=False,
                    order={"orderId": pending.order_id, "clientOrderId": pending.client_order_id},
                )
                await self._notify_entry(result, pending.decision, plan, entry_mode=self._router_entry_mode("filled"))
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
            await self._cleanup_stale_entry_orders(symbol)
        self._last_flat_manage_check_ms[symbol] = self._now_ms()

        if today_net >= target_stop:
            if not self._target_stop_notified:
                self._target_stop_notified = True
                await self._notify_text(
                    "🎯 <b>已達成今日目標</b>\n"
                    f"交易對：<code>{escape(symbol)}</code>\n"
                    f"今日淨損益：${today_net:.4f} / 目標：${target_stop:.4f}\n"
                    "今天不再開新倉。"
                )
            pending = self._pending_entries.get(symbol)
            if pending is not None:
                await self._cancel_pending_entry(symbol, pending, reason="daily target reached")
            return

        pending = self._pending_entries.get(symbol)
        if pending is not None:
            await self._manage_pending_entry(symbol, pending)
            return

        if not allow_new_entries:
            return

        candles = await self._load_candles(symbol)
        decision = self._live_signal_decision(symbol, candles, today_net)
        signal = decision.signal
        if self._blocks_exploratory_live_entry(decision):
            logger.info(
                "testnet_signal_wait_exploratory_block",
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
                reasons=signal.reasons[:8],
            )
            return
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
                reasons=signal.reasons[:8],
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
                reasons=signal.reasons[:8],
            )
            return
        planned_entry = self._planned_entry_price(signal)
        planned_stop, planned_take_profit = self._live_exit_levels(signal, direction, planned_entry)
        fill_policy = normalize_entry_fill_policy(self._settings.testnet_entry_fill_policy)
        tolerance_bps = self._entry_tolerance_bps(decision)
        order_entry_price = self._entry_limit_price(
            direction,
            planned_entry,
            planned_stop,
            planned_take_profit,
            tolerance_bps=tolerance_bps,
        )
        tolerated_reward_pct = self._reward_pct_for_entry(order_entry_price, planned_take_profit, direction)
        if tolerated_reward_pct < self._settings.testnet_min_reward_pct:
            logger.info(
                "testnet_signal_wait_entry_tolerance_fee_buffer",
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
                planned_entry=planned_entry,
                order_entry_price=order_entry_price,
                take_profit=planned_take_profit,
                reward_pct=tolerated_reward_pct,
                min_reward_pct=self._settings.testnet_min_reward_pct,
                fill_policy=fill_policy,
                tolerance_bps=tolerance_bps,
                configured_tolerance_bps=self._settings.testnet_entry_tolerance_bps,
                reasons=signal.reasons[:8],
            )
            return
        reprice_headroom_reward_pct = self._reprice_headroom_reward_pct(
            direction,
            order_entry_price,
            planned_stop,
            planned_take_profit,
            tolerance_bps=tolerance_bps,
        )
        reprice_min_reward_pct = self._effective_reprice_min_reward_pct(decision)
        if (
            reprice_headroom_reward_pct is not None
            and reprice_headroom_reward_pct < reprice_min_reward_pct
        ):
            logger.info(
                "testnet_signal_wait_reprice_headroom",
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
                order_entry_price=order_entry_price,
                take_profit=planned_take_profit,
                reward_pct=tolerated_reward_pct,
                reprice_headroom_reward_pct=reprice_headroom_reward_pct,
                min_reward_pct=reprice_min_reward_pct,
                configured_min_reward_pct=self._settings.testnet_min_reward_pct,
                reprice_trigger_bps=self._settings.testnet_entry_reprice_trigger_bps,
                fill_policy=fill_policy,
                tolerance_bps=tolerance_bps,
                reasons=signal.reasons[:8],
            )
            return
        if self._settings.testnet_telegram_signal_only:
            await self._notify_manual_signal(
                symbol=symbol,
                decision=decision,
                direction=direction,
                notional=notional,
                leverage=leverage,
                planned_entry=planned_entry,
                order_entry_price=order_entry_price,
                planned_stop=planned_stop,
                planned_take_profit=planned_take_profit,
                fill_policy=fill_policy,
                tolerance_bps=tolerance_bps,
            )
            return
        await self._place_pending_entry_limit(
            symbol=symbol,
            decision=decision,
            direction=direction,
            notional=notional,
            leverage=leverage,
            planned_entry=planned_entry,
            order_entry_price=order_entry_price,
            planned_stop=planned_stop,
            planned_take_profit=planned_take_profit,
            fill_policy=fill_policy,
            tolerance_bps=tolerance_bps,
        )

    async def _run_signal_only_symbol(self, symbol: str, allow_new_entries: bool) -> None:
        if not allow_new_entries:
            return

        candles = await self._load_candles(symbol)
        decision = self._live_signal_decision(symbol, candles, 0.0)
        signal = decision.signal
        if self._blocks_exploratory_live_entry(decision):
            logger.info(
                "manual_signal_wait_exploratory_block",
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
                reasons=signal.reasons[:8],
            )
            return
        if signal.action not in {"PLAN_LONG", "PLAN_SHORT"} or signal.score < self._settings.testnet_min_signal_score:
            logger.info(
                "manual_signal_wait",
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
                reasons=signal.reasons[:8],
            )
            return

        notional = self._bounded_signal_notional(signal)
        leverage = self._bounded_signal_leverage(signal)
        direction = "short" if signal.action == "PLAN_SHORT" else "long"
        reward_pct = self._planned_reward_pct(signal, direction)
        if reward_pct < self._settings.testnet_min_reward_pct:
            logger.info(
                "manual_signal_wait_fee_buffer",
                symbol=symbol,
                strategy=decision.strategy,
                action=signal.action,
                score=signal.score,
                reward_pct=reward_pct,
                min_reward_pct=self._settings.testnet_min_reward_pct,
                reasons=signal.reasons[:8],
            )
            return

        planned_entry = self._planned_entry_price(signal)
        planned_stop, planned_take_profit = self._live_exit_levels(signal, direction, planned_entry)
        fill_policy = normalize_entry_fill_policy(self._settings.testnet_entry_fill_policy)
        tolerance_bps = self._entry_tolerance_bps(decision)
        order_entry_price = self._entry_limit_price(
            direction,
            planned_entry,
            planned_stop,
            planned_take_profit,
            tolerance_bps=tolerance_bps,
        )
        tolerated_reward_pct = self._reward_pct_for_entry(order_entry_price, planned_take_profit, direction)
        if tolerated_reward_pct < self._settings.testnet_min_reward_pct:
            logger.info(
                "manual_signal_wait_entry_tolerance_fee_buffer",
                symbol=symbol,
                strategy=decision.strategy,
                action=signal.action,
                score=signal.score,
                planned_entry=planned_entry,
                order_entry_price=order_entry_price,
                take_profit=planned_take_profit,
                reward_pct=tolerated_reward_pct,
                min_reward_pct=self._settings.testnet_min_reward_pct,
                fill_policy=fill_policy,
                tolerance_bps=tolerance_bps,
                reasons=signal.reasons[:8],
            )
            return

        reprice_headroom_reward_pct = self._reprice_headroom_reward_pct(
            direction,
            order_entry_price,
            planned_stop,
            planned_take_profit,
            tolerance_bps=tolerance_bps,
        )
        reprice_min_reward_pct = self._effective_reprice_min_reward_pct(decision)
        if (
            reprice_headroom_reward_pct is not None
            and reprice_headroom_reward_pct < reprice_min_reward_pct
        ):
            logger.info(
                "manual_signal_wait_reprice_headroom",
                symbol=symbol,
                strategy=decision.strategy,
                action=signal.action,
                score=signal.score,
                order_entry_price=order_entry_price,
                take_profit=planned_take_profit,
                reward_pct=tolerated_reward_pct,
                reprice_headroom_reward_pct=reprice_headroom_reward_pct,
                min_reward_pct=reprice_min_reward_pct,
                fill_policy=fill_policy,
                tolerance_bps=tolerance_bps,
                reasons=signal.reasons[:8],
            )
            return

        await self._notify_manual_signal(
            symbol=symbol,
            decision=decision,
            direction=direction,
            notional=notional,
            leverage=leverage,
            planned_entry=planned_entry,
            order_entry_price=order_entry_price,
            planned_stop=planned_stop,
            planned_take_profit=planned_take_profit,
            fill_policy=fill_policy,
            tolerance_bps=tolerance_bps,
        )
        self._cooldown_until[decision.strategy] = time.time() + 300

    async def _place_pending_entry_limit(
        self,
        symbol: str,
        decision: LiveDecisionContext,
        direction: str,
        notional: float,
        leverage: int,
        planned_entry: float,
        order_entry_price: float,
        planned_stop: float,
        planned_take_profit: float,
        fill_policy: str,
        tolerance_bps: float,
    ) -> None:
        result = await self._trader.place_entry_limit(
            symbol,
            direction,
            order_entry_price,
            notional,
            leverage=leverage,
        )
        now_ms = self._now_ms()
        client_order_id = str(result.order.get("clientOrderId") or result.order.get("clientOrderId".lower()) or "")
        if not client_order_id:
            client_order_id = str(result.order.get("newClientOrderId") or "")
        self._pending_entries[symbol] = PendingEntry(
            symbol=symbol,
            decision=decision,
            direction=direction,
            notional=notional,
            leverage=leverage,
            planned_entry=planned_entry,
            order_entry_price=order_entry_price,
            planned_stop=planned_stop,
            planned_take_profit=planned_take_profit,
            fill_policy=fill_policy,
            tolerance_bps=tolerance_bps,
            order_id=self._order_int(result.order, "orderId"),
            client_order_id=client_order_id,
            quantity=result.quantity,
            created_at_ms=now_ms,
            last_update_ms=now_ms,
            update_count=0,
            expires_at_ms=now_ms + self._settings.testnet_entry_order_ttl_bars * self._interval_ms(),
            reason="router entry limit",
        )
        logger.info(
            "testnet_router_entry_limit_placed",
            symbol=symbol,
            strategy=decision.strategy,
            regime=decision.regime,
            risk_mode=decision.risk_mode,
            market_playbook=decision.market_playbook,
            allocator_profile=decision.allocator_profile,
            allocator_state=decision.allocator_state,
            allocator_scale=decision.allocator_scale,
            action=decision.signal.action,
            score=decision.signal.score,
            entry=planned_entry,
            order_entry_price=order_entry_price,
            stop=planned_stop,
            take_profit=planned_take_profit,
            ttl_bars=self._settings.testnet_entry_order_ttl_bars,
            fill_policy=fill_policy,
            tolerance_bps=tolerance_bps,
            configured_tolerance_bps=self._settings.testnet_entry_tolerance_bps,
            order_id=result.order.get("orderId"),
            client_order_id=client_order_id,
        )
        await self._notify_pending_entry(result, decision, self._pending_entries[symbol])

    async def _manage_pending_entry(self, symbol: str, pending: PendingEntry) -> None:
        open_orders = await self._client.get_open_orders(symbol)
        order_open = any(self._entry_order_matches(order, pending) for order in open_orders)
        if not order_open:
            self._pending_entries.pop(symbol, None)
            logger.info(
                "testnet_router_entry_order_missing",
                symbol=symbol,
                strategy=pending.decision.strategy,
                order_id=pending.order_id,
                client_order_id=pending.client_order_id,
            )
            return
        if await self._maybe_reprice_pending_entry(symbol, pending):
            return
        if self._now_ms() < pending.expires_at_ms:
            logger.info(
                "testnet_router_entry_limit_wait",
                symbol=symbol,
                strategy=pending.decision.strategy,
                action=pending.decision.signal.action,
                entry=pending.order_entry_price,
                planned_entry=pending.planned_entry,
                fill_policy=pending.fill_policy,
                tolerance_bps=pending.tolerance_bps,
                expires_in_seconds=max(0, (pending.expires_at_ms - self._now_ms()) // 1000),
            )
            return
        await self._cancel_pending_entry(symbol, pending, reason="entry expired")

    async def _maybe_reprice_pending_entry(self, symbol: str, pending: PendingEntry) -> bool:
        if not self._settings.testnet_entry_reprice_enabled:
            return False
        if pending.update_count >= self._settings.testnet_entry_reprice_max_updates:
            return False
        if pending.fill_policy != "limit_tolerance":
            return False
        now_ms = self._now_ms()
        cooldown_ms = max(0, self._settings.testnet_entry_reprice_cooldown_seconds) * 1000
        if now_ms - pending.last_update_ms < cooldown_ms:
            return False

        mark_price = await self._current_mark_price(symbol)
        if not self._entry_price_drifted(pending.direction, pending.order_entry_price, mark_price):
            return False

        refreshed_entry_price = self._entry_limit_price(
            pending.direction,
            mark_price,
            pending.planned_stop,
            pending.planned_take_profit,
            tolerance_bps=pending.tolerance_bps,
        )
        if abs(refreshed_entry_price - pending.order_entry_price) < 1e-9:
            return False

        refreshed_reward_pct = self._reward_pct_for_entry(
            refreshed_entry_price,
            pending.planned_take_profit,
            pending.direction,
        )
        reprice_min_reward_pct = self._effective_reprice_min_reward_pct(pending.decision)
        if refreshed_reward_pct < reprice_min_reward_pct:
            held_pending = self._hold_pending_entry_on_first_low_reward_reprice(
                symbol,
                pending,
                refreshed_reward_pct,
                reprice_min_reward_pct,
                now_ms,
            )
            if held_pending:
                return True
            await self._cancel_pending_entry(symbol, pending, reason="entry repriced reward too low")
            return True

        if pending.order_id is not None:
            await self._client.cancel_order(symbol, order_id=pending.order_id)
        result = await self._trader.place_entry_limit(
            symbol,
            pending.direction,
            refreshed_entry_price,
            pending.notional,
            leverage=pending.leverage,
        )
        client_order_id = str(result.order.get("clientOrderId") or result.order.get("clientOrderId".lower()) or "")
        if not client_order_id:
            client_order_id = str(result.order.get("newClientOrderId") or "")
        updated_pending = replace(
            pending,
            order_entry_price=refreshed_entry_price,
            order_id=self._order_int(result.order, "orderId"),
            client_order_id=client_order_id,
            quantity=result.quantity,
            last_update_ms=now_ms,
            update_count=pending.update_count + 1,
            reason="router entry repriced",
        )
        self._pending_entries[symbol] = updated_pending
        logger.info(
            "testnet_router_entry_limit_repriced",
            symbol=symbol,
            strategy=pending.decision.strategy,
            action=pending.decision.signal.action,
            old_order_id=pending.order_id,
            new_order_id=result.order.get("orderId"),
            old_entry=pending.order_entry_price,
            new_entry=refreshed_entry_price,
            mark_price=mark_price,
            update_count=updated_pending.update_count,
            reward_pct=refreshed_reward_pct,
        )
        return True

    async def _cancel_pending_entry(self, symbol: str, pending: PendingEntry, reason: str) -> None:
        if pending.order_id is not None:
            await self._client.cancel_order(symbol, order_id=pending.order_id)
        self._pending_entries.pop(symbol, None)
        logger.info(
            "testnet_router_entry_limit_cancelled",
            symbol=symbol,
            strategy=pending.decision.strategy,
            order_id=pending.order_id,
            client_order_id=pending.client_order_id,
            fill_policy=pending.fill_policy,
            tolerance_bps=pending.tolerance_bps,
            reason=reason,
        )

    def _plan_from_pending_fill(self, symbol: str, pending: PendingEntry, position: PositionInfo) -> ActivePlan:
        executed_entry = self._position_entry_price(position) or pending.planned_entry
        return ActivePlan(
            symbol=symbol,
            side=pending.direction,
            strategy=pending.decision.strategy,
            regime=pending.decision.regime,
            risk_mode=pending.decision.risk_mode,
            market_playbook=pending.decision.market_playbook,
            allocator_state=pending.decision.allocator_state,
            allocator_profile=pending.decision.allocator_profile,
            allocator_scale=pending.decision.allocator_scale,
            opened_at_ms=pending.created_at_ms,
            entry_price=executed_entry,
            stop_loss=pending.planned_stop,
            take_profit=pending.planned_take_profit,
            max_holding_bars=pending.decision.max_holding_bars,
            score=pending.decision.signal.score,
            reasons=[f"strategy={pending.decision.strategy}", "router_entry_limit_fill"] + pending.decision.signal.reasons,
        )

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
            if reason == "strategy stop loss":
                self._cooldown_until[plan.strategy] = time.time() + 300
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
            maker_fee_rate=self._settings.testnet_maker_fee_rate,
            taker_fee_rate=self._settings.testnet_taker_fee_rate,
        )
        return NTrendConfig(base=base)

    def _live_signal_decision(
        self,
        symbol: str,
        candles: list[Candle],
        today_net: float,
    ) -> LiveDecisionContext:
        if self._settings.testnet_strategy_label == "wildcat_v2_adverse_guard":
            from src.gridbot.strategy.wildcat_live import generate_wildcat_v2_adverse_guard_live_decision
            decision = generate_wildcat_v2_adverse_guard_live_decision(
                candles=candles,
                today_pnl_usdc=today_net,
                target_daily_usdc=self._settings.testnet_equity_usdc * 0.03,
                notional_usdc=self._settings.testnet_order_notional_usdc,
                leverage=self._settings.testnet_order_leverage,
            )
            if decision is None:
                from src.gridbot.strategy.wildcat_live import explain_wildcat_no_signal
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
                        reasons=explain_wildcat_no_signal(
                            candles=candles,
                            today_pnl_usdc=today_net,
                            target_daily_usdc=self._settings.testnet_equity_usdc * 0.03,
                            leverage=self._settings.testnet_order_leverage,
                        ),
                    ),
                    strategy="wildcat_wait",
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
                regime=decision.params_label,
                risk_mode="wildcat",
                market_playbook=decision.side,
                allocator_state="active",
                allocator_profile="wildcat",
                allocator_scale=1.0,
                max_holding_bars=decision.max_holding_bars,
            )

        if self._settings.testnet_strategy_label == "winrate_optimized_portfolio":
            from src.gridbot.strategy.winrate_optimized_portfolio import (
                explain_winrate_optimized_portfolio_no_signal,
                generate_winrate_optimized_portfolio_decision,
            )
            decision = generate_winrate_optimized_portfolio_decision(
                candles=candles,
                today_net=today_net,
                cooldown_until=self._cooldown_until,
                equity_usdc=self._settings.testnet_equity_usdc,
            )
            if decision is None:
                reasons = explain_winrate_optimized_portfolio_no_signal(
                    candles=candles,
                    today_net=today_net,
                    cooldown_until=self._cooldown_until,
                    equity_usdc=self._settings.testnet_equity_usdc,
                )
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
                        reasons=reasons,
                    ),
                    strategy="portfolio_wait",
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

        if self._uses_router_live_family():
            base = StrategyConfig(
                symbol=symbol,
                equity_usdc=self._settings.testnet_equity_usdc,
                compounding_enabled=True,
                daily_target_min_pct=self._settings.testnet_daily_target_pct,
                daily_target_max_pct=self._settings.testnet_daily_target_pct,
                risk_per_trade_pct=100.0,
                min_score=60,
                max_effective_leverage=self._settings.max_effective_leverage,
                maker_fee_rate=self._settings.testnet_maker_fee_rate,
                taker_fee_rate=self._settings.testnet_taker_fee_rate,
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
            decision_fn = (
                generate_router_allocator_v13_trend350_live_decision
                if self._settings.testnet_strategy_label.startswith("router_allocator_v13_trend350")
                else generate_router_allocator_high_return_live_decision
            )
            block_reason_fn = (
                explain_router_allocator_v13_trend350_live_block
                if self._settings.testnet_strategy_label.startswith("router_allocator_v13_trend350")
                else explain_router_allocator_high_return_live_block
            )
            decision = decision_fn(candles, base, today_net)
            if decision is None:
                blocked_reason = block_reason_fn(candles, base, today_net)
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
                        reasons=[f"router live decision blocked: {blocked_reason}"],
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

    def _uses_router_live_family(self) -> bool:
        label = self._settings.testnet_strategy_label
        return (
            label.startswith("router_allocator_high_return")
            or label.startswith("router_allocator_v9")
            or label.startswith("router_allocator_v11")
            or label.startswith("router_allocator_v13_trend350")
        )

    def _router_entry_mode(self, phase: str) -> str:
        prefix = "trend350" if self._settings.testnet_strategy_label.startswith("router_allocator_v13_trend350") else "router"
        return f"{prefix}_{phase}"

    def _blocks_exploratory_live_entry(self, decision: LiveDecisionContext) -> bool:
        return self._uses_router_live_family() and decision.allocator_profile == "exploratory_long"

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
        return self._reward_pct_for_entry(planned_entry, planned_tp, direction)

    def _reward_pct_for_entry(self, entry: float, take_profit: float, direction: str) -> float:
        return reward_pct_for_entry(entry, take_profit, direction)

    def _reprice_headroom_reward_pct(
        self,
        direction: str,
        order_entry_price: float,
        planned_stop: float,
        planned_take_profit: float,
        *,
        tolerance_bps: float,
    ) -> float | None:
        if not self._settings.testnet_entry_reprice_enabled:
            return None
        if normalize_entry_fill_policy(self._settings.testnet_entry_fill_policy) != "limit_tolerance":
            return None
        if order_entry_price <= 0:
            return None
        trigger_ratio = self._settings.testnet_entry_reprice_trigger_bps / 10_000
        if direction == "short":
            simulated_mark_price = order_entry_price * (1 - trigger_ratio)
        else:
            simulated_mark_price = order_entry_price * (1 + trigger_ratio)
        simulated_entry = self._entry_limit_price(
            direction,
            simulated_mark_price,
            planned_stop,
            planned_take_profit,
            tolerance_bps=tolerance_bps,
        )
        return self._reward_pct_for_entry(simulated_entry, planned_take_profit, direction)

    def _effective_reprice_min_reward_pct(self, decision: LiveDecisionContext) -> float:
        min_reward_pct = self._settings.testnet_min_reward_pct
        if not self._uses_router_live_family():
            return min_reward_pct
        if (
            decision.strategy == "orb_long"
            and decision.regime == "trend_up"
            and decision.allocator_profile in {"trend_up_normal_weak", "trend_up_normal", "trend_up_aggressive"}
            and decision.allocator_scale >= 0.35
            and decision.signal.score >= 84
        ):
            return max(0.08, min_reward_pct * 0.80)
        if decision.signal.score < 90:
            return min_reward_pct
        if decision.strategy == "orb_long" and decision.regime in {"trend_up", "low_liquidity"}:
            return max(0.08, min_reward_pct * 0.80)
        if decision.strategy == "orb_short" and decision.regime in {"trend_down", "high_volatility"}:
            return max(0.08, min_reward_pct * 0.80)
        return min_reward_pct

    def _hold_pending_entry_on_first_low_reward_reprice(
        self,
        symbol: str,
        pending: PendingEntry,
        refreshed_reward_pct: float,
        reprice_min_reward_pct: float,
        now_ms: int,
    ) -> bool:
        decision = pending.decision
        if pending.reason == "router entry hold after low reward reprice":
            return False
        if (
            decision.strategy != "orb_long"
            or decision.regime != "trend_up"
            or decision.allocator_profile not in {"trend_up_normal_weak", "trend_up_normal", "trend_up_aggressive"}
            or decision.allocator_scale < 0.35
            or decision.signal.score < 84
        ):
            return False
        held_pending = replace(
            pending,
            last_update_ms=now_ms,
            reason="router entry hold after low reward reprice",
        )
        self._pending_entries[symbol] = held_pending
        logger.info(
            "testnet_router_entry_limit_hold_low_reward",
            symbol=symbol,
            strategy=decision.strategy,
            order_id=pending.order_id,
            client_order_id=pending.client_order_id,
            current_entry=pending.order_entry_price,
            refreshed_reward_pct=refreshed_reward_pct,
            min_reward_pct=reprice_min_reward_pct,
            update_count=pending.update_count,
        )
        return True

    def _entry_price_drifted(self, direction: str, order_entry_price: float, mark_price: float) -> bool:
        if order_entry_price <= 0 or mark_price <= 0:
            return False
        trigger_ratio = self._settings.testnet_entry_reprice_trigger_bps / 10_000
        if direction == "short":
            return mark_price <= order_entry_price * (1 - trigger_ratio)
        return mark_price >= order_entry_price * (1 + trigger_ratio)

    def _entry_tolerance_bps(self, decision: LiveDecisionContext) -> float:
        return effective_entry_tolerance_bps(
            self._settings.testnet_entry_fill_policy,
            self._settings.testnet_entry_tolerance_bps,
            score=decision.signal.score,
            min_score=self._settings.testnet_entry_tolerance_min_score,
        )

    def _entry_limit_price(
        self,
        direction: str,
        planned_entry: float,
        planned_stop: float,
        planned_take_profit: float,
        tolerance_bps: float | None = None,
    ) -> float:
        if tolerance_bps is None:
            tolerance_bps = effective_entry_tolerance_bps(
                self._settings.testnet_entry_fill_policy,
                self._settings.testnet_entry_tolerance_bps,
            )
        return entry_limit_price(direction, planned_entry, planned_stop, planned_take_profit, tolerance_bps)

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

    async def _cleanup_stale_entry_orders(self, symbol: str) -> None:
        if symbol in self._pending_entries:
            return
        open_orders = await self._client.get_open_orders(symbol)
        for order in open_orders:
            client_order_id = self._protection_client_order_id(order)
            if not client_order_id.startswith(ENTRY_ORDER_PREFIX):
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

    @staticmethod
    def _position_entry_price(position: PositionInfo) -> float:
        try:
            return abs(float(position.entry_price))
        except (TypeError, ValueError):
            return 0.0

    def _protection_order_matches(
        self,
        order: dict,
        expected_side: str,
        expected_type: str,
        expected_trigger_price: float,
    ) -> bool:
        side = self._order_value(order, "side")
        order_type = self._order_value(order, "orderType", "type", "origType", "order_type")
        if side is None or str(side).upper() != expected_side:
            return False
        if order_type is None or str(order_type).upper() != expected_type:
            return False
        return self._order_price_matches(order, expected_trigger_price, "triggerPrice", "stopPrice", "trigger_price")

    def _limit_order_matches(
        self,
        order: dict,
        expected_side: str,
        expected_price: float,
    ) -> bool:
        side = self._order_value(order, "side")
        order_type = self._order_value(order, "orderType", "type", "origType", "order_type")
        if side is None or str(side).upper() != expected_side:
            return False
        if order_type is None or str(order_type).upper() != "LIMIT":
            return False
        return self._order_price_matches(order, expected_price, "price", "stopPrice", "triggerPrice")

    @classmethod
    def _order_price_matches(cls, order: dict, expected_price: float, *keys: str) -> bool:
        value = cls._order_value(order, *keys)
        if value is None:
            return False
        try:
            actual = Decimal(str(value))
            if actual <= 0:
                return False
            normalized = actual.normalize()
            decimals = max(0, -normalized.as_tuple().exponent)
            quant = Decimal("1").scaleb(-decimals)
            expected = Decimal(str(expected_price)).quantize(quant, rounding=ROUND_HALF_UP)
        except (InvalidOperation, TypeError, ValueError):
            return False
        return actual == expected

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

    @classmethod
    def _order_int(cls, order: dict, *keys: str) -> int | None:
        value = cls._order_value(order, *keys)
        if value is None:
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    def _entry_order_matches(self, order: dict, pending: PendingEntry) -> bool:
        order_id = self._order_int(order, "orderId")
        if pending.order_id is not None and order_id == pending.order_id:
            return True
        client_order_id = self._protection_client_order_id(order)
        return bool(pending.client_order_id and client_order_id == pending.client_order_id)

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
                reasons=signal.reasons[:8],
            )
            return None

        fallback_entry = float(signal.entries[0] if signal.entries else signal.price)
        order_avg = self._order_float(entry_order, "avgPrice", "averagePrice")
        position_entry = self._position_entry_price(position)
        executed_entry = (
            order_avg
            if order_avg is not None and order_avg > 0
            else position_entry if position_entry > 0
            else fallback_entry
        )
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

    async def _notify_manual_signal(
        self,
        *,
        symbol: str,
        decision: LiveDecisionContext,
        direction: str,
        notional: float,
        leverage: int,
        planned_entry: float,
        order_entry_price: float,
        planned_stop: float,
        planned_take_profit: float,
        fill_policy: str,
        tolerance_bps: float,
    ) -> None:
        signal = decision.signal
        now_ms = self._now_ms()
        execution_id = f"{symbol}-{now_ms}"
        side_text = "做空" if direction == "short" else "做多"
        side_icon = "🔴" if direction == "short" else "🟢"
        reasons = "\n".join(f"- {escape(reason)}" for reason in signal.reasons[:8])
        payload = {
            "execution_id": execution_id,
            "symbol": symbol,
            "direction": direction,
            "strategy": decision.strategy,
            "regime": decision.regime,
            "risk_mode": decision.risk_mode,
            "market_playbook": decision.market_playbook,
            "allocator_profile": decision.allocator_profile,
            "allocator_state": decision.allocator_state,
            "allocator_scale": decision.allocator_scale,
            "score": signal.score,
            "confidence": signal.confidence,
            "planned_entry": planned_entry,
            "order_entry_price": order_entry_price,
            "planned_stop": planned_stop,
            "planned_take_profit": planned_take_profit,
            "notional_usdc": notional,
            "leverage": leverage,
            "fill_policy": fill_policy,
            "tolerance_bps": tolerance_bps,
            "sent_at_ms": now_ms,
        }

        logger.info(
            "manual_signal_ready",
            execution_id=execution_id,
            symbol=symbol,
            direction=direction,
            strategy=decision.strategy,
            score=signal.score,
            notional=notional,
            leverage=leverage,
        )
        self._latest_manual_signal = payload

        if not self._telegram_app or not self._settings.telegram_chat_id_int:
            logger.info("manual_signal_notice_skipped", execution_id=execution_id)
            return

        message = await self._telegram_app.bot.send_message(
            chat_id=self._settings.telegram_chat_id_int,
            text=(
                f"{side_icon} <b>{side_text}訊號</b> | <b>{escape(symbol)}</b>\n"
                f"策略：<b>{escape(decision.strategy)}</b> | score=<code>{signal.score}</code>\n"
                f"名目金額：<b>${notional:.2f} USDC</b> | 槓桿：<b>{leverage}x</b>\n"
                f"建議 Entry：<b>${planned_entry:.4f}</b>\n"
                f"Limit 參考：<b>${order_entry_price:.4f}</b> ({escape(fill_policy)}, {tolerance_bps:.2f} bps)\n"
                f"Stop：<b>${planned_stop:.4f}</b> | TP：<b>${planned_take_profit:.4f}</b>\n"
                f"Regime：<code>{escape(decision.regime)}</code> / <code>{escape(decision.market_playbook)}</code>\n"
                f"訊號代碼：<code>{escape(execution_id)}</code>\n"
                f"{reasons}"
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("已下單", callback_data=f"manual_signal:{execution_id}")]]
            ),
        )
        payload["message_id"] = message.message_id
        self._manual_signal_messages[message.message_id] = payload
        self._latest_manual_signal = payload

        audit_repo = self._telegram_app.bot_data.get("audit_repo") if self._telegram_app else None
        if audit_repo is not None:
            await audit_repo.log(
                "manual_signal_sent",
                "testnet_auto_trader",
                {
                    **payload,
                    "signal": payload,
                },
            )

    async def _notify_entry(
        self,
        result: TestnetOrderResult,
        decision: LiveDecisionContext,
        plan: ActivePlan,
        entry_mode: str = "router_filled",
    ) -> None:
        signal = decision.signal
        reasons = "\n".join(f"- {escape(reason)}" for reason in signal.reasons[:8])
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
            entry=plan.entry_price,
            stop=plan.stop_loss,
            take_profit=plan.take_profit,
            entry_mode=entry_mode,
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
            f"執行模式：<b>{escape(_label_entry_mode(entry_mode))}</b>\n"
            f"進場基準：${plan.entry_price:.4f}\n"
            f"停損：${plan.stop_loss:.4f} | TP1：${plan.take_profit:.4f}\n"
            f"訂單 ID：<code>{escape(str(result.order.get('orderId', 'N/A')))}</code>\n"
            f"{reasons}"
        )

    async def _notify_pending_entry(
        self,
        result: TestnetOrderResult,
        decision: LiveDecisionContext,
        pending: PendingEntry,
    ) -> None:
        signal = decision.signal
        reasons = "\n".join(f"- {escape(reason)}" for reason in signal.reasons[:8])
        heading = _label_entry_mode(self._router_entry_mode("limit"))
        await self._notify_text(
            f"🧾 <b>Testnet {escape(heading)}</b>\n"
            f"交易對：<code>{escape(result.symbol)}</code>\n"
            f"方向：<b>{escape(_label_side(result.side))}</b>\n"
            f"數量：<code>{escape(result.quantity)}</code>\n"
            f"名目金額：${result.notional_usdc:.2f} | 槓桿：{result.leverage}x\n"
            f"策略：<b>{escape(decision.strategy)}</b>\n"
            f"趨勢判定：<b>{escape(_label_regime(decision.regime))}</b> | 風險模式：<b>{escape(_label_risk_mode(decision.risk_mode))}</b>\n"
            f"市況劇本：<b>{escape(_label_playbook(decision.market_playbook))}</b>\n"
            f"訊號分數：{signal.score} | 信心度：{signal.confidence}\n"
            f"成交政策：<b>{escape(_label_fill_policy(pending.fill_policy))}</b>\n"
            f"Entry Limit：${pending.order_entry_price:.4f}\n"
            f"原始 Entry：${pending.planned_entry:.4f} | 實際容忍：{pending.tolerance_bps:.2f} bps\n"
            f"停損：${pending.planned_stop:.4f} | TP1：${pending.planned_take_profit:.4f}\n"
            f"有效：{self._settings.testnet_entry_order_ttl_bars} 根 K 線\n"
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
            self._cooldown_until[plan.strategy] = time.time() + 300
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

    def _should_throttle_flat_manage(self, symbol: str) -> bool:
        if symbol in self._plans:
            return False
        if symbol in self._pending_entries:
            return False
        last_check_ms = self._last_flat_manage_check_ms.get(symbol)
        if last_check_ms is None:
            return False
        flat_interval_ms = max(
            int(self._settings.testnet_manage_interval_seconds),
            int(self._settings.testnet_manage_flat_interval_seconds),
        ) * 1000
        return self._now_ms() - last_check_ms < flat_interval_ms

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


def _label_entry_mode(value: str) -> str:
    return ENTRY_MODE_LABELS.get(value, value)


def _label_fill_policy(value: str) -> str:
    return FILL_POLICY_LABELS.get(value, value)
