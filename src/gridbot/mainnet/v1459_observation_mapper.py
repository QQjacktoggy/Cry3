"""Pure mappings from v1.4.59 runtime evidence to durable repository rows."""

from __future__ import annotations

from typing import Any, Mapping

from src.gridbot.mainnet.run_reconciler import RunReconciliation
from src.gridbot.mainnet.runtime_identity import compare_runtime_identity
from src.gridbot.mainnet.shadow_simulator_v3_result import ShadowTradeOutcomeV3
from src.gridbot.mainnet.v1459_observation_contract import (
    ObservationContractError,
    V1459SessionCheckpoint,
)


def session_checkpoint_payload(
    checkpoint: V1459SessionCheckpoint,
) -> tuple[dict[str, Any], str]:
    """Build one fail-closed session row and return its identity reason."""

    comparison = compare_runtime_identity(
        checkpoint.expected_identity, checkpoint.observed_identity
    )
    identity = checkpoint.expected_identity
    paused = not comparison.accepted
    stopped = checkpoint.stopped_at_ms is not None
    payload: dict[str, Any] = {
        "session_id": checkpoint.session_id,
        "environment": identity.environment,
        "account_fingerprint": identity.account_fingerprint,
        "database_identity": identity.db_namespace,
        "exchange_endpoint": identity.exchange_endpoint,
        "is_testnet": identity.exchange_testnet,
        "symbol": identity.symbol,
        "account_mode": identity.account_mode,
        "deployment_commit": identity.deployment_commit,
        "code_version": checkpoint.code_version,
        "config_sha256": identity.config_hash,
        # A terminal checkpoint must release the one-open-session scope even
        # when it is written after a restart.  It is no longer eligible to
        # re-arm, so STOPPED takes precedence over an identity pause.
        "status": "STOPPED" if stopped else ("PAUSED_REQUIRES_ACK" if paused else "ACTIVE"),
        "started_at_ms": checkpoint.started_at_ms,
        "last_checkpoint_at_ms": checkpoint.checkpoint_at_ms,
        "stopped_at_ms": checkpoint.stopped_at_ms,
        "terminal_runs": checkpoint.terminal_runs,
        "gross_pnl_usdc": checkpoint.gross_pnl_usdc,
        "commission_usdc": checkpoint.commission_usdc,
        "funding_usdc": checkpoint.funding_usdc,
        "net_pnl_usdc": checkpoint.net_pnl_usdc,
        "high_water_net_pnl_usdc": checkpoint.high_water_net_pnl_usdc,
        "rearm_pending": False if paused or stopped else checkpoint.rearm_pending,
        "pause_reason": comparison.reason if paused and not stopped else None,
        "stop_reason": checkpoint.stop_reason,
        "counters": dict(checkpoint.counters),
        "disabled_states": list(checkpoint.disabled_states),
        "route_stats": dict(checkpoint.route_stats),
        "revision": checkpoint.revision,
    }
    return payload, comparison.reason


def shadow_evaluation_payload(
    session_id: str,
    outcome: ShadowTradeOutcomeV3,
    *,
    recorded_at_ms: int,
    extra_input: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Map a formal V3 outcome without inventing missing financial values."""

    if not isinstance(session_id, str) or not session_id.strip():
        raise ObservationContractError("session_id is required")
    if not isinstance(outcome, ShadowTradeOutcomeV3):
        raise ObservationContractError("formal ShadowTradeOutcomeV3 is required")
    if isinstance(recorded_at_ms, bool) or not isinstance(recorded_at_ms, int) or recorded_at_ms < 0:
        raise ObservationContractError("recorded_at_ms must be non-negative")
    if extra_input is not None and not isinstance(extra_input, Mapping):
        raise ObservationContractError("extra_input must be a mapping")

    filled = outcome.fill_status == "FILLED"
    input_payload = {
        "formal_outcome": outcome.as_dict(),
        "metric_contract": outcome.metric_contract,
        "simulation_scope": outcome.simulation_scope,
        "ev_opportunity_eligible": outcome.ev_opportunity_eligible,
        "ev_opportunity_contribution_usdc": (
            None
            if outcome.ev_opportunity_contribution_usdc is None
            else format(outcome.ev_opportunity_contribution_usdc, "f")
        ),
        "extra": dict(extra_input or {}),
    }
    return {
        "session_id": session_id.strip(),
        "opportunity_id": outcome.opportunity_id,
        "variant": outcome.variant,
        "fill_model": outcome.fill_model,
        "simulation_version": outcome.simulation_version,
        "entry_offset_bp": float(outcome.entry_offset_bp),
        "entry_limit_price": float(outcome.entry_limit_price),
        "decision_latency_ms": outcome.decision_latency_ms,
        "entry_ttl_ms": outcome.entry_deadline_ms - outcome.start_ms,
        "fill_status": outcome.fill_status,
        "filled_qty": float(outcome.filled_qty),
        "avg_fill_price": (
            None if outcome.avg_fill_price is None else float(outcome.avg_fill_price)
        ),
        "first_fill_at_ms": outcome.first_fill_at_ms,
        "fill_age_ms": outcome.fill_age_ms,
        "partial_fill_ratio": 1.0 if filled else 0.0,
        "tp_anchor": outcome.tp_anchor,
        "tp_bp": None,
        "sl_anchor": outcome.sl_anchor,
        "sl_bp": None,
        "max_hold_ms": outcome.outcome_deadline_ms - outcome.start_ms,
        "mfe_bp": None if outcome.mfe_bp is None else float(outcome.mfe_bp),
        "mae_bp": None if outcome.mae_bp is None else float(outcome.mae_bp),
        "exit_at_ms": outcome.exit_at_ms,
        "exit_price": None if outcome.exit_price is None else float(outcome.exit_price),
        "exit_reason": outcome.exit_reason,
        "gross_pnl_usdc": (
            None if outcome.gross_pnl_usdc is None else float(outcome.gross_pnl_usdc)
        ),
        "commission_usdc": (
            None if outcome.commission_usdc is None else float(outcome.commission_usdc)
        ),
        "funding_usdc": (
            None if outcome.funding_usdc is None else float(outcome.funding_usdc)
        ),
        "net_pnl_usdc": (
            None if outcome.net_pnl_usdc is None else float(outcome.net_pnl_usdc)
        ),
        "data_quality": outcome.data_quality,
        "ambiguous_touch": False,
        "input": input_payload,
        "recorded_at_ms": recorded_at_ms,
    }


def reconciliation_parent_payload(
    result: RunReconciliation,
    *,
    run_id: str,
    reconciliation_revision: int,
    environment: str,
    account_fingerprint: str,
    symbol: str,
    reconciled_at_ms: int,
    source: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Map a pure reconciliation result to its atomic parent row."""

    if not isinstance(result, RunReconciliation):
        raise ObservationContractError("RunReconciliation is required")
    texts = {
        "run_id": run_id,
        "environment": environment,
        "account_fingerprint": account_fingerprint,
        "symbol": symbol,
    }
    if any(not isinstance(value, str) or not value.strip() for value in texts.values()):
        raise ObservationContractError("reconciliation scope text is required")
    for name, value in (
        ("reconciliation_revision", reconciliation_revision),
        ("reconciled_at_ms", reconciled_at_ms),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ObservationContractError(f"{name} must be non-negative")
    return {
        **texts,
        "reconciliation_revision": reconciliation_revision,
        "reconciliation_status": result.reconciliation_status,
        "completeness_reason": result.completeness_reason,
        "gross_realized_pnl_usdc": result.gross_realized_pnl_usdc or 0.0,
        "commission_usdc": result.commission_usdc,
        "funding_usdc": result.funding_usdc,
        "net_pnl_usdc": result.net_pnl_usdc,
        "entry_maker_fills": result.entry_maker_fills,
        "entry_taker_fills": result.entry_taker_fills,
        "exit_maker_fills": result.exit_maker_fills,
        "exit_taker_fills": result.exit_taker_fills,
        "source": {
            "eligible_for_wr_ev": result.eligible_for_wr_ev,
            "exchange_trade_ids": list(result.exchange_trade_ids),
            "exchange_income_ids": list(result.exchange_income_ids),
            **dict(source or {}),
        },
        "reconciled_at_ms": reconciled_at_ms,
    }


__all__ = [
    "reconciliation_parent_payload",
    "session_checkpoint_payload",
    "shadow_evaluation_payload",
]
