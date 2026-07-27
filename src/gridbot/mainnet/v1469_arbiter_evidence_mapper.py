"""Fail-closed durable evidence mapping for the v1.4.69 pure arbiter.

The arbiter intentionally accepts simple immutable dataclasses.  This module is
the trust boundary between SQLite rows and those dataclasses.  In particular,
it never trusts a caller-supplied ``paired`` boolean.  Pairing is reconstructed
from a durable contract shared by every execution profile in one
opportunity/candidate group.

The caller must provide the complete append-only evidence ledger for the
requested environment/symbol.  Rolling-window evidence is selected only after
the complete ledger has been hashed into a monotonic source revision.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from math import isclose, isfinite
from typing import Any, Mapping, Sequence

from src.gridbot.mainnet.v1469_adaptive_identity import (
    EXECUTION_PROFILE_SCHEMA,
    canonical_sha256,
)
from src.gridbot.mainnet.v1469_arm_arbiter import (
    ArmCandidate,
    ArmEvidence,
    ArmIdentity,
    normalize_evidence_outcome,
)
from src.gridbot.mainnet.v1469_arm_profiles import (
    ArmProfileDefinition,
    PASSIVE_BALANCED,
    RANGE_SCALP,
    TREND_PARTIAL,
    get_arm_profile,
)
from src.gridbot.mainnet.v1469_legacy_control import (
    LEGACY_CONTROL,
    LegacyExecutionSnapshot,
)
from src.gridbot.mainnet.v1469_paired_evaluator import TERMINAL_RESULT_SCHEMA


PAIRED_CONTRACT_SCHEMA = "v1469.paired-contract.1"
EVIDENCE_LEDGER_REVISION_SCHEMA = "v1469.durable-evidence-revision.1"

_REQUIRED_ROW_FIELDS = frozenset(
    {
        "evidence_id",
        "opportunity_id",
        "candidate_id",
        "arm_key",
        "execution_profile_id",
        "execution_profile_schema",
        "execution_profile_hash",
        "source_type",
        "diagnostic_only",
        "observed_at_ms",
        "status",
        "terminal_at_ms",
        "outcome",
        "fill_status",
        "data_complete",
        "ambiguous",
        "reward_net_bp",
        "mfe_bp",
        "mae_bp",
        "terminal_reason",
        "terminal_payload_json",
        "evidence_hash",
        "lane_code",
        "effective_side",
        "strategy",
        "coarse_regime",
        "data_quality",
        "candidate_status",
    }
)
_TERMINAL_REASONS = frozenset({"TP", "SL", "MAX_HOLD", "NO_FILL"})


@dataclass(frozen=True, slots=True)
class EvidenceMappingIssue:
    code: str
    opportunity_id: str = ""
    candidate_id: str = ""
    arm_key: str = ""
    detail: str = ""


@dataclass(frozen=True, slots=True)
class DurableEvidenceMapping:
    candidates: tuple[ArmCandidate, ...]
    issues: tuple[EvidenceMappingIssue, ...]
    ledger_revision: str | None
    durable_rows: int
    trusted_paired_rows: int


@dataclass(frozen=True, slots=True)
class _ParsedRow:
    raw: Mapping[str, Any]
    payload: Mapping[str, Any]
    contract: Mapping[str, Any] | None
    identity: ArmIdentity
    evidence: ArmEvidence
    profile_definition: ArmProfileDefinition
    legacy_snapshot: LegacyExecutionSnapshot | None


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _text(value: Any) -> str:
    return str(value or "").strip()


def _integer(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("boolean is not an integer")
    parsed = int(value)
    if parsed < 0:
        raise ValueError("integer must be nonnegative")
    return parsed


def _flag(value: Any) -> bool:
    if value not in (False, True, 0, 1):
        raise ValueError("invalid boolean")
    return bool(value)


def _finite_or_none(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("boolean is not finite evidence")
    parsed = float(value)
    if not isfinite(parsed):
        raise ValueError("non-finite evidence")
    return parsed


def _same_number(left: Any, right: Any) -> bool:
    try:
        lhs = float(left)
        rhs = float(right)
    except (TypeError, ValueError, OverflowError):
        return False
    return isfinite(lhs) and isfinite(rhs) and lhs == rhs


def expected_profile_ids(coarse_regime: str) -> tuple[str, ...]:
    """Return the closed, tradable paired menu for one causal coarse regime."""

    regime = _text(coarse_regime).upper()
    if regime == "RANGE":
        return (PASSIVE_BALANCED, RANGE_SCALP)
    if regime in {"TREND_UP", "TREND_DOWN"}:
        return (PASSIVE_BALANCED, TREND_PARTIAL)
    return ()


def paired_group_identity(
    opportunity_id: str,
    candidate_id: str,
    profile_ids: Sequence[str],
) -> str:
    """Return a deterministic identifier for one complete paired group."""

    opportunity = _text(opportunity_id)
    candidate = _text(candidate_id)
    profiles = tuple(sorted({_text(item).upper() for item in profile_ids}))
    if not opportunity or not candidate or not profiles or any(not item for item in profiles):
        raise ValueError("paired group identity fields must be non-empty")
    return "v1469pg_" + canonical_sha256(
        {
            "schema": PAIRED_CONTRACT_SCHEMA,
            "opportunity_id": opportunity,
            "candidate_id": candidate,
            "expected_profile_ids": list(profiles),
        }
    )


def _profile_definition_for_row(
    row: Mapping[str, Any],
) -> tuple[ArmProfileDefinition, LegacyExecutionSnapshot | None]:
    profile_id = _text(row.get("execution_profile_id")).upper()
    if profile_id != LEGACY_CONTROL:
        return get_arm_profile(profile_id), None

    raw_payload = row.get(
        "execution_profile_payload",
        row.get("execution_profile_payload_json"),
    )
    if raw_payload is None:
        raise ValueError("LEGACY_CONTROL execution profile sidecar is missing")
    if isinstance(raw_payload, str):
        try:
            parsed_payload = json.loads(raw_payload)
        except json.JSONDecodeError as exc:
            raise ValueError(
                "LEGACY_CONTROL execution profile sidecar is corrupt"
            ) from exc
        if raw_payload != _canonical_json(parsed_payload):
            raise ValueError(
                "LEGACY_CONTROL execution profile sidecar is not canonical"
            )
        raw_payload = parsed_payload
    if not isinstance(raw_payload, Mapping):
        raise ValueError(
            "LEGACY_CONTROL execution profile sidecar must be an object"
        )
    try:
        snapshot = LegacyExecutionSnapshot.from_payload(raw_payload)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "LEGACY_CONTROL execution profile sidecar is corrupt:"
            f"{exc}"
        ) from exc
    return snapshot.profile_definition, snapshot


def _verify_legacy_invariants(
    row: Mapping[str, Any],
    payload: Mapping[str, Any],
    snapshot: LegacyExecutionSnapshot,
) -> None:
    feature_snapshot = row.get("feature_snapshot")
    if not isinstance(feature_snapshot, Mapping):
        raise ValueError("LEGACY_CONTROL durable feature snapshot is missing")
    reference_price = _finite_or_none(feature_snapshot.get("signal_reference_price"))
    if (
        reference_price is None
        or reference_price <= 0
        or not isclose(
            reference_price,
            float(snapshot.reference_price),
            rel_tol=1e-12,
            abs_tol=1e-12,
        )
    ):
        raise ValueError("LEGACY_CONTROL durable reference price mismatch")

    identity = snapshot.market_identity
    mismatches: list[str] = []
    if identity.lane_code != _text(row.get("lane_code")):
        mismatches.append("lane_code")
    if identity.effective_side != _text(
        row.get("effective_side")
    ).upper():
        mismatches.append("effective_side")
    if identity.strategy != _text(row.get("strategy")):
        mismatches.append("strategy")
    if identity.coarse_regime != _text(row.get("coarse_regime")).upper():
        mismatches.append("coarse_regime")
    if mismatches:
        raise ValueError(
            "LEGACY_CONTROL market identity mismatch:"
            + ",".join(mismatches)
        )
    if _text(payload.get("market_state_hash")) != identity.identity_hash:
        raise ValueError("LEGACY_CONTROL market state hash mismatch")

    entry_limit = _finite_or_none(payload.get("entry_limit_price"))
    if entry_limit is None or entry_limit <= 0:
        raise ValueError(
            "LEGACY_CONTROL terminal entry limit price is missing"
        )
    offset = float(snapshot.entry_offset_bp) / 10_000.0
    factor = (
        1.0 - offset
        if identity.effective_side == "LONG"
        else 1.0 + offset
    )
    expected_limit = float(snapshot.reference_price) * factor
    if (
        factor <= 0
        or not isclose(
            entry_limit,
            expected_limit,
            rel_tol=1e-12,
            abs_tol=1e-12,
        )
    ):
        raise ValueError("LEGACY_CONTROL reference price invariant mismatch")


def _profile_deadline(
    observed_at_ms: int,
    definition: ArmProfileDefinition,
) -> int:
    execution = definition.execution_profile
    if execution is None:
        raise ValueError("risk-off profile has no evidence deadline")
    return observed_at_ms + (
        int(execution.entry_ttl_s) + int(execution.max_hold_s)
    ) * 1_000


def _expected_group_profiles(
    members: Sequence[_ParsedRow],
) -> dict[str, ArmProfileDefinition]:
    regimes = {item.identity.regime for item in members}
    if len(regimes) != 1:
        raise ValueError("paired group regime identity differs")
    profiles = {
        profile_id: get_arm_profile(profile_id)
        for profile_id in expected_profile_ids(next(iter(regimes)))
    }
    legacy_members = [
        item
        for item in members
        if item.identity.execution_profile_id == LEGACY_CONTROL
    ]
    if len(legacy_members) > 1:
        raise ValueError("paired group has duplicate LEGACY_CONTROL rows")
    if legacy_members:
        legacy = legacy_members[0]
        if legacy.legacy_snapshot is None:
            raise ValueError("LEGACY_CONTROL sidecar was not resolved")
        profiles[LEGACY_CONTROL] = legacy.profile_definition
    return dict(sorted(profiles.items()))


def _expected_arm_key(row: Mapping[str, Any]) -> str:
    return "v1469a_" + canonical_sha256(
        {
            "lane_code": _text(row.get("lane_code")),
            "effective_side": _text(row.get("effective_side")).upper(),
            "strategy": _text(row.get("strategy")),
            "coarse_regime": _text(row.get("coarse_regime")).upper(),
            "execution_profile_id": _text(
                row.get("execution_profile_id")
            ).upper(),
            "execution_profile_schema": _text(
                row.get("execution_profile_schema")
            ),
            "execution_profile_hash": _text(
                row.get("execution_profile_hash")
            ),
        }
    )


def _terminal_payload(row: Mapping[str, Any]) -> tuple[Mapping[str, Any], str]:
    raw = row.get("terminal_payload_json")
    if not isinstance(raw, str) or not raw:
        raise ValueError("terminal_payload_json must be a non-empty JSON string")
    parsed = json.loads(raw)
    if not isinstance(parsed, Mapping):
        raise ValueError("terminal_payload_json must contain an object")
    canonical = _canonical_json(parsed)
    if raw != canonical:
        raise ValueError("terminal_payload_json is not canonical")
    return parsed, canonical


def _verify_evidence_hash(
    row: Mapping[str, Any],
    terminal_payload_json: str,
) -> None:
    status = _text(row.get("status")).upper()
    if status not in {"TERMINAL", "DROPPED"}:
        raise ValueError("evidence is not terminal")
    terminal = {
        "status": status,
        "terminal_at_ms": _integer(row.get("terminal_at_ms")),
        "outcome": _text(row.get("outcome")).lower(),
        "fill_status": _text(row.get("fill_status")).upper(),
        "data_complete": int(_flag(row.get("data_complete"))),
        "ambiguous": int(_flag(row.get("ambiguous"))),
        "reward_net_bp": _finite_or_none(row.get("reward_net_bp")),
        "mfe_bp": _finite_or_none(row.get("mfe_bp")),
        "mae_bp": _finite_or_none(row.get("mae_bp")),
        "terminal_reason": _text(row.get("terminal_reason")) or None,
        "terminal_payload_json": terminal_payload_json,
    }
    digest = hashlib.sha256(
        _canonical_json(
            {
                "evidence_id": _text(row.get("evidence_id")),
                "opportunity_id": _text(row.get("opportunity_id")),
                "candidate_id": _text(row.get("candidate_id")),
                "arm_key": _text(row.get("arm_key")),
                "execution_profile_hash": _text(
                    row.get("execution_profile_hash")
                ),
                "source_type": _text(row.get("source_type")).upper(),
                "diagnostic_only": int(_flag(row.get("diagnostic_only"))),
                "observed_at_ms": _integer(row.get("observed_at_ms")),
                **terminal,
            }
        ).encode("utf-8")
    ).hexdigest()
    if _text(row.get("evidence_hash")) != digest:
        raise ValueError("evidence_hash mismatch")


def _verify_terminal_result(
    row: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> None:
    if _text(payload.get("schema")) != TERMINAL_RESULT_SCHEMA:
        raise ValueError("terminal result schema mismatch")
    exact_text = {
        "opportunity_id": row.get("opportunity_id"),
        "profile_id": _text(row.get("execution_profile_id")).upper(),
        "arm_hash": row.get("arm_key"),
        "execution_profile_hash": row.get("execution_profile_hash"),
        "side": _text(row.get("effective_side")).upper(),
        "fill_status": _text(row.get("fill_status")).upper(),
        "terminal_reason": _text(row.get("terminal_reason")).upper(),
    }
    for name, expected in exact_text.items():
        if _text(payload.get(name)) != _text(expected):
            raise ValueError(f"terminal result {name} mismatch")
    if _integer(payload.get("terminal_at_ms")) != _integer(
        row.get("terminal_at_ms")
    ):
        raise ValueError("terminal result timestamp mismatch")
    if not _flag(payload.get("data_complete")) or not _flag(
        payload.get("evaluable")
    ):
        raise ValueError("terminal result is not complete/evaluable")
    if not _same_number(
        payload.get("reward_net_bp"), row.get("reward_net_bp")
    ):
        raise ValueError("terminal result reward mismatch")
    terminal_hash = _text(payload.get("terminal_hash"))
    base = dict(payload)
    base.pop("terminal_hash", None)
    base.pop("paired_contract", None)
    if not terminal_hash or terminal_hash != canonical_sha256(base):
        raise ValueError("terminal result hash mismatch")


def _parse_individual_row(row: Mapping[str, Any]) -> _ParsedRow:
    missing = sorted(_REQUIRED_ROW_FIELDS - set(row))
    if missing:
        raise ValueError("missing durable fields:" + ",".join(missing))
    for name in (
        "evidence_id",
        "opportunity_id",
        "candidate_id",
        "arm_key",
        "execution_profile_id",
        "execution_profile_schema",
        "execution_profile_hash",
        "lane_code",
        "effective_side",
        "strategy",
        "coarse_regime",
    ):
        if not _text(row.get(name)):
            raise ValueError(f"empty durable field:{name}")
    if _text(row.get("source_type")).upper() != "SHADOW":
        raise ValueError("only SHADOW evidence is mappable")
    if _flag(row.get("diagnostic_only")):
        raise ValueError("diagnostic evidence is not mappable")
    if _text(row.get("status")).upper() != "TERMINAL":
        raise ValueError("dropped/pending evidence is not evaluable")
    if _text(row.get("data_quality")).upper() != "COMPLETE":
        raise ValueError("opportunity data is incomplete")
    if _text(row.get("candidate_status")).upper() not in {
        "SAFE",
        "NOT_EVALUATED",
    }:
        raise ValueError("candidate status is not shadow-eligible")
    if not _flag(row.get("data_complete")) or _flag(row.get("ambiguous")):
        raise ValueError("terminal evidence is incomplete or ambiguous")
    profile_id = _text(row.get("execution_profile_id")).upper()
    definition, legacy_snapshot = _profile_definition_for_row(
        row
    )
    if (
        definition.execution_profile is None
        or _text(row.get("execution_profile_schema"))
        != EXECUTION_PROFILE_SCHEMA
        or _text(row.get("execution_profile_hash"))
        != definition.execution_profile_hash
    ):
        raise ValueError("execution profile identity mismatch")
    if (
        profile_id != LEGACY_CONTROL
        and profile_id not in expected_profile_ids(row.get("coarse_regime"))
    ):
        raise ValueError("profile is not legal for coarse regime")
    arm_key = _text(row.get("arm_key"))
    if arm_key != _expected_arm_key(row):
        raise ValueError("arm key mismatch")

    observed = _integer(row.get("observed_at_ms"))
    terminal = _integer(row.get("terminal_at_ms"))
    if terminal < observed:
        raise ValueError("terminal timestamp precedes observation")
    normalized_outcome = normalize_evidence_outcome(
        _text(row.get("outcome"))
    )
    if normalized_outcome is None:
        raise ValueError("unsupported arbiter outcome")
    reward = _finite_or_none(row.get("reward_net_bp"))
    if reward is None:
        raise ValueError("terminal evidence reward is missing")
    payload, payload_json = _terminal_payload(row)
    _verify_evidence_hash(row, payload_json)
    _verify_terminal_result(row, payload)
    if legacy_snapshot is not None:
        _verify_legacy_invariants(row, payload, legacy_snapshot)
    terminal_reason = _text(row.get("terminal_reason")).upper()
    if terminal_reason not in _TERMINAL_REASONS:
        raise ValueError("invalid terminal reason")

    identity = ArmIdentity(
        arm_key=arm_key,
        lane_code=_text(row.get("lane_code")),
        side=_text(row.get("effective_side")).upper(),
        strategy=_text(row.get("strategy")),
        regime=_text(row.get("coarse_regime")).upper(),
        execution_profile_id=profile_id,
        execution_profile_hash=_text(row.get("execution_profile_hash")),
    )
    placeholder_deadline = _profile_deadline(observed, definition)
    evidence = ArmEvidence(
        arm_key=arm_key,
        opportunity_id=_text(row.get("opportunity_id")),
        observed_at_ms=observed,
        terminal_at_ms=terminal,
        deadline_at_ms=placeholder_deadline,
        outcome=normalized_outcome,
        reward_net_bp=reward,
        regime=identity.regime,
        paired=False,
        evaluable=True,
        data_complete=True,
        identity_valid=True,
        # A strategy stop is an ordinary sampled outcome.  Only the producer's
        # explicit risk-policy marker may trip the arbiter hard-loss circuit.
        hard_loss=(
            payload.get("hard_loss") is True
            or payload.get("risk_policy_hard_loss") is True
        ),
    )
    contract = payload.get("paired_contract")
    return _ParsedRow(
        raw=row,
        payload=payload,
        contract=contract if isinstance(contract, Mapping) else None,
        identity=identity,
        evidence=evidence,
        profile_definition=definition,
        legacy_snapshot=legacy_snapshot,
    )


def _contract_fingerprint(
    item: _ParsedRow,
    expected_profiles: Mapping[str, ArmProfileDefinition],
) -> tuple[Any, ...]:
    contract = item.contract
    if contract is None:
        raise ValueError("missing paired_contract")
    row = item.raw
    if _text(contract.get("schema")) != PAIRED_CONTRACT_SCHEMA:
        raise ValueError("paired contract schema mismatch")
    profile_id = item.identity.execution_profile_id
    expected = tuple(sorted(expected_profiles))
    raw_profiles = contract.get("expected_profile_ids")
    if isinstance(raw_profiles, (str, bytes)) or not isinstance(
        raw_profiles, Sequence
    ):
        raise ValueError("paired contract profile set is invalid")
    contract_profiles = tuple(
        sorted(_text(value).upper() for value in raw_profiles)
    )
    if (
        not contract_profiles
        or len(set(contract_profiles)) != len(contract_profiles)
        or contract_profiles != expected
    ):
        raise ValueError("paired contract profile set mismatch")
    opportunity_id = _text(row.get("opportunity_id"))
    candidate_id = _text(row.get("candidate_id"))
    if (
        _text(contract.get("opportunity_id")) != opportunity_id
        or _text(contract.get("candidate_id")) != candidate_id
        or _text(contract.get("profile_id")).upper() != profile_id
    ):
        raise ValueError("paired contract identity mismatch")
    expected_group_id = paired_group_identity(
        opportunity_id, candidate_id, expected
    )
    if _text(contract.get("paired_group_id")) != expected_group_id:
        raise ValueError("paired group identity mismatch")

    observed = item.evidence.observed_at_ms
    terminal = item.evidence.terminal_at_ms
    coverage_start = _integer(contract.get("coverage_start_ms"))
    coverage_through = _integer(contract.get("coverage_through_ms"))
    decision_at = _integer(contract.get("decision_at_ms"))
    profile_deadline = _integer(contract.get("profile_deadline_at_ms"))
    group_deadline = _integer(contract.get("group_deadline_at_ms"))
    if _integer(contract.get("observed_at_ms")) != observed:
        raise ValueError("paired contract observation mismatch")
    if not _flag(contract.get("coverage_complete")):
        raise ValueError("paired contract coverage is incomplete")
    if not (
        coverage_start <= observed
        <= terminal
        <= coverage_through
        <= decision_at
    ):
        raise ValueError("paired contract coverage ordering is invalid")
    expected_deadline = _profile_deadline(
        observed,
        item.profile_definition,
    )
    expected_group_deadline = max(
        _profile_deadline(observed, definition)
        for definition in expected_profiles.values()
    )
    if (
        profile_deadline != expected_deadline
        or group_deadline != expected_group_deadline
        or terminal > profile_deadline
    ):
        raise ValueError("paired contract deadline mismatch")
    envelope_hash = _text(item.payload.get("envelope_hash"))
    shared_envelope_hash = _text(contract.get("shared_envelope_hash"))
    if not envelope_hash or envelope_hash != shared_envelope_hash:
        raise ValueError("paired contract envelope mismatch")
    return (
        expected_group_id,
        contract_profiles,
        shared_envelope_hash,
        observed,
        coverage_start,
        coverage_through,
        decision_at,
        group_deadline,
    )


def _ledger_revision(
    arm_key: str,
    rows: Sequence[Mapping[str, Any]],
) -> str:
    ledger = [
        {
            "evidence_id": _text(row.get("evidence_id")),
            "evidence_hash": _text(row.get("evidence_hash")),
            "opportunity_id": _text(row.get("opportunity_id")),
            "candidate_id": _text(row.get("candidate_id")),
            "status": _text(row.get("status")).upper(),
            "observed_at_ms": _integer(row.get("observed_at_ms")),
        }
        for row in rows
    ]
    ledger.sort(key=_canonical_json)
    return "v1469r_" + canonical_sha256(
        {
            "schema": EVIDENCE_LEDGER_REVISION_SCHEMA,
            "arm_key": arm_key,
            "evidence": ledger,
        }
    )


def map_durable_paired_evidence(
    rows: Sequence[Mapping[str, Any]],
    *,
    ledger_scope_complete: bool,
) -> DurableEvidenceMapping:
    """Map a complete durable ledger into fail-closed arbiter candidates.

    ``ledger_scope_complete`` is deliberately mandatory.  A caller using
    ``LIMIT`` or only the 180-minute window must pass ``False`` and receives no
    candidates, because rows aging out cannot be mistaken for a new evidence
    revision.
    """

    if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence):
        raise TypeError("rows must be a sequence of mappings")
    if not ledger_scope_complete:
        return DurableEvidenceMapping(
            candidates=(),
            issues=(
                EvidenceMappingIssue(
                    code="incomplete_ledger_scope",
                    detail="complete append-only ledger is required",
                ),
            ),
            ledger_revision=None,
            durable_rows=len(rows),
            trusted_paired_rows=0,
        )

    issues: list[EvidenceMappingIssue] = []
    parsed: list[_ParsedRow] = []
    raw_by_arm: dict[str, list[Mapping[str, Any]]] = {}
    corrupt_arms: set[str] = set()
    ledger_items: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            issues.append(
                EvidenceMappingIssue(
                    code="row_not_mapping", detail=f"index={index}"
                )
            )
            continue
        arm_key = _text(row.get("arm_key"))
        opportunity_id = _text(row.get("opportunity_id"))
        candidate_id = _text(row.get("candidate_id"))
        try:
            payload, payload_json = _terminal_payload(row)
            _verify_evidence_hash(row, payload_json)
            if not arm_key:
                raise ValueError("empty durable field:arm_key")
            raw_by_arm.setdefault(arm_key, []).append(row)
            ledger_items.append(
                {
                    "arm_key": arm_key,
                    "evidence_id": _text(row.get("evidence_id")),
                    "evidence_hash": _text(row.get("evidence_hash")),
                    "status": _text(row.get("status")).upper(),
                }
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            if arm_key:
                corrupt_arms.add(arm_key)
            issues.append(
                EvidenceMappingIssue(
                    code="invalid_durable_row",
                    opportunity_id=opportunity_id,
                    candidate_id=candidate_id,
                    arm_key=arm_key,
                    detail=str(exc),
                )
            )
            continue
        try:
            parsed.append(_parse_individual_row(row))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            # Valid DROPPED or otherwise non-evaluable rows still participate in
            # the append-only revision, but never become arbiter evidence.
            code = (
                "non_evaluable_durable_row"
                if _text(row.get("status")).upper() == "DROPPED"
                else "invalid_terminal_evidence"
            )
            issues.append(
                EvidenceMappingIssue(
                    code=code,
                    opportunity_id=opportunity_id,
                    candidate_id=candidate_id,
                    arm_key=arm_key,
                    detail=str(exc),
                )
            )

    group_rows: dict[tuple[str, str], list[_ParsedRow]] = {}
    for item in parsed:
        group_rows.setdefault(
            (
                item.evidence.opportunity_id,
                _text(item.raw.get("candidate_id")),
            ),
            [],
        ).append(item)

    trusted_groups: set[tuple[str, str]] = set()
    envelope_by_group: dict[tuple[str, str], str] = {}
    for group_key, members in sorted(group_rows.items()):
        opportunity_id, candidate_id = group_key
        try:
            expected_profiles = _expected_group_profiles(members)
        except (TypeError, ValueError) as exc:
            issues.append(
                EvidenceMappingIssue(
                    code="incomplete_paired_profile_group",
                    opportunity_id=opportunity_id,
                    candidate_id=candidate_id,
                    detail=str(exc),
                )
            )
            continue
        expected = tuple(expected_profiles)
        actual = tuple(
            sorted(item.identity.execution_profile_id for item in members)
        )
        if (
            not expected
            or actual != expected
            or len({item.identity.arm_key for item in members}) != len(members)
        ):
            issues.append(
                EvidenceMappingIssue(
                    code="incomplete_paired_profile_group",
                    opportunity_id=opportunity_id,
                    candidate_id=candidate_id,
                    detail=f"actual={actual};expected={expected}",
                )
            )
            continue
        try:
            fingerprints = tuple(
                _contract_fingerprint(item, expected_profiles)
                for item in members
            )
            if len(set(fingerprints)) != 1:
                raise ValueError("paired contract differs across profiles")
        except (TypeError, ValueError) as exc:
            issues.append(
                EvidenceMappingIssue(
                    code="invalid_paired_contract",
                    opportunity_id=opportunity_id,
                    candidate_id=candidate_id,
                    detail=str(exc),
                )
            )
            continue
        trusted_groups.add(group_key)
        envelope_by_group[group_key] = str(fingerprints[0][2])

    # A lane challenger and incumbent may share an opportunity identifier only
    # when both were evaluated on the exact same causal market envelope.
    groups_by_opportunity: dict[str, list[tuple[str, str]]] = {}
    for group_key in trusted_groups:
        groups_by_opportunity.setdefault(group_key[0], []).append(group_key)
    for opportunity_id, group_keys in groups_by_opportunity.items():
        hashes = {envelope_by_group[key] for key in group_keys}
        if len(hashes) <= 1:
            continue
        for key in group_keys:
            trusted_groups.discard(key)
        issues.append(
            EvidenceMappingIssue(
                code="opportunity_envelope_conflict",
                opportunity_id=opportunity_id,
                detail="shared envelope differs across lane candidates",
            )
        )

    parsed_by_arm: dict[str, list[_ParsedRow]] = {}
    for item in parsed:
        parsed_by_arm.setdefault(item.identity.arm_key, []).append(item)
    candidates: list[ArmCandidate] = []
    trusted_paired_rows = 0
    for arm_key, arm_rows in sorted(parsed_by_arm.items()):
        if arm_key in corrupt_arms:
            issues.append(
                EvidenceMappingIssue(
                    code="arm_contains_corrupt_ledger_row", arm_key=arm_key
                )
            )
            continue
        identities = {item.identity for item in arm_rows}
        if len(identities) != 1:
            issues.append(
                EvidenceMappingIssue(
                    code="arm_identity_conflict", arm_key=arm_key
                )
            )
            continue
        opportunity_ids = [
            item.evidence.opportunity_id for item in arm_rows
        ]
        if len(opportunity_ids) != len(set(opportunity_ids)):
            issues.append(
                EvidenceMappingIssue(
                    code="duplicate_arm_opportunity", arm_key=arm_key
                )
            )
            continue
        evidence: list[ArmEvidence] = []
        for item in arm_rows:
            key = (
                item.evidence.opportunity_id,
                _text(item.raw.get("candidate_id")),
            )
            paired = key in trusted_groups
            if paired:
                trusted_paired_rows += 1
            evidence.append(
                ArmEvidence(
                    arm_key=item.evidence.arm_key,
                    opportunity_id=item.evidence.opportunity_id,
                    observed_at_ms=item.evidence.observed_at_ms,
                    terminal_at_ms=item.evidence.terminal_at_ms,
                    deadline_at_ms=item.evidence.deadline_at_ms,
                    outcome=item.evidence.outcome,
                    reward_net_bp=item.evidence.reward_net_bp,
                    regime=item.evidence.regime,
                    paired=paired,
                    evaluable=item.evidence.evaluable,
                    data_complete=item.evidence.data_complete,
                    identity_valid=item.evidence.identity_valid,
                    hard_loss=item.evidence.hard_loss,
                )
            )
        candidates.append(
            ArmCandidate(
                identity=next(iter(identities)),
                evidence=tuple(
                    sorted(
                        evidence,
                        key=lambda item: (
                            item.observed_at_ms,
                            item.opportunity_id,
                        ),
                    )
                ),
                source_evidence_revision=_ledger_revision(
                    arm_key, raw_by_arm.get(arm_key, ())
                ),
            )
        )

    ledger_items.sort(key=_canonical_json)
    ledger_revision = "v1469l_" + canonical_sha256(
        {
            "schema": EVIDENCE_LEDGER_REVISION_SCHEMA,
            "evidence": ledger_items,
        }
    )
    issues.sort(
        key=lambda item: (
            item.code,
            item.opportunity_id,
            item.candidate_id,
            item.arm_key,
            item.detail,
        )
    )
    return DurableEvidenceMapping(
        candidates=tuple(candidates),
        issues=tuple(issues),
        ledger_revision=ledger_revision,
        durable_rows=len(rows),
        trusted_paired_rows=trusted_paired_rows,
    )


__all__ = [
    "DurableEvidenceMapping",
    "EVIDENCE_LEDGER_REVISION_SCHEMA",
    "EvidenceMappingIssue",
    "PAIRED_CONTRACT_SCHEMA",
    "expected_profile_ids",
    "map_durable_paired_evidence",
    "paired_group_identity",
]
