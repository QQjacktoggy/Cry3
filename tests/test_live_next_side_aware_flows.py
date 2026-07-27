from decimal import Decimal

from src.gridbot.strategy.live_next.exact_replay import ExactAggTrade
from src.gridbot.strategy.live_next.market_features import iter_causal_feature_frames


def test_lower_range_inward_buy_flow_is_not_reused_as_shock_short_flow() -> None:
    trades = []
    for index in range(75):
        price = Decimal("100.00")
        if index == 20:
            price = Decimal("100.20")
        elif index == 72:
            price = Decimal("99.80")
        elif index == 73:
            price = Decimal("99.81")
        elif index == 74:
            price = Decimal("99.82")
        trades.append(
            ExactAggTrade(
                transact_time_ms=1_700_000_000_100 + index * 1_000,
                agg_trade_id=2_000 + index,
                price=price,
                quantity=Decimal("10") if index == 74 else Decimal("1"),
                is_buyer_maker=index != 74,
            )
        )

    values = tuple(iter_causal_feature_frames(tuple(trades)))[-1].snapshot.values

    assert values["range_position_60s"] <= 0.15
    assert values["move_2s_bps"] > 1.0
    assert values["range_inward_flow_ratio"] == 1.0
    assert values["shock_reversal_flow_ratio"] == 0.0
