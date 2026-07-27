"""Frozen split and bounded-candidate configuration for Live Next research."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from itertools import product
import json
from typing import Any, Mapping, Sequence

from .contracts import ContractError, canonical_json, canonical_sha256


UTC_DAY_MS = 86_400_000
MAX_EXPERT_FAMILIES = 4
MAX_EXECUTION_PROFILES = 3
MAX_EXIT_PROFILES = 2
MAX_STRUCTURED_CANDIDATES = 24


class SplitRole(str, Enum):
    TRAIN = "TRAIN"
    VALIDATION = "VALIDATION"
    HOLDOUT = "HOLDOUT"


class EvaluationStage(str, Enum):
    TRAIN = "TRAIN"
    VALIDATION = "VALIDATION"
    HOLDOUT = "HOLDOUT"


@dataclass(frozen=True, slots=True)
class TimeWindow:
    role: SplitRole
    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "role", SplitRole(self.role))
        for name in ("start_ms", "end_ms"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ContractError(f"{name} must be a non-negative integer")
        if self.end_ms <= self.start_ms:
            raise ContractError("time window end must follow start")

    @property
    def duration_days(self) -> float:
        return (self.end_ms - self.start_ms) / UTC_DAY_MS

    def contains(self, timestamp_ms: int) -> bool:
        return self.start_ms <= timestamp_ms < self.end_ms


@dataclass(frozen=True, slots=True)
class FrozenSplitManifest:
    dataset_id: str
    source_kind: str
    source_artifact_sha256: str
    created_at_ms: int
    train: TimeWindow
    validation: TimeWindow
    holdout: TimeWindow
    holdout_sealed: bool = True
    schema_version: str = "live_next.split_manifest.v1"

    def __post_init__(self) -> None:
        for name in (
            "dataset_id",
            "source_kind",
            "source_artifact_sha256",
            "schema_version",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ContractError(f"{name} is required")
        if isinstance(self.created_at_ms, bool) or not isinstance(self.created_at_ms, int):
            raise ContractError("created_at_ms must be an integer")
        if self.created_at_ms < 0:
            raise ContractError("created_at_ms must be non-negative")
        if not isinstance(self.holdout_sealed, bool):
            raise ContractError("holdout_sealed must be boolean")
        if self.train.role is not SplitRole.TRAIN:
            raise ContractError("train window must have TRAIN role")
        if self.validation.role is not SplitRole.VALIDATION:
            raise ContractError("validation window must have VALIDATION role")
        if self.holdout.role is not SplitRole.HOLDOUT:
            raise ContractError("holdout window must have HOLDOUT role")
        if self.train.end_ms != self.validation.start_ms:
            raise ContractError("TRAIN and VALIDATION windows must be contiguous")
        if self.validation.end_ms != self.holdout.start_ms:
            raise ContractError("VALIDATION and HOLDOUT windows must be contiguous")

    @classmethod
    def chronological_days(
        cls,
        *,
        dataset_id: str,
        source_kind: str,
        source_artifact_sha256: str,
        end_ms: int,
        created_at_ms: int,
        train_days: int = 60,
        validation_days: int = 15,
        holdout_days: int = 15,
    ) -> "FrozenSplitManifest":
        for name, value in (
            ("train_days", train_days),
            ("validation_days", validation_days),
            ("holdout_days", holdout_days),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ContractError(f"{name} must be a positive integer")
        if end_ms % UTC_DAY_MS != 0:
            raise ContractError("end_ms must be aligned to a UTC day boundary")
        holdout_start = end_ms - holdout_days * UTC_DAY_MS
        validation_start = holdout_start - validation_days * UTC_DAY_MS
        train_start = validation_start - train_days * UTC_DAY_MS
        return cls(
            dataset_id=dataset_id,
            source_kind=source_kind,
            source_artifact_sha256=source_artifact_sha256,
            created_at_ms=created_at_ms,
            train=TimeWindow(SplitRole.TRAIN, train_start, validation_start),
            validation=TimeWindow(SplitRole.VALIDATION, validation_start, holdout_start),
            holdout=TimeWindow(SplitRole.HOLDOUT, holdout_start, end_ms),
        )

    @property
    def manifest_hash(self) -> str:
        return canonical_sha256(self.to_dict(include_hash=False))

    def window_for(self, role: SplitRole | str) -> TimeWindow:
        normalized = SplitRole(role)
        return {
            SplitRole.TRAIN: self.train,
            SplitRole.VALIDATION: self.validation,
            SplitRole.HOLDOUT: self.holdout,
        }[normalized]

    def to_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        payload = {
            "created_at_ms": self.created_at_ms,
            "dataset_id": self.dataset_id,
            "holdout": {
                "end_ms": self.holdout.end_ms,
                "role": self.holdout.role.value,
                "start_ms": self.holdout.start_ms,
            },
            "holdout_sealed": self.holdout_sealed,
            "schema_version": self.schema_version,
            "source_artifact_sha256": self.source_artifact_sha256,
            "source_kind": self.source_kind,
            "train": {
                "end_ms": self.train.end_ms,
                "role": self.train.role.value,
                "start_ms": self.train.start_ms,
            },
            "validation": {
                "end_ms": self.validation.end_ms,
                "role": self.validation.role.value,
                "start_ms": self.validation.start_ms,
            },
        }
        if include_hash:
            payload["manifest_hash"] = canonical_sha256(payload)
        return payload


@dataclass(frozen=True, slots=True)
class CandidateSpec:
    candidate_id: str
    expert_family: str
    execution_profile: str
    exit_profile: str
    parameters_json: str
    schema_version: str = "live_next.candidate.v1"

    def __post_init__(self) -> None:
        for name in (
            "candidate_id",
            "expert_family",
            "execution_profile",
            "exit_profile",
            "schema_version",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ContractError(f"{name} is required")
        try:
            parameters = json.loads(self.parameters_json)
        except (TypeError, ValueError) as exc:
            raise ContractError("parameters_json must be valid JSON") from exc
        if not isinstance(parameters, Mapping):
            raise ContractError("candidate parameters must be an object")
        if canonical_json(parameters) != self.parameters_json:
            raise ContractError("candidate parameters must use canonical JSON")
        expected = self.build_id(
            expert_family=self.expert_family,
            execution_profile=self.execution_profile,
            exit_profile=self.exit_profile,
            parameters=parameters,
        )
        if self.candidate_id != expected:
            raise ContractError("candidate_id does not match candidate configuration")

    @staticmethod
    def build_id(
        *,
        expert_family: str,
        execution_profile: str,
        exit_profile: str,
        parameters: Mapping[str, Any],
    ) -> str:
        digest = canonical_sha256(
            {
                "execution_profile": execution_profile,
                "exit_profile": exit_profile,
                "expert_family": expert_family,
                "parameters": parameters,
            }
        )
        return f"lncand_{digest[:24]}"

    @classmethod
    def create(
        cls,
        *,
        expert_family: str,
        execution_profile: str,
        exit_profile: str,
        parameters: Mapping[str, Any] | None = None,
    ) -> "CandidateSpec":
        parameters = dict(parameters or {})
        return cls(
            candidate_id=cls.build_id(
                expert_family=expert_family,
                execution_profile=execution_profile,
                exit_profile=exit_profile,
                parameters=parameters,
            ),
            expert_family=expert_family,
            execution_profile=execution_profile,
            exit_profile=exit_profile,
            parameters_json=canonical_json(parameters),
        )


def build_candidate_factory(
    *,
    expert_families: Sequence[str],
    execution_profiles: Sequence[str],
    exit_profiles: Sequence[str],
    parameter_overrides: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[CandidateSpec, ...]:
    """Expand one bounded structural menu; blind large grids are rejected."""

    menus = (
        ("expert families", tuple(expert_families), MAX_EXPERT_FAMILIES),
        ("execution profiles", tuple(execution_profiles), MAX_EXECUTION_PROFILES),
        ("exit profiles", tuple(exit_profiles), MAX_EXIT_PROFILES),
    )
    for name, values, limit in menus:
        if not values:
            raise ContractError(f"{name} cannot be empty")
        if len(values) > limit:
            raise ContractError(f"{name} exceeds bounded limit {limit}")
        if len(set(values)) != len(values):
            raise ContractError(f"{name} must be unique")
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise ContractError(f"{name} must contain non-empty strings")
    overrides = dict(parameter_overrides or {})
    candidates = tuple(
        CandidateSpec.create(
            expert_family=expert,
            execution_profile=execution,
            exit_profile=exit_profile,
            parameters=overrides.get(
                f"{expert}|{execution}|{exit_profile}", {}
            ),
        )
        for expert, execution, exit_profile in product(
            menus[0][1], menus[1][1], menus[2][1]
        )
    )
    if len(candidates) > MAX_STRUCTURED_CANDIDATES:
        raise ContractError("candidate factory exceeds 24 structured candidates")
    return candidates


@dataclass(frozen=True, slots=True)
class FrozenReplayProtocol:
    stage: EvaluationStage
    split_manifest_hash: str
    candidate_ids: tuple[str, ...]
    feature_config_hash: str
    cost_model_hash: str
    portfolio_config_hash: str | None = None
    schema_version: str = "live_next.replay_protocol.v1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "stage", EvaluationStage(self.stage))
        for name in (
            "split_manifest_hash",
            "feature_config_hash",
            "cost_model_hash",
            "schema_version",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ContractError(f"{name} is required")
        if not self.candidate_ids or len(self.candidate_ids) > MAX_STRUCTURED_CANDIDATES:
            raise ContractError("protocol must contain 1 to 24 candidate IDs")
        if len(set(self.candidate_ids)) != len(self.candidate_ids):
            raise ContractError("candidate IDs must be unique")
        if self.stage is EvaluationStage.HOLDOUT:
            if not isinstance(self.portfolio_config_hash, str) or not self.portfolio_config_hash.strip():
                raise ContractError("sealed HOLDOUT requires one frozen portfolio_config_hash")
        elif self.portfolio_config_hash is not None:
            raise ContractError("portfolio_config_hash is reserved for sealed HOLDOUT")

    @property
    def protocol_hash(self) -> str:
        return canonical_sha256(
            {
                "candidate_ids": self.candidate_ids,
                "cost_model_hash": self.cost_model_hash,
                "feature_config_hash": self.feature_config_hash,
                "portfolio_config_hash": self.portfolio_config_hash,
                "schema_version": self.schema_version,
                "split_manifest_hash": self.split_manifest_hash,
                "stage": self.stage.value,
            }
        )

    def assert_partition_access(self, role: SplitRole | str) -> None:
        normalized = SplitRole(role)
        expected = SplitRole(self.stage.value)
        if normalized is not expected:
            raise ContractError(
                f"{self.stage.value} protocol cannot evaluate {normalized.value} outcomes"
            )


__all__ = [
    "CandidateSpec",
    "EvaluationStage",
    "FrozenReplayProtocol",
    "FrozenSplitManifest",
    "MAX_STRUCTURED_CANDIDATES",
    "SplitRole",
    "TimeWindow",
    "UTC_DAY_MS",
    "build_candidate_factory",
]
