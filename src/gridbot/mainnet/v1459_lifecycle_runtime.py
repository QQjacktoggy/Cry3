"""Restart-aware extension of the orderless v1.4.59 observation runtime."""

from __future__ import annotations

import json
from typing import Any, Mapping

from src.gridbot.mainnet.v1459_observation_contract import ObservationContractError
from src.gridbot.mainnet.v1459_observation_coordinator import (
    V1459ObservationCoordinator,
)
from src.gridbot.mainnet.v1459_observation_runtime import (
    V1459ObservationRuntime,
    V1459RuntimeContext,
)


def _json_value(row: Mapping[str, Any], key: str, expected_type: type):
    value = row.get(key)
    if not isinstance(value, str):
        raise ObservationContractError(f"{key} is required")
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise ObservationContractError(f"{key} is invalid JSON") from exc
    if not isinstance(parsed, expected_type):
        raise ObservationContractError(f"{key} has invalid type")
    return parsed


def restore_adaptive_session_snapshot(row: Mapping[str, Any]) -> dict[str, Any]:
    """Restore durable counters and the explicitly persisted re-arm authority."""

    if not isinstance(row, Mapping):
        raise ObservationContractError("durable session row must be a mapping")
    session_id = row.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ObservationContractError("durable session_id is required")
    counters = _json_value(row, "counters_json", dict)
    disabled_states = _json_value(row, "disabled_states_json", list)
    route_stats = _json_value(row, "route_stats_json", dict)
    revision = row.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise ObservationContractError("durable revision is invalid")
    status = str(row.get("status") or "").upper()
    rearm_pending = row.get("rearm_pending", 0)
    if isinstance(rearm_pending, bool):
        rearm_pending = int(rearm_pending)
    if rearm_pending not in (0, 1):
        raise ObservationContractError("durable rearm_pending is invalid")
    can_rearm = bool(
        status == "ACTIVE"
        and rearm_pending == 1
        and not row.get("stop_reason")
        and not row.get("pause_reason")
    )
    return {
        "session_id": session_id,
        "started_at_ms": int(row["started_at_ms"]),
        "last_checkpoint_at_ms": int(row["last_checkpoint_at_ms"]),
        "terminal_runs": int(row.get("terminal_runs") or 0),
        "gross_pnl_usdc": float(row.get("gross_pnl_usdc") or 0.0),
        "commission_usdc": float(row.get("commission_usdc") or 0.0),
        "funding_usdc": float(row.get("funding_usdc") or 0.0),
        "net_pnl_usdc": float(row.get("net_pnl_usdc") or 0.0),
        "high_water_net_pnl_usdc": float(
            row.get("high_water_net_pnl_usdc") or 0.0
        ),
        "counters": counters,
        "disabled_states": set(str(value) for value in disabled_states),
        "route_stats": route_stats,
        "durable_status": status,
        "rearm_enabled": can_rearm,
        "stop_requested": not can_rearm,
        "restart_recovered": True,
        "durable_revision": revision,
        "stop_reason": row.get("stop_reason"),
        "pause_reason": row.get("pause_reason"),
    }


class V1459LifecycleObservationRuntime(V1459ObservationRuntime):
    """Seeds checkpoint revisions from durable state without adding orders."""

    def __init__(
        self,
        *,
        coordinator: V1459ObservationCoordinator,
        context: V1459RuntimeContext,
        durable_session: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(coordinator=coordinator, context=context)
        self._durable_session = (
            None
            if durable_session is None
            else restore_adaptive_session_snapshot(durable_session)
        )
        if self._durable_session is not None:
            self._session_revisions[
                self._durable_session["session_id"]
            ] = self._durable_session["durable_revision"]

    @property
    def durable_session(self) -> dict[str, Any] | None:
        if self._durable_session is None:
            return None
        restored = dict(self._durable_session)
        restored["counters"] = dict(self._durable_session["counters"])
        restored["disabled_states"] = set(
            self._durable_session["disabled_states"]
        )
        restored["route_stats"] = dict(self._durable_session["route_stats"])
        return restored

    async def retire_durable_session(
        self,
        *,
        checkpoint_at_ms: int,
        stop_reason: str,
    ):
        """Durably close an orphaned restored session without re-arming it.

        The caller must already have established that no active run exists.
        This only writes observational state and never accesses an exchange.
        """

        if self._durable_session is None:
            from src.gridbot.mainnet.v1459_observation_coordinator import ObservationWriteResult

            return ObservationWriteResult(False, False, "NO_DURABLE_SESSION")
        if not isinstance(stop_reason, str) or not stop_reason.strip():
            raise ObservationContractError("stop_reason is required")
        snapshot = self.durable_session
        assert snapshot is not None
        snapshot.update(
            {
                "rearm_enabled": False,
                "stop_requested": True,
                "stopped_at_ms": checkpoint_at_ms,
                "stop_reason": stop_reason.strip(),
            }
        )
        result = await self.checkpoint_session(
            snapshot,
            checkpoint_at_ms=checkpoint_at_ms,
        )
        if result.status == "STOPPED" and (
            result.inserted or result.reason == "IDEMPOTENT_RETRY"
        ):
            self._durable_session = None
        return result


__all__ = [
    "V1459LifecycleObservationRuntime",
    "restore_adaptive_session_snapshot",
]
