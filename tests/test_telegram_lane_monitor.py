import asyncio
import json
import re
import sqlite3
from html import unescape
from pathlib import Path

from src.gridbot.telegram import lane_monitor as lane_monitor_module
from src.gridbot.mainnet.v1462_admission import V1462_POLICY_HASH
from src.gridbot.mainnet.v1462_lane_registry import (
    CNL_SAFE_LINEAGE_KIND,
    REGISTRY_HASH as V1462_REGISTRY_HASH,
)
from src.gridbot.storage.database import Database
from src.gridbot.storage.v1469_arm_observation_repository import (
    V1469ArmObservationRepository,
    candidate_identity,
)
from src.gridbot.telegram.lane_monitor import (
    _TABLE_QUERIES,
    _event_query,
    _is_reject_reopen_live_breach,
    build_lane_detail,
    build_lane_monitor,
    collect_lane_evidence,
    lane_monitor_html_chunks,
    lane_monitor_keyboard,
    normalize_lane_registry,
)


class FakeDb:
    def __init__(self, tables, *, with_snapshot=True):
        self.tables = dict(tables)
        if with_snapshot:
            legacy = self.tables.get("v1462_lane_monitor_legacy_summary", [])
            event_cutoff = max(
                (int(row.get("snapshot_max_event_id") or 0) for row in legacy),
                default=0,
            )
            time_cutoff = max(
                (int(row.get("snapshot_at_ms") or 0) for row in legacy),
                default=0,
            )
            self.tables.setdefault("v1462_lane_monitor_legacy_summary", legacy)
            self.tables.setdefault("v1463_lane_monitor_snapshot_meta", [{
                "singleton_id": 1,
                "snapshot_max_event_id": event_cutoff,
                "snapshot_at_ms": time_cutoff,
                "created_at_ms": 1,
            }])
        self.queries = []

    async def fetchall(self, sql):
        self.queries.append(sql)
        table = next(
            (name for name in self.tables if f"FROM {name}" in sql),
            "",
        )
        if table not in self.tables:
            raise RuntimeError("no such table")
        return self.tables[table]


def test_monitor_keeps_all_frozen_lanes_and_escapes_evidence():
    db = FakeDb({
        "adaptive_opportunities": [
            {"session_id": "s", "opportunity_id": "one", "lane_code": "STUP-S", "observed_at_ms": 1_000},
            {"session_id": "s", "opportunity_id": "two", "lane_code": "CNL-WPR-L", "observed_at_ms": 1_001},
        ],
        "shadow_evaluations": [
            {"session_id": "s", "opportunity_id": "one", "data_quality": "COMPLETE", "input_json": json.dumps({"outcome": "tp1_first", "net_pnl_usdc": 0.02}), "recorded_at_ms": 2_000, "evidence_evaluator_eligible": True, "fill_model": "limit_touch"},
            {"session_id": "s", "opportunity_id": "two", "data_quality": "DATA_INCOMPLETE", "invalid_reason": "<feed missing>", "recorded_at_ms": 2_001, "evidence_evaluator_eligible": True, "fill_model": "limit_touch"},
        ],
        "adaptive_sessions": [],
        "mainnet_runs": [
            {"run_id": "paid", "status": "COMPLETED", "signal_json": json.dumps({"codex_v1": {"lane_code": "STUP-S"}}), "realized_pnl_usdc": 0.1, "commission_usdc": 0.01, "armed_at_ms": 3_000},
        ],
        "mainnet_run_events": [],
    })

    text = asyncio.run(build_lane_monitor(db, now_ms=4_000))

    assert "27 frozen lanes" in text
    assert "<code>W6B" in text and "cap 0" in text
    assert "STUP-S" in text and "DATA_BLOCKED" in text
    assert "CNL-WPR-L" in text and "DATA_BLOCKED" in text
    assert "&lt;feed missing&gt;" in text
    assert "evaluable=1/8 (need 7)" in text
    assert all(query.startswith("SELECT * FROM ") for query in db.queries)
    assert all(query.startswith("SELECT * FROM ") for query in db.queries)


def test_missing_non_snapshot_tables_are_safe_and_detail_is_zero_sample():
    text = asyncio.run(build_lane_monitor(FakeDb({}), now_ms=10_000))
    detail = asyncio.run(build_lane_detail(FakeDb({}), "W6C", now_ms=10_000))

    assert "unavailable" in text
    assert "v1.4.69 rolling 90m observation-only unavailable" in text
    assert "observation tables/query unavailable" in detail
    assert "no live/order authority" in detail
    assert "W6C" in text
    assert "0/0/0/0" in detail
    assert "COLLECTING" in detail


def test_v1469_observation_metrics_are_additive_and_never_authority(
    tmp_path: Path,
):
    async def scenario() -> tuple[str, str]:
        db = Database(str(tmp_path / "lane-monitor-v1469.db"))
        await db.initialize()
        repo = V1469ArmObservationRepository(db)
        try:
            first_opportunity = {
                "opportunity_id": "opp-selected-blocked",
                "environment": "MAINNET",
                "symbol": "ETHUSDC",
                "observed_at_ms": 1_000,
                "feature_at_ms": 950,
                "coarse_regime": "RANGE",
                "regime_confidence": 0.7,
                "feature_schema": "v1469.feature.1",
                "feature_snapshot": {"score": 70},
                "source_run_id": "run-1",
                "source_event_id": "event-1",
                "data_quality": "COMPLETE",
                "created_at_ms": 1_000,
            }
            selected_blocked = {
                "opportunity_id": "opp-selected-blocked",
                "lane_code": "W6A",
                "effective_side": "LONG",
                "strategy": "S1_BB_RSI",
                "match_status": "MATCH",
                "safety_status": "HARD_BLOCK",
                "is_selected": True,
                "selection_rank": 0,
                "suppression_reason": "legacy_selected_then_blocked",
                "suppressed_by_lane_code": "STUP-S",
                "matcher_version": "v1.4.69",
                "matcher_hash": "matcher-selected-blocked",
                "data_complete": False,
                "annotations": {},
                "created_at_ms": 1_001,
            }
            await repo.insert_observation(
                first_opportunity, [selected_blocked]
            )

            second_opportunity = {
                **first_opportunity,
                "opportunity_id": "opp-safe-shadow",
                "observed_at_ms": 2_000,
                "feature_at_ms": 1_950,
                "feature_snapshot": {"score": 72},
                "source_run_id": "run-2",
                "source_event_id": "event-2",
                "created_at_ms": 2_000,
            }
            safe_shadow = {
                **selected_blocked,
                "opportunity_id": "opp-safe-shadow",
                "safety_status": "SAFE",
                "is_selected": False,
                "selection_rank": 1,
                "suppression_reason": "legacy_selector_owned_elsewhere",
                "suppressed_by_lane_code": "ANCHOR-S",
                "matcher_hash": "matcher-safe-shadow",
                "data_complete": True,
                "created_at_ms": 2_001,
            }
            await repo.insert_observation(second_opportunity, [safe_shadow])
            evidence = {
                "opportunity_id": "opp-safe-shadow",
                "candidate_id": candidate_identity(safe_shadow),
                "execution_profile_id": "RANGE_SCALP",
                "execution_profile_schema": "v1469.execution-profile.1",
                "execution_profile_hash": "profile-range-scalp",
                "source_type": "SHADOW",
                "diagnostic_only": False,
                "observed_at_ms": 2_000,
                "created_at_ms": 2_001,
            }
            await repo.append_evidence(evidence)
            stored = await db.fetchone(
                """SELECT evidence_id FROM v1469_arm_evidence
                WHERE opportunity_id = 'opp-safe-shadow'"""
            )
            assert stored is not None
            await repo.terminal_evidence(
                stored["evidence_id"],
                {
                    "status": "TERMINAL",
                    "terminal_at_ms": 2_100,
                    "outcome": "tp1_first",
                    "fill_status": "FILLED",
                    "data_complete": True,
                    "ambiguous": False,
                    "reward_net_bp": 4.2,
                    "mfe_bp": 6.0,
                    "mae_bp": 1.0,
                    "terminal_reason": "tp1_first",
                    "terminal_payload": {},
                    "updated_at_ms": 2_100,
                },
            )
            return (
                await build_lane_monitor(db, now_ms=3_000),
                await build_lane_detail(db, "W6A", now_ms=3_000),
            )
        finally:
            await db.close()

    overview, detail = asyncio.run(scenario())
    assert "v1.4.69 rolling 90m observation-only AVAILABLE" in overview
    assert "opportunities 2/2 complete" in overview
    assert "matched 2 | selected 1 | suppressed 2" in overview
    assert "safe 1 | hard 1 | data 0 | not-eval 0 | evaluable 1" in overview
    assert "EV +4.20 bp | fresh 1s" in overview
    assert "no live/order authority" in overview
    assert "matched/selected/suppressed: <b>2/1/2</b>" in detail
    assert (
        "safe/hard/data/not-eval/evaluable: <b>1/1/0/0/1</b>"
        in detail
    )
    assert "EV: <b>+4.20 bp</b> | freshness: <b>1s</b>" in detail
    assert "suppressed-by: <code>ANCHOR-S=1, STUP-S=1</code>" in detail
    assert "Read-only evidence; no live/order authority." in detail


def test_v1469_zero_match_reason_distinguishes_capture_from_no_capture(
    tmp_path: Path,
):
    async def scenario() -> str:
        db = Database(str(tmp_path / "lane-monitor-v1469-zero.db"))
        await db.initialize()
        repo = V1469ArmObservationRepository(db)
        try:
            await repo.insert_observation(
                {
                    "opportunity_id": "opp-other-lane",
                    "environment": "MAINNET",
                    "symbol": "ETHUSDC",
                    "observed_at_ms": 1_000,
                    "feature_at_ms": 1_000,
                    "coarse_regime": "RANGE",
                    "regime_confidence": None,
                    "feature_schema": "v1469.feature.1",
                    "feature_snapshot": {"score": 70},
                    "source_run_id": "run-other",
                    "source_event_id": None,
                    "data_quality": "COMPLETE",
                    "created_at_ms": 1_000,
                },
                [{
                    "opportunity_id": "opp-other-lane",
                    "lane_code": "W6A",
                    "effective_side": "LONG",
                    "strategy": "S1_BB_RSI",
                    "match_status": "MATCH",
                    "safety_status": "SAFE",
                    "is_selected": True,
                    "selection_rank": 0,
                    "suppression_reason": None,
                    "suppressed_by_lane_code": None,
                    "matcher_version": "v1.4.69",
                    "matcher_hash": "matcher-zero-reason",
                    "data_complete": True,
                    "annotations": {},
                    "created_at_ms": 1_000,
                }],
            )
            return await build_lane_detail(
                db,
                "W1E",
                now_ms=2_000,
            )
        finally:
            await db.close()

    detail = asyncio.run(scenario())
    assert (
        "predicate did not match across 1 captured opportunities"
        in detail
    )


def test_registry_and_callbacks_are_complete_and_short():
    registry = normalize_lane_registry([
        {"lane_code": "RP1", "intended_mode": "LIVE_ALLOWLIST"},
        {"lane_code": "EXTRA", "intended_mode": "LIVE_ALLOWLIST"},
    ])
    assert len(registry) == 27
    assert all(row["lane_code"] != "EXTRA" for row in registry)
    markup = lane_monitor_keyboard(registry)
    rows = markup.inline_keyboard if hasattr(markup, "inline_keyboard") else markup
    callbacks = [button.callback_data for row in rows for button in row]
    assert "mainnet:lanes:refresh" in callbacks
    assert "mainnet:lane:CNL-WPR-L" in callbacks
    assert all(len(value.encode()) <= 64 for value in callbacks)


def test_normal_one_run_shadow_events_are_counted_without_adaptive_session():
    event_base = {
        "run_id": "cry3mn_event_only",
        "event_time_ms": 10_000,
    }
    db = FakeDb({
        "adaptive_opportunities": [],
        "shadow_evaluations": [],
        "adaptive_sessions": [],
        "mainnet_runs": [],
        "mainnet_run_events": [
            {
                **event_base,
                "id": 1,
                "event_type": "entry_codex_v1462_shadow_opportunity",
                "details_json": json.dumps({
                    "opportunity_id": "v1462_opp_1",
                    "lane_code": "W6B",
                }),
            },
            {
                **event_base,
                "id": 2,
                "event_type": "entry_codex_v1_shadow_outcome",
                "details_json": json.dumps({
                    "opportunity_id": "v1462_opp_1",
                    "lane_code": "W6B",
                    "shadow_outcome": "sl_first",
                    "data_complete": True,
                        "data_quality": {"complete": True},
                        "paper_pnl_usdc_after_fee": -0.08,
                        "evidence_evaluator_eligible": True,
                        "fill_model": "limit_touch",
                }),
            },
        ],
    })

    detail = asyncio.run(build_lane_detail(db, "W6B", now_ms=11_000))

    assert "1/1/1/0" in detail
    assert "sl_first=1" in detail
    assert "-0.0800 USDC" in detail


def test_distinct_ticket_same_run_lane_is_not_swallowed_by_sample():
    db = FakeDb({
        "adaptive_opportunities": [
            {"session_id": "s", "opportunity_id": "same", "lane_code": "W6B", "observed_at_ms": 1_000},
        ],
        "shadow_evaluations": [
            {
                "session_id": "s", "opportunity_id": "same", "data_quality": "COMPLETE",
                "input_json": json.dumps({"outcome": "tp1_first"}), "net_pnl_usdc": 0.2,
                "recorded_at_ms": 2_000, "evidence_evaluator_eligible": True,
                "fill_model": "limit_touch",
            },
        ],
        "mainnet_runs": [],
        "mainnet_run_events": [
            {
                "id": 1, "run_id": "r", "event_time_ms": 900,
                "event_type": "entry_codex_v1462_shadow_opportunity",
                "details_json": json.dumps({"opportunity_id": "ticket-id", "lane_code": "W6B"}),
            },
            {
                "id": 2, "run_id": "r", "event_time_ms": 1_000,
                "event_type": "entry_codex_v1_shadow_sample_started",
                "details_json": json.dumps({"opportunity_id": "same", "sample_id": "strict", "lane_code": "W6B"}),
            },
            {
                "id": 3, "run_id": "r", "event_time_ms": 2_100,
                "event_type": "entry_codex_v1_shadow_outcome",
                "details_json": json.dumps({
                    "opportunity_id": "same", "sample_id": "diag", "lane_code": "W6B",
                    "shadow_outcome": "sl_first", "data_complete": True,
                    "data_quality": {"complete": True}, "paper_pnl_usdc_after_fee": -0.5,
                    "diagnostic_only": True, "fill_model": "immediate_shadow",
                }),
            },
        ],
    })

    lanes, _ = asyncio.run(collect_lane_evidence(db))
    lane = lanes["W6B"]

    assert lane.captured == 2
    assert lane.pending == 1
    assert lane.invalid == 0
    assert lane.incomplete == 0
    assert lane.complete == 1
    assert lane.outcomes == {"tp1_first": 1}
    assert lane.ev_count == 1
    assert lane.ev_total == 0.2


def test_diagnostic_only_terminal_never_counts_as_formal_outcome():
    identity = {
        "lane_code": "W6B",
        "v1462_opportunity_id": "durable-diagnostic",
        "state": "clean",
        "effective_side": "LONG",
        "registry_hash": V1462_REGISTRY_HASH,
        "policy_hash": V1462_POLICY_HASH,
        "resolved_profile_hash": "profile-one",
    }
    db = FakeDb({
        "adaptive_opportunities": [],
        "shadow_evaluations": [],
        "mainnet_runs": [],
        "mainnet_run_events": [
            {
                "id": 1, "run_id": "r", "event_time_ms": 1_000,
                "event_type": "entry_codex_v1_shadow_sample_started",
                "details_json": json.dumps({
                    **identity, "sample_id": "strict", "diagnostic_only": False,
                    "fill_model": "limit_touch", "evidence_evaluator_eligible": True,
                }),
            },
            {
                "id": 2, "run_id": "r", "event_time_ms": 1_100,
                "event_type": "entry_codex_v1_shadow_outcome",
                "details_json": json.dumps({
                    **identity, "sample_id": "diag", "diagnostic_only": True,
                    "fill_model": "immediate_shadow",
                    "evidence_evaluator_eligible": False,
                    "shadow_outcome": "tp1_first", "data_complete": True,
                    "data_quality": {"complete": True},
                    "paper_pnl_usdc_after_fee": 0.5,
                }),
            },
        ],
    })

    lane = asyncio.run(collect_lane_evidence(db))[0]["W6B"]

    assert (lane.captured, lane.complete, lane.evaluable, lane.pending) == (1, 0, 0, 1)
    assert lane.ev_count == 0
    assert lane.ev_total == 0.0


def test_v1465_profile_clones_do_not_pollute_legacy_lane_totals():
    common = {
        "lane_code": "W6A",
        "v1465_profile_evidence": True,
        "v1465_opportunity_id": "paired-one",
        "sample_id": "v1465-profile-one",
        "v1465_profile_id": "W6A_TIGHT",
    }
    db = FakeDb({
        "adaptive_opportunities": [],
        "shadow_evaluations": [],
        "mainnet_runs": [],
        "mainnet_run_events": [
            {
                "id": 1,
                "run_id": "r",
                "event_time_ms": 1_000,
                "event_type": "entry_codex_v1_shadow_sample_started",
                "details_json": json.dumps(common),
            },
            {
                "id": 2,
                "run_id": "r",
                "event_time_ms": 2_000,
                "event_type": "entry_codex_v1_shadow_outcome",
                "details_json": json.dumps({
                    **common,
                    "shadow_outcome": "tp1_first",
                    "data_complete": True,
                    "paper_pnl_usdc_after_fee": 0.1,
                }),
            },
        ],
    })

    lane = asyncio.run(collect_lane_evidence(db))[0]["W6A"]

    assert lane.captured == 0
    assert lane.complete == 0
    assert lane.pending == 0
    assert lane.ev_count == 0


def test_incomplete_is_excluded_from_ev_and_no_fill_has_zero_ev():
    db = FakeDb({
        "adaptive_opportunities": [
            {"session_id": "s", "opportunity_id": "bad", "lane_code": "W6B"},
            {"session_id": "s", "opportunity_id": "nf", "lane_code": "W6B"},
            {"session_id": "s", "opportunity_id": "amb", "lane_code": "W6B"},
        ],
        "shadow_evaluations": [
            {
                "session_id": "s", "opportunity_id": "bad", "data_quality": "DATA_INCOMPLETE",
                "input_json": json.dumps({"outcome": "sl_first", "data_complete": "false"}),
                "net_pnl_usdc": -9.0, "recorded_at_ms": 1,
                "evidence_evaluator_eligible": True, "fill_model": "limit_touch",
            },
            {
                "session_id": "s", "opportunity_id": "nf", "data_quality": "COMPLETE",
                "input_json": json.dumps({"outcome": "no_fill"}), "net_pnl_usdc": None,
                "recorded_at_ms": 1, "evidence_evaluator_eligible": True,
                "fill_model": "limit_touch",
            },
            {
                "session_id": "s", "opportunity_id": "amb", "data_quality": "COMPLETE",
                "input_json": json.dumps({"outcome": "ambiguous_both", "data_complete": True}),
                "net_pnl_usdc": -1.0, "recorded_at_ms": 1,
                "evidence_evaluator_eligible": True, "fill_model": "limit_touch",
            },
        ],
        "mainnet_runs": [],
        "mainnet_run_events": [],
    })

    lane = asyncio.run(collect_lane_evidence(db))[0]["W6B"]

    assert (lane.captured, lane.complete, lane.evaluable, lane.invalid) == (3, 1, 0, 2)
    assert lane.incomplete == 1
    assert lane.ambiguous == 1
    assert lane.ev_count == 1
    assert lane.ev_total == 0.0


def test_paid_uses_completed_net_after_commission_and_event_freshness():
    signal = json.dumps({"codex_v1": {"lane_code": "RP1"}})
    db = FakeDb({
        "adaptive_opportunities": [],
        "shadow_evaluations": [],
        "mainnet_runs": [
            {"run_id": "win", "status": "COMPLETED", "signal_json": signal, "realized_pnl_usdc": 1.0, "commission_usdc": 0.2, "completed_at_ms": 8_000},
            {"run_id": "cancel", "status": "CANCELLED", "signal_json": signal, "realized_pnl_usdc": 10.0, "commission_usdc": 0.0, "completed_at_ms": 9_000},
        ],
        "mainnet_run_events": [
            {
                "id": 1, "run_id": "ticket-only", "event_time_ms": 9_500,
                "event_type": "entry_codex_v1462_shadow_opportunity",
                "details_json": json.dumps({"opportunity_id": "ticket", "lane_code": "RP1"}),
            },
        ],
    })

    lane = asyncio.run(collect_lane_evidence(db))[0]["RP1"]
    detail = asyncio.run(build_lane_detail(db, "rp1", now_ms=10_000))

    assert lane.captured == 1
    assert lane.paid_count == 1
    assert lane.paid_wins == 1
    assert lane.paid_net == 0.8
    assert "Freshness: <b>0s</b>" in detail


def test_monitor_reads_durable_sources_and_optional_legacy_snapshot():
    db = FakeDb({
        "adaptive_opportunities": [], "shadow_evaluations": [],
        "mainnet_runs": [], "mainnet_run_events": [],
    })

    asyncio.run(build_lane_monitor(db))

    assert len(db.queries) == 12
    assert "FROM v1463_lane_monitor_snapshot_meta" in db.queries[0]
    assert "FROM adaptive_opportunities" in db.queries[1]
    assert "FROM shadow_evaluations" in db.queries[2]
    assert "FROM mainnet_runs" in db.queries[3]
    assert "FROM v1462_lane_monitor_legacy_summary" in db.queries[4]
    assert any("FROM v1464_promotion_evidence" in query for query in db.queries)
    assert any(
        "FROM v1464_lane_promotion_leases" in query for query in db.queries
    )
    assert any(
        "FROM v1464_lane_promotion_events" in query for query in db.queries
    )
    assert any(
        "FROM v1465_w6a_profile_evidence" in query for query in db.queries
    )
    assert any(
        "FROM v1465_w6a_profile_selections" in query for query in db.queries
    )
    assert any(
        "FROM v1465_w6a_profile_selection_events" in query for query in db.queries
    )
    assert "FROM mainnet_run_events" in db.queries[5]
    assert db.queries[5].count("UNION ALL") == 4
    assert "event_type IN" not in db.queries[5]
    assert all(
        f"event_type = '{event_type}'" in db.queries[5]
        for event_type in (
            "entry_codex_v1462_admission",
            "entry_codex_v1462_shadow_opportunity",
            "entry_codex_v1_shadow_sample_started",
            "entry_codex_v1_shadow_outcome",
        )
    )
    assert "LIMIT" not in db.queries[1]
    assert "LIMIT" not in db.queries[2]
    assert "LIMIT" not in db.queries[5]


def test_legacy_snapshot_is_visible_but_excluded_from_current_cohort():
    identity = {
        "registry_hash": V1462_REGISTRY_HASH,
        "policy_hash": V1462_POLICY_HASH,
        "resolved_profile_hash": "profile-one",
        "state": "clean",
        "effective_side": "LONG",
    }
    db = FakeDb({
        "adaptive_opportunities": [
            {"opportunity_id": "old", "lane_code": "W6B", "observed_at_ms": 900},
            {
                "opportunity_id": "new",
                "v1462_opportunity_id": "new",
                "lane_code": "W6B",
                "observed_at_ms": 1_100,
                **identity,
            },
        ],
        "shadow_evaluations": [{
            "opportunity_id": "new",
            "v1462_opportunity_id": "new",
            "lane_code": "W6B",
            "recorded_at_ms": 1_200,
                "data_quality": "COMPLETE",
                "outcome": "tp1_first",
                "net_pnl_usdc": 0.2,
                "evidence_evaluator_eligible": True,
                "fill_model": "limit_touch",
                **identity,
        }],
        "mainnet_runs": [],
        "v1462_lane_monitor_legacy_summary": [{
            "lane_code": "W6B",
            "outcome_opportunities": 12,
            "last_outcome_at_ms": 950,
            "snapshot_max_event_id": 100,
            "snapshot_at_ms": 1_000,
        }],
        "mainnet_run_events": [],
    })

    lane = asyncio.run(collect_lane_evidence(db))[0]["W6B"]

    assert lane.captured == 1
    assert lane.complete == 1
    assert lane.legacy_adaptive == 1
    assert lane.legacy_shadow_outcomes == 12
    assert "id > 100" in db.queries[-1]


def test_event_query_scans_post_snapshot_rowids_without_temp_sort_and_preserves_results():
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE mainnet_run_events ("
        "id INTEGER PRIMARY KEY, event_type TEXT NOT NULL, details_json TEXT)"
    )
    db.execute(
        "CREATE INDEX idx_mainnet_run_events_event_type_id "
        "ON mainnet_run_events(event_type, id DESC)"
    )
    relevant_types = (
        "entry_codex_v1462_admission",
        "entry_codex_v1462_shadow_opportunity",
        "entry_codex_v1_shadow_sample_started",
        "entry_codex_v1_shadow_sample_dropped",
        "entry_codex_v1_shadow_outcome",
    )
    rows = [
        (row_id, relevant_types[row_id % len(relevant_types)] if row_id % 3 else "noise", "{}")
        for row_id in range(1, 80)
    ]
    db.executemany("INSERT INTO mainnet_run_events VALUES (?, ?, ?)", rows)

    query = _event_query(40)
    old_query = (
        "SELECT * FROM mainnet_run_events WHERE id > 40 "
        "AND event_type IN (?,?,?,?,?) "
        "ORDER BY id DESC"
    )
    expected = db.execute(old_query, relevant_types).fetchall()
    actual = db.execute(query).fetchall()
    plan = [row[3] for row in db.execute(f"EXPLAIN QUERY PLAN {query}")]

    assert actual == expected
    assert any("INTEGER PRIMARY KEY (rowid>?)" in step for step in plan)
    assert all("idx_mainnet_run_events_event_type_id" not in step for step in plan)
    assert all("USE TEMP B-TREE" not in step for step in plan)


def test_event_details_json_is_decoded_once_per_collection(monkeypatch):
    details_json = json.dumps(
        {
            "lane_code": "W6B",
            "opportunity_id": "single-decode",
            "sample_id": "single-decode",
            "diagnostic_only": True,
        }
    )
    decode_calls = 0
    original_json = lane_monitor_module._json

    def counted_json(value):
        nonlocal decode_calls
        if value == details_json:
            decode_calls += 1
        return original_json(value)

    monkeypatch.setattr(lane_monitor_module, "_json", counted_json)
    db = FakeDb(
        {
            "adaptive_opportunities": [],
            "shadow_evaluations": [],
            "mainnet_runs": [],
            "mainnet_run_events": [
                {
                    "id": 1,
                    "run_id": "single-decode-run",
                    "event_time_ms": 1_000,
                    "event_type": "entry_codex_v1_shadow_sample_started",
                    "details_json": details_json,
                }
            ],
        }
    )

    asyncio.run(collect_lane_evidence(db))

    assert decode_calls == 1


def test_legacy_snapshot_migration_is_idempotent_and_freezes_global_cutoff():
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE mainnet_run_events ("
        "id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, event_time_ms INTEGER NOT NULL, "
        "event_type TEXT NOT NULL, details_json TEXT NOT NULL)"
    )
    rows = [
        (1, "r1", 100, "entry_codex_v1_shadow_outcome", json.dumps({"lane_code": "W6B", "opportunity_id": "one"})),
        (2, "r2", 200, "entry_codex_v1_shadow_outcome", json.dumps({"lane_code": "W6B", "opportunity_id": "two"})),
        (3, "r3", 300, "noise", "{}"),
    ]
    db.executemany("INSERT INTO mainnet_run_events VALUES (?, ?, ?, ?, ?)", rows)
    migration = Path(
        "src/gridbot/storage/migrations/009_v1462_lane_monitor_legacy_snapshot.sql"
    ).read_text(encoding="utf-8")

    db.executescript(migration)
    db.executescript(migration)
    snapshot = db.execute(
        "SELECT lane_code, outcome_opportunities, last_outcome_at_ms, "
        "snapshot_max_event_id, snapshot_at_ms "
        "FROM v1462_lane_monitor_legacy_summary"
    ).fetchall()

    assert snapshot == [("W6B", 2, 200, 3, 300)]

    v1463_migration = Path(
        "src/gridbot/storage/migrations/010_v1463_lane_monitor_snapshot_meta.sql"
    ).read_text(encoding="utf-8")
    db.executescript(v1463_migration)
    db.executescript(v1463_migration)
    meta = db.execute(
        "SELECT singleton_id, snapshot_max_event_id, snapshot_at_ms "
        "FROM v1463_lane_monitor_snapshot_meta"
    ).fetchall()
    assert meta == [(1, 3, 300)]


def _closed_row(opportunity_id, day, outcome, pnl):
    return {
        "opportunity_id": opportunity_id,
        "lane_code": "W6B",
        "state": "clean",
        "effective_side": "LONG",
        "registry_hash": V1462_REGISTRY_HASH,
        "policy_hash": V1462_POLICY_HASH,
        "resolved_profile_hash": "profile-one",
        "data_quality": "COMPLETE",
        "data_complete": True,
        "outcome": outcome,
        "net_pnl_usdc": pnl,
        "recorded_at_ms": day,
        "evidence_evaluator_eligible": True,
        "fill_model": "limit_touch",
    }


def test_closed_evidence_gate_is_manual_review_ready_only_when_all_thresholds_hold():
    day_one = 1_783_468_800_000  # 2026-07-07 UTC
    day_two = day_one + 86_400_000
    evaluations = [
        _closed_row(f"tp-{index}", day_one if index < 3 else day_two, "tp1_first", 0.2)
        for index in range(6)
    ] + [
        _closed_row("sl-1", day_one, "sl_first", -0.1),
        _closed_row("sl-2", day_two, "sl_first", -0.1),
    ]
    db = FakeDb({
        "adaptive_opportunities": [
            {key: row[key] for key in (
                "opportunity_id", "lane_code", "state", "effective_side",
                "registry_hash", "policy_hash", "resolved_profile_hash",
                "recorded_at_ms",
            )}
            for row in evaluations
        ],
        "shadow_evaluations": evaluations,
        "mainnet_runs": [],
        "mainnet_run_events": [],
    })

    lane = asyncio.run(collect_lane_evidence(db))[0]["W6B"]
    detail = asyncio.run(build_lane_detail(db, "W6B", now_ms=day_two))

    assert lane.readiness == "REVIEW_READY"
    assert lane.promotion_blockers == ()
    assert lane.evaluable == 8
    assert lane.tp_first == 6
    assert round(lane.ev_per_opportunity or 0.0, 6) == 0.125
    assert len(lane.utc_dates) == 2
    assert "Legacy exact cohorts — historical review diagnostics only" in detail
    assert "Legacy REVIEW_READY and UTC diversity are informational" in detail
    assert "v1.4.64 rolling 90m state/lease" in detail


def test_mixed_lane_is_split_into_exact_cohorts_without_lane_wide_blocking():
    day_one = 1_783_468_800_000
    rows = [
        _closed_row(f"tp-{index}", day_one + (index % 2) * 86_400_000, "tp1_first", 0.2)
        for index in range(8)
    ]
    del rows[0]["resolved_profile_hash"]
    rows[1]["state"] = "different-state"
    rows[2]["registry_hash"] = "registry-other"
    rows[3]["policy_hash"] = "policy-other"
    rows[4]["resolved_profile_hash"] = "profile-two"
    rows[5]["effective_side"] = "SHORT"
    db = FakeDb({
        "adaptive_opportunities": [],
        "shadow_evaluations": rows,
        "mainnet_runs": [],
        "mainnet_run_events": [],
    })

    lane = asyncio.run(collect_lane_evidence(db))[0]["W6B"]
    detail = asyncio.run(build_lane_detail(db, "W6B", now_ms=day_one))

    assert lane.readiness == "COHORT_SPLIT"
    assert lane.identity_missing == 1
    assert len(lane.cohorts) == 7
    assert lane.promotion_blockers == (
        "exact_cohorts=7 (review separately)",
    )
    assert any("identity_missing=1" in cohort.promotion_blockers for cohort in lane.cohorts.values())
    assert any("registry_hash_not_current" in cohort.promotion_blockers for cohort in lane.cohorts.values())
    assert any("policy_hash_not_current" in cohort.promotion_blockers for cohort in lane.cohorts.values())
    assert "identity_missing=1" in detail
    assert "Legacy exact cohorts — historical review diagnostics only" in detail
    assert "COHORT_SPLIT" in detail


def test_admission_reject_reopen_live_breach_blocks_without_inflating_captured():
    row = _closed_row("durable", 1_783_468_800_000, "tp1_first", 1.0)
    db = FakeDb({
        "adaptive_opportunities": [row],
        "shadow_evaluations": [row],
        "mainnet_runs": [],
        "mainnet_run_events": [{
            "id": 1,
            "run_id": "paid-run",
            "event_type": "entry_codex_v1462_admission",
            "details_json": json.dumps({
                "v1462_opportunity_id": "durable",
                "lane_code": "W6B",
                "mode": "LIVE",
                "permits_order": True,
                "raw_accepted": True,
                "reject_lineage": ["v1428.reject"],
            }),
        }],
    })

    lane = asyncio.run(collect_lane_evidence(db))[0]["W6B"]

    assert lane.captured == 1
    assert lane.live_reopen_breaches == 1
    assert lane.readiness == "DATA_BLOCKED"
    assert "reject_reopen_live_breach=1 (must 0)" in lane.promotion_blockers


def test_exact_current_cnl_safe_control_is_not_a_false_live_reopen_breach():
    exact = {
        "lane_code": "CNL-WPR-L",
        "matrix_rule_id": "v1460.cnl_reclaim.control",
        "safe_lineage_kind": CNL_SAFE_LINEAGE_KIND,
        "registry_identity_valid": True,
        "registry_lane_code": "CNL-WPR-L",
        "registry_hash": V1462_REGISTRY_HASH,
        "policy_hash": V1462_POLICY_HASH,
        "mode": "LIVE",
        "permits_order": True,
        "raw_accepted": False,
        "reject_lineage": [],
    }

    assert _is_reject_reopen_live_breach(exact) is False

    near_misses = (
        {**exact, "lane_code": "W6B"},
        {**exact, "matrix_rule_id": "v1460.rp1.control"},
        {**exact, "safe_lineage_kind": "v1463.not-the-safe-kind"},
        {**exact, "registry_identity_valid": False},
        {**exact, "registry_lane_code": "W6B"},
        {**exact, "registry_hash": "stale-registry"},
        {**exact, "policy_hash": "stale-policy"},
        {**exact, "reject_lineage": ["legacy.reject"]},
        {**exact, "reject_reopen_detected": True},
    )
    assert all(_is_reject_reopen_live_breach(row) for row in near_misses)


def test_shadow_sample_drop_is_explainable_data_block_not_permanent_pending():
    identity = {
        "state": "state-a",
        "effective_side": "LONG",
        "resolved_profile_hash": "profile-a",
        "registry_hash": V1462_REGISTRY_HASH,
        "policy_hash": V1462_POLICY_HASH,
    }
    db = FakeDb({
        "adaptive_opportunities": [],
        "shadow_evaluations": [],
        "mainnet_runs": [],
        "mainnet_run_events": [
            {
                "id": 1,
                "run_id": "drop-run",
                "event_time_ms": 1_000,
                "event_type": "entry_codex_v1462_shadow_opportunity",
                "details_json": json.dumps({
                    "v1462_opportunity_id": "durable-drop",
                    "lane_code": "W6B",
                    **identity,
                }),
            },
            {
                "id": 2,
                "run_id": "drop-run",
                "event_time_ms": 1_100,
                "event_type": "entry_codex_v1_shadow_sample_dropped",
                "details_json": json.dumps({
                    "v1462_opportunity_id": "durable-drop",
                    "opportunity_id": "variant-drop",
                    "lane_code": "W6B",
                    "drop_reason": "per_run_cap",
                }),
            },
        ],
    })

    lane = asyncio.run(collect_lane_evidence(db))[0]["W6B"]
    detail = asyncio.run(build_lane_detail(db, "W6B", now_ms=2_000))

    assert lane.captured == 1
    assert lane.dropped == 1
    assert lane.invalid == 1
    assert lane.pending == 0
    assert lane.outcomes["no_fill"] == 0
    assert lane.readiness == "DATA_BLOCKED"
    assert lane.invalid_reasons["COLLECTION_DROPPED:per_run_cap"] == 1
    assert "COLLECTION_DROPPED:per_run_cap" in detail


def test_replacement_drop_joins_started_sample_by_sample_id():
    identity = {
        "state": "state-b",
        "effective_side": "SHORT",
        "resolved_profile_hash": "profile-b",
        "registry_hash": V1462_REGISTRY_HASH,
        "policy_hash": V1462_POLICY_HASH,
    }
    db = FakeDb({
        "adaptive_opportunities": [],
        "shadow_evaluations": [],
        "mainnet_runs": [],
        "mainnet_run_events": [
            {
                "id": 1,
                "run_id": "replace-run",
                "event_type": "entry_codex_v1462_shadow_opportunity",
                "details_json": json.dumps({
                    "v1462_opportunity_id": "durable-replaced",
                    "lane_code": "W6B",
                    **identity,
                }),
            },
            {
                "id": 2,
                "run_id": "replace-run",
                "event_type": "entry_codex_v1_shadow_sample_started",
                "details_json": json.dumps({
                    "v1462_opportunity_id": "durable-replaced",
                    "opportunity_id": "variant-replaced",
                    "sample_id": "sample-replaced",
                    "lane_code": "W6B",
                    **identity,
                }),
            },
            {
                "id": 3,
                "run_id": "replace-run",
                "event_type": "entry_codex_v1_shadow_sample_dropped",
                "details_json": json.dumps({
                    "opportunity_id": "variant-replaced",
                    "sample_id": "sample-replaced",
                    "lane_code": "W6B",
                    "drop_reason": "replaced_by_higher_priority",
                }),
            },
        ],
    })

    lane = asyncio.run(collect_lane_evidence(db))[0]["W6B"]

    assert lane.captured == 1
    assert lane.dropped == 1
    assert lane.pending == 0
    assert lane.invalid_reasons[
        "COLLECTION_DROPPED:replaced_by_higher_priority"
    ] == 1


def test_replacement_drop_for_diagnostic_sample_is_not_formal_evidence():
    db = FakeDb({
        "adaptive_opportunities": [],
        "shadow_evaluations": [],
        "mainnet_runs": [],
        "mainnet_run_events": [
            {
                "id": 1,
                "run_id": "diagnostic-replace",
                "event_type": "entry_codex_v1_shadow_sample_started",
                "details_json": json.dumps({
                    "v1462_opportunity_id": "diagnostic-durable",
                    "sample_id": "diagnostic-sample",
                    "lane_code": "W6B",
                    "diagnostic_only": True,
                }),
            },
            {
                "id": 2,
                "run_id": "diagnostic-replace",
                "event_type": "entry_codex_v1_shadow_sample_dropped",
                "details_json": json.dumps({
                    "opportunity_id": "diagnostic-variant",
                    "sample_id": "diagnostic-sample",
                    "lane_code": "W6B",
                    "drop_reason": "replaced_by_higher_priority",
                }),
            },
        ],
    })

    lane = asyncio.run(collect_lane_evidence(db))[0]["W6B"]

    assert lane.dropped == 0
    assert lane.invalid == 0
    assert lane.pending == 1


def test_legacy_registry_mismatch_drop_after_cutoff_does_not_block_forward_cohort():
    db = FakeDb({
        "adaptive_opportunities": [],
        "shadow_evaluations": [],
        "mainnet_runs": [],
        "mainnet_run_events": [{
            "id": 101,
            "run_id": "legacy-rehydration",
            "event_time_ms": 2_000,
            "event_type": "entry_codex_v1_shadow_sample_dropped",
            "details_json": json.dumps({
                "opportunity_id": "legacy-variant-only",
                "sample_id": "legacy-sample",
                "lane_code": "W6A",
                "drop_reason": "data_incomplete:registry_version_mismatch",
            }),
        }],
        "v1463_lane_monitor_snapshot_meta": [{
            "singleton_id": 1,
            "snapshot_max_event_id": 100,
            "snapshot_at_ms": 1_000,
            "created_at_ms": 1_000,
        }],
    })

    lanes = asyncio.run(collect_lane_evidence(db))[0]

    assert lanes["W6A"].dropped == 0
    assert lanes["W6A"].data_blockers == ()
    assert all(
        not any("shadow_sample_drop" in blocker for blocker in lane.data_blockers)
        for lane in lanes.values()
    )


def test_synthetic_shadow_lane_drop_does_not_block_frozen_legacy_lanes():
    db = FakeDb({
        "adaptive_opportunities": [],
        "shadow_evaluations": [],
        "mainnet_runs": [],
        "mainnet_run_events": [{
            "id": 1,
            "run_id": "synthetic-shadow",
            "event_time_ms": 1_000,
            "event_type": "entry_codex_v1_shadow_sample_dropped",
            "details_json": json.dumps({
                "v1462_opportunity_id": "durable-synthetic",
                "opportunity_id": "synthetic-variant",
                "sample_id": "synthetic-sample",
                "lane_code": "SH_UNC_L_S1",
                "drop_reason": "active_opportunity_pending",
            }),
        }],
    })

    lanes = asyncio.run(collect_lane_evidence(db))[0]

    assert all(
        not any("unattributed_shadow_sample_drop" in blocker for blocker in lane.data_blockers)
        for lane in lanes.values()
    )


def test_truly_unattributed_current_drop_still_blocks_all_legacy_lanes():
    db = FakeDb({
        "adaptive_opportunities": [],
        "shadow_evaluations": [],
        "mainnet_runs": [],
        "mainnet_run_events": [{
            "id": 1,
            "run_id": "malformed-current",
            "event_time_ms": 1_000,
            "event_type": "entry_codex_v1_shadow_sample_dropped",
            "details_json": json.dumps({
                "opportunity_id": "unattributed-current",
                "sample_id": "unattributed-sample",
                "drop_reason": "active_opportunity_pending",
            }),
        }],
    })

    lanes = asyncio.run(collect_lane_evidence(db))[0]

    assert all(
        "unattributed_shadow_sample_drop=1" in lane.data_blockers[0]
        for lane in lanes.values()
    )


def test_drop_is_superseded_by_authoritative_outcome_for_same_durable_id():
    identity = {
        "state": "state-c",
        "effective_side": "LONG",
        "resolved_profile_hash": "profile-c",
        "registry_hash": V1462_REGISTRY_HASH,
        "policy_hash": V1462_POLICY_HASH,
    }
    base = {
        "v1462_opportunity_id": "durable-cooldown",
        "lane_code": "W6B",
        **identity,
    }
    db = FakeDb({
        "adaptive_opportunities": [],
        "shadow_evaluations": [],
        "mainnet_runs": [],
        "mainnet_run_events": [
            {
                "id": 1,
                "run_id": "cooldown-run",
                "event_type": "entry_codex_v1462_shadow_opportunity",
                "details_json": json.dumps(base),
            },
            {
                "id": 2,
                "run_id": "cooldown-run",
                "event_type": "entry_codex_v1_shadow_sample_dropped",
                "details_json": json.dumps({
                    **base,
                    "drop_reason": "cooldown",
                }),
            },
            {
                "id": 3,
                "run_id": "active-original-run",
                "event_type": "entry_codex_v1_shadow_outcome",
                "details_json": json.dumps({
                    **base,
                    "shadow_outcome": "tp1_first",
                    "data_complete": True,
                    "data_quality": {"complete": True},
                    "paper_pnl_usdc_after_fee": 0.2,
                    "evidence_evaluator_eligible": True,
                    "fill_model": "limit_touch",
                }),
            },
        ],
    })

    lane = asyncio.run(collect_lane_evidence(db))[0]["W6B"]

    assert lane.captured == 1
    assert lane.complete == 1
    assert lane.evaluable == 1
    assert lane.dropped == 0
    assert lane.invalid == 0
    assert lane.pending == 0


def test_orphan_live_reopen_without_opportunity_id_blocks_attributed_lane():
    day_one = 1_783_468_800_000
    day_two = day_one + 86_400_000
    rows = [
        _closed_row(
            f"clean-{index}",
            day_one if index < 4 else day_two,
            "tp1_first" if index < 6 else "sl_first",
            0.2 if index < 6 else -0.1,
        )
        for index in range(8)
    ]
    db = FakeDb({
        "adaptive_opportunities": [],
        "shadow_evaluations": rows,
        "mainnet_runs": [],
        "mainnet_run_events": [{
            "id": 99,
            "run_id": "orphan-live",
            "event_time_ms": day_two,
            "event_type": "entry_codex_v1462_admission",
            "details_json": json.dumps({
                "lane_code": "W6B",
                "mode": "LIVE",
                "raw_accepted": False,
                "reject_lineage": ["legacy.reject"],
            }),
        }],
    })

    lane = asyncio.run(collect_lane_evidence(db))[0]["W6B"]
    overview = asyncio.run(build_lane_monitor(db, now_ms=day_two))

    assert lane.complete == 8
    assert lane.evaluable == 8
    assert lane.live_reopen_breaches == 1
    assert lane.readiness == "DATA_BLOCKED"
    assert "orphan_reject_reopen_live_breach=1" in lane.promotion_blockers[0]
    assert "⚠️ safety:" in overview


def test_unattributed_live_reopen_blocks_all_lanes_and_warns_monitor():
    db = FakeDb({
        "adaptive_opportunities": [],
        "shadow_evaluations": [],
        "mainnet_runs": [],
        "mainnet_run_events": [{
            "id": 100,
            "run_id": "unattributed-live",
            "event_time_ms": 1_000,
            "event_type": "entry_codex_v1462_admission",
            "details_json": json.dumps({
                "mode": "LIVE",
                "raw_accepted": False,
                "reject_lineage": ["legacy.reject"],
            }),
        }],
    })

    lanes = asyncio.run(collect_lane_evidence(db))[0]
    overview = asyncio.run(build_lane_monitor(db, now_ms=1_000))

    assert all(lane.readiness == "DATA_BLOCKED" for lane in lanes.values())
    assert all(
        any("unattributed_reject_reopen_live_breach=1" in item for item in lane.data_blockers)
        for lane in lanes.values()
    )
    assert "⚠️ safety:" in overview
    assert "missing lane and opportunity id" in overview


def test_v1462_durable_opportunity_id_wins_over_legacy_variant_ids():
    common_identity = {
        "lane_code": "W6B",
        "state": "clean",
        "effective_side": "LONG",
        "registry_hash": V1462_REGISTRY_HASH,
        "policy_hash": V1462_POLICY_HASH,
        "resolved_profile_hash": "profile-one",
    }
    db = FakeDb({
        "adaptive_opportunities": [],
        "shadow_evaluations": [],
        "mainnet_runs": [],
        "mainnet_run_events": [
            {
                "id": 1, "run_id": "a", "event_time_ms": 1_000,
                "event_type": "entry_codex_v1_shadow_sample_started",
                "details_json": json.dumps({
                    **common_identity, "opportunity_id": "legacy-a",
                    "v1462_opportunity_id": "durable-one",
                }),
            },
            {
                "id": 2, "run_id": "b", "event_time_ms": 2_000,
                "event_type": "entry_codex_v1_shadow_outcome",
                "details_json": json.dumps({
                    **common_identity, "opportunity_id": "legacy-b",
                    "v1462_opportunity_id": "durable-one",
                    "shadow_outcome": "tp1_first", "data_complete": True,
                        "data_quality": {"complete": True},
                        "paper_pnl_usdc_after_fee": 0.2,
                        "evidence_evaluator_eligible": True,
                        "fill_model": "limit_touch",
                }),
            },
        ],
    })

    lane = asyncio.run(collect_lane_evidence(db))[0]["W6B"]

    assert lane.captured == 1
    assert lane.complete == 1
    assert lane.outcomes == {"tp1_first": 1}


def test_pending_terminal_is_collecting_not_data_blocked():
    db = FakeDb({
        "adaptive_opportunities": [{
            "opportunity_id": "pending-one",
            "lane_code": "W6B",
            "state": "clean",
            "effective_side": "LONG",
            "registry_hash": V1462_REGISTRY_HASH,
            "policy_hash": V1462_POLICY_HASH,
            "resolved_profile_hash": "profile-one",
            "observed_at_ms": 1_783_468_800_000,
        }],
        "shadow_evaluations": [],
        "mainnet_runs": [],
        "mainnet_run_events": [],
    })

    lane = asyncio.run(collect_lane_evidence(db))[0]["W6B"]
    detail = asyncio.run(build_lane_detail(db, "W6B"))

    assert lane.captured == 1
    assert lane.pending == 1
    assert lane.invalid == 0
    assert lane.incomplete == 0
    assert lane.readiness == "COLLECTING"
    assert "pending=1 (wait terminal)" in lane.threshold_gaps
    assert "pending=1 (wait terminal)" in detail


def test_missing_snapshot_meta_is_fail_closed_and_visible():
    db = FakeDb({
        "adaptive_opportunities": [],
        "shadow_evaluations": [],
        "mainnet_runs": [],
        "v1462_lane_monitor_legacy_summary": [],
        "mainnet_run_events": [],
    }, with_snapshot=False)

    lane = asyncio.run(collect_lane_evidence(db))[0]["W6B"]
    detail = asyncio.run(build_lane_detail(db, "W6B"))

    assert lane.readiness == "DATA_BLOCKED"
    assert lane.data_blockers == ("snapshot_meta_missing_or_invalid",)
    assert "snapshot_meta_missing_or_invalid" in detail
    assert "v1463_lane_monitor_snapshot_meta" in detail


def test_conflicting_authoritative_terminals_are_data_blocked():
    identity = {
        "lane_code": "W6B",
        "v1462_opportunity_id": "terminal-conflict",
        "state": "clean",
        "effective_side": "LONG",
        "registry_hash": V1462_REGISTRY_HASH,
        "policy_hash": V1462_POLICY_HASH,
        "resolved_profile_hash": "profile-one",
        "evidence_evaluator_eligible": True,
        "diagnostic_only": False,
        "fill_model": "limit_touch",
        "data_complete": True,
        "data_quality": {"complete": True},
    }
    db = FakeDb({
        "adaptive_opportunities": [],
        "shadow_evaluations": [],
        "mainnet_runs": [],
        "mainnet_run_events": [
            {
                "id": 1, "run_id": "r", "event_time_ms": 1_000,
                "event_type": "entry_codex_v1_shadow_outcome",
                "details_json": json.dumps({
                    **identity, "shadow_outcome": "tp1_first",
                    "paper_pnl_usdc_after_fee": 0.2,
                }),
            },
            {
                "id": 2, "run_id": "r", "event_time_ms": 1_100,
                "event_type": "entry_codex_v1_shadow_outcome",
                "details_json": json.dumps({
                    **identity, "shadow_outcome": "sl_first",
                    "paper_pnl_usdc_after_fee": -0.1,
                }),
            },
        ],
    })

    lane = asyncio.run(collect_lane_evidence(db))[0]["W6B"]

    assert lane.captured == 1
    assert lane.complete == 0
    assert lane.evaluable == 0
    assert lane.invalid == 1
    assert lane.terminal_conflicts == 1
    assert lane.readiness == "DATA_BLOCKED"
    assert "terminal_conflicts=1" in lane.promotion_blockers


def test_unknown_authoritative_terminal_outcome_is_invalid_and_blocks_ready_cohort():
    day_one = 1_783_468_800_000
    day_two = day_one + 86_400_000
    rows = [
        _closed_row(
            f"valid-{index}",
            day_one if index < 4 else day_two,
            "tp1_first" if index < 6 else "sl_first",
            0.2 if index < 6 else -0.1,
        )
        for index in range(8)
    ]
    rows.append(_closed_row("schema-drift", day_two, "schema_drift_terminal", 9.0))
    db = FakeDb({
        "adaptive_opportunities": [],
        "shadow_evaluations": rows,
        "mainnet_runs": [],
        "mainnet_run_events": [],
    })

    lane = asyncio.run(collect_lane_evidence(db))[0]["W6B"]

    assert lane.complete == 8
    assert lane.evaluable == 8
    assert lane.invalid == 1
    assert lane.ev_count == 8
    assert lane.readiness == "DATA_BLOCKED"
    assert lane.invalid_reasons["UNKNOWN_OUTCOME:schema_drift_terminal"] == 1


def test_durable_id_lane_conflict_blocks_both_lanes_and_cannot_false_ready():
    day_one = 1_783_468_800_000
    day_two = day_one + 86_400_000
    evaluations = [
        _closed_row(
            f"lane-conflict-{index}",
            day_one if index < 4 else day_two,
            "tp1_first" if index < 6 else "sl_first",
            0.2 if index < 6 else -0.1,
        )
        for index in range(8)
    ]
    adaptive = [dict(row) for row in evaluations]
    evaluations[0] = {**evaluations[0], "lane_code": "CNL-WPR-L"}
    db = FakeDb({
        "adaptive_opportunities": adaptive,
        "shadow_evaluations": evaluations,
        "mainnet_runs": [],
        "mainnet_run_events": [],
    })

    lanes = asyncio.run(collect_lane_evidence(db))[0]
    original = lanes["W6B"]
    conflicting = lanes["CNL-WPR-L"]

    assert original.captured == 8
    assert original.complete == 7
    assert original.lane_conflicts == 1
    assert original.readiness == "DATA_BLOCKED"
    assert "lane_code_conflicts=1" in original.promotion_blockers
    assert conflicting.captured == 1
    assert conflicting.complete == 0
    assert conflicting.lane_conflicts == 1
    assert conflicting.readiness == "DATA_BLOCKED"


def test_ready_current_cohort_and_wrong_hash_cohort_are_reviewed_separately():
    day_one = 1_783_468_800_000
    day_two = day_one + 86_400_000
    current = [
        _closed_row(
            f"current-{index}",
            day_one if index < 4 else day_two,
            "tp1_first" if index < 6 else "sl_first",
            0.2 if index < 6 else -0.1,
        )
        for index in range(8)
    ]
    wrong_hash = _closed_row("wrong-hash", day_two, "tp1_first", 0.2)
    wrong_hash["registry_hash"] = "not-current"
    rows = [*current, wrong_hash]
    db = FakeDb({
        "adaptive_opportunities": [],
        "shadow_evaluations": rows,
        "mainnet_runs": [],
        "mainnet_run_events": [],
    })

    lane = asyncio.run(collect_lane_evidence(db))[0]["W6B"]
    detail = asyncio.run(build_lane_detail(db, "W6B", now_ms=day_two))
    statuses = sorted(cohort.readiness for cohort in lane.cohorts.values())

    assert lane.readiness == "COHORT_SPLIT"
    assert statuses == ["DATA_BLOCKED", "REVIEW_READY"]
    assert "registry_hash_not_current" in detail
    assert "Lane totals are informational; readiness is cohort-specific." in detail
    assert "exact_cohorts=2 (review separately)" in lane.promotion_blockers


def test_v1463_snapshot_meta_migration_is_idempotent_when_legacy_summary_empty():
    db = sqlite3.connect(":memory:")
    db.executescript(
        "CREATE TABLE mainnet_run_events ("
        "id INTEGER PRIMARY KEY, event_time_ms INTEGER NOT NULL);"
        "CREATE TABLE v1462_lane_monitor_legacy_summary ("
        "lane_code TEXT PRIMARY KEY, outcome_opportunities INTEGER NOT NULL, "
        "last_outcome_at_ms INTEGER NOT NULL, snapshot_max_event_id INTEGER NOT NULL, "
        "snapshot_at_ms INTEGER NOT NULL);"
    )
    migration = Path(
        "src/gridbot/storage/migrations/010_v1463_lane_monitor_snapshot_meta.sql"
    ).read_text(encoding="utf-8")

    db.executescript(migration)
    db.executescript(migration)
    rows = db.execute(
        "SELECT singleton_id, snapshot_max_event_id, snapshot_at_ms "
        "FROM v1463_lane_monitor_snapshot_meta"
    ).fetchall()

    assert rows == [(1, 0, 0)]


def test_v1463_forward_reset_migration_freezes_one_clean_cutoff():
    db = sqlite3.connect(":memory:")
    db.executescript(
        "CREATE TABLE mainnet_run_events ("
        "id INTEGER PRIMARY KEY, event_time_ms INTEGER NOT NULL);"
        "CREATE TABLE v1463_lane_monitor_snapshot_meta ("
        "singleton_id INTEGER PRIMARY KEY, snapshot_max_event_id INTEGER NOT NULL, "
        "snapshot_at_ms INTEGER NOT NULL, created_at_ms INTEGER NOT NULL);"
        "INSERT INTO v1463_lane_monitor_snapshot_meta VALUES (1, 10, 1000, 1000);"
        "INSERT INTO mainnet_run_events VALUES (20, 2000);"
    )
    migration = Path(
        "src/gridbot/storage/migrations/012_v1463_lane_monitor_forward_reset.sql"
    ).read_text(encoding="utf-8")

    db.executescript(migration)
    first = db.execute(
        "SELECT snapshot_max_event_id FROM v1463_lane_monitor_snapshot_meta"
    ).fetchone()
    db.execute("INSERT INTO mainnet_run_events VALUES (30, 3000)")
    db.executescript(migration)
    second = db.execute(
        "SELECT snapshot_max_event_id FROM v1463_lane_monitor_snapshot_meta"
    ).fetchone()
    marker = db.execute(
        "SELECT previous_snapshot_max_event_id, reset_max_event_id "
        "FROM v1463_lane_monitor_forward_reset_meta"
    ).fetchone()

    assert first == (20,)
    assert second == (20,)
    assert marker == (10, 20)


def test_html_chunker_balances_tags_and_never_splits_entities():
    text = (
        "<b>Lane &amp; cohort</b>\n"
        "<code>" + ("blocker=&lt;unsafe&gt;; " * 12) + "</code>"
    )

    chunks = lane_monitor_html_chunks(text, limit=80)

    assert len(chunks) > 1
    assert all(len(chunk) <= 80 for chunk in chunks)
    assert all(chunk.count("<b>") == chunk.count("</b>") for chunk in chunks)
    assert all(chunk.count("<code>") == chunk.count("</code>") for chunk in chunks)
    assert all(not re.search(r"&(?:#x?[0-9A-Fa-f]*|[A-Za-z0-9]*)$", chunk) for chunk in chunks)
    visible = unescape(re.sub(r"<[^>]+>", "", "".join(chunks)))
    expected = unescape(re.sub(r"<[^>]+>", "", text))
    assert visible == expected


def test_thirteen_cohort_detail_chunks_without_losing_blockers():
    day = 1_783_468_800_000
    rows = []
    for index in range(13):
        row = _closed_row(f"cohort-{index}", day + index, "tp1_first", 0.1)
        row["state"] = f"state-{index}"
        rows.append(row)
    db = FakeDb({
        "adaptive_opportunities": [],
        "shadow_evaluations": rows,
        "mainnet_runs": [],
        "mainnet_run_events": [],
    })

    detail = asyncio.run(build_lane_detail(db, "W6B", now_ms=day))
    chunks = lane_monitor_html_chunks(detail)

    assert len(detail) > 3900
    assert len(chunks) > 1
    assert all(len(chunk) <= 3900 for chunk in chunks)
    assert all(chunk.count("<b>") == chunk.count("</b>") for chunk in chunks)
    assert all(chunk.count("<code>") == chunk.count("</code>") for chunk in chunks)
    joined_visible = unescape(re.sub(r"<[^>]+>", "", "".join(chunks)))
    for index in range(13):
        assert f"state-{index}" in joined_visible
    assert joined_visible.count("evaluable=1/8") == 13


def test_v1464_runtime_renders_live_lease_90m_evidence_paid_and_latest_event():
    now_ms = 10_000_000
    identity = {
        "environment": "mainnet",
        "symbol": "ETHUSDC",
        "lane_code": "RP1",
        "market_state": "supportive_range",
        "effective_side": "LONG",
        "strategy": "S1_BB_RSI",
        "resolved_profile_hash": "profile-current-abcdef",
        "registry_hash": "registry-current-abcdef",
        "admission_policy_hash": "admission-current-abcdef",
    }
    evidence = [
        {
            **identity,
            "opportunity_id": "opp-tp",
            "observed_at_ms": now_ms - 60_000,
            "terminal_at_ms": now_ms - 50_000,
            "outcome": "tp1_first",
            "data_complete": 1,
            "ambiguous": 0,
            "diagnostic_only": 0,
            "net_pnl_usdc": 0.10,
        },
        {
            **identity,
            "opportunity_id": "opp-sl",
            "observed_at_ms": now_ms - 50_000,
            "terminal_at_ms": now_ms - 40_000,
            "outcome": "sl_first",
            "data_complete": 1,
            "ambiguous": 0,
            "diagnostic_only": 0,
            "net_pnl_usdc": -0.05,
        },
        {
            **identity,
            "opportunity_id": "opp-nf",
            "observed_at_ms": now_ms - 40_000,
            "terminal_at_ms": now_ms - 30_000,
            "outcome": "no_fill",
            "data_complete": 1,
            "ambiguous": 0,
            "diagnostic_only": 0,
            "net_pnl_usdc": 0.0,
        },
        {
            **identity,
            "opportunity_id": "old-late-terminal",
            "observed_at_ms": now_ms - 91 * 60_000,
            "terminal_at_ms": now_ms - 1_000,
            "outcome": "tp1_first",
            "data_complete": 1,
            "ambiguous": 0,
            "diagnostic_only": 0,
            "net_pnl_usdc": 99.0,
        },
    ]
    lease = {
        **identity,
        "cohort_key": "v1464_exact_cohort_a",
        "lease_id": "lease-a",
        "generation": 2,
        "promotion_policy_hash": "promotion-current-abcdef",
        "phase": "CONTROL",
        "status": "ACTIVE",
        "notional_cap_usdc": 50.0,
        "expires_at_ms": now_ms + 120_000,
        "evidence_snapshot_json": json.dumps(
            {
                "data_complete": True,
                "paid_complete": 3,
                "paid_wins": 2,
                "paid_net_pnl_usdc": 0.03,
            }
        ),
    }
    db = FakeDb(
        {
            "adaptive_opportunities": [],
            "shadow_evaluations": [],
            "mainnet_runs": [],
            "mainnet_run_events": [],
            "v1464_promotion_evidence": evidence,
            "v1464_lane_promotion_leases": [lease],
            "v1464_lane_promotion_events": [
                {
                    "cohort_key": lease["cohort_key"],
                    "event_type": "CONTROL_GRANTED",
                    "event_time_ms": now_ms - 120_000,
                    "payload_json": json.dumps(
                        {"details": {"reason": "paid_probation_pass"}}
                    ),
                }
            ],
        }
    )

    overview = asyncio.run(build_lane_monitor(db, now_ms=now_ms))
    detail = asyncio.run(build_lane_detail(db, "RP1", now_ms=now_ms))

    assert "v1.4.64 rolling 90m authority HEALTHY" in overview
    assert "v1464 exact 1 | LIVE=1" in overview
    assert "<b>P1 LIVE</b>" in detail
    assert "lease gen 2 | remaining 2m00s | cap $50" in detail
    assert "90m evidence n/eval 3/2 | TP 1 SL 1 NF 1" in detail
    assert "fee-net EV/op +0.0167" in detail
    assert "paid 2W/3 complete | net +0.0300 USDC" in detail
    assert "CONTROL_GRANTED (2m ago) reason=paid_probation_pass" in detail
    assert "99." not in detail
    assert "No manual promotion action is available." in detail


def test_v1464_missing_tables_degrade_without_blocking_v1463_or_adding_actions():
    db = FakeDb(
        {
            "adaptive_opportunities": [],
            "shadow_evaluations": [],
            "mainnet_runs": [],
            "mainnet_run_events": [],
        }
    )

    overview = asyncio.run(build_lane_monitor(db, now_ms=120_000))
    detail = asyncio.run(build_lane_detail(db, "RP1", now_ms=120_000))

    assert "v1.4.64 rolling 90m authority DEGRADED" in overview
    assert "promotion schema unavailable" in overview
    assert "<code>RP1" in overview
    assert "snapshot_meta_missing_or_invalid" not in overview
    assert "v1.4.64 rolling 90m auto-promotion authority (read-only)" in detail
    assert "No manual promotion action is available." in detail
    assert 'callback_data="promote"' not in detail


def test_freshness_over_sixty_seconds_uses_real_minutes():
    now_ms = 1_000_000
    db = FakeDb(
        {
            "adaptive_opportunities": [],
            "shadow_evaluations": [],
            "mainnet_runs": [
                {
                    "run_id": "paid-rp1",
                    "status": "COMPLETED",
                    "lane_code": "RP1",
                    "updated_at_ms": now_ms - 125_000,
                    "net_pnl_usdc": 0.1,
                }
            ],
            "mainnet_run_events": [],
        }
    )

    detail = asyncio.run(build_lane_detail(db, "RP1", now_ms=now_ms))

    assert "Freshness: <b>2m</b>" in detail
    assert "Freshness: <b>125m</b>" not in detail


def test_v1465_w6a_profile_selector_renders_windows_winner_and_escaped_values():
    now_ms = 10_000_000
    evidence = [
        {
            "environment": "mainnet", "symbol": "ETHUSDC", "lane_code": "W6A",
            "market_state": "range", "effective_side": "LONG", "strategy": "S1",
            "profile_id": "p<one>", "resolved_profile_hash": "hash<&one>",
            "profile_plan_hash": "plan-a", "observed_at_ms": now_ms - 10_000,
            "terminal_at_ms": now_ms - 9_000, "outcome": "tp1_first",
            "data_complete": 1, "ambiguous": 0, "diagnostic_only": 0,
            "net_pnl_bp": 12,
        },
        {
            "environment": "mainnet", "symbol": "ETHUSDC", "lane_code": "W6A",
            "market_state": "range", "effective_side": "LONG", "strategy": "S1",
            "profile_id": "p<one>", "resolved_profile_hash": "hash<&one>",
            "profile_plan_hash": "plan-a", "observed_at_ms": now_ms - 20_000,
            "terminal_at_ms": now_ms - 19_000, "outcome": "sl_first",
            "data_complete": 1, "ambiguous": 0, "diagnostic_only": 0,
            "net_pnl_bp": -6,
        },
        {
            "environment": "mainnet", "symbol": "ETHUSDC", "lane_code": "W6A",
            "market_state": "range", "effective_side": "LONG", "strategy": "S1",
            "profile_id": "p<one>", "resolved_profile_hash": "hash<&one>",
            "profile_plan_hash": "plan-a", "observed_at_ms": now_ms - 30_000,
            "terminal_at_ms": now_ms - 29_000, "outcome": "no_fill",
            "data_complete": 1, "ambiguous": 0, "diagnostic_only": 0,
            "net_pnl_bp": 0,
        },
    ]
    selection = {
        "selector_key": "mainnet:W6A:<selector>",
        "winner_profile_id": "p<one>", "winner_profile_hash": "hash<&one>",
        "generation": 3, "status": "ACTIVE", "notional_cap_usdc": 25,
        "expires_at_ms": now_ms + 120_000, "updated_at_ms": now_ms - 1_000,
        "evidence_snapshot_json": json.dumps({"blockers": ["review <needed>"]}),
    }
    db = FakeDb({
        "adaptive_opportunities": [], "shadow_evaluations": [], "mainnet_runs": [],
        "mainnet_run_events": [], "v1464_promotion_evidence": [],
        "v1464_lane_promotion_leases": [], "v1464_lane_promotion_events": [],
        "v1465_w6a_profile_evidence": evidence,
        "v1465_w6a_profile_selections": [selection],
        "v1465_w6a_profile_selection_events": [{
            "selector_key": selection["selector_key"], "event_type": "SELECTED<script>",
            "event_time_ms": now_ms - 500,
            "payload_json": json.dumps({"reason": "winner <verified>"}),
        }],
    })

    overview = asyncio.run(build_lane_monitor(db, now_ms=now_ms))
    detail = asyncio.run(build_lane_detail(db, "W6A", now_ms=now_ms))

    assert "v1.4.65 W6A 15/30/90m profile selector HEALTHY" in overview
    assert "v1465 W6A winner p&lt;one&gt;/hash&lt;&amp;one&gt; | state ACTIVE" in overview
    assert "15m safety | 30m authority | 90m guard" in detail
    assert "15m safety n/eval 3/2 | TP 1 SL 1 NF 1 | EV +2.00bp" in detail
    assert "30m authority n/eval 3/2 | TP 1 SL 1 NF 1 | EV +2.00bp" in detail
    assert "90m guard n/eval 3/2 | TP 1 SL 1 NF 1 | EV +2.00bp" in detail
    assert "lease 2m00s | cap $25" in detail
    assert "p&lt;one&gt;" in detail and "review &lt;needed&gt;" in detail
    assert "SELECTED&lt;script&gt;" in detail and "winner &lt;verified&gt;" in detail
    assert "<script>" not in detail


def test_v1465_w6a_selector_keeps_unselected_exact_evidence_identity_visible():
    now_ms = 10_000_000
    common = {
        "environment": "mainnet", "symbol": "ETHUSDC", "lane_code": "W6A",
        "effective_side": "LONG", "strategy": "S1", "profile_plan_hash": "plan",
        "observed_at_ms": now_ms - 1_000, "terminal_at_ms": now_ms - 500,
        "outcome": "tp_first", "data_complete": 1, "ambiguous": 0,
        "diagnostic_only": 0, "net_pnl_bp": 5,
    }
    selection = {
        "selector_key": "mainnet:W6A:range", "environment": "mainnet",
        "symbol": "ETHUSDC", "lane_code": "W6A", "market_state": "range",
        "effective_side": "LONG", "strategy": "S1", "status": "SHADOW",
        "updated_at_ms": now_ms - 100,
    }
    db = FakeDb({
        "adaptive_opportunities": [], "shadow_evaluations": [], "mainnet_runs": [],
        "mainnet_run_events": [], "v1464_promotion_evidence": [],
        "v1464_lane_promotion_leases": [], "v1464_lane_promotion_events": [],
        "v1465_w6a_profile_evidence": [
            {**common, "market_state": "range", "profile_id": "range-profile",
             "resolved_profile_hash": "range-hash"},
            {**common, "market_state": "trend", "profile_id": "trend-profile",
             "resolved_profile_hash": "trend-hash"},
        ],
        "v1465_w6a_profile_selections": [selection],
        "v1465_w6a_profile_selection_events": [],
    })

    detail = asyncio.run(build_lane_detail(db, "W6A", now_ms=now_ms))

    assert "SHADOW/range" in detail
    assert "SHADOW/TREND" in detail
    assert "profile=range-profile" in detail
    assert "profile=trend-profile" in detail
    assert "no_profile_selection" in detail


def test_v1465_w6a_selector_marks_expired_shadow_lease_expired():
    now_ms = 10_000_000
    db = FakeDb({
        "adaptive_opportunities": [], "shadow_evaluations": [], "mainnet_runs": [],
        "mainnet_run_events": [], "v1464_promotion_evidence": [],
        "v1464_lane_promotion_leases": [], "v1464_lane_promotion_events": [],
        "v1465_w6a_profile_evidence": [],
        "v1465_w6a_profile_selections": [{
            "selector_key": "mainnet:W6A:range", "status": "SHADOW",
            "expires_at_ms": now_ms - 1, "updated_at_ms": now_ms - 2,
        }],
        "v1465_w6a_profile_selection_events": [],
    })

    detail = asyncio.run(build_lane_detail(db, "W6A", now_ms=now_ms))

    assert "W6A-S1 EXPIRED" in detail
    assert "lease_expired_not_reconciled" in detail


def test_v1465_w6a_profile_selector_missing_tables_degrades_without_v1464_breakage():
    db = FakeDb({
        "adaptive_opportunities": [], "shadow_evaluations": [], "mainnet_runs": [],
        "mainnet_run_events": [], "v1464_promotion_evidence": [],
        "v1464_lane_promotion_leases": [], "v1464_lane_promotion_events": [],
    })

    overview = asyncio.run(build_lane_monitor(db, now_ms=120_000))
    detail = asyncio.run(build_lane_detail(db, "W6A", now_ms=120_000))

    assert "v1.4.64 rolling 90m authority HEALTHY" in overview
    assert "v1.4.65 W6A 15/30/90m profile selector DEGRADED" in overview
    assert "v1465_w6a_profile_evidence" in overview
    assert "v1.4.65 W6A rolling 15/30/90m profile selector (read-only)" in detail
    assert "No selector action is available" in detail


def test_v1465_w6a_empty_rolling_window_reports_evidence_stalled_not_legacy_blocker():
    db = FakeDb({
        "adaptive_opportunities": [], "shadow_evaluations": [], "mainnet_runs": [],
        "mainnet_run_events": [], "v1464_promotion_evidence": [],
        "v1464_lane_promotion_leases": [], "v1464_lane_promotion_events": [],
        "v1465_w6a_profile_evidence": [], "v1465_w6a_profile_selections": [],
        "v1465_w6a_profile_selection_events": [],
    })

    detail = asyncio.run(build_lane_detail(db, "W6A", now_ms=120_000))

    assert "EVIDENCE_STALLED" in detail
    assert "current rolling 90m window" in detail
    assert "Legacy review diagnostics (not v1.4.64/v1.4.65 authority)" in detail
    assert "legacy UTC diversity" in detail


def test_v1465_w6a_profile_evidence_uses_observed_window_and_terminal_now_bound():
    now_ms = 1_000
    valid = {
        "lane_code": "W6A", "profile_id": "p", "resolved_profile_hash": "h",
        "profile_plan_hash": "plan", "observed_at_ms": 10, "terminal_at_ms": 20,
        "outcome": "tp", "data_complete": 1, "ambiguous": 0,
        "diagnostic_only": 0, "net_pnl_bp": 5,
    }
    db = FakeDb({
        "adaptive_opportunities": [], "shadow_evaluations": [], "mainnet_runs": [],
        "mainnet_run_events": [], "v1464_promotion_evidence": [],
        "v1464_lane_promotion_leases": [], "v1464_lane_promotion_events": [],
        "v1465_w6a_profile_evidence": [
            valid,
            {**valid, "observed_at_ms": -1},
            {**valid, "terminal_at_ms": now_ms + 1},
        ],
        "v1465_w6a_profile_selections": [{
            "selector_key": "W6A:p", "winner_profile_id": "p",
            "winner_profile_hash": "h", "status": "SHADOW", "updated_at_ms": 1,
        }],
        "v1465_w6a_profile_selection_events": [],
    })

    detail = asyncio.run(build_lane_detail(db, "W6A", now_ms=now_ms))

    assert "90m guard n/eval 1/1 | TP 1 SL 0 NF 0 | EV +5.00bp" in detail
    evidence_query = next(
        query for query in db.queries if "FROM v1465_w6a_profile_evidence" in query
    )
    assert "observed_at_ms >= 0" in evidence_query
    assert "terminal_at_ms <= 1000" in evidence_query
    assert "LIMIT 5000" in evidence_query
