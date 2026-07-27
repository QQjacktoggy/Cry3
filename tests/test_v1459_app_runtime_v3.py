from __future__ import annotations

from types import SimpleNamespace

import pytest

from config.settings import Settings
from src.gridbot.mainnet.v1459_app_runtime_v3 import build_v1459_app_runtime_v3
from src.gridbot.storage.database import Database


class _ExplodingIdentityClient:
    @property
    def exchange_endpoint(self):
        raise AssertionError("flags-off path must not inspect exchange identity")


def test_v1459_observation_defaults_enable_recording_only() -> None:
    defaults = Settings.model_fields
    assert defaults["mainnet_v1459_observation_enabled"].default is True
    assert defaults["mainnet_v1459_observation_persist_session_enabled"].default is True
    assert defaults["mainnet_v1459_observation_record_opportunities_enabled"].default is True
    assert defaults["mainnet_v1459_observation_record_shadow_enabled"].default is True
    assert defaults["mainnet_v1459_observation_record_reconciliation_enabled"].default is True
    assert (
        defaults["mainnet_v1459_account_fingerprint_marker_path"].default
        == ".codex_identity/mainnet_v1459.marker"
    )
    assert len(defaults["mainnet_v1459_deployment_commit"].default) == 40
    assert defaults["mainnet_codex_v1459_live_enforcement_enabled"].default is False
    assert defaults["mainnet_codex_v1459_runner_enabled"].default is False
    assert defaults["mainnet_codex_v1459_regime_switch_enabled"].default is False


@pytest.mark.asyncio
async def test_flags_off_build_has_no_identity_probe_and_no_runtime(tmp_path) -> None:
    settings = SimpleNamespace(
        mainnet_v1459_observation_enabled=False,
        mainnet_v1459_observation_persist_session_enabled=False,
        mainnet_v1459_observation_record_opportunities_enabled=False,
        mainnet_v1459_observation_record_shadow_enabled=False,
        mainnet_v1459_observation_record_reconciliation_enabled=False,
    )
    db = Database(str(tmp_path / "flags-off.db"))

    built = await build_v1459_app_runtime_v3(
        settings=settings,
        db=db,
        read_only_identity_client=_ExplodingIdentityClient(),
        code_version="v1.4.59-continuation-observation-v2",
    )

    assert built.runtime is None
    assert built.permits_order_mutation is False
    assert built.composition.coordinator.flags.enabled is False
