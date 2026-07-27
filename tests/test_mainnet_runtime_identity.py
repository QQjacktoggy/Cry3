from dataclasses import replace

import pytest

from src.gridbot.mainnet.runtime_identity import (
    IDENTITY_MATCH,
    INVALID_EXPECTED_IDENTITY,
    INVALID_OBSERVED_IDENTITY,
    RuntimeIdentity,
    compare_runtime_identity,
)


@pytest.fixture
def identity() -> RuntimeIdentity:
    return RuntimeIdentity(
        environment="mainnet",
        exchange_endpoint="https://fapi.binance.com",
        exchange_testnet=False,
        account_fingerprint="acct:9ed3c790",
        db_namespace="gridbot_mainnet",
        symbol="ETHUSDC",
        account_mode="one_way",
        deployment_commit="0123456789abcdef",
        config_hash="f3d76068",
    )


def test_identical_runtime_identity_is_accepted_and_has_deterministic_fingerprint(
    identity: RuntimeIdentity,
):
    equivalent = RuntimeIdentity(**identity.canonical_payload())

    comparison = compare_runtime_identity(identity, equivalent)

    assert comparison.accepted is True
    assert comparison.reason == IDENTITY_MATCH
    assert comparison.expected_fingerprint == identity.fingerprint
    assert comparison.observed_fingerprint == equivalent.fingerprint
    assert identity.fingerprint == equivalent.fingerprint


@pytest.mark.parametrize(
    ("field_name", "replacement", "expected_reason"),
    [
        ("environment", "testnet", "environment_mismatch"),
        (
            "exchange_endpoint",
            "https://testnet.binancefuture.com",
            "exchange_endpoint_mismatch",
        ),
        ("exchange_testnet", True, "exchange_testnet_mismatch"),
        ("account_fingerprint", "acct:other", "account_fingerprint_mismatch"),
        ("db_namespace", "gridbot_testnet", "db_namespace_mismatch"),
        ("symbol", "BTCUSDC", "symbol_mismatch"),
        ("account_mode", "hedge", "account_mode_mismatch"),
        ("deployment_commit", "fedcba9876543210", "deployment_commit_mismatch"),
        ("config_hash", "other-config", "config_hash_mismatch"),
    ],
)
def test_each_runtime_identity_mismatch_is_rejected_with_a_stable_reason(
    identity: RuntimeIdentity,
    field_name: str,
    replacement: str | bool,
    expected_reason: str,
):
    observed = replace(identity, **{field_name: replacement})

    comparison = compare_runtime_identity(identity, observed)

    assert comparison.accepted is False
    assert comparison.reason == expected_reason
    assert comparison.expected_fingerprint == identity.fingerprint
    assert comparison.observed_fingerprint == observed.fingerprint
    assert comparison.expected_fingerprint != comparison.observed_fingerprint


def test_invalid_comparison_inputs_fail_closed(identity: RuntimeIdentity):
    invalid_expected = compare_runtime_identity(object(), identity)
    invalid_observed = compare_runtime_identity(identity, object())

    assert invalid_expected.accepted is False
    assert invalid_expected.reason == INVALID_EXPECTED_IDENTITY
    assert invalid_observed.accepted is False
    assert invalid_observed.reason == INVALID_OBSERVED_IDENTITY
