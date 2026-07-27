"""Async App boundary for constructing the v1.4.59 observation runtime.

The exchange client is used only while collecting read-only endpoint and
position-mode facts.  It is not retained by the returned observation runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from src.gridbot.mainnet.runtime_identity import RuntimeIdentity
from src.gridbot.mainnet.v1459_lifecycle_runtime import (
    V1459LifecycleObservationRuntime,
)
from src.gridbot.mainnet.v1459_observation_composition import (
    V1459ObservationComposition,
    build_v1459_observation_composition,
    observation_flags_from_settings,
)
from src.gridbot.mainnet.v1459_observation_runtime import (
    V1459ObservationRuntime,
    V1459RuntimeContext,
)
from src.gridbot.mainnet.v1459_runtime_identity_source import (
    build_observed_runtime_identity,
)
from src.gridbot.storage.database import Database


@dataclass(frozen=True)
class V1459AppRuntime:
    composition: V1459ObservationComposition
    runtime: V1459ObservationRuntime | None

    @property
    def permits_order_mutation(self) -> bool:
        return False


def _session_identity(row: Mapping[str, Any]) -> RuntimeIdentity:
    return RuntimeIdentity(
        environment=str(row["environment"]),
        exchange_endpoint=str(row["exchange_endpoint"]),
        exchange_testnet=bool(row["is_testnet"]),
        account_fingerprint=str(row["account_fingerprint"]),
        db_namespace=str(row["database_identity"]),
        symbol=str(row["symbol"]),
        account_mode=str(row["account_mode"]),
        deployment_commit=str(row["deployment_commit"]),
        config_hash=str(row["config_sha256"]),
    )


def identity_config_envelope(settings: Any) -> dict[str, Any]:
    """Return only order-relevant, secret-free v1.4.59 configuration."""

    names = (
        "mainnet_symbol",
        "mainnet_strategy_label",
        "mainnet_leverage",
        "mainnet_effective_max_cumulative_notional_usdc",
        "mainnet_max_session_loss_usdc",
        "mainnet_one_run_enabled",
        "mainnet_codex_v1458_cnl_wpr_deep_gate_enabled",
        "mainnet_v1459_observation_enabled",
        "mainnet_v1459_observation_persist_session_enabled",
        "mainnet_v1459_observation_record_opportunities_enabled",
        "mainnet_v1459_observation_record_shadow_enabled",
        "mainnet_v1459_observation_record_reconciliation_enabled",
    )
    return {name: getattr(settings, name, None) for name in names}


async def build_v1459_app_runtime(
    *,
    settings: Any,
    db: Database,
    read_only_identity_client: Any,
    code_version: str,
) -> V1459AppRuntime:
    """Build disabled composition cheaply, or fail closed on identity gaps."""

    composition = build_v1459_observation_composition(settings, db)
    flags = observation_flags_from_settings(settings)
    if not flags.enabled:
        return V1459AppRuntime(composition=composition, runtime=None)

    endpoint = getattr(read_only_identity_client, "exchange_endpoint", None)
    is_testnet = getattr(read_only_identity_client, "is_testnet", None)
    get_position_mode = getattr(read_only_identity_client, "get_position_mode", None)
    if not isinstance(endpoint, str) or not endpoint:
        raise ValueError("read-only client endpoint is unavailable")
    if not isinstance(is_testnet, bool):
        raise ValueError("read-only client testnet flag is unavailable")
    if not callable(get_position_mode):
        raise ValueError("read-only account-mode probe is unavailable")
    account_mode = await get_position_mode()
    observed = build_observed_runtime_identity(
        environment="testnet" if is_testnet else "mainnet",
        exchange_endpoint=endpoint,
        exchange_testnet=is_testnet,
        account_fingerprint_marker=getattr(
            settings, "mainnet_v1459_account_fingerprint_marker_path", ""
        ),
        db_path=getattr(settings, "db_path", ""),
        symbol=getattr(settings, "mainnet_symbol", ""),
        account_mode=account_mode,
        deployment_commit=getattr(
            settings, "mainnet_v1459_deployment_commit", ""
        ),
        config=identity_config_envelope(settings),
    )
    stored = await composition.evidence_repository.get_open_session(
        environment=observed.environment,
        account_fingerprint=observed.account_fingerprint,
        database_identity=observed.db_namespace,
        symbol=observed.symbol,
    )
    expected = observed if stored is None else _session_identity(stored)
    runtime = V1459LifecycleObservationRuntime(
        coordinator=composition.coordinator,
        context=V1459RuntimeContext(
            expected_identity=expected,
            observed_identity=observed,
            code_version=code_version,
        ),
        durable_session=stored,
    )
    return V1459AppRuntime(composition=composition, runtime=runtime)


__all__ = [
    "V1459AppRuntime",
    "build_v1459_app_runtime",
    "identity_config_envelope",
]
