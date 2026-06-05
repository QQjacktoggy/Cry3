import pytest

from config.settings import Settings
from src.gridbot.mainnet.one_run import MainnetOneRunManager
from src.gridbot.storage.repositories import MainnetRunRepository


class FakeMainnetClient:
    def __init__(self):
        self.position = None
        self.open_orders = []
        self.commission_rate = {"makerCommissionRate": "0", "takerCommissionRate": "0.0004"}

    async def get_commission_rate(self, symbol):
        return self.commission_rate

    async def get_position(self, symbol):
        return self.position

    async def get_open_orders(self, symbol):
        return list(self.open_orders)


class MemoryMainnetRunRepo:
    def __init__(self):
        self.run = None
        self.events = []

    async def create_run(self, run):
        self.run = {
            "run_id": run["run_id"],
            "symbol": run["symbol"],
            "strategy_label": run["strategy_label"],
            "status": run.get("status", "ARMED"),
            "params_json": "{}",
            "armed_at_ms": run.get("armed_at_ms", 1),
            "updated_at_ms": 1,
        }

    async def get_active_run(self):
        if self.run and self.run["status"] in MainnetRunRepository.ACTIVE_STATUSES:
            return self.run
        return None

    async def get_latest_run(self):
        return self.run

    async def log_event(self, run_id, event_type, details):
        self.events.append((run_id, event_type, details))


def _settings(**kwargs):
    data = {
        "binance_api_key": "testnet-key",
        "binance_api_secret": "testnet-secret",
        "binance_testnet": True,
        "trading_symbols": "ETHUSDC",
        "telegram_chat_id": "123",
        "mainnet_api_key": "mainnet-key",
        "mainnet_api_secret": "mainnet-secret",
        "mainnet_one_run_enabled": True,
    }
    data.update(kwargs)
    return Settings(**data)


@pytest.mark.asyncio
async def test_arm_requires_feature_enabled():
    manager = MainnetOneRunManager(
        settings=_settings(mainnet_one_run_enabled=False),
        client=FakeMainnetClient(),
        repo=MemoryMainnetRunRepo(),
    )

    result = await manager.arm()

    assert "尚未啟用" in result


@pytest.mark.asyncio
async def test_arm_blocks_existing_active_run():
    repo = MemoryMainnetRunRepo()
    await repo.create_run(
        {
            "run_id": "cry3mn_existing",
            "symbol": "ETHUSDC",
            "strategy_label": "wildcat_v2_adverse_guard",
            "status": "ARMED",
        }
    )
    manager = MainnetOneRunManager(
        settings=_settings(),
        client=FakeMainnetClient(),
        repo=repo,
    )

    result = await manager.arm()

    assert "已有 active run" in result


@pytest.mark.asyncio
async def test_arm_blocks_nonzero_maker_fee():
    client = FakeMainnetClient()
    client.commission_rate = {"makerCommissionRate": "0.0002", "takerCommissionRate": "0.0004"}
    manager = MainnetOneRunManager(
        settings=_settings(),
        client=client,
        repo=MemoryMainnetRunRepo(),
    )

    result = await manager.arm()

    assert "preflight 失敗" in result
    assert "maker fee" in result


@pytest.mark.asyncio
async def test_arm_creates_single_armed_run_when_safe():
    repo = MemoryMainnetRunRepo()
    manager = MainnetOneRunManager(
        settings=_settings(),
        client=FakeMainnetClient(),
        repo=repo,
    )

    result = await manager.arm()

    assert "已啟動" in result
    assert repo.run["status"] == "ARMED"
    assert repo.run["symbol"] == "ETHUSDC"
    assert repo.events[-1][1] == "armed"
