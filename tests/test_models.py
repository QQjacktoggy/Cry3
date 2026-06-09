from src.gridbot.binance.models import PositionInfo


def test_position_info_infers_leverage_from_notional_and_margin_when_missing():
    position = PositionInfo.from_api(
        {
            "symbol": "ETHUSDC",
            "positionAmt": "-0.043",
            "entryPrice": "2016.53",
            "markPrice": "2004.94000000",
            "unRealizedProfit": "0.49837000",
            "liquidationPrice": "117677.94956268",
            "isolatedMargin": "0",
            "notional": "-86.21242000",
            "marginType": "cross",
            "initialMargin": "0.86212420",
            "positionInitialMargin": "0.86212420",
            "maintMargin": "0.34484968",
        }
    )

    assert position.leverage == 100
    assert position.initial_margin == 0.8621242
