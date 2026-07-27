"""Auditable sources for the observed v1.4.59 runtime identity.

An account fingerprint is provisioned as a non-secret deployment marker.  It
must never be derived from an API key or secret.  The marker identifies the
intended Binance account operationally; on restart the value is compared with
the identity already stored in the durable adaptive session.
"""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any, Mapping

from src.gridbot.mainnet.runtime_identity import RuntimeIdentity


_FINGERPRINT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$")
_ACCOUNT_MODES = frozenset({"ONE_WAY", "HEDGE"})


class RuntimeIdentitySourceError(ValueError):
    """Raised when an observed identity source is absent or unsafe."""


def read_account_fingerprint_marker(path: str | Path) -> str:
    """Read one non-secret, operator-provisioned account marker."""

    marker_path = Path(path)
    try:
        value = marker_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeIdentitySourceError(
            "account fingerprint marker is unavailable"
        ) from exc
    if not _FINGERPRINT_RE.fullmatch(value):
        raise RuntimeIdentitySourceError("account fingerprint marker is invalid")
    return value


def canonical_config_hash(config: Mapping[str, Any]) -> str:
    """Hash an explicit, secret-free trading configuration envelope."""

    if not isinstance(config, Mapping) or not config:
        raise RuntimeIdentitySourceError("config envelope is required")
    forbidden = tuple(
        key
        for key in config
        if any(token in str(key).lower() for token in ("secret", "api_key", "token"))
    )
    if forbidden:
        raise RuntimeIdentitySourceError(
            "config envelope contains secret-bearing keys"
        )
    try:
        encoded = json.dumps(
            dict(config),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RuntimeIdentitySourceError("config envelope is not canonical") from exc
    return sha256(encoded).hexdigest()


def build_observed_runtime_identity(
    *,
    environment: str,
    exchange_endpoint: str,
    exchange_testnet: bool,
    account_fingerprint_marker: str | Path,
    db_path: str | Path,
    symbol: str,
    account_mode: str,
    deployment_commit: str,
    config: Mapping[str, Any],
) -> RuntimeIdentity:
    """Build the identity strictly from observed/provisioned runtime facts."""

    mode = str(account_mode).strip().upper()
    if mode not in _ACCOUNT_MODES:
        raise RuntimeIdentitySourceError("unsupported or unknown account mode")
    commit = str(deployment_commit).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{7,64}", commit):
        raise RuntimeIdentitySourceError("deployment commit is invalid")
    endpoint = str(exchange_endpoint).strip().rstrip("/")
    if not endpoint.startswith("https://"):
        raise RuntimeIdentitySourceError("exchange endpoint must use https")
    database_identity = str(Path(db_path).expanduser().resolve(strict=False))
    return RuntimeIdentity(
        environment=str(environment).strip(),
        exchange_endpoint=endpoint,
        exchange_testnet=exchange_testnet,
        account_fingerprint=read_account_fingerprint_marker(
            account_fingerprint_marker
        ),
        db_namespace=database_identity,
        symbol=str(symbol).strip().upper(),
        account_mode=mode,
        deployment_commit=commit,
        config_hash=canonical_config_hash(config),
    )


__all__ = [
    "RuntimeIdentitySourceError",
    "build_observed_runtime_identity",
    "canonical_config_hash",
    "read_account_fingerprint_marker",
]
