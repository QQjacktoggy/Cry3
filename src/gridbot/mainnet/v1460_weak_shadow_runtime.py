"""Read-only v1.4.60 weak-STUP aggTrade first-touch shadow tracker.

The tracker observes public Binance aggregate trades only.  It has no order
submission, amendment, cancellation, position, or account methods.  A shadow
opportunity is not terminal until pagination proves coverage through the
relevant deadline.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
import inspect
import math
import time
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence

from src.gridbot.mainnet.v1460_lane_adaptive import STUP_WEAK_STATES, V1460_VERSION
from src.gridbot.utils.logging import get_logger


logger = get_logger(__name__)

LANE_CODE = "STUP-S"
STARTED_EVENT = "v1460_weak_stup_shadow_started"
OUTCOME_EVENT = "v1460_weak_stup_shadow_outcome"


class WeakShadowLabel(str, Enum):
    TP_FIRST = "TP_FIRST"
    SL_FIRST = "SL_FIRST"
    MAX_HOLD = "MAX_HOLD"
    NO_FILL = "NO_FILL"
    AMBIGUOUS = "AMBIGUOUS"
    DATA_INCOMPLETE = "DATA_INCOMPLETE"


class _FetchStatus(str, Enum):
    COMPLETE = "COMPLETE"
    CAPPED = "CAPPED"
    FAILED = "FAILED"
    MALFORMED = "MALFORMED"


@dataclass(frozen=True, slots=True)
class AggTrade:
    """Normalized Binance aggregate trade used by the pure replay helpers."""

    agg_trade_id: int
    time_ms: int
    price: float


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    """Explicit fee assumptions for the read-only counterfactual."""

    maker_fee_rate: float = 0.0
    taker_fee_rate: float = 0.0004

    def __post_init__(self) -> None:
        for name, value in (
            ("maker_fee_rate", self.maker_fee_rate),
            ("taker_fee_rate", self.taker_fee_rate),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not 0.0 <= float(value) < 1.0
            ):
                raise ValueError(f"{name} must be finite and in [0, 1)")


@dataclass(frozen=True, slots=True)
class FirstTouch:
    label: WeakShadowLabel
    trade: AggTrade | None
    terminal_time_ms: int


@dataclass(frozen=True, slots=True)
class _FetchProgress:
    status: _FetchStatus
    reason: str | None = None


OutcomeCallback = Callable[[Mapping[str, Any]], Any | Awaitable[Any]]


def _now_ms() -> int:
    return int(time.time() * 1000)


def _required_text(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _positive_float(name: str, value: Any) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0.0
    ):
        raise ValueError(f"{name} must be a positive finite number")
    return float(value)


def _nonnegative_int(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _setting_float(settings: Any, name: str, default: float) -> float:
    raw = getattr(settings, name, default)
    if raw is None:
        raw = default
    if (
        isinstance(raw, bool)
        or not isinstance(raw, (int, float))
        or not math.isfinite(float(raw))
    ):
        raise ValueError(f"{name} must be a finite number")
    return float(raw)


def _setting_int(
    settings: Any,
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw = getattr(settings, name, default)
    if raw is None:
        raw = default
    if isinstance(raw, bool) or not isinstance(raw, int) or not minimum <= raw <= maximum:
        raise ValueError(f"{name} must be an integer in [{minimum}, {maximum}]")
    return raw


def normalize_side(side: str) -> str:
    normalized = _required_text("side", side).upper()
    aliases = {"LONG": "BUY", "SHORT": "SELL"}
    normalized = aliases.get(normalized, normalized)
    if normalized not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")
    return normalized


def normalize_weak_market_state(market_state: str) -> str:
    raw = _required_text("market_state", market_state).lower()
    if ":" in raw:
        lane, state = raw.rsplit(":", 1)
        if lane.strip().upper().replace("_", "-") != LANE_CODE:
            raise ValueError("market_state lane prefix must be STUP-S")
    else:
        state = raw
    state = state.strip().replace("-", "_").replace(" ", "_")
    if state not in STUP_WEAK_STATES:
        allowed = ", ".join(sorted(STUP_WEAK_STATES))
        raise ValueError(f"market_state must be a weak STUP state: {allowed}")
    return f"{LANE_CODE}:{state}"


def _parse_agg_trade(row: Mapping[str, Any]) -> AggTrade:
    if not isinstance(row, Mapping):
        raise ValueError("aggTrade row must be a mapping")
    raw_id = row.get("a", row.get("id"))
    raw_time = row.get("T", row.get("time"))
    raw_price = row.get("p", row.get("price"))
    if isinstance(raw_id, bool) or isinstance(raw_time, bool) or isinstance(raw_price, bool):
        raise ValueError("aggTrade id, time, and price must be numeric")
    try:
        agg_trade_id = int(raw_id)
        time_ms = int(raw_time)
        price = float(raw_price)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("aggTrade id, time, and price must be numeric") from exc
    if agg_trade_id < 0 or time_ms < 0 or not math.isfinite(price) or price <= 0.0:
        raise ValueError("aggTrade id/time must be non-negative and price positive finite")
    return AggTrade(agg_trade_id=agg_trade_id, time_ms=time_ms, price=price)


def normalize_agg_trades(rows: Iterable[Mapping[str, Any]]) -> tuple[AggTrade, ...]:
    """Normalize, deterministically order, and de-duplicate aggTrades by id.

    Identical retries are collapsed.  Reusing an aggTrade id with different
    time or price is a data-quality error and must not be guessed through.
    """

    by_id: dict[int, AggTrade] = {}
    for row in rows:
        trade = _parse_agg_trade(row)
        existing = by_id.get(trade.agg_trade_id)
        if existing is not None and existing != trade:
            raise ValueError(f"conflicting aggTrade id {trade.agg_trade_id}")
        by_id[trade.agg_trade_id] = trade
    return tuple(sorted(by_id.values(), key=lambda trade: (trade.time_ms, trade.agg_trade_id)))


def find_maker_fill(
    trades: Sequence[AggTrade],
    *,
    side: str,
    entry_limit_price: float,
    entry_submitted_at_ms: int,
    entry_deadline_ms: int,
) -> AggTrade | None:
    """Return the first maker-touch trade within the inclusive entry window."""

    normalized_side = normalize_side(side)
    limit_price = _positive_float("entry_limit_price", entry_limit_price)
    submitted_ms = _nonnegative_int("entry_submitted_at_ms", entry_submitted_at_ms)
    deadline_ms = _nonnegative_int("entry_deadline_ms", entry_deadline_ms)
    if deadline_ms < submitted_ms:
        raise ValueError("entry_deadline_ms cannot precede entry_submitted_at_ms")
    for trade in sorted(trades, key=lambda item: (item.time_ms, item.agg_trade_id)):
        if not submitted_ms <= trade.time_ms <= deadline_ms:
            continue
        if normalized_side == "BUY" and trade.price <= limit_price:
            return trade
        if normalized_side == "SELL" and trade.price >= limit_price:
            return trade
    return None


def evaluate_first_touch(
    trades: Sequence[AggTrade],
    *,
    fill: AggTrade,
    side: str,
    tp_price: float,
    sl_price: float,
    outcome_deadline_ms: int,
) -> FirstTouch:
    """Classify TP/SL first touch after fill using timestamp ambiguity rules."""

    normalized_side = normalize_side(side)
    tp = _positive_float("tp_price", tp_price)
    sl = _positive_float("sl_price", sl_price)
    deadline_ms = _nonnegative_int("outcome_deadline_ms", outcome_deadline_ms)
    ordered = sorted(trades, key=lambda item: (item.time_ms, item.agg_trade_id))
    post_fill = [
        trade
        for trade in ordered
        if (trade.time_ms, trade.agg_trade_id) > (fill.time_ms, fill.agg_trade_id)
        and trade.time_ms <= deadline_ms
    ]

    index = 0
    while index < len(post_fill):
        timestamp = post_fill[index].time_ms
        same_timestamp: list[AggTrade] = []
        while index < len(post_fill) and post_fill[index].time_ms == timestamp:
            same_timestamp.append(post_fill[index])
            index += 1
        if normalized_side == "BUY":
            tp_hits = [trade for trade in same_timestamp if trade.price >= tp]
            sl_hits = [trade for trade in same_timestamp if trade.price <= sl]
        else:
            tp_hits = [trade for trade in same_timestamp if trade.price <= tp]
            sl_hits = [trade for trade in same_timestamp if trade.price >= sl]
        if tp_hits and sl_hits:
            first = min(tp_hits + sl_hits, key=lambda trade: trade.agg_trade_id)
            return FirstTouch(WeakShadowLabel.AMBIGUOUS, first, timestamp)
        if tp_hits:
            first = min(tp_hits, key=lambda trade: trade.agg_trade_id)
            return FirstTouch(WeakShadowLabel.TP_FIRST, first, timestamp)
        if sl_hits:
            first = min(sl_hits, key=lambda trade: trade.agg_trade_id)
            return FirstTouch(WeakShadowLabel.SL_FIRST, first, timestamp)
    return FirstTouch(WeakShadowLabel.MAX_HOLD, None, deadline_ms)


class V1460WeakStupShadowTracker:
    """Read-only tracker for one shadow sample per deduplicated opportunity."""

    def __init__(
        self,
        *,
        client: Any,
        repo: Any,
        settings: Any,
        on_outcome: OutcomeCallback,
        samples: dict[str, dict[str, Any]],
        started_groups: set[str],
        lock: asyncio.Lock,
    ) -> None:
        if not callable(on_outcome):
            raise ValueError("on_outcome must be callable")
        self._client = client
        self._repo = repo
        self._settings = settings
        self._on_outcome = on_outcome
        self._samples = samples
        self._started_groups = started_groups
        self._lock = lock

        maker_fee = _setting_float(
            settings,
            "mainnet_codex_v1460_weak_shadow_maker_fee_rate",
            0.0,
        )
        taker_default = _setting_float(
            settings,
            "mainnet_expected_taker_fee_rate",
            0.0004,
        )
        taker_fee = _setting_float(
            settings,
            "mainnet_codex_v1460_weak_shadow_taker_fee_rate",
            taker_default,
        )
        self._fees = FeeSchedule(maker_fee_rate=maker_fee, taker_fee_rate=taker_fee)
        self._max_pages = _setting_int(
            settings,
            "mainnet_codex_v1460_weak_shadow_max_pages",
            10,
            minimum=1,
            maximum=10_000,
        )
        self._page_limit = _setting_int(
            settings,
            "mainnet_codex_v1460_weak_shadow_page_limit",
            1_000,
            minimum=1,
            maximum=1_000,
        )
        self._max_fetch_failures = _setting_int(
            settings,
            "mainnet_codex_v1460_weak_shadow_max_fetch_failures",
            3,
            minimum=1,
            maximum=100,
        )

    async def start(
        self,
        *,
        run_id: str,
        session_id: str,
        opportunity_id: str,
        symbol: str,
        side: str,
        market_state: str,
        entry_limit_price: float,
        notional_usdc: float,
        tp_price: float,
        sl_price: float,
        entry_submitted_at_ms: int,
        entry_deadline_ms: int,
        outcome_deadline_ms: int,
    ) -> bool:
        """Start one weak-STUP shadow opportunity without touching an order API."""

        run = _required_text("run_id", run_id)
        session = _required_text("session_id", session_id)
        opportunity = _required_text("opportunity_id", opportunity_id)
        normalized_symbol = _required_text("symbol", symbol).upper()
        normalized_side = normalize_side(side)
        normalized_state = normalize_weak_market_state(market_state)
        entry = _positive_float("entry_limit_price", entry_limit_price)
        notional = _positive_float("notional_usdc", notional_usdc)
        tp = _positive_float("tp_price", tp_price)
        sl = _positive_float("sl_price", sl_price)
        submitted_ms = _nonnegative_int("entry_submitted_at_ms", entry_submitted_at_ms)
        entry_end_ms = _nonnegative_int("entry_deadline_ms", entry_deadline_ms)
        outcome_end_ms = _nonnegative_int("outcome_deadline_ms", outcome_deadline_ms)
        if not submitted_ms <= entry_end_ms <= outcome_end_ms:
            raise ValueError(
                "timestamps must satisfy entry_submitted_at_ms <= entry_deadline_ms "
                "<= outcome_deadline_ms"
            )
        if normalized_side == "BUY" and not sl < entry < tp:
            raise ValueError("BUY requires sl_price < entry_limit_price < tp_price")
        if normalized_side == "SELL" and not tp < entry < sl:
            raise ValueError("SELL requires tp_price < entry_limit_price < sl_price")

        opportunity_key = f"{session}:{opportunity}"
        sample: dict[str, Any] = {
            "sample_id": opportunity_key,
            "group_id": opportunity_key,
            "opportunity_key": opportunity_key,
            "run_id": run,
            "session_id": session,
            "opportunity_id": opportunity,
            "symbol": normalized_symbol,
            "side": normalized_side,
            "lane_code": LANE_CODE,
            "market_state": normalized_state,
            "entry_limit_price": entry,
            "notional_usdc": notional,
            "tp_price": tp,
            "sl_price": sl,
            "entry_submitted_at_ms": submitted_ms,
            "entry_deadline_ms": entry_end_ms,
            "outcome_deadline_ms": outcome_end_ms,
            "_trades": {},
            "_next_from_id": None,
            "_fetch_start_ms": submitted_ms,
            "_coverage_through_ms": submitted_ms - 1,
            "_fetch_calls": 0,
            "_fetch_failures": 0,
            "_last_data_quality_reason": None,
            "_last_fetch_capped": False,
            "_ever_fetch_capped": False,
            "_resolved": False,
        }
        started_payload = self._base_payload(sample)
        started_payload.update(
            {
                "data_quality": {
                    "status": "PENDING",
                    "complete": False,
                    "coverage_through_ms": submitted_ms - 1,
                    "required_deadline_ms": entry_end_ms,
                    "reason": None,
                },
                "cost_model": self._cost_model_payload(),
            }
        )

        async with self._lock:
            if opportunity_key in self._started_groups or opportunity_key in self._samples:
                return False
            self._started_groups.add(opportunity_key)
            self._samples[opportunity_key] = sample
            try:
                await self._repo.log_event(run, STARTED_EVENT, started_payload)
            except Exception:
                self._samples.pop(opportunity_key, None)
                self._started_groups.discard(opportunity_key)
                raise
        return True

    async def update(self) -> None:
        """Advance all samples using only ``client.get_agg_trades``."""

        if not self._samples:
            return
        callbacks: list[Mapping[str, Any]] = []
        async with self._lock:
            now_ms = _now_ms()
            for sample_id in list(self._samples):
                sample = self._samples.get(sample_id)
                if sample is None or sample.get("_resolved"):
                    continue
                payload = await self._process_sample(sample_id, sample, now_ms)
                if payload is not None:
                    callbacks.append(payload)

        callback_error: BaseException | None = None
        for payload in callbacks:
            try:
                result = self._on_outcome(payload)
                if inspect.isawaitable(result):
                    await result
            except BaseException as exc:  # callback was invoked; never invoke it twice
                logger.warning(
                    "v1460_weak_shadow_outcome_callback_failed",
                    opportunity_key=payload.get("opportunity_key"),
                    error=str(exc)[:200],
                )
                if callback_error is None:
                    callback_error = exc
        if callback_error is not None:
            raise callback_error

    async def _process_sample(
        self,
        sample_id: str,
        sample: dict[str, Any],
        now_ms: int,
    ) -> Mapping[str, Any] | None:
        entry_deadline = int(sample["entry_deadline_ms"])
        outcome_deadline = int(sample["outcome_deadline_ms"])
        entry_target = min(now_ms, entry_deadline)
        progress = await self._advance_to(sample, entry_target)
        if int(sample["_coverage_through_ms"]) < entry_target:
            if now_ms >= entry_deadline and self._must_terminalize_incomplete(sample, progress):
                return await self._resolve_data_incomplete(
                    sample_id,
                    sample,
                    required_deadline_ms=entry_deadline,
                    reason=progress.reason or "entry_window_not_covered",
                    fill=None,
                )
            return None
        if now_ms < entry_deadline:
            return None

        trades = self._ordered_sample_trades(sample)
        fill = find_maker_fill(
            trades,
            side=str(sample["side"]),
            entry_limit_price=float(sample["entry_limit_price"]),
            entry_submitted_at_ms=int(sample["entry_submitted_at_ms"]),
            entry_deadline_ms=entry_deadline,
        )
        if fill is None:
            outcome = self._build_no_fill_outcome(sample)
            return await self._resolve(
                sample_id,
                sample,
                outcome,
                required_deadline_ms=entry_deadline,
            )

        outcome_target = min(now_ms, outcome_deadline)
        progress = await self._advance_to(sample, outcome_target)
        if int(sample["_coverage_through_ms"]) < outcome_target:
            if now_ms >= outcome_deadline and self._must_terminalize_incomplete(sample, progress):
                return await self._resolve_data_incomplete(
                    sample_id,
                    sample,
                    required_deadline_ms=outcome_deadline,
                    reason=progress.reason or "outcome_window_not_covered",
                    fill=fill,
                )
            return None
        if now_ms < outcome_deadline:
            return None

        trades = self._ordered_sample_trades(sample)
        touch = evaluate_first_touch(
            trades,
            fill=fill,
            side=str(sample["side"]),
            tp_price=float(sample["tp_price"]),
            sl_price=float(sample["sl_price"]),
            outcome_deadline_ms=outcome_deadline,
        )
        outcome = self._build_filled_outcome(sample, trades, fill, touch)
        return await self._resolve(
            sample_id,
            sample,
            outcome,
            required_deadline_ms=outcome_deadline,
        )

    async def _advance_to(
        self,
        sample: dict[str, Any],
        target_ms: int,
    ) -> _FetchProgress:
        if int(sample["_coverage_through_ms"]) >= target_ms:
            return _FetchProgress(_FetchStatus.COMPLETE)
        getter = getattr(self._client, "get_agg_trades", None)
        if not callable(getter):
            sample["_fetch_failures"] = int(sample["_fetch_failures"]) + 1
            sample["_last_data_quality_reason"] = "client_missing_get_agg_trades"
            return _FetchProgress(_FetchStatus.FAILED, "client_missing_get_agg_trades")

        sample["_last_fetch_capped"] = False
        try:
            for page_index in range(self._max_pages):
                from_id_raw = sample.get("_next_from_id")
                from_id = int(from_id_raw) if from_id_raw is not None else None
                if from_id is None:
                    kwargs = {
                        "start_time": max(
                            int(sample["entry_submitted_at_ms"]),
                            int(sample["_fetch_start_ms"]),
                        ),
                        "end_time": target_ms,
                        "from_id": None,
                        "limit": self._page_limit,
                    }
                else:
                    kwargs = {
                        "start_time": None,
                        "end_time": None,
                        "from_id": from_id,
                        "limit": self._page_limit,
                    }
                raw_rows = list(
                    await getter(str(sample["symbol"]), **kwargs)
                    or []
                )
                sample["_fetch_calls"] = int(sample["_fetch_calls"]) + 1
                if not raw_rows:
                    sample["_coverage_through_ms"] = target_ms
                    if from_id is None:
                        sample["_fetch_start_ms"] = target_ms + 1
                    sample["_fetch_failures"] = 0
                    sample["_last_data_quality_reason"] = None
                    return _FetchProgress(_FetchStatus.COMPLETE)

                try:
                    parsed = normalize_agg_trades(raw_rows)
                    self._merge_trades(sample, parsed)
                except ValueError as exc:
                    sample["_last_data_quality_reason"] = str(exc)
                    return _FetchProgress(_FetchStatus.MALFORMED, str(exc))

                highest_id = max(trade.agg_trade_id for trade in parsed)
                if from_id is not None and highest_id < from_id:
                    reason = "aggTrade pagination did not advance"
                    sample["_last_data_quality_reason"] = reason
                    return _FetchProgress(_FetchStatus.MALFORMED, reason)
                sample["_next_from_id"] = highest_id + 1
                crossed_target = any(trade.time_ms > target_ms for trade in parsed)
                if crossed_target or len(raw_rows) < self._page_limit:
                    sample["_coverage_through_ms"] = target_ms
                    sample["_fetch_failures"] = 0
                    sample["_last_data_quality_reason"] = None
                    return _FetchProgress(_FetchStatus.COMPLETE)
                if page_index + 1 == self._max_pages:
                    sample["_last_fetch_capped"] = True
                    sample["_ever_fetch_capped"] = True
                    sample["_last_data_quality_reason"] = "pagination_cap"
                    return _FetchProgress(_FetchStatus.CAPPED, "pagination_cap")
        except Exception as exc:  # read-only telemetry failure must never imply an outcome
            sample["_fetch_failures"] = int(sample["_fetch_failures"]) + 1
            sample["_last_data_quality_reason"] = "aggTrade_fetch_failed"
            logger.warning(
                "v1460_weak_shadow_aggtrade_fetch_failed",
                opportunity_key=sample.get("opportunity_key"),
                error=str(exc)[:200],
            )
            return _FetchProgress(_FetchStatus.FAILED, "aggTrade_fetch_failed")
        return _FetchProgress(_FetchStatus.CAPPED, "pagination_cap")

    def _merge_trades(
        self,
        sample: dict[str, Any],
        trades: Sequence[AggTrade],
    ) -> None:
        stored: dict[int, AggTrade] = sample["_trades"]
        submitted_ms = int(sample["entry_submitted_at_ms"])
        outcome_deadline_ms = int(sample["outcome_deadline_ms"])
        for trade in trades:
            existing = stored.get(trade.agg_trade_id)
            if existing is not None and existing != trade:
                raise ValueError(f"conflicting aggTrade id {trade.agg_trade_id}")
            if submitted_ms <= trade.time_ms <= outcome_deadline_ms:
                stored[trade.agg_trade_id] = trade

    @staticmethod
    def _ordered_sample_trades(sample: Mapping[str, Any]) -> tuple[AggTrade, ...]:
        stored = sample.get("_trades") or {}
        return tuple(
            sorted(
                stored.values(),
                key=lambda trade: (trade.time_ms, trade.agg_trade_id),
            )
        )

    def _must_terminalize_incomplete(
        self,
        sample: Mapping[str, Any],
        progress: _FetchProgress,
    ) -> bool:
        if progress.status in {_FetchStatus.CAPPED, _FetchStatus.MALFORMED}:
            return True
        return int(sample.get("_fetch_failures") or 0) >= self._max_fetch_failures

    def _build_no_fill_outcome(self, sample: Mapping[str, Any]) -> dict[str, Any]:
        duration_ms = int(sample["entry_deadline_ms"]) - int(sample["entry_submitted_at_ms"])
        return {
            "first_touch_result": WeakShadowLabel.NO_FILL.value,
            "evaluable": True,
            "filled": False,
            "fill_trade_id": None,
            "fill_time_ms": None,
            "fill_trade_price": None,
            "touch_trade_id": None,
            "touch_time_ms": None,
            "touch_trade_price": None,
            "exit_price": None,
            "exit_liquidity": None,
            "mfe_bp": 0.0,
            "mae_bp": 0.0,
            "duration_ms": duration_ms,
            "gross_pnl_usdc": 0.0,
            "entry_fee_usdc": 0.0,
            "exit_fee_usdc": 0.0,
            "total_fee_usdc": 0.0,
            "net_pnl_usdc": 0.0,
            "ev_contribution_usdc": 0.0,
            "terminal_at_ms": int(sample["entry_deadline_ms"]),
        }

    def _build_filled_outcome(
        self,
        sample: Mapping[str, Any],
        trades: Sequence[AggTrade],
        fill: AggTrade,
        touch: FirstTouch,
    ) -> dict[str, Any]:
        label = touch.label
        if label is WeakShadowLabel.TP_FIRST:
            exit_price = float(sample["tp_price"])
            exit_fee_rate = self._fees.maker_fee_rate
            exit_liquidity = "MAKER"
        elif label is WeakShadowLabel.SL_FIRST:
            exit_price = float(sample["sl_price"])
            exit_fee_rate = self._fees.taker_fee_rate
            exit_liquidity = "TAKER"
        elif label is WeakShadowLabel.MAX_HOLD:
            through_deadline = [
                trade
                for trade in trades
                if (trade.time_ms, trade.agg_trade_id)
                >= (fill.time_ms, fill.agg_trade_id)
                and trade.time_ms <= int(sample["outcome_deadline_ms"])
            ]
            exit_price = (
                through_deadline[-1].price
                if through_deadline
                else float(sample["entry_limit_price"])
            )
            # Live MAX_HOLD is a hard-close path unless a separate maker
            # scratch/profit-lock happens to qualify.  Charge taker cost here
            # so promotion evidence does not depend on that optimistic fill.
            exit_fee_rate = self._fees.taker_fee_rate
            exit_liquidity = "TAKER"
        else:
            exit_price = None
            exit_fee_rate = None
            exit_liquidity = None

        metric_end_ms = touch.terminal_time_ms
        mfe_bp, mae_bp = self._mfe_mae_bp(sample, trades, fill, metric_end_ms)
        duration_ms = max(0, metric_end_ms - fill.time_ms)
        costs = self._filled_costs(sample, exit_price, exit_fee_rate)
        evaluable = label is not WeakShadowLabel.AMBIGUOUS
        return {
            "first_touch_result": label.value,
            "evaluable": evaluable,
            "filled": True,
            "fill_trade_id": fill.agg_trade_id,
            "fill_time_ms": fill.time_ms,
            "fill_trade_price": fill.price,
            "fill_age_ms": fill.time_ms - int(sample["entry_submitted_at_ms"]),
            "touch_trade_id": touch.trade.agg_trade_id if touch.trade else None,
            "touch_time_ms": touch.trade.time_ms if touch.trade else None,
            "touch_trade_price": touch.trade.price if touch.trade else None,
            "exit_price": exit_price,
            "exit_liquidity": exit_liquidity,
            "mfe_bp": mfe_bp,
            "mae_bp": mae_bp,
            "duration_ms": duration_ms,
            **costs,
            "ev_contribution_usdc": costs["net_pnl_usdc"] if evaluable else None,
            "terminal_at_ms": metric_end_ms,
        }

    def _filled_costs(
        self,
        sample: Mapping[str, Any],
        exit_price: float | None,
        exit_fee_rate: float | None,
    ) -> dict[str, float | None]:
        notional = float(sample["notional_usdc"])
        entry_price = float(sample["entry_limit_price"])
        entry_fee = notional * self._fees.maker_fee_rate
        if exit_price is None or exit_fee_rate is None:
            return {
                "gross_pnl_usdc": None,
                "entry_fee_usdc": entry_fee,
                "exit_fee_usdc": None,
                "total_fee_usdc": None,
                "net_pnl_usdc": None,
            }
        quantity = notional / entry_price
        if sample["side"] == "BUY":
            gross = quantity * (exit_price - entry_price)
        else:
            gross = quantity * (entry_price - exit_price)
        exit_fee = quantity * exit_price * exit_fee_rate
        total_fee = entry_fee + exit_fee
        return {
            "gross_pnl_usdc": gross,
            "entry_fee_usdc": entry_fee,
            "exit_fee_usdc": exit_fee,
            "total_fee_usdc": total_fee,
            "net_pnl_usdc": gross - total_fee,
        }

    @staticmethod
    def _mfe_mae_bp(
        sample: Mapping[str, Any],
        trades: Sequence[AggTrade],
        fill: AggTrade,
        terminal_time_ms: int,
    ) -> tuple[float, float]:
        entry = float(sample["entry_limit_price"])
        path_prices = [entry]
        path_prices.extend(
            trade.price
            for trade in trades
            if (trade.time_ms, trade.agg_trade_id)
            > (fill.time_ms, fill.agg_trade_id)
            and trade.time_ms <= terminal_time_ms
        )
        if sample["side"] == "BUY":
            favorable = [(price / entry - 1.0) * 10_000.0 for price in path_prices]
            adverse = [(1.0 - price / entry) * 10_000.0 for price in path_prices]
        else:
            favorable = [(1.0 - price / entry) * 10_000.0 for price in path_prices]
            adverse = [(price / entry - 1.0) * 10_000.0 for price in path_prices]
        return max(0.0, max(favorable)), max(0.0, max(adverse))

    async def _resolve_data_incomplete(
        self,
        sample_id: str,
        sample: dict[str, Any],
        *,
        required_deadline_ms: int,
        reason: str,
        fill: AggTrade | None,
    ) -> Mapping[str, Any]:
        entry_fee = (
            float(sample["notional_usdc"]) * self._fees.maker_fee_rate
            if fill is not None
            else 0.0
        )
        outcome = {
            "first_touch_result": WeakShadowLabel.DATA_INCOMPLETE.value,
            "evaluable": False,
            "filled": fill is not None,
            "fill_trade_id": fill.agg_trade_id if fill else None,
            "fill_time_ms": fill.time_ms if fill else None,
            "fill_trade_price": fill.price if fill else None,
            "touch_trade_id": None,
            "touch_time_ms": None,
            "touch_trade_price": None,
            "exit_price": None,
            "exit_liquidity": None,
            "mfe_bp": None,
            "mae_bp": None,
            "duration_ms": None,
            "gross_pnl_usdc": None,
            "entry_fee_usdc": entry_fee,
            "exit_fee_usdc": None,
            "total_fee_usdc": None,
            "net_pnl_usdc": None,
            "ev_contribution_usdc": None,
            "terminal_at_ms": required_deadline_ms,
        }
        return await self._resolve(
            sample_id,
            sample,
            outcome,
            required_deadline_ms=required_deadline_ms,
            data_quality_reason=reason,
        )

    async def _resolve(
        self,
        sample_id: str,
        sample: dict[str, Any],
        outcome: Mapping[str, Any],
        *,
        required_deadline_ms: int,
        data_quality_reason: str | None = None,
    ) -> Mapping[str, Any]:
        if sample.get("_resolved"):
            raise RuntimeError("shadow sample already resolved")
        label = str(outcome["first_touch_result"])
        complete = label != WeakShadowLabel.DATA_INCOMPLETE.value
        payload = {
            **self._base_payload(sample),
            **outcome,
            "cost_model": self._cost_model_payload(),
            "data_quality": self._data_quality_payload(
                sample,
                complete=complete,
                required_deadline_ms=required_deadline_ms,
                reason=data_quality_reason,
            ),
            "resolved_at_ms": _now_ms(),
        }
        await self._repo.log_event(str(sample["run_id"]), OUTCOME_EVENT, payload)
        sample["_resolved"] = True
        self._samples.pop(sample_id, None)
        return payload

    @staticmethod
    def _base_payload(sample: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "version": V1460_VERSION,
            "lane_code": LANE_CODE,
            "market_state": sample["market_state"],
            "shadow_only": True,
            "places_real_order": False,
            "opportunity_key": sample["opportunity_key"],
            "run_id": sample["run_id"],
            "session_id": sample["session_id"],
            "opportunity_id": sample["opportunity_id"],
            "symbol": sample["symbol"],
            "side": sample["side"],
            "entry_limit_price": sample["entry_limit_price"],
            "notional_usdc": sample["notional_usdc"],
            "tp_price": sample["tp_price"],
            "sl_price": sample["sl_price"],
            "entry_submitted_at_ms": sample["entry_submitted_at_ms"],
            "entry_deadline_ms": sample["entry_deadline_ms"],
            "outcome_deadline_ms": sample["outcome_deadline_ms"],
        }

    def _cost_model_payload(self) -> dict[str, Any]:
        return {
            "entry_liquidity": "MAKER",
            "tp_liquidity": "MAKER",
            "max_hold_liquidity": "TAKER",
            "sl_liquidity": "TAKER",
            "maker_fee_rate": self._fees.maker_fee_rate,
            "taker_fee_rate": self._fees.taker_fee_rate,
            "entry_fill_price_assumption": "LIMIT_PRICE",
            "tp_slippage_bp": 0.0,
            "sl_slippage_bp": 0.0,
        }

    @staticmethod
    def _data_quality_payload(
        sample: Mapping[str, Any],
        *,
        complete: bool,
        required_deadline_ms: int,
        reason: str | None,
    ) -> dict[str, Any]:
        return {
            "status": "COMPLETE" if complete else WeakShadowLabel.DATA_INCOMPLETE.value,
            "complete": complete,
            "coverage_through_ms": int(sample["_coverage_through_ms"]),
            "required_deadline_ms": required_deadline_ms,
            "pagination_capped": bool(sample.get("_last_fetch_capped")),
            "ever_pagination_capped": bool(sample.get("_ever_fetch_capped")),
            "fetch_calls": int(sample.get("_fetch_calls") or 0),
            "fetch_failures": int(sample.get("_fetch_failures") or 0),
            "unique_trade_count": len(sample.get("_trades") or {}),
            "reason": reason,
        }


# Discoverable aliases without expanding the runtime surface.
WeakStupAggTradeShadowTracker = V1460WeakStupShadowTracker
V1460WeakShadowTracker = V1460WeakStupShadowTracker


__all__ = [
    "AggTrade",
    "FeeSchedule",
    "FirstTouch",
    "V1460WeakShadowTracker",
    "V1460WeakStupShadowTracker",
    "WeakShadowLabel",
    "WeakStupAggTradeShadowTracker",
    "evaluate_first_touch",
    "find_maker_fill",
    "normalize_agg_trades",
    "normalize_side",
    "normalize_weak_market_state",
]
