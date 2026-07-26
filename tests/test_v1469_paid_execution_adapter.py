from src.gridbot.mainnet.v1469_paid_execution_adapter import (
    deterministic_client_order_id,
)


def test_deterministic_client_order_id_is_stable_unique_and_binance_safe():
    first = deterministic_client_order_id("v1469c_one")
    assert first == deterministic_client_order_id("v1469c_one")
    assert first != deterministic_client_order_id("v1469c_two")
    assert first.startswith("c69_")
    assert len(first) == 36
    assert first.isascii() and first.replace("_", "").isalnum()


def test_deterministic_client_order_id_rejects_missing_claim():
    try:
        deterministic_client_order_id("  ")
    except ValueError as exc:
        assert str(exc) == "claim_id must be non-empty"
    else:
        raise AssertionError("missing claim id was accepted")
