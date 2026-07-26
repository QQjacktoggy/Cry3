"""Observation-only all-lane discovery payloads for v1.4.69 Phase A.

This module has no admission, sizing, promotion, or order authority.  It turns
one immutable market snapshot into a normalized opportunity plus the lane
candidates that the legacy first-match selector currently hides.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from src.gridbot.mainnet.v1459_regime_runtime import map_market_state
from src.gridbot.mainnet.v1469_adaptive_identity import canonical_sha256
from src.gridbot.strategy.codex_v1_live import (
    LANES,
    CodexV1Decision,
    CodexV1LaneMatch,
    match_all_codex_v1_lanes,
)


V1469_MATCHER_VERSION = "v1469.match-all.2"
V1469_FEATURE_SCHEMA = "v1469.lane-features.2"


def _band_contract(band: Any) -> dict[str, Any]:
    return {
        "feature": str(band.feature),
        "low": float(band.low),
        "high": float(band.high),
    }


def _lane_predicate_contract(lane: Any) -> dict[str, Any]:
    return {
        "lane_code": str(lane.lane_code),
        "lane": str(lane.name),
        "strategies": sorted(str(item) for item in lane.strategies),
        "side": str(lane.side),
        "bands": [_band_contract(item) for item in lane.bands],
        "feature_bands": [
            _band_contract(item) for item in lane.feature_bands
        ],
        "deny_rules": sorted(
            (
                {
                    "name": str(rule.name),
                    "bands": [_band_contract(item) for item in rule.bands],
                }
                for rule in lane.deny_rules
            ),
            key=lambda item: item["name"],
        ),
    }


def _matcher_contract() -> dict[str, Any]:
    return {
        "schema": V1469_MATCHER_VERSION,
        # Hash predicate semantics only.  Registry order, notes, sizing, entry
        # offsets, and the release label do not change discovery identity.
        "base_lanes": sorted(
            (_lane_predicate_contract(lane) for lane in LANES),
            key=lambda item: (item["lane_code"], item["side"]),
        ),
        "base_special_guards": {
            "HUE-L": "is_hot_up_extension",
            "S1P-L": "ordinary_pullback_pre_vwap",
        },
        "positive_special_builders": sorted([
            "build_stale_upmove_canary_decision",
            "build_strong_fall_follow_short_decision",
        ]),
        "async_route_boundary": sorted(["CNL-WPR-L"]),
    }


V1469_MATCHER_HASH = canonical_sha256(_matcher_contract())


@dataclass(frozen=True, slots=True)
class V1469LaneObservationBatch:
    """Repository-ready, compact observation payloads."""

    opportunity_id: str
    dedup_key: str
    opportunity: dict[str, Any]
    candidates: tuple[dict[str, Any], ...]


def _detail_state(
    decision: CodexV1Decision,
    features: Mapping[str, Any] | None = None,
) -> str:
    metrics = decision.metrics if isinstance(decision.metrics, Mapping) else {}
    feature_values = features if isinstance(features, Mapping) else {}
    for value in (
        feature_values.get("market_state"),
        feature_values.get("state"),
        feature_values.get("v1469_regime_market_state"),
        feature_values.get("v1469_regime_state"),
        decision.regime,
        metrics.get("market_state"),
        metrics.get("state"),
        feature_values.get("v1459_regime_state"),
        feature_values.get("v1459_regime_market_state"),
        metrics.get("v1459_regime_state"),
        metrics.get("v1459_regime_market_state"),
    ):
        text = str(value or "").strip()
        if text:
            return text
    return "UNKNOWN"


def _coarse_regime(detail_state: str) -> str:
    mapped = str(map_market_state(detail_state).value).upper()
    if mapped in {"TREND_UP", "TREND_DOWN", "RANGE", "SHOCK", "UNCERTAIN"}:
        return mapped
    return "UNCERTAIN"


def _causal_coarse_regime(
    detail_state: str,
    features: Mapping[str, Any],
    *decisions: CodexV1Decision,
) -> str:
    values: list[Any] = [
        features.get("v1469_regime_state"),
        features.get("v1469_regime_market_state"),
        features.get("v1459_regime_state"),
        features.get("v1459_regime_market_state"),
    ]
    for decision in decisions:
        metrics = (
            decision.metrics
            if isinstance(decision.metrics, Mapping)
            else {}
        )
        values.extend(
            (
                metrics.get("v1459_regime_state"),
                metrics.get("v1459_regime_market_state"),
            )
        )
    for value in values:
        normalized = str(value or "").strip().upper()
        if normalized in {
            "TREND_UP",
            "TREND_DOWN",
            "RANGE",
            "SHOCK",
            "UNCERTAIN",
        }:
            return normalized
    return _coarse_regime(detail_state)


def _safety_status(decision: CodexV1Decision) -> str:
    if decision.accepted:
        return "SAFE"
    reason = str(decision.reason or "").lower()
    if decision.missing_features or any(
        token in reason
        for token in ("missing", "incomplete", "identity", "feature")
    ):
        return "DATA_BLOCKED"
    hard_safety_tokens = (
        "kill_switch",
        "stale",
        "ownership",
        "open_position",
        "open_order",
        "preflight",
        "reconcile",
        "unsafe",
        "hard_",
        "direction_invalid",
        "side_mismatch",
        "loss_cap",
        "live_reopen",
    )
    if any(token in reason for token in hard_safety_tokens):
        return "HARD_BLOCK"
    # Disabled/research/lifecycle gates are not proof that the underlying lane
    # is unsafe.  Keep them out of the permanent hard-safety taxonomy.
    return "NOT_EVALUATED"


def _candidate_key(
    *,
    lane_code: str,
    effective_side: str,
    strategy: str,
) -> tuple[str, str, str]:
    return (
        str(lane_code or "").strip().upper(),
        str(effective_side or "").strip().upper(),
        str(strategy or "").strip(),
    )


def _match_set(
    matches: Sequence[CodexV1LaneMatch],
) -> list[str]:
    return sorted(
        {
            "|".join(
                _candidate_key(
                    lane_code=item.lane_code,
                    effective_side=item.side,
                    strategy=item.strategy,
                )
            )
            for item in matches
        }
    )


def _dedup_key(
    *,
    environment: str,
    symbol: str,
    run_id: str,
    observed_at_ms: int,
    bucket_seconds: int,
    matches: Sequence[CodexV1LaneMatch],
    selector_owner_lane: str,
    coarse_regime: str,
) -> str:
    bucket_ms = max(1, int(bucket_seconds)) * 1000
    bucket_start_ms = int(observed_at_ms) - (int(observed_at_ms) % bucket_ms)
    digest = canonical_sha256(
        {
            "schema": "v1469.market-opportunity-dedup.1",
            "environment": str(environment or "").strip().upper(),
            "symbol": str(symbol or "").strip().upper(),
            "source_run_id": str(run_id or "").strip(),
            "bucket_start_ms": bucket_start_ms,
            "match_set": _match_set(matches),
            "coarse_regime": str(coarse_regime or "").strip().upper(),
            "matcher_hash": V1469_MATCHER_HASH,
        }
    )
    return f"v1469d_{digest}"


def _opportunity_id(
    *,
    environment: str,
    symbol: str,
    run_id: str,
    observed_at_ms: int,
    feature_at_ms: int,
    feature_snapshot: Mapping[str, Any],
    matches: Sequence[CodexV1LaneMatch],
    selector_owner_lane: str,
) -> str:
    """Return an immutable snapshot identity, never a time-bucket identity."""

    # Legacy first-match ownership is route telemetry, not market identity.
    # Excluding it keeps opportunity identity invariant under registry reorder.
    identity_snapshot = {
        key: value
        for key, value in feature_snapshot.items()
        if key not in {"selector_owner_lane", "zero_match_reason"}
    }
    digest = canonical_sha256(
        {
            "schema": "v1469.market-opportunity-id.2",
            "environment": str(environment or "").strip().upper(),
            "symbol": str(symbol or "").strip().upper(),
            "source_run_id": str(run_id or "").strip(),
            "observed_at_ms": int(observed_at_ms),
            "feature_at_ms": int(feature_at_ms),
            "feature_snapshot_hash": canonical_sha256(identity_snapshot),
            "match_set": _match_set(matches),
            "matcher_hash": V1469_MATCHER_HASH,
        }
    )
    return f"v1469_{digest}"


def build_v1469_lane_observation(
    *,
    environment: str,
    run_id: str,
    observed_at_ms: int,
    bucket_seconds: int,
    features: Mapping[str, Any],
    feature_snapshot: Mapping[str, Any],
    selector_decision: CodexV1Decision,
    effective_decision: CodexV1Decision,
    feature_gaps: Sequence[str] = (),
    feature_at_ms: int | None = None,
) -> V1469LaneObservationBatch:
    """Build one all-lane observation without changing the legacy decision.

    ``selector_decision`` is the lane owner before disabled/research gates;
    ``effective_decision`` is the same route after those gates.  Keeping both
    makes a selected-then-blocked lane visible instead of silently starving
    compatible candidates behind it.
    """

    observed = int(observed_at_ms)
    if observed < 0:
        raise ValueError("observed_at_ms must be non-negative")
    bucket = int(bucket_seconds)
    if bucket <= 0:
        raise ValueError("bucket_seconds must be positive")

    symbol = str(
        features.get("symbol")
        or feature_snapshot.get("symbol")
        or ""
    ).strip().upper()
    strategy = str(
        features.get("strategy")
        or selector_decision.strategy
        or effective_decision.strategy
        or ""
    ).strip()
    side = str(
        features.get("side")
        or selector_decision.side
        or effective_decision.side
        or ""
    ).strip().upper()
    if not symbol or not strategy or side not in {"LONG", "SHORT"}:
        raise ValueError("symbol, strategy, and LONG/SHORT side are required")

    normalized_feature_gaps = {
        str(item).strip() for item in feature_gaps if str(item).strip()
    }
    feature_time_source = "explicit"
    if feature_at_ms is None:
        feature_age = features.get(
            "feature_age_seconds",
            feature_snapshot.get("feature_age_seconds"),
        )
        try:
            age_seconds = float(feature_age)
        except (TypeError, ValueError, OverflowError):
            age_seconds = math.nan
        if math.isfinite(age_seconds) and age_seconds >= 0:
            feature_at = max(0, observed - int(round(age_seconds * 1000)))
            feature_time_source = "feature_age_seconds"
        else:
            feature_at = observed
            feature_time_source = "observation_time_fallback"
            normalized_feature_gaps.add("feature_timestamp")
    else:
        feature_at = int(feature_at_ms)
    if feature_at < 0 or feature_at > observed:
        raise ValueError("feature_at_ms must be between 0 and observed_at_ms")
    normalized_feature_gaps_tuple = tuple(sorted(normalized_feature_gaps))

    matches = list(match_all_codex_v1_lanes(features))
    selector_owner_lane = str(
        selector_decision.lane_code
        or effective_decision.lane_code
        or ""
    ).strip().upper()
    selector_owner_name = str(
        selector_decision.lane
        or effective_decision.lane
        or selector_owner_lane
    ).strip()

    existing_keys = {
        _candidate_key(
            lane_code=item.lane_code,
            effective_side=item.side,
            strategy=item.strategy,
        )
        for item in matches
    }
    owner_key = _candidate_key(
        lane_code=selector_owner_lane,
        effective_side=str(selector_decision.side or effective_decision.side or side),
        strategy=str(selector_decision.strategy or effective_decision.strategy or strategy),
    )
    if selector_owner_lane and owner_key not in existing_keys:
        matches.insert(
            0,
            CodexV1LaneMatch(
                lane=selector_owner_name,
                lane_code=selector_owner_lane,
                side=owner_key[1],
                strategy=owner_key[2],
                regime=_detail_state(selector_decision, features),
                annotations=(
                    "selector_owner_outside_pure_matcher",
                    (
                        "async_route_boundary:cnl_wpr"
                        if selector_owner_lane == "CNL-WPR-L"
                        else "positive_special_route"
                    ),
                ),
            ),
        )

    matches.sort(
        key=lambda item: _candidate_key(
            lane_code=item.lane_code,
            effective_side=item.side,
            strategy=item.strategy,
        )
    )

    detail_state = _detail_state(effective_decision, features)
    if detail_state == "UNKNOWN":
        detail_state = _detail_state(selector_decision, features)
    coarse_regime = _causal_coarse_regime(
        detail_state,
        features,
        effective_decision,
        selector_decision,
    )
    dedup_key = _dedup_key(
        environment=environment,
        symbol=symbol,
        run_id=run_id,
        observed_at_ms=observed,
        bucket_seconds=bucket,
        matches=matches,
        selector_owner_lane=selector_owner_lane,
        coarse_regime=coarse_regime,
    )

    snapshot = dict(feature_snapshot)
    zero_match_reason = None
    if not matches:
        zero_match_reason = (
            "FEATURES_INCOMPLETE"
            if normalized_feature_gaps_tuple
            else str(
                effective_decision.reason
                or selector_decision.reason
                or "NO_PREDICATE_MATCH"
            ).strip()
        )
    snapshot.update(
        {
            "market_state": detail_state,
            "coarse_regime": coarse_regime,
            "selector_owner_lane": selector_owner_lane or None,
            "matched_lane_codes": sorted(
                {str(item.lane_code).upper() for item in matches}
            ),
            "matcher_version": V1469_MATCHER_VERSION,
            "matcher_hash": V1469_MATCHER_HASH,
            "feature_at_ms": feature_at,
            "feature_time_source": feature_time_source,
            "feature_gaps": list(normalized_feature_gaps_tuple),
            "zero_match_reason": zero_match_reason,
        }
    )
    opportunity_id = _opportunity_id(
        environment=environment,
        symbol=symbol,
        run_id=run_id,
        observed_at_ms=observed,
        feature_at_ms=feature_at,
        feature_snapshot=snapshot,
        matches=matches,
        selector_owner_lane=selector_owner_lane,
    )
    opportunity = {
        "opportunity_id": opportunity_id,
        "environment": str(environment or "").strip().upper(),
        "symbol": symbol,
        "observed_at_ms": observed,
        "feature_at_ms": feature_at,
        "coarse_regime": coarse_regime,
        "regime_confidence": None,
        "feature_schema": V1469_FEATURE_SCHEMA,
        "feature_snapshot": snapshot,
        "source_run_id": str(run_id or "").strip() or None,
        # Persist the bucket identity so scheduler retries and process restarts
        # cannot create a second statistical opportunity for the same match
        # set/regime bucket.
        "source_event_id": dedup_key,
        "data_quality": (
            "COMPLETE"
            if not normalized_feature_gaps_tuple
            else "DATA_INCOMPLETE"
        ),
        "created_at_ms": observed,
    }

    effective_owner_status = _safety_status(effective_decision)
    global_block_reason = (
        str(effective_decision.reason or "").strip()
        if not selector_owner_lane and not effective_decision.accepted
        else ""
    )
    candidates: list[dict[str, Any]] = []
    for rank, match in enumerate(matches):
        lane_code = str(match.lane_code).strip().upper()
        is_selected = bool(selector_owner_lane and lane_code == selector_owner_lane)
        if normalized_feature_gaps_tuple:
            safety_status = "DATA_BLOCKED"
        elif is_selected:
            safety_status = effective_owner_status
        else:
            # A lane hidden behind first-match has not traversed its own
            # lane-specific safety/research gates.  Never call it SAFE yet.
            safety_status = "NOT_EVALUATED"
        if is_selected:
            suppression_reason = None
            suppressed_by = None
        elif selector_owner_lane:
            suppression_reason = "LEGACY_FIRST_MATCH"
            suppressed_by = selector_owner_lane
        elif global_block_reason:
            suppression_reason = f"GLOBAL_VETO:{global_block_reason}"
            suppressed_by = None
        else:
            suppression_reason = "UNRESOLVED_SELECTOR"
            suppressed_by = None
        if is_selected and effective_decision.accepted:
            route_status = "SELECTED"
        elif is_selected and safety_status in {"HARD_BLOCK", "DATA_BLOCKED"}:
            route_status = "SELECTED_BLOCKED"
        elif is_selected:
            route_status = (
                "DISABLED"
                if str(effective_decision.reason or "")
                == "codex_v1_lane_disabled"
                else "SOFT_SHADOW"
            )
        elif selector_owner_lane:
            route_status = "SUPPRESSED"
        elif global_block_reason:
            route_status = "GLOBAL_VETO"
        else:
            route_status = "UNSELECTED"

        candidates.append(
            {
                "opportunity_id": opportunity_id,
                "lane_code": lane_code,
                "effective_side": str(match.side).strip().upper(),
                "strategy": str(match.strategy).strip(),
                "match_status": "MATCH",
                "safety_status": safety_status,
                "is_selected": is_selected,
                "selection_rank": rank,
                "suppression_reason": suppression_reason,
                "suppressed_by_lane_code": suppressed_by,
                "matcher_version": V1469_MATCHER_VERSION,
                "matcher_hash": V1469_MATCHER_HASH,
                "data_complete": not bool(normalized_feature_gaps_tuple),
                "annotations": {
                    "lane_name": match.lane,
                    "matcher_annotations": list(match.annotations),
                    "feature_gaps": list(normalized_feature_gaps_tuple),
                    "match_regime": match.regime,
                    "market_state": detail_state,
                    "coarse_regime": coarse_regime,
                    "selector_owner": is_selected,
                    "route_status": route_status,
                    "selector_reason": selector_decision.reason,
                    "effective_reason": effective_decision.reason,
                    "observation_stage": (
                        "post_disabled_research_pre_execution_guards"
                    ),
                    "observation_only": True,
                    "order_api_calls": 0,
                },
                "created_at_ms": observed,
            }
        )

    return V1469LaneObservationBatch(
        opportunity_id=opportunity_id,
        dedup_key=dedup_key,
        opportunity=opportunity,
        candidates=tuple(candidates),
    )


__all__ = [
    "V1469_FEATURE_SCHEMA",
    "V1469_MATCHER_HASH",
    "V1469_MATCHER_VERSION",
    "V1469LaneObservationBatch",
    "build_v1469_lane_observation",
]
