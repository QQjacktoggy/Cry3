"""Pure, fail-closed identity checks for a mainnet runtime.

The runtime manager will later construct one identity from its configured
environment and one from the exchange/database/process it reconciles with.
This module intentionally has no settings, database, exchange, or process
dependencies so that the safety decision is deterministic and easy to test.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Final


IDENTITY_MATCH: Final = "identity_match"
INVALID_EXPECTED_IDENTITY: Final = "invalid_expected_identity"
INVALID_OBSERVED_IDENTITY: Final = "invalid_observed_identity"


@dataclass(frozen=True)
class RuntimeIdentity:
    """The immutable identifiers that must agree before order APIs are enabled.

    ``account_fingerprint`` must be a non-secret, stable account identifier
    (for example a salted hash of account id), never an API key or secret.
    Values are deliberately compared exactly: normalisation belongs at the
    integration boundary, where the source semantics are known.
    """

    environment: str
    exchange_endpoint: str
    exchange_testnet: bool
    account_fingerprint: str
    db_namespace: str
    symbol: str
    account_mode: str
    deployment_commit: str
    config_hash: str

    def __post_init__(self) -> None:
        for field_name in _TEXT_FIELD_NAMES:
            value = getattr(self, field_name)
            if not isinstance(value, str):
                raise TypeError(f"{field_name} must be a str")
            if not value.strip():
                raise ValueError(f"{field_name} must not be blank")
        if not isinstance(self.exchange_testnet, bool):
            raise TypeError("exchange_testnet must be a bool")

    def canonical_payload(self) -> dict[str, str | bool]:
        """Return a stable, secret-free representation suitable for auditing."""

        return {
            "environment": self.environment,
            "exchange_endpoint": self.exchange_endpoint,
            "exchange_testnet": self.exchange_testnet,
            "account_fingerprint": self.account_fingerprint,
            "db_namespace": self.db_namespace,
            "symbol": self.symbol,
            "account_mode": self.account_mode,
            "deployment_commit": self.deployment_commit,
            "config_hash": self.config_hash,
        }

    @property
    def fingerprint(self) -> str:
        """A deterministic digest for logs and persisted reconciliation events."""

        encoded = json.dumps(
            self.canonical_payload(),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return sha256(encoded).hexdigest()


@dataclass(frozen=True)
class RuntimeIdentityComparison:
    """The complete, safe-to-persist result of a runtime identity comparison."""

    accepted: bool
    reason: str
    expected_fingerprint: str | None
    observed_fingerprint: str | None


_TEXT_FIELD_NAMES: Final = (
    "environment",
    "exchange_endpoint",
    "account_fingerprint",
    "db_namespace",
    "symbol",
    "account_mode",
    "deployment_commit",
    "config_hash",
)

_COMPARISON_FIELDS: Final = (
    ("environment", "environment_mismatch"),
    ("exchange_endpoint", "exchange_endpoint_mismatch"),
    ("exchange_testnet", "exchange_testnet_mismatch"),
    ("account_fingerprint", "account_fingerprint_mismatch"),
    ("db_namespace", "db_namespace_mismatch"),
    ("symbol", "symbol_mismatch"),
    ("account_mode", "account_mode_mismatch"),
    ("deployment_commit", "deployment_commit_mismatch"),
    ("config_hash", "config_hash_mismatch"),
)


def compare_runtime_identity(
    expected: RuntimeIdentity | object,
    observed: RuntimeIdentity | object,
) -> RuntimeIdentityComparison:
    """Compare all safety identifiers and reject the first stable mismatch.

    An invalid input never defaults to acceptance.  The explicit reason codes
    are intended for a paused runtime and its durable event log; they contain
    no endpoint, account, or configuration values.
    """

    if not isinstance(expected, RuntimeIdentity):
        return RuntimeIdentityComparison(
            accepted=False,
            reason=INVALID_EXPECTED_IDENTITY,
            expected_fingerprint=None,
            observed_fingerprint=(
                observed.fingerprint if isinstance(observed, RuntimeIdentity) else None
            ),
        )
    if not isinstance(observed, RuntimeIdentity):
        return RuntimeIdentityComparison(
            accepted=False,
            reason=INVALID_OBSERVED_IDENTITY,
            expected_fingerprint=expected.fingerprint,
            observed_fingerprint=None,
        )

    for field_name, mismatch_reason in _COMPARISON_FIELDS:
        if getattr(expected, field_name) != getattr(observed, field_name):
            return RuntimeIdentityComparison(
                accepted=False,
                reason=mismatch_reason,
                expected_fingerprint=expected.fingerprint,
                observed_fingerprint=observed.fingerprint,
            )

    return RuntimeIdentityComparison(
        accepted=True,
        reason=IDENTITY_MATCH,
        expected_fingerprint=expected.fingerprint,
        observed_fingerprint=observed.fingerprint,
    )
