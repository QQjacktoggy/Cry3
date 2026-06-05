from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    binance_api_key: str
    binance_api_secret: str
    binance_testnet: bool = False

    trading_symbols: str = "BTCUSDC,ETHUSDC,SOLUSDC"

    @property
    def symbols_list(self) -> list[str]:
        """Parse comma-separated trading_symbols into a list."""
        return [s.strip() for s in self.trading_symbols.split(",") if s.strip()]

    fetch_interval_minutes: int = 30

    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.1-flash-preview"

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    @property
    def telegram_chat_id_int(self) -> int:
        """Parse chat_id as integer. Returns 0 if not configured."""
        try:
            return int(self.telegram_chat_id)
        except (ValueError, TypeError):
            return 0

    db_path: str = "data/gridbot.db"
    active_strategy_name: str = "moderate"
    log_level: str = "INFO"

    # Testnet trader guardrails and candidate strategy metadata.
    trading_mode: str = "signal_only"
    testnet_strategy_label: str = "router_allocator_high_return_live"
    testnet_equity_usdc: float = 150.0
    testnet_daily_target_pct: float = 2.7
    testnet_auto_trade_interval_minutes: int = 5
    testnet_manage_interval_seconds: int = 15
    testnet_manage_flat_interval_seconds: int = 60
    testnet_kline_interval: str = "5m"
    testnet_kline_limit: int = 300
    testnet_min_signal_score: int = 58
    testnet_max_position_margin_pct: float = 35.0
    max_effective_leverage: float = 70.0
    max_daily_loss_pct: float = 36.0
    daily_soft_loss_pct: float = 16.0
    max_trade_risk_pct: float = 100.0
    trend_aggressive_scale: float = 3.5
    testnet_order_notional_usdc: float = 10.0
    testnet_order_leverage: int = 5
    testnet_max_order_notional_usdc: float = 150.0
    testnet_maker_fee_rate: float = 0.0002
    testnet_taker_fee_rate: float = 0.0004
    testnet_exchange_protection_enabled: bool = True
    testnet_min_reward_pct: float = 0.12
    testnet_entry_order_ttl_bars: int = 8
    testnet_entry_fill_policy: str = "limit_tolerance"
    testnet_entry_tolerance_bps: float = 0.0
    testnet_entry_tolerance_min_score: int = 0
    testnet_entry_reprice_enabled: bool = True
    testnet_entry_reprice_trigger_bps: float = 2.0
    testnet_entry_reprice_cooldown_seconds: int = 15
    testnet_entry_reprice_max_updates: int = 3
    testnet_daily_report_enabled: bool = True
    testnet_daily_report_hour: int = 21
    testnet_daily_report_minute: int = 0
    testnet_daily_report_timezone: str = "Asia/Taipei"

    # Telegram signal / manual mainnet analysis helpers.
    testnet_telegram_signal_only: bool = False
    manual_mainnet_api_key: str = ""
    manual_mainnet_api_secret: str = ""
    manual_signal_match_window_minutes: int = 20
    minimax_api_key: str = ""
    minimax_base_url: str = "https://api.minimax.io/v1"
    minimax_model: str = "MiniMax-M3"

    # Mainnet one-run validation. This is intentionally opt-in and separate
    # from the testnet trader so testnet safeguards remain intact.
    mainnet_one_run_enabled: bool = False
    mainnet_api_key: str = ""
    mainnet_api_secret: str = ""
    mainnet_symbol: str = "ETHUSDC"
    mainnet_strategy_label: str = "wildcat_v2_adverse_guard"
    mainnet_equity_cap_usdc: float = 200.0
    mainnet_initial_notional_usdc: float = 1000.0
    mainnet_max_cumulative_notional_usdc: float = 4000.0
    mainnet_leverage: int = 75
    mainnet_fallback_leverage: int = 100
    mainnet_require_zero_maker_fee: bool = True
    mainnet_expected_taker_fee_rate: float = 0.0004
    mainnet_one_run_entry_scan_interval_seconds: int = 15
    mainnet_one_run_manage_interval_seconds: int = 10
    mainnet_one_run_signal_timeout_minutes: int = 60
    mainnet_entry_order_ttl_seconds: int = 45
    mainnet_entry_reprice_interval_seconds: int = 10
    mainnet_entry_reprice_max_updates: int = 3
    mainnet_entry_max_deviation_bps: float = 8.0
    mainnet_partial_exit_pct: float = 0.40
    mainnet_partial_tp_pct: float = 0.0005
    mainnet_recovery_enabled: bool = True
    mainnet_recovery_steps: int = 3
    mainnet_recovery_trigger_pct: float = 0.0009
    mainnet_recovery_tp_shrink: float = 0.45
    mainnet_adverse_exit_bars: int = 10
    mainnet_adverse_exit_loss_pct: float = 0.0007
    mainnet_max_holding_bars: int = 20
    mainnet_client_order_prefix: str = "cry3mn"

    # Risk management
    margin_ratio_warning: float = 0.6    # 60% → send warning
    margin_ratio_critical: float = 0.8   # 80% → urgent alert
    max_leverage: int = 10               # absolute max leverage allowed
