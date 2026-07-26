import json

import pytest

from src.gridbot.telegram.adaptive_status import build_adaptive_status


class FakeDb:
    def __init__(self, rows):
        self.rows = rows

    async def fetchall(self, sql, params=()):
        return self.rows


@pytest.mark.asyncio
async def test_adaptive_status_shows_current_run_and_route():
    rows = [
        {
            "run_id": "cry3mn_1",
            "status": "COMPLETED",
            "params_json": json.dumps({"mode": "adaptive_continuous", "adaptive": {"session_id": "adaptive_1"}}),
            "signal_json": json.dumps({}),
            "realized_pnl_usdc": 0.05,
            "commission_usdc": 0.04,
            "exit_reason": "TP",
        },
        {
            "run_id": "cry3mn_2",
            "status": "ARMED",
            "params_json": json.dumps({"mode": "adaptive_continuous", "adaptive": {"session_id": "adaptive_1"}}),
            "signal_json": json.dumps(
                {
                    "action": "L_E2_TP14_SL8_T90_LOCK90_6_0",
                    "codex_v1": {
                        "version": "_codex_v1.4.58",
                        "lane_code": "CNL-WPR-L",
                        "market_state": "deep_discount_stable",
                        "effective_execution": {"route": "OBSERVE_ONLY"},
                    },
                }
            ),
            "realized_pnl_usdc": 0.0,
            "commission_usdc": 0.0,
            "exit_reason": None,
        },
    ]

    text = await build_adaptive_status(FakeDb(rows))

    assert "第 2 run" in text
    assert "ARMED" in text
    assert "CNL-WPR-L" in text
    assert "deep_discount_stable" in text
    assert "OBSERVE_ONLY" in text
    assert "L_E2_TP14_SL8_T90_LOCK90_6_0" in text
    assert "+0.0100 USDC" in text
    assert "舊 Testnet：<b>OFF</b>" in text

