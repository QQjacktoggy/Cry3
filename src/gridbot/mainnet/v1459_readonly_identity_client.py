"""Read-only identity adapter for the v1.4.59 observation runtime.

The adapter lets the observation-only runtime work with both the current
``BinanceFuturesClient`` and the older VM client.  It never exposes order
mutation methods: it only reports endpoint metadata and reads position mode.
"""

from __future__ import annotations

from typing import Any


class V1459ReadOnlyIdentityClient:
    """Normalize the minimal identity interface required by v1.4.59."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @property
    def is_testnet(self) -> bool:
        value = getattr(self._client, "is_testnet", None)
        if isinstance(value, bool):
            return value
        settings = getattr(self._client, "_settings", None)
        configured = getattr(settings, "binance_testnet", None)
        if isinstance(configured, bool):
            return configured
        raise ValueError("read-only client testnet flag is unavailable")

    @property
    def exchange_endpoint(self) -> str:
        endpoint = getattr(self._client, "exchange_endpoint", None)
        if isinstance(endpoint, str) and endpoint:
            return endpoint
        if self.is_testnet:
            return "https://testnet.binancefuture.com"
        return "https://fapi.binance.com"

    async def get_position_mode(self) -> str:
        probe = getattr(self._client, "get_position_mode", None)
        if callable(probe):
            return str(await probe())

        api = getattr(self._client, "client", None)
        raw_probe = getattr(api, "futures_get_position_mode", None)
        if not callable(raw_probe):
            raise ValueError("read-only account-mode probe is unavailable")
        payload = await raw_probe()
        if not isinstance(payload, dict) or not isinstance(payload.get("dualSidePosition"), bool):
            raise RuntimeError("Binance position mode response is incomplete")
        return "HEDGE" if payload["dualSidePosition"] else "ONE_WAY"


__all__ = ["V1459ReadOnlyIdentityClient"]
