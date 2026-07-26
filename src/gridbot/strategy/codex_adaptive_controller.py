"""Pure, fail-closed route controller for Codex adaptive policy experiments.

This module deliberately owns no execution state.  It selects a route around an
already-selected executor action; an adaptive challenger can never replace that
action or its executor profile.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum
from hashlib import sha256
from json import dumps
from math import isfinite
from typing import Any, Mapping


class AdaptiveRoute(str, Enum):
    BLOCK = "BLOCK"
    OBSERVE_ONLY = "OBSERVE_ONLY"
    THIN_SCALP = "THIN_SCALP"
    NORMAL = "NORMAL"


class ExecutionQuality(str, Enum):
    EXECUTABLE = "EXECUTABLE"
    OBSERVE_ONLY = "OBSERVE_ONLY"
    RECOVERY = "RECOVERY"


class DecisionMode(str, Enum):
    INCUMBENT = "INCUMBENT"
    CHALLENGER_ROUTE = "CHALLENGER_ROUTE"
    FAIL_CLOSED = "FAIL_CLOSED"


@dataclass(frozen=True)
class ExecutorAction:
    """The immutable executor profile selected before adaptive routing."""

    action_id: str
    tp_bp: float
    executor_profile: str


@dataclass(frozen=True)
class AdaptiveControllerConfig:
    policy_version: str = "codex_adaptive_controller_v1.4.58"
    challenger_enabled: bool = False
    live_enforcement_enabled: bool = False
    live_enforced_market_states: tuple[str, ...] = (
        "CNL-WPR-L:deep_discount_stable",
    )
    blocked_tp_min_bp: float = 14.0
    thin_scalp_tp_max_bp: float = 10.0
    known_market_states: tuple[str, ...] = (
        "STUP-S:clean_extension",
        "STUP-S:mixed",
        "STUP-S:weak_chop",
        "STUP-S:no_momentum_edge",
        "STUP-S:hot_continuation",
        "STUP-S:counter_recoil",
        "STUP-S:near_vwap_flat",
        "STUP-S:stale_squeeze_top",
        "STUP-S:missing_features",
        "CNL-WPR-L",
        "CNL-WPR-L:deep_discount_stable",
        "CNL-WPR-L:discount_mixed",
        "CNL-WPR-L:falling_discount_trap",
        "CNL-WPR-L:fast_reclaim",
        "CNL-WPR-L:discount_delayed_reclaim",
        "CNL-WPR-L:falling_continuation_probe",
        "CNL-WPR-L:missing_features",
        "W1D:mixed",
        "S1P-L:ordinary_pullback_pre_vwap",
        "SFD-S:strong_down_continuation",
    )


@dataclass(frozen=True)
class AdaptiveControllerInput:
    adaptive_session_id: str
    symbol: str
    lane_code: str
    market_state: str
    side: str
    opportunity_bucket: int
    execution_quality: ExecutionQuality | str
    incumbent_accepted: bool
    incumbent_action: ExecutorAction | None
    challenger_route: AdaptiveRoute | str | None = None


@dataclass(frozen=True)
class AdaptiveDecisionEnvelope:
    adaptive_session_id: str
    policy_version: str
    config_sha256: str
    opportunity_id: str
    market_state: str
    execution_quality: ExecutionQuality | None
    incumbent_route: AdaptiveRoute
    incumbent_action: ExecutorAction | None
    challenger_route: AdaptiveRoute
    challenger_action: ExecutorAction | None
    selected_route: AdaptiveRoute
    selected_action: ExecutorAction | None
    live_effective_route: AdaptiveRoute
    live_effective_action: ExecutorAction | None
    enforcement_applied: bool
    live_gate_reason: str | None
    decision_mode: DecisionMode
    stop_reason: str | None


_ROUTE_RISK = {
    AdaptiveRoute.BLOCK: 0,
    AdaptiveRoute.OBSERVE_ONLY: 1,
    AdaptiveRoute.THIN_SCALP: 2,
    AdaptiveRoute.NORMAL: 3,
}


def deterministic_config_sha256(config: AdaptiveControllerConfig | Mapping[str, Any]) -> str:
    """Hash configuration with a canonical representation suitable for audit logs."""

    payload = _canonicalize(asdict(config) if is_dataclass(config) else config)
    encoded = dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return sha256(encoded.encode("utf-8")).hexdigest()


def build_opportunity_id(
    *,
    symbol: str,
    lane_code: str,
    market_state: str,
    side: str,
    action_id: str,
    opportunity_bucket: int,
) -> str:
    """Return a stable opportunity identity without run-specific state."""

    payload = _canonicalize(
        {
            "action_id": action_id,
            "lane_code": lane_code,
            "market_state": market_state,
            "opportunity_bucket": opportunity_bucket,
            "side": side,
            "symbol": symbol,
        }
    )
    encoded = dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return f"opp_{sha256(encoded.encode('utf-8')).hexdigest()[:20]}"


opportunity_id_for = build_opportunity_id


class CodexAdaptiveController:
    """Deterministic v1.4.58 route-only adaptive controller."""

    def __init__(self, config: AdaptiveControllerConfig | None = None) -> None:
        self._config = config or AdaptiveControllerConfig()
        self._config_sha256 = deterministic_config_sha256(self._config)

    @property
    def config(self) -> AdaptiveControllerConfig:
        return self._config

    @property
    def config_sha256(self) -> str:
        return self._config_sha256

    def decide(self, request: AdaptiveControllerInput) -> AdaptiveDecisionEnvelope:
        return decide_adaptive_route(request, self._config)


def decide_adaptive_route(
    request: AdaptiveControllerInput,
    config: AdaptiveControllerConfig | None = None,
) -> AdaptiveDecisionEnvelope:
    """Route one opportunity, failing closed for invalid or unknown inputs."""

    config = config or AdaptiveControllerConfig()
    config_sha = deterministic_config_sha256(config)
    quality = _coerce_execution_quality(request.execution_quality)
    action = request.incumbent_action
    opportunity_id = _request_opportunity_id(request)

    invalid_reason = _invalid_reason(request, config, quality)
    if invalid_reason:
        return _fail_closed(request, config, config_sha, opportunity_id, quality, invalid_reason)

    assert quality is not None
    assert action is not None
    incumbent_route, stop_reason = _incumbent_route(request, config, quality)
    challenger_route = incumbent_route
    selected_route = incumbent_route
    mode = DecisionMode.INCUMBENT

    requested_challenger_route = _coerce_route(request.challenger_route)
    if requested_challenger_route is None and request.challenger_route is not None:
        return _fail_closed(request, config, config_sha, opportunity_id, quality, "invalid_challenger_route")
    if (
        config.challenger_enabled
        and requested_challenger_route is not None
        and requested_challenger_route != incumbent_route
    ):
        # A challenger can reduce exposure, never relax a route selected by a
        # mandatory safety policy.  Its executor action remains the incumbent.
        challenger_route = min(
            incumbent_route,
            requested_challenger_route,
            key=lambda route: _ROUTE_RISK[route],
        )
        if challenger_route != incumbent_route:
            selected_route = challenger_route
            mode = DecisionMode.CHALLENGER_ROUTE
            stop_reason = "challenger_route_selected"

    selected_action = action if selected_route in {AdaptiveRoute.THIN_SCALP, AdaptiveRoute.NORMAL} else None
    enforcement_applied = bool(
        config.live_enforcement_enabled
        and request.market_state in config.live_enforced_market_states
        and mode is DecisionMode.CHALLENGER_ROUTE
        and selected_route in {AdaptiveRoute.BLOCK, AdaptiveRoute.OBSERVE_ONLY}
    )
    live_effective_route = selected_route if enforcement_applied else incumbent_route
    live_effective_action = (
        action if live_effective_route in {AdaptiveRoute.THIN_SCALP, AdaptiveRoute.NORMAL} else None
    )
    live_gate_reason = "v1458_cnl_wpr_deep_no_lane_canary_gate" if enforcement_applied else None
    return AdaptiveDecisionEnvelope(
        adaptive_session_id=request.adaptive_session_id,
        policy_version=config.policy_version,
        config_sha256=config_sha,
        opportunity_id=opportunity_id,
        market_state=request.market_state,
        execution_quality=quality,
        incumbent_route=incumbent_route,
        incumbent_action=action,
        challenger_route=challenger_route,
        challenger_action=action,
        selected_route=selected_route,
        selected_action=selected_action,
        live_effective_route=live_effective_route,
        live_effective_action=live_effective_action,
        enforcement_applied=enforcement_applied,
        live_gate_reason=live_gate_reason,
        decision_mode=mode,
        stop_reason=stop_reason,
    )


def _incumbent_route(
    request: AdaptiveControllerInput,
    config: AdaptiveControllerConfig,
    quality: ExecutionQuality,
) -> tuple[AdaptiveRoute, str | None]:
    if quality is ExecutionQuality.RECOVERY:
        return AdaptiveRoute.OBSERVE_ONLY, "recovery_observe_only"
    if quality is ExecutionQuality.OBSERVE_ONLY:
        return AdaptiveRoute.OBSERVE_ONLY, "execution_quality_observe_only"
    if not request.incumbent_accepted:
        return AdaptiveRoute.OBSERVE_ONLY, "not_live_accepted"

    action = request.incumbent_action
    assert action is not None
    if request.lane_code == "STUP-S" and request.market_state == "STUP-S:clean_extension":
        if action.tp_bp >= config.blocked_tp_min_bp:
            return AdaptiveRoute.BLOCK, "stups_clean_extension_tp14_loss_guard"
        if action.tp_bp <= config.thin_scalp_tp_max_bp:
            return AdaptiveRoute.THIN_SCALP, "stups_clean_extension_tp8_tp10_gate_pass"
        return AdaptiveRoute.NORMAL, "stups_clean_extension_non_tp14_profile"
    return AdaptiveRoute.NORMAL, "default_live_profile"


def _invalid_reason(
    request: AdaptiveControllerInput,
    config: AdaptiveControllerConfig,
    quality: ExecutionQuality | None,
) -> str | None:
    if not _valid_config(config):
        return "invalid_config"
    if not all(isinstance(value, str) and value.strip() for value in (request.adaptive_session_id, request.symbol, request.lane_code, request.market_state, request.side)):
        return "missing_required_data"
    if not isinstance(request.opportunity_bucket, int) or isinstance(request.opportunity_bucket, bool):
        return "invalid_opportunity_bucket"
    if request.market_state not in config.known_market_states:
        return "unknown_market_state"
    if quality is None:
        return "invalid_execution_quality"
    if not isinstance(request.incumbent_accepted, bool):
        return "invalid_incumbent_acceptance"
    if request.incumbent_action is None or not _valid_action(request.incumbent_action):
        return "invalid_incumbent_action"
    return None


def _fail_closed(
    request: AdaptiveControllerInput,
    config: AdaptiveControllerConfig,
    config_sha: str,
    opportunity_id: str,
    quality: ExecutionQuality | None,
    reason: str,
) -> AdaptiveDecisionEnvelope:
    action = request.incumbent_action if _valid_action(request.incumbent_action) else None
    return AdaptiveDecisionEnvelope(
        adaptive_session_id=request.adaptive_session_id,
        policy_version=config.policy_version,
        config_sha256=config_sha,
        opportunity_id=opportunity_id,
        market_state=request.market_state,
        execution_quality=quality,
        incumbent_route=AdaptiveRoute.BLOCK,
        incumbent_action=action,
        challenger_route=AdaptiveRoute.BLOCK,
        challenger_action=action,
        selected_route=AdaptiveRoute.BLOCK,
        selected_action=None,
        live_effective_route=AdaptiveRoute.BLOCK,
        live_effective_action=None,
        enforcement_applied=False,
        live_gate_reason=None,
        decision_mode=DecisionMode.FAIL_CLOSED,
        stop_reason=reason,
    )


def _request_opportunity_id(request: AdaptiveControllerInput) -> str:
    action_id = request.incumbent_action.action_id if _valid_action(request.incumbent_action) else "INVALID_ACTION"
    bucket = request.opportunity_bucket if isinstance(request.opportunity_bucket, int) and not isinstance(request.opportunity_bucket, bool) else -1
    return build_opportunity_id(
        symbol=request.symbol,
        lane_code=request.lane_code,
        market_state=request.market_state,
        side=request.side,
        action_id=action_id,
        opportunity_bucket=bucket,
    )


def _valid_config(config: AdaptiveControllerConfig) -> bool:
    return (
        isinstance(config.policy_version, str)
        and bool(config.policy_version.strip())
        and isinstance(config.challenger_enabled, bool)
        and isinstance(config.live_enforcement_enabled, bool)
        and isinstance(config.live_enforced_market_states, tuple)
        and all(isinstance(state, str) and state for state in config.live_enforced_market_states)
        and _finite_positive(config.blocked_tp_min_bp)
        and _finite_positive(config.thin_scalp_tp_max_bp)
        and config.thin_scalp_tp_max_bp < config.blocked_tp_min_bp
        and bool(config.known_market_states)
        and all(isinstance(state, str) and state for state in config.known_market_states)
    )


def _valid_action(action: ExecutorAction | None) -> bool:
    return bool(
        action
        and isinstance(action.action_id, str)
        and action.action_id.strip()
        and isinstance(action.executor_profile, str)
        and action.executor_profile.strip()
        and _finite_positive(action.tp_bp)
    )


def _finite_positive(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(float(value)) and float(value) > 0.0


def _coerce_execution_quality(value: ExecutionQuality | str) -> ExecutionQuality | None:
    try:
        return value if isinstance(value, ExecutionQuality) else ExecutionQuality(value)
    except (TypeError, ValueError):
        return None


def _coerce_route(value: AdaptiveRoute | str | None) -> AdaptiveRoute | None:
    if value is None:
        return None
    try:
        return value if isinstance(value, AdaptiveRoute) else AdaptiveRoute(value)
    except (TypeError, ValueError):
        return None


def _canonicalize(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return _canonicalize(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _canonicalize(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (tuple, list)):
        return [_canonicalize(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_canonicalize(item) for item in value)
    if isinstance(value, float) and not isfinite(value):
        raise ValueError("configuration cannot contain non-finite floats")
    return value
