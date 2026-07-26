import json

from src.gridbot.mainnet.one_run import MainnetOneRunManager
from src.gridbot.mainnet.run_reconciler import reconcile_run
from src.gridbot.mainnet.v1459_live_reconciliation import build_reconciliation_payloads


def test_sl_client_algo_id_links_actual_terminal_fill_to_exit_role() -> None:
    run_id = "cry3mn_event_evidence"
    events = [
        {
            "event_type": "sl_placed",
            "details_json": json.dumps(
                {"order": {"algoId": 12, "clientAlgoId": "x-Cb7y-stop-algo"}}
            ),
        },
        {
            "event_type": "fill_v1",
            "details_json": json.dumps(
                {"order_id": 99, "client_order_id": "x-Cb7y-stop-algo"}
            ),
        },
    ]

    records, event_ids, sl_ids, event_roles = MainnetOneRunManager._v1459_event_order_evidence(
        {"run_id": run_id, "entry_order_id": 11}, events
    )
    payloads = build_reconciliation_payloads(
        run_id=run_id,
        orders=[
            {"orderId": 11, "clientOrderId": f"{run_id}_entry"},
            *records,
        ],
        trades=[
            {"id": 101, "orderId": 11, "maker": True, "realizedPnl": "0", "commission": "0", "commissionAsset": "USDC"},
            {"id": 102, "orderId": 99, "maker": False, "realizedPnl": "-0.10", "commission": "0.02", "commissionAsset": "USDC"},
        ],
        event_owned_order_ids=event_ids,
        explicit_sl_order_ids=sl_ids,
        event_owned_order_roles=event_roles,
    )

    assert event_roles["12"] == "EXIT"
    assert event_roles["99"] == "EXIT"
    assert reconcile_run(payloads.trades, payloads.incomes, require_closed_run=True).reconciliation_status == "COMPLETE"
