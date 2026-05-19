from config.settings import Settings
from src.gridbot.binance.models import AccountInfo, IncomeRecord, PositionInfo
from src.gridbot.telegram.formatters import format_testnet_dashboard


def test_format_testnet_dashboard_reports_guardrails_and_fees():
    settings = Settings(
        binance_api_key="key",
        binance_api_secret="secret",
        binance_testnet=True,
        trading_symbols="ETHUSDC",
        trading_mode="signal_only",
        testnet_strategy_label="router_allocator_v13_trend350",
        testnet_daily_target_pct=2.7,
        max_effective_leverage=70,
        daily_soft_loss_pct=16,
        max_daily_loss_pct=36,
        max_trade_risk_pct=100,
        trend_aggressive_scale=3.5,
    )
    account = AccountInfo(
        total_wallet_balance=200,
        total_unrealized_profit=1.25,
        total_margin_balance=201.25,
        total_maint_margin=2.0,
        available_balance=180,
    )
    position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=0.2,
        entry_price=3000,
        mark_price=3010,
        unrealized_pnl=1.25,
        liquidation_price=2500,
        leverage=20,
        margin_type="isolated",
    )
    income = [
        IncomeRecord(1, "ETHUSDC", "REALIZED_PNL", 2.0, "USDC", 1, "", ""),
        IncomeRecord(2, "ETHUSDC", "COMMISSION", -0.1, "USDC", 1, "", ""),
        IncomeRecord(3, "ETHUSDC", "FUNDING_FEE", -0.02, "USDC", 1, "", ""),
    ]

    text = format_testnet_dashboard(
        settings=settings,
        account=account,
        positions={"ETHUSDC": position},
        open_orders={"ETHUSDC": [{"orderId": 1}]},
        today_income={"ETHUSDC": income},
        commission_rates={
            "ETHUSDC": {
                "makerCommissionRate": "0",
                "takerCommissionRate": "0.0004",
            }
        },
    )

    assert "TESTNET" in text
    assert "signal_only" in text
    assert "router_allocator_v13_trend350" in text
    assert "Trend aggressive scale: 3.50" in text
    assert "Soft / Hard daily loss: 16.0% / 36.0%" in text
    assert "持倉: LONG" in text
    assert "Open orders: 1" in text
    assert "Fee rate maker/taker: 0.0000% / 0.0400%" in text
    assert "Net: $1.8800" in text
