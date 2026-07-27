"""Durable paired-shadow orchestration for the v1.4.69 arm framework.

This module has no order API and no paid-admission authority.  It starts every
tradable profile for every matched candidate as one atomic bundle, evaluates
each candidate against one shared aggTrade envelope, and terminalizes the
paired results atomically.  Pending rows can be reconstructed after restart.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from math import ceil, isclose, isfinite
import time
from typing import Any, Mapping, Sequence

from src.gridbot.mainnet.v1469_adaptive_identity import (
    EXECUTION_PROFILE_SCHEMA,
    MarketStateIdentity,
)
from src.gridbot.mainnet.v1469_arm_profiles import (
    ArmProfileDefinition,
    get_arm_profile,
    profiles_for_matched_candidate,
)
from src.gridbot.mainnet.v1469_legacy_control import LEGACY_CONTROL, LegacyExecutionSnapshot
from src.gridbot.mainnet.v1469_arbiter_evidence_mapper import (
    PAIRED_CONTRACT_SCHEMA,
    paired_group_identity,
)
from src.gridbot.mainnet.v1469_paired_evaluator import (
    AggTradePathTick,
    MatchedArmOpportunity,
    ShadowCostModel,
    TickEnvelope,
    evaluate_paired_arms,
)
from src.gridbot.storage.v1469_arm_observation_repository import (
    V1469ArmObservationRepository,
    candidate_identity,
)


@dataclass(frozen=True, slots=True)
class PendingPairedCandidate:
    candidate_id: str
    opportunity_id: str
    source_run_id: str
    symbol: str
    observed_at_ms: int
    max_deadline_ms: int
    opportunity: MatchedArmOpportunity
    evidence_by_profile: tuple[tuple[str, str], ...]

    @property
    def evidence_map(self) -> dict[str, str]:
        return dict(self.evidence_by_profile)


def _float(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("signal_reference_price must be finite and positive") from exc
    if not isfinite(parsed) or parsed <= 0.0:
        raise ValueError("signal_reference_price must be finite and positive")
    return parsed


def _market_identity(
    *,
    opportunity: Mapping[str, Any],
    candidate: Mapping[str, Any],
    snapshot: Mapping[str, Any],
) -> MarketStateIdentity:
    coarse_regime = str(opportunity.get("coarse_regime") or "").strip().upper()
    if coarse_regime in {"TREND", "UNKNOWN"}:
        coarse_regime = "UNCERTAIN"
    market_state = str(
        snapshot.get("market_state") or coarse_regime or "UNCERTAIN"
    ).strip()
    return MarketStateIdentity(
        environment=str(opportunity.get("environment") or "").strip().upper(),
        symbol=str(opportunity.get("symbol") or "").strip().upper(),
        lane_code=str(candidate.get("lane_code") or "").strip().upper(),
        effective_side=str(
            candidate.get("effective_side") or ""
        ).strip().upper(),
        strategy=str(candidate.get("strategy") or "").strip(),
        coarse_regime=coarse_regime,
        market_state=market_state,
    )


def _tradable_profiles(
    identity: MarketStateIdentity,
    candidate_status: str,
    legacy_profile: ArmProfileDefinition | None = None,
) -> tuple[ArmProfileDefinition, ...]:
    adaptive = tuple(
        profile
        for profile in profiles_for_matched_candidate(
            identity, candidate_status
        )
        if profile.execution_profile is not None
    )
    return ((legacy_profile,) + adaptive) if legacy_profile is not None else adaptive


def _profile_for_group(group: PendingPairedCandidate, profile_id: str) -> ArmProfileDefinition:
    legacy = group.opportunity.legacy_profile
    if legacy is not None and legacy.profile_id == profile_id:
        return legacy
    return get_arm_profile(profile_id)


def _max_deadline_ms(
    observed_at_ms: int,
    profiles: Sequence[ArmProfileDefinition],
) -> int:
    horizons = [
        profile.execution_profile.entry_ttl_s
        + profile.execution_profile.max_hold_s
        for profile in profiles
        if profile.execution_profile is not None
    ]
    if not horizons:
        return observed_at_ms
    return observed_at_ms + max(horizons) * 1_000


class V1469PairedShadowRuntime:
    """Bounded in-memory coordinator backed by the append-only repository."""

    def __init__(
        self,
        repository: V1469ArmObservationRepository,
        *,
        max_active_evidence: int = 2_048,
    ) -> None:
        if not isinstance(repository, V1469ArmObservationRepository):
            raise TypeError(
                "repository must be V1469ArmObservationRepository"
            )
        if int(max_active_evidence) <= 0:
            raise ValueError("max_active_evidence must be positive")
        self._repository = repository
        self._max_active_evidence = int(max_active_evidence)
        self._start_lock = asyncio.Lock()
        self._active: dict[str, PendingPairedCandidate] = {}
        self._rehydrated_runs: set[str] = set()

    @property
    def active_candidate_count(self) -> int:
        return len(self._active)

    @property
    def active_evidence_count(self) -> int:
        return sum(
            len(group.evidence_by_profile)
            for group in self._active.values()
        )

    async def start_observation(
        self,
        opportunity: Mapping[str, Any],
        candidates: Sequence[Mapping[str, Any]],
    ) -> dict[str, int]:
        """Serialize durable start so capacity and active ownership are exact."""

        async with self._start_lock:
            return await self._start_observation_unlocked(
                opportunity,
                candidates,
            )

    async def _start_observation_unlocked(
        self,
        opportunity: Mapping[str, Any],
        candidates: Sequence[Mapping[str, Any]],
    ) -> dict[str, int]:
        """Start all eligible matched candidates as one durable bundle."""

        legacy_snapshot = opportunity.get("legacy_execution_snapshot")
        if legacy_snapshot is not None and not isinstance(
            legacy_snapshot, LegacyExecutionSnapshot
        ):
            raise TypeError(
                "legacy_execution_snapshot must be LegacyExecutionSnapshot"
            )
        exact_requested = legacy_snapshot is not None
        if str(opportunity.get("data_quality") or "").upper() != "COMPLETE":
            if exact_requested:
                raise ValueError(
                    "legacy control requires COMPLETE durable opportunity"
                )
            return {
                "candidates_started": 0,
                "evidence_started": 0,
                "skipped": len(candidates),
                "capacity_dropped": 0,
            }
        snapshot_value = opportunity.get("feature_snapshot")
        snapshot = (
            dict(snapshot_value)
            if isinstance(snapshot_value, Mapping)
            else {}
        )
        try:
            signal_price = _float(snapshot.get("signal_reference_price"))
        except ValueError as exc:
            if exact_requested:
                raise ValueError(
                    "legacy control requires durable signal reference price"
                ) from exc
            return {
                "candidates_started": 0,
                "evidence_started": 0,
                "skipped": len(candidates),
                "capacity_dropped": 0,
            }

        group_specs: list[
            tuple[
                str,
                MatchedArmOpportunity,
                tuple[ArmProfileDefinition, ...],
            ]
        ] = []
        payloads: list[dict[str, Any]] = []
        skipped = 0
        observed_at_ms = int(opportunity.get("observed_at_ms") or 0)
        if (
            legacy_snapshot is not None
            and not isclose(
                signal_price,
                float(legacy_snapshot.reference_price),
                rel_tol=1e-12,
                abs_tol=1e-12,
            )
        ):
            raise ValueError(
                "legacy control reference price does not match durable opportunity"
            )
        legacy_matches = 0
        for candidate in candidates:
            status = str(
                candidate.get("safety_status") or ""
            ).strip().upper()
            if (
                str(candidate.get("match_status") or "").upper() != "MATCH"
                or status not in {"SAFE", "NOT_EVALUATED"}
                or not bool(candidate.get("data_complete"))
            ):
                skipped += 1
                continue
            candidate_id = candidate_identity(candidate)
            if candidate_id in self._active:
                skipped += 1
                continue
            try:
                market_identity = _market_identity(
                    opportunity=opportunity,
                    candidate=candidate,
                    snapshot=snapshot,
                )
                legacy_profile = None
                if (legacy_snapshot is not None and legacy_snapshot.market_identity == market_identity):
                    legacy_matches += 1
                    legacy_profile = legacy_snapshot.profile_definition
                profiles = _tradable_profiles(market_identity, status, legacy_profile)
            except (TypeError, ValueError):
                skipped += 1
                continue
            if not profiles:
                skipped += 1
                continue
            arm_opportunity = MatchedArmOpportunity(
                opportunity_id=str(opportunity.get("opportunity_id") or ""),
                candidate_status=status,
                market_identity=market_identity,
                signal_price=signal_price,
                legacy_profile=legacy_profile,
            )
            group_specs.append((candidate_id, arm_opportunity, profiles))
            for profile in profiles:
                execution = profile.execution_profile
                if execution is None:
                    continue
                payloads.append(
                    {
                        "opportunity_id": arm_opportunity.opportunity_id,
                        "candidate_id": candidate_id,
                        "execution_profile_id": profile.profile_id,
                        "execution_profile_schema": EXECUTION_PROFILE_SCHEMA,
                        "execution_profile_hash": execution.profile_hash,
                        "source_type": "SHADOW",
                        "diagnostic_only": False,
                        "observed_at_ms": observed_at_ms,
                        "created_at_ms": observed_at_ms,
                        "execution_profile_payload": (
                            legacy_snapshot.to_payload()
                            if profile.profile_id == LEGACY_CONTROL
                            and legacy_snapshot is not None
                            else None
                        ),
                    }
                )

        if legacy_snapshot is not None and legacy_matches != 1:
            raise ValueError(
                "legacy control must match exactly one durable candidate"
            )
        if not payloads:
            return {
                "candidates_started": 0,
                "evidence_started": 0,
                "skipped": skipped,
                "capacity_dropped": 0,
            }
        durable = await self._repository.append_evidence_bundle(payloads)
        # Active capacity is in-memory only; evidence must survive a restart.
        if (
            self.active_evidence_count + len(payloads)
            > self._max_active_evidence
        ):
            # The evidence bundle (including LEGACY_CONTROL sidecar) is already
            # durable.  Invalidate prior rehydrate scans so this same runtime
            # will retry once older active groups release memory capacity.
            environment = str(
                opportunity.get("environment") or ""
            ).strip().upper()
            symbol = str(opportunity.get("symbol") or "").strip().upper()
            source_run_id = str(
                opportunity.get("source_run_id") or ""
            ).strip()
            self._rehydrated_runs.discard(
                f"scope:{environment}:{symbol}"
            )
            if source_run_id:
                self._rehydrated_runs.discard(f"run:{source_run_id}")
            return {
                "candidates_started": 0,
                "evidence_started": len(payloads),
                "skipped": skipped,
                "capacity_dropped": len(payloads),
            }

        durable_by_candidate: dict[str, dict[str, str]] = {}
        for row in durable["evidence"]:
            durable_by_candidate.setdefault(
                str(row["candidate_id"]), {}
            )[str(row["execution_profile_id"])] = str(row["evidence_id"])

        started = 0
        source_run_id = str(
            opportunity.get("source_run_id") or ""
        ).strip()
        symbol = str(opportunity.get("symbol") or "").strip().upper()
        for candidate_id, arm_opportunity, profiles in group_specs:
            evidence_map = durable_by_candidate.get(candidate_id, {})
            expected_profile_ids = {
                profile.profile_id for profile in profiles
            }
            if set(evidence_map) != expected_profile_ids:
                raise RuntimeError(
                    "durable paired evidence profile set is incomplete"
                )
            self._active[candidate_id] = PendingPairedCandidate(
                candidate_id=candidate_id,
                opportunity_id=arm_opportunity.opportunity_id,
                source_run_id=source_run_id,
                symbol=symbol,
                observed_at_ms=observed_at_ms,
                max_deadline_ms=_max_deadline_ms(
                    observed_at_ms, profiles
                ),
                opportunity=arm_opportunity,
                evidence_by_profile=tuple(sorted(evidence_map.items())),
            )
            started += 1
        return {
            "candidates_started": started,
            "evidence_started": len(payloads),
            "skipped": skipped,
            "capacity_dropped": 0,
        }

    async def rehydrate_run(
        self,
        *,
        environment: str,
        symbol: str,
        source_run_id: str | None,
        observed_after_ms: int = 0,
    ) -> dict[str, int]:
        """Rebuild pending groups once per run, or the whole recent scope."""

        run_id = (
            None
            if source_run_id is None
            else str(source_run_id or "").strip()
        )
        if source_run_id is not None and not run_id:
            return {"groups": 0, "evidence": 0, "invalid": 0}
        marker = (
            f"scope:{str(environment).upper()}:{str(symbol).upper()}"
            if run_id is None
            else f"run:{run_id}"
        )
        if marker in self._rehydrated_runs:
            return {"groups": 0, "evidence": 0, "invalid": 0}
        pending_total = await self._repository.count_pending_evidence(
            environment=environment,
            symbol=symbol,
            source_run_id=run_id,
            observed_after_ms=max(0, int(observed_after_ms)),
        )
        rows = await self._repository.list_pending_evidence(
            environment=environment,
            symbol=symbol,
            source_run_id=run_id,
            observed_after_ms=max(0, int(observed_after_ms)),
            limit=self._max_active_evidence,
        )
        grouped: dict[str, list[Mapping[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(str(row["candidate_id"]), []).append(row)

        restored = 0
        restored_evidence = 0
        invalid = 0
        capacity_exhausted = False
        invalid_drop_failed = False
        for candidate_id, members in grouped.items():
            if candidate_id in self._active:
                continue
            first = members[0]
            snapshot_value = first.get("feature_snapshot")
            snapshot = (
                dict(snapshot_value)
                if isinstance(snapshot_value, Mapping)
                else {}
            )
            try:
                identity = _market_identity(
                    opportunity=first,
                    candidate={
                        "lane_code": first.get("lane_code"),
                        "effective_side": first.get("effective_side"),
                        "strategy": first.get("strategy"),
                    },
                    snapshot=snapshot,
                )
                status = str(first.get("candidate_status") or "").upper()
                legacy_members = [member for member in members if str(
                    member["execution_profile_id"]).upper() == LEGACY_CONTROL]
                legacy_snapshot = None
                if legacy_members:
                    if len(legacy_members) != 1:
                        raise ValueError("duplicate durable legacy profile")
                    legacy_snapshot = LegacyExecutionSnapshot.from_payload(
                        legacy_members[0].get("execution_profile_payload")
                    )
                    if legacy_snapshot.market_identity != identity:
                        raise ValueError("durable legacy market identity mismatch")
                signal_price = _float(
                    snapshot.get("signal_reference_price")
                )
                if (
                    legacy_snapshot is not None
                    and not isclose(
                        signal_price,
                        float(legacy_snapshot.reference_price),
                        rel_tol=1e-12,
                        abs_tol=1e-12,
                    )
                ):
                    raise ValueError(
                        "durable legacy reference price mismatch"
                    )
                arm_opportunity = MatchedArmOpportunity(
                    opportunity_id=str(first["opportunity_id"]),
                    candidate_status=status,
                    market_identity=identity,
                    signal_price=signal_price,
                    legacy_profile=(legacy_snapshot.profile_definition
                                    if legacy_snapshot is not None else None),
                )
                expected_profiles = {
                    profile.profile_id: profile
                    for profile in _tradable_profiles(
                        identity, status,
                        legacy_snapshot.profile_definition
                        if legacy_snapshot is not None else None,
                    )
                }
                evidence_map: dict[str, str] = {}
                for member in members:
                    profile_id = str(
                        member["execution_profile_id"]
                    ).upper()
                    profile = (legacy_snapshot.profile_definition
                               if profile_id == LEGACY_CONTROL and legacy_snapshot is not None
                               else get_arm_profile(profile_id))
                    execution = profile.execution_profile
                    if (
                        profile_id not in expected_profiles
                        or execution is None
                        or member["execution_profile_schema"]
                        != EXECUTION_PROFILE_SCHEMA
                        or member["execution_profile_hash"]
                        != execution.profile_hash
                    ):
                        raise ValueError(
                            "pending execution-profile identity mismatch"
                        )
                    evidence_map[profile_id] = str(member["evidence_id"])
                if set(evidence_map) != set(expected_profiles):
                    raise ValueError(
                        "pending paired profile set is incomplete"
                    )
                observed_at_ms = int(first["observed_at_ms"])
                profiles = tuple(expected_profiles.values())
                if (
                    self.active_evidence_count + len(evidence_map)
                    > self._max_active_evidence
                ):
                    capacity_exhausted = True
                    break
                self._active[candidate_id] = PendingPairedCandidate(
                    candidate_id=candidate_id,
                    opportunity_id=str(first["opportunity_id"]),
                    source_run_id=str(
                        first.get("source_run_id") or ""
                    ).strip(),
                    symbol=str(first["symbol"]).upper(),
                    observed_at_ms=observed_at_ms,
                    max_deadline_ms=_max_deadline_ms(
                        observed_at_ms, profiles
                    ),
                    opportunity=arm_opportunity,
                    evidence_by_profile=tuple(
                        sorted(evidence_map.items())
                    ),
                )
                restored += 1
                restored_evidence += len(evidence_map)
            except (KeyError, TypeError, ValueError) as exc:
                invalid += len(members)
                terminal_at_ms = max(
                    int(time.time() * 1000),
                    max(
                        int(member.get("observed_at_ms") or 0)
                        for member in members
                    ),
                )
                try:
                    await self._repository.terminal_evidence_bundle(
                        [
                            {
                                "evidence_id": str(
                                    member["evidence_id"]
                                ),
                                "terminal": {
                                    "status": "DROPPED",
                                    "terminal_at_ms": terminal_at_ms,
                                    "outcome": "data_incomplete",
                                    "fill_status": "UNKNOWN",
                                    "data_complete": False,
                                    "ambiguous": False,
                                    "reward_net_bp": None,
                                    "mfe_bp": None,
                                    "mae_bp": None,
                                    "terminal_reason": (
                                        "REHYDRATE_IDENTITY_INVALID"
                                    ),
                                    "terminal_payload": {
                                        "schema": (
                                            "v1469.rehydrate-drop.1"
                                        ),
                                        "candidate_id": candidate_id,
                                        "reason": str(exc)[:300],
                                    },
                                    "updated_at_ms": terminal_at_ms,
                                },
                            }
                            for member in members
                        ]
                    )
                except Exception:
                    invalid_drop_failed = True
        truncated = len(rows) < pending_total
        if not (
            truncated or capacity_exhausted or invalid_drop_failed
        ):
            self._rehydrated_runs.add(marker)
        return {
            "groups": restored,
            "evidence": restored_evidence,
            "invalid": invalid,
        }

    def cache_samples(
        self, source_run_id: str | None = None
    ) -> tuple[dict[str, Any], ...]:
        run_id = (
            None
            if source_run_id is None
            else str(source_run_id or "").strip()
        )
        return tuple(
            {
                "sample_id": group.candidate_id,
                "run_id": group.source_run_id,
                "symbol": group.symbol,
                "start_ms": group.observed_at_ms,
                "entry_ttl_s": 1,
                "outcome_ttl_s": max(
                    1,
                    int(
                        ceil(
                            (
                                group.max_deadline_ms
                                - group.observed_at_ms
                            )
                            / 1_000
                        )
                    ),
                ),
            }
            for group in sorted(
                self._active.values(),
                key=lambda value: (
                    value.observed_at_ms,
                    value.candidate_id,
                ),
            )
            if run_id is None or group.source_run_id == run_id
        )

    @staticmethod
    def _agg_ticks(
        rows: Sequence[Mapping[str, Any]],
    ) -> tuple[AggTradePathTick, ...]:
        ticks: list[AggTradePathTick] = []
        for row in rows:
            if not isinstance(row, Mapping):
                raise ValueError("aggTrade row must be a mapping")
            timestamp_ms = int(row.get("T", row.get("time")))
            ticks.append(
                AggTradePathTick(
                    timestamp_ms=timestamp_ms,
                    available_at_ms=timestamp_ms,
                    aggregate_trade_id=int(row.get("a", row.get("id"))),
                    price=float(row.get("p", row.get("price"))),
                )
            )
        return tuple(ticks)

    async def advance(
        self,
        *,
        source_run_id: str | None,
        agg_trade_rows: Sequence[Mapping[str, Any]],
        coverage_start_ms: int,
        coverage_end_ms: int,
        coverage_complete: bool,
        force_data_failure: bool,
        now_ms: int,
        cost_model: ShadowCostModel,
    ) -> dict[str, int]:
        """Evaluate and atomically terminalize candidate-level paired results."""

        run_id = (
            None
            if source_run_id is None
            else str(source_run_id or "").strip()
        )
        now = int(now_ms)
        coverage_start = int(coverage_start_ms)
        coverage_end = int(coverage_end_ms)
        groups = [
            group
            for group in self._active.values()
            if run_id is None or group.source_run_id == run_id
        ]
        if not groups:
            return {
                "groups_terminal": 0,
                "evidence_terminal": 0,
                "groups_pending": 0,
                "errors": 0,
            }
        try:
            all_ticks = self._agg_ticks(agg_trade_rows)
        except (TypeError, ValueError, OverflowError):
            all_ticks = ()
            force_data_failure = True

        groups_terminal = 0
        evidence_terminal = 0
        errors = 0
        for group in groups:
            through = min(
                max(group.observed_at_ms, coverage_end),
                group.max_deadline_ms,
                now,
            )
            missing_prefix = coverage_start > group.observed_at_ms
            group_force_failure = bool(
                force_data_failure or missing_prefix
            )
            relevant_ticks = tuple(
                tick
                for tick in all_ticks
                if group.observed_at_ms < tick.timestamp_ms <= through
            )
            try:
                envelope = TickEnvelope(
                    opportunity_id=group.opportunity_id,
                    observed_at_ms=group.observed_at_ms,
                    decision_at_ms=max(now, through),
                    coverage_through_ms=through,
                    ticks=relevant_ticks,
                    provenance="BINANCE_PUBLIC_AGGTRADE_SHARED_V1469",
                )
                evaluation = evaluate_paired_arms(
                    group.opportunity,
                    envelope,
                    cost_model,
                )
                evidence_map = group.evidence_map
                results = tuple(
                    result
                    for result in evaluation.results
                    if result.profile_id in evidence_map
                )
                if set(result.profile_id for result in results) != set(
                    evidence_map
                ):
                    raise ValueError(
                        "paired evaluator profile set mismatch"
                    )
                terminal_ready = bool(
                    coverage_complete
                    and all(
                        result.terminal_reason != "DATA_INCOMPLETE"
                        for result in results
                    )
                )
                deadline_exhausted = through >= group.max_deadline_ms
                if not terminal_ready and not (
                    deadline_exhausted or group_force_failure
                ):
                    continue
                terminal_rows = []
                expected_profile_ids = tuple(sorted(evidence_map))
                paired_group_id = paired_group_identity(
                    group.opportunity_id,
                    group.candidate_id,
                    expected_profile_ids,
                )
                for result in results:
                    profile = _profile_for_group(group, result.profile_id)
                    execution = profile.execution_profile
                    if execution is None:
                        raise ValueError(
                            "paired terminal profile is not executable"
                        )
                    profile_deadline_at_ms = (
                        group.observed_at_ms
                        + (
                            int(execution.entry_ttl_s)
                            + int(execution.max_hold_s)
                        )
                        * 1_000
                    )
                    paired_contract = {
                        "schema": PAIRED_CONTRACT_SCHEMA,
                        "paired_group_id": paired_group_id,
                        "opportunity_id": group.opportunity_id,
                        "candidate_id": group.candidate_id,
                        "profile_id": result.profile_id,
                        "expected_profile_ids": list(
                            expected_profile_ids
                        ),
                        "observed_at_ms": group.observed_at_ms,
                        "coverage_start_ms": coverage_start,
                        "coverage_through_ms": through,
                        "decision_at_ms": envelope.decision_at_ms,
                        "coverage_complete": bool(
                            terminal_ready and coverage_complete
                        ),
                        "profile_deadline_at_ms": profile_deadline_at_ms,
                        "group_deadline_at_ms": group.max_deadline_ms,
                        "shared_envelope_hash": evaluation.envelope_hash,
                    }
                    if terminal_ready:
                        terminal_payload = (
                            result.to_repository_terminal_payload(
                                updated_at_ms=max(
                                    now,
                                    int(result.terminal_at_ms or now),
                                )
                            )
                        )
                        result_payload = dict(
                            terminal_payload["terminal_payload"]
                        )
                        result_payload[
                            "paired_contract"
                        ] = paired_contract
                        terminal_payload[
                            "terminal_payload"
                        ] = result_payload
                    else:
                        # One incomplete/broken envelope invalidates the whole
                        # paired comparison.  Persist explicit drops for every
                        # profile so a selectively early TP cannot bias EV.
                        terminal_payload = {
                            "status": "DROPPED",
                            "terminal_at_ms": through,
                            "outcome": "data_incomplete",
                            "fill_status": "UNKNOWN",
                            "data_complete": False,
                            "ambiguous": False,
                            "reward_net_bp": None,
                            "mfe_bp": result.mfe_bp,
                            "mae_bp": result.mae_bp,
                            "terminal_reason": (
                                "PAIRED_ENVELOPE_INCOMPLETE"
                            ),
                            "terminal_payload": {
                                "schema": (
                                    "v1469.paired-envelope-drop.1"
                                ),
                                "envelope_hash": evaluation.envelope_hash,
                                "coverage_start_ms": coverage_start,
                                "coverage_through_ms": through,
                                "coverage_complete": bool(
                                    coverage_complete
                                ),
                                "force_data_failure": bool(
                                    group_force_failure
                                ),
                                "profile_id": result.profile_id,
                                "paired_contract": paired_contract,
                                "evaluator_terminal_reason": (
                                    result.terminal_reason
                                ),
                            },
                            "updated_at_ms": max(now, through),
                        }
                    terminal_rows.append(
                        {
                            "evidence_id": evidence_map[
                                result.profile_id
                            ],
                            "terminal": terminal_payload,
                        }
                    )
                await self._repository.terminal_evidence_bundle(
                    terminal_rows
                )
                self._active.pop(group.candidate_id, None)
                groups_terminal += 1
                evidence_terminal += len(terminal_rows)
            except Exception:
                errors += 1
        return {
            "groups_terminal": groups_terminal,
            "evidence_terminal": evidence_terminal,
            "groups_pending": sum(
                1
                for group in self._active.values()
                if run_id is None or group.source_run_id == run_id
            ),
            "errors": errors,
        }


__all__ = [
    "PendingPairedCandidate",
    "V1469PairedShadowRuntime",
]
