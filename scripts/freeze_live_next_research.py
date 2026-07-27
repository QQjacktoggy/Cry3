#!/usr/bin/env python3
"""Freeze the first Live Next 60/15/15 research contract before data access."""

from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta, timezone
import json
from pathlib import Path
from typing import Any, Sequence

from src.gridbot.strategy.live_next.config import (
    FrozenReplayProtocol,
    FrozenSplitManifest,
    UTC_DAY_MS,
    build_candidate_factory,
)
from src.gridbot.strategy.live_next.contracts import ContractError, canonical_sha256


DEFAULT_END_DATE = date(2026, 4, 1)
DEFAULT_OUTPUT = Path("reports/live_next_research_contract_v5_2026-07-16.json")
SOURCE = {
    "archive_url_template": (
        "https://data.binance.vision/data/futures/um/daily/aggTrades/"
        "ETHUSDC/ETHUSDC-aggTrades-{date}.zip"
    ),
    "causal_order": ["transact_time", "agg_trade_id"],
    "columns": [
        "agg_trade_id",
        "price",
        "quantity",
        "first_trade_id",
        "last_trade_id",
        "transact_time",
        "is_buyer_maker",
    ],
    "dataset": "aggTrades",
    "market": "BINANCE_UM_FUTURES",
    "symbol": "ETHUSDC",
    "timestamp_semantics": "EXCHANGE_TRANSACT_TIME_NOT_LOCAL_RECEIVE_TIME",
}
EXPERTS = (
    "impulse_retest",
    "trend_pullback",
    "range_reclaim",
    "shock_exhaustion",
)
EXECUTIONS = (
    "maker_near_0bp",
    "taker_confirm_100ms",
    "hybrid_maker0_500ms",
)
EXITS = (
    "net_tp24_sl8_hold30",
    "net_tp32_sl10_hold45",
)
REGISTRY_VERSION = "live_next.registry.v5"
EXPERT_PARAMETER_SETS = {
    "impulse_retest": {
        "impulse_window_ms": 3_000,
        "max_retrace_fraction": 0.65,
        "min_impulse_bps": 12.0,
        "min_impulse_flow_ratio": 0.58,
        "min_retrace_fraction": 0.20,
        "retest_window_ms": 5_000,
    },
    "trend_pullback": {
        "max_retrace_fraction": 0.55,
        "min_resume_flow_ratio": 0.56,
        "min_retrace_fraction": 0.20,
        "min_trend_bps": 20.0,
        "pullback_window_ms": 8_000,
        "resume_window_ms": 2_000,
        "trend_window_ms": 30_000,
    },
    "range_reclaim": {
        "boundary_fraction": 0.15,
        "min_boundary_reversal_bps": 1.0,
        "min_false_break_bps": 4.0,
        "min_reclaim_bps": 3.0,
        "min_reversal_flow_ratio": 0.56,
        "range_window_ms": 60_000,
        "reclaim_window_ms": 4_000,
    },
    "shock_exhaustion": {
        "cooldown_ms": 1_000,
        "max_retrace_fraction": 0.55,
        "max_setup_ms": 8_000,
        "min_retrace_fraction": 0.20,
        "min_reversal_flow_ratio": 0.58,
        "min_shock_bps": 25.0,
        "shock_window_ms": 2_000,
    },
}
EXECUTION_PROFILE_CONFIGS = {
    "maker_near_0bp": {
        "base_latency_ms": 100,
        "entry_offset_bps": 0.0,
        "entry_ttl_ms": 1_500,
        "maker_fill_model": "TRADE_THROUGH",
        "maker_phase_ms": 0,
        "max_reprices": 0,
        "mode": "MAKER",
    },
    "taker_confirm_100ms": {
        "base_latency_ms": 100,
        "entry_offset_bps": 0.0,
        "entry_ttl_ms": 1_000,
        "maker_fill_model": "TRADE_THROUGH",
        "maker_phase_ms": 0,
        "max_reprices": 0,
        "mode": "TAKER_CONFIRM",
    },
    "hybrid_maker0_500ms": {
        "base_latency_ms": 100,
        "entry_offset_bps": 0.0,
        "entry_ttl_ms": 2_000,
        "maker_fill_model": "TRADE_THROUGH",
        "maker_phase_ms": 500,
        "max_reprices": 0,
        "mode": "HYBRID",
    },
}
EXIT_PROFILE_CONFIGS = {
    "net_tp24_sl8_hold30": {
        "stop_loss_bps": 8.0, "t1_min_mfe_bps": 0.0,
        "t1_ms": 8_000, "t2_ms": 30_000, "take_profit_bps": 24.0,
    },
    "net_tp32_sl10_hold45": {
        "stop_loss_bps": 10.0, "t1_min_mfe_bps": 0.0,
        "t1_ms": 10_000, "t2_ms": 45_000, "take_profit_bps": 32.0,
    },
}
FEATURE_CONFIG = {
    "causal_closed_bins_only": True,
    "feature_version": "live_next.tradeflow_features.v2",
    "forbidden": ["future", "outcome", "realized_pnl", "mfe", "mae"],
    "regime_version": "live_next.regime.v1",
}
COST_MODEL = {
    "active_adverse_slippage_bps": 1.0,
    "active_exit_fee_bps": 5.0,
    "active_adverse_stress_bps": [0.0, 1.0, 2.5],
    "entry_fee_bps": 2.0,
    "fee_stress_multipliers": [1.0, 1.25, 1.5],
    "fill_models": ["TRADE_THROUGH", "TOUCH"],
    "funding_cost_usdc_per_fill": 0.005,
    "notional_usdc": 50.0,
    "tick_size": "0.01",
    "tp_exit_fee_bps": 2.0,
    "taker_entry_fee_bps": 5.0,
    "taker_entry_slippage_bps": 1.0,
}


def _midnight_ms(day: date) -> int:
    value = datetime.combine(day, time.min, tzinfo=timezone.utc)
    return int(value.timestamp() * 1000)


def _days(start: date, end: date) -> list[str]:
    return [
        (start + timedelta(days=index)).isoformat()
        for index in range((end - start).days)
    ]


def _registry_contract() -> dict[str, Any]:
    return {
        "execution_profiles": EXECUTION_PROFILE_CONFIGS,
        "exit_profiles": EXIT_PROFILE_CONFIGS,
        "expert_parameter_sets": EXPERT_PARAMETER_SETS,
        "registry_version": REGISTRY_VERSION,
    }


def _candidate_parameter_overrides() -> dict[str, dict[str, Any]]:
    return {
        f"{expert}|{execution}|{exit_profile}": {
            "execution": EXECUTION_PROFILE_CONFIGS[execution],
            "exit": EXIT_PROFILE_CONFIGS[exit_profile],
            "expert": EXPERT_PARAMETER_SETS[expert],
            "registry_version": REGISTRY_VERSION,
        }
        for expert in EXPERTS
        for execution in EXECUTIONS
        for exit_profile in EXITS
    }


def build_contract(
    *,
    end_date: date = DEFAULT_END_DATE,
    created_at_ms: int | None = None,
) -> dict[str, Any]:
    end_ms = _midnight_ms(end_date)
    created_at_ms = end_ms if created_at_ms is None else created_at_ms
    source_contract_hash = canonical_sha256(SOURCE)
    split = FrozenSplitManifest.chronological_days(
        dataset_id="live_next_ethusdc_aggtrades_2026q1_v1",
        source_kind="binance_public_um_futures_daily_aggtrades",
        source_artifact_sha256=source_contract_hash,
        end_ms=end_ms,
        created_at_ms=created_at_ms,
    )
    candidates = build_candidate_factory(
        expert_families=EXPERTS,
        execution_profiles=EXECUTIONS,
        exit_profiles=EXITS,
        parameter_overrides=_candidate_parameter_overrides(),
    )
    protocol = FrozenReplayProtocol(
        stage="TRAIN",
        split_manifest_hash=split.manifest_hash,
        candidate_ids=tuple(candidate.candidate_id for candidate in candidates),
        feature_config_hash=canonical_sha256(FEATURE_CONFIG),
        cost_model_hash=canonical_sha256(COST_MODEL),
    )
    train_start = datetime.fromtimestamp(split.train.start_ms / 1000, tz=timezone.utc).date()
    validation_start = datetime.fromtimestamp(split.validation.start_ms / 1000, tz=timezone.utc).date()
    holdout_start = datetime.fromtimestamp(split.holdout.start_ms / 1000, tz=timezone.utc).date()
    holdout_end = datetime.fromtimestamp(split.holdout.end_ms / 1000, tz=timezone.utc).date()
    body: dict[str, Any] = {
        "schema_version": "live_next.research_contract.v1",
        "status": "FROZEN_TRAIN_ONLY",
        "created_at_ms": created_at_ms,
        "source": SOURCE,
        "source_contract_hash": source_contract_hash,
        "split_manifest": split.to_dict(),
        "split_days": {
            "TRAIN": _days(train_start, validation_start),
            "VALIDATION": _days(validation_start, holdout_start),
            "HOLDOUT": _days(holdout_start, holdout_end),
        },
        "candidate_menu": [
            {
                "candidate_id": candidate.candidate_id,
                "execution_profile": candidate.execution_profile,
                "exit_profile": candidate.exit_profile,
                "expert_family": candidate.expert_family,
                "parameters_json": candidate.parameters_json,
            }
            for candidate in candidates
        ],
        "registry_contract": _registry_contract(),
        "registry_config_hash": canonical_sha256(_registry_contract()),
        "feature_config": FEATURE_CONFIG,
        "cost_model": COST_MODEL,
        "train_replay_protocol": {
            "candidate_ids": list(protocol.candidate_ids),
            "cost_model_hash": protocol.cost_model_hash,
            "feature_config_hash": protocol.feature_config_hash,
            "portfolio_config_hash": protocol.portfolio_config_hash,
            "protocol_hash": protocol.protocol_hash,
            "schema_version": protocol.schema_version,
            "split_manifest_hash": protocol.split_manifest_hash,
            "stage": protocol.stage.value,
        },
        "access_policy": {
            "current_allowed_split": "TRAIN",
            "holdout_accessed": False,
            "holdout_one_shot_ledger_required": True,
            "network_access_from_replay": False,
            "validation_accessed": False,
        },
        "safety": {
            "credentials_used": False,
            "live_deployment": False,
            "offline_research_only": True,
            "orders_enabled": False,
            "runtime_wiring_modified": False,
        },
    }
    body["contract_hash"] = canonical_sha256(body)
    return body


def validate_contract(contract: dict[str, Any]) -> str:
    body = {key: value for key, value in contract.items() if key != "contract_hash"}
    expected = canonical_sha256(body)
    if contract.get("contract_hash") != expected:
        raise ContractError("Live Next research contract hash mismatch")
    if contract.get("status") != "FROZEN_TRAIN_ONLY":
        raise ContractError("research contract is not TRAIN-only")
    days = contract.get("split_days", {})
    if [len(days.get(role, ())) for role in ("TRAIN", "VALIDATION", "HOLDOUT")] != [60, 15, 15]:
        raise ContractError("research contract must contain exact 60/15/15 days")
    if contract.get("access_policy", {}).get("current_allowed_split") != "TRAIN":
        raise ContractError("only TRAIN may be opened before frontier freeze")
    menu = contract.get("candidate_menu", ())
    if not isinstance(menu, list) or len(menu) != 24:
        raise ContractError("research contract must freeze exactly 24 candidates")
    if len({item.get("candidate_id") for item in menu}) != 24:
        raise ContractError("candidate IDs must be unique")
    for item in menu:
        try:
            parameters = json.loads(item["parameters_json"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ContractError("candidate parameters must be frozen JSON") from exc
        if set(parameters) != {"execution", "exit", "expert", "registry_version"}:
            raise ContractError("candidate parameters are incomplete")
        if parameters["registry_version"] != REGISTRY_VERSION:
            raise ContractError("candidate registry version mismatch")
    if contract.get("safety", {}).get("orders_enabled") is not False:
        raise ContractError("research contract must prohibit orders")
    return expected


def write_contract(path: Path, contract: dict[str, Any]) -> None:
    validate_contract(contract)
    payload = json.dumps(contract, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if existing != payload:
            raise ContractError("refusing to overwrite a different frozen contract")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(payload, encoding="utf-8", newline="\n")
    temporary.replace(path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--end-date", type=date.fromisoformat, default=DEFAULT_END_DATE)
    args = parser.parse_args(argv)
    contract = build_contract(end_date=args.end_date)
    write_contract(args.output, contract)
    print(json.dumps({
        "candidate_count": len(contract["candidate_menu"]),
        "contract_hash": contract["contract_hash"],
        "output": str(args.output),
        "split_counts": {key: len(value) for key, value in contract["split_days"].items()},
        "status": contract["status"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
