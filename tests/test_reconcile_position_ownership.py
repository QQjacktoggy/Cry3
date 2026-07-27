from pathlib import Path

from scripts.reconcile_position_ownership import classify_ownership, resolve_db_path


def test_position_ownership_mainnet_defaults_to_runtime_db_path():
    assert resolve_db_path(
        explicit_db=None,
        environment="mainnet",
        settings_db_path="runtime/mainnet.db",
    ) == Path("runtime/mainnet.db")


def test_position_ownership_testnet_keeps_testnet_db_default():
    assert resolve_db_path(
        explicit_db=None,
        environment="testnet",
        settings_db_path="runtime/mainnet.db",
    ) == Path("testnet/data/gridbot_testnet.db")


def test_position_ownership_explicit_db_wins():
    explicit = Path("evidence/snapshot.db")
    assert resolve_db_path(
        explicit_db=explicit,
        environment="mainnet",
        settings_db_path="runtime/mainnet.db",
    ) == explicit


def test_position_ownership_flat():
    assert classify_ownership(
        has_position=False,
        active_run=None,
        bot_open_orders=[],
        bot_recent_orders=[],
        matched_bot_order_ids=[],
        non_bot_open_orders=[],
    ) == "FLAT"


def test_position_ownership_active_run_without_order_evidence_is_ambiguous():
    assert classify_ownership(
        has_position=True,
        active_run={"run_id": "cry3mn_1"},
        bot_open_orders=[],
        bot_recent_orders=[],
        matched_bot_order_ids=[],
        non_bot_open_orders=[],
    ) == "AMBIGUOUS"


def test_position_ownership_bot_matched_from_trade_order_identity():
    assert classify_ownership(
        has_position=True,
        active_run={"run_id": "cry3mn_1"},
        bot_open_orders=[],
        bot_recent_orders=[{"order_id": 1}],
        matched_bot_order_ids=[1],
        non_bot_open_orders=[],
    ) == "BOT_MATCHED"


def test_position_ownership_manual_or_external_without_bot_evidence():
    assert classify_ownership(
        has_position=True,
        active_run=None,
        bot_open_orders=[],
        bot_recent_orders=[],
        matched_bot_order_ids=[],
        non_bot_open_orders=[],
    ) == "MANUAL_OR_EXTERNAL"


def test_position_ownership_ambiguous_when_bot_and_external_orders_coexist():
    assert classify_ownership(
        has_position=True,
        active_run={"run_id": "cry3mn_1"},
        bot_open_orders=[{"order_id": 1}],
        bot_recent_orders=[],
        matched_bot_order_ids=[],
        non_bot_open_orders=[{"order_id": 2}],
    ) == "AMBIGUOUS"
