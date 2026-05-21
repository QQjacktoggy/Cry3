from config.settings import Settings
from src.gridbot.binance.models import AccountInfo, IncomeRecord, PositionInfo
from src.gridbot.telegram.formatters import format_testnet_daily_report, format_testnet_dashboard


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
    assert "只產生訊號" in text
    assert "router_allocator_v13_trend350" in text
    assert "趨勢激進倍率: 3.50" in text
    assert "單日軟停損 / 硬停損: 16.0% / 36.0%" in text
    assert "持倉: 做多" in text
    assert "保證金模式: 逐倉" in text
    assert "未成交掛單: 1" in text
    assert "費率 Maker / Taker: 0.0000% / 0.0400%" in text
    assert "今日淨損益: $1.8800" in text


def test_format_testnet_daily_report_reports_target_progress_and_protection_orders():
    settings = Settings(
        binance_api_key="key",
        binance_api_secret="secret",
        binance_testnet=True,
        trading_symbols="ETHUSDC",
        testnet_strategy_label="router_allocator_v13_trend350",
        testnet_equity_usdc=150,
        testnet_daily_target_pct=2.7,
    )
    position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=0.069,
        entry_price=2142.62,
        mark_price=2147.13,
        unrealized_pnl=0.31,
        liquidation_price=0,
        leverage=1,
        margin_type="isolated",
    )
    income = [
        IncomeRecord(1, "ETHUSDC", "REALIZED_PNL", 2.0, "USDC", 1, "", ""),
        IncomeRecord(2, "ETHUSDC", "COMMISSION", -0.1, "USDC", 1, "", ""),
        IncomeRecord(3, "ETHUSDC", "FUNDING_FEE", -0.02, "USDC", 1, "", ""),
    ]

    text = format_testnet_daily_report(
        settings=settings,
        positions={"ETHUSDC": position},
        open_algo_orders={"ETHUSDC": [{"algoId": 1}, {"algoId": 2}]},
        today_income={"ETHUSDC": income},
        report_timezone="Asia/Taipei",
    )

    assert "Testnet 每日獲利匯報" in text
    assert "統計日界線: <b>Asia/Taipei</b>" in text
    assert "日目標: 2.70% ($4.0500)" in text
    assert "交易所保護單: 2" in text
    assert "已實現淨額: $1.8800" in text
    assert "含未實現合計: $2.1900" in text
