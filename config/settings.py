from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
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
    testnet_strategy_label: str = "router_allocator_v13_trend350"
    testnet_equity_usdc: float = 150.0
    testnet_daily_target_pct: float = 2.7
    testnet_auto_trade_interval_minutes: int = 5
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

    # Risk management
    margin_ratio_warning: float = 0.6    # 60% → send warning
    margin_ratio_critical: float = 0.8   # 80% → urgent alert
    max_leverage: int = 10               # absolute max leverage allowed
