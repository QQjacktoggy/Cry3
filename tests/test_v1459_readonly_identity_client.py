from types import SimpleNamespace

import pytest

from src.gridbot.mainnet.v1459_readonly_identity_client import V1459ReadOnlyIdentityClient


class _LegacyRawClient:
    async def futures_get_position_mode(self) -> dict[str, bool]:
        return {"dualSidePosition": False}


class _LegacyClient:
    def __init__(self) -> None:
        self._settings = SimpleNamespace(binance_testnet=False)
        self.client = _LegacyRawClient()


@pytest.mark.asyncio
async def test_legacy_client_adapter_uses_only_read_only_identity_calls() -> None:
    adapter = V1459ReadOnlyIdentityClient(_LegacyClient())

    assert adapter.is_testnet is False
    assert adapter.exchange_endpoint == "https://fapi.binance.com"
    assert await adapter.get_position_mode() == "ONE_WAY"
