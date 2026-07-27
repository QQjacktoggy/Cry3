from __future__ import annotations

import json

import pytest

from src.gridbot.storage.database import Database
from src.gridbot.storage.repositories import MainnetRunRepository


async def _terminal_run(
    repo: MainnetRunRepository,
    run_id: str,
) -> None:
    await repo.create_run(
        {
            "run_id": run_id,
            "symbol": "ETHUSDC",
            "strategy_label": "codex_v1",
            "status": "ARMED",
        }
    )
    await repo.complete_run(run_id, "COMPLETED")


@pytest.mark.asyncio
async def test_v1463_unresolved_shadow_query_matches_exact_sample(
    tmp_path,
) -> None:
    db = Database(str(tmp_path / "v1463-reconcile.db"))
    await db.initialize()
    repo = MainnetRunRepository(db)
    try:
        await _terminal_run(repo, "unresolved")
        await repo.log_event(
            "unresolved",
            "entry_codex_v1_shadow_sample_started",
            {"sample_id": "sample-a"},
        )

        await _terminal_run(repo, "resolved")
        await repo.log_event(
            "resolved",
            "entry_codex_v1_shadow_sample_started",
            {"sample_id": "sample-b"},
        )
        await repo.log_event(
            "resolved",
            "entry_codex_v1_shadow_outcome",
            {"sample_id": "sample-b"},
        )

        await _terminal_run(repo, "wrong-terminal")
        await repo.log_event(
            "wrong-terminal",
            "entry_codex_v1_shadow_sample_started",
            {"sample_id": "sample-c"},
        )
        await repo.log_event(
            "wrong-terminal",
            "entry_codex_v1_shadow_sample_dropped",
            {"sample_id": "another-sample"},
        )

        rows = (
            await repo.get_terminal_runs_with_unresolved_v1463_shadow_samples()
        )

        assert {row["run_id"] for row in rows} == {
            "unresolved",
            "wrong-terminal",
        }
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_v132_outcome_drop_and_commit_are_all_terminal(
    tmp_path,
) -> None:
    db = Database(str(tmp_path / "v132-reconcile.db"))
    await db.initialize()
    repo = MainnetRunRepository(db)
    try:
        terminal_types = {
            "outcome": "entry_codex_v1_tp_policy_shadow_outcome",
            "drop": "entry_codex_v1_tp_policy_shadow_dropped",
            "commit": "entry_codex_v1_tp_policy_shadow_terminal_committed",
        }
        for suffix, event_type in terminal_types.items():
            run_id = f"resolved-{suffix}"
            paired_id = f"pair-{suffix}"
            await _terminal_run(repo, run_id)
            await repo.log_event(
                run_id,
                "entry_codex_v1_tp_policy_shadow_started",
                {"paired_sample_id": paired_id},
            )
            await repo.log_event(
                run_id,
                event_type,
                {"paired_sample_id": paired_id},
            )

        await _terminal_run(repo, "unresolved")
        await repo.log_event(
            "unresolved",
            "entry_codex_v1_tp_policy_shadow_started",
            {"paired_sample_id": "pair-unresolved"},
        )

        rows = (
            await repo.get_terminal_runs_with_unresolved_v132_tp_policy_samples()
        )

        assert [row["run_id"] for row in rows] == ["unresolved"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_v1462_only_later_linked_event_resolves_opportunity(
    tmp_path,
) -> None:
    db = Database(str(tmp_path / "v1462-reconcile.db"))
    await db.initialize()
    repo = MainnetRunRepository(db)
    try:
        await repo.create_run(
            {
                "run_id": "ordering",
                "symbol": "ETHUSDC",
                "strategy_label": "codex_v1",
                "status": "ARMED",
            }
        )
        # A corrupt/pre-existing terminal before a durable opportunity must
        # not suppress reconciliation of the later opportunity.
        await repo.log_event(
            "ordering",
            "entry_codex_v1_shadow_outcome",
            {"v1462_opportunity_id": "opp-before"},
        )
        await repo.log_event(
            "ordering",
            "entry_codex_v1462_shadow_opportunity",
            {"v1462_opportunity_id": "opp-before"},
        )

        await repo.log_event(
            "ordering",
            "entry_codex_v1462_shadow_opportunity",
            {"v1462_opportunity_id": "opp-after"},
        )
        await repo.log_event(
            "ordering",
            "entry_codex_v1_shadow_sample_started",
            {"v1462_opportunity_id": "opp-after"},
        )

        rows = (
            await repo.get_unresolved_v1462_shadow_opportunity_events()
        )
        opportunity_ids = {
            json.loads(row["details_json"])["v1462_opportunity_id"]
            for row in rows
        }

        assert opportunity_ids == {"opp-before"}
    finally:
        await db.close()
