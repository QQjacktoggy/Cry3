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
    testnet_execution_sizing_mode: str = "signal"
    testnet_auto_trade_interval_seconds: int = 300
    testnet_kline_refresh_seconds: int = 30
    testnet_portfolio_min_volume_ratio: float = 1.2
    testnet_portfolio_trigger_lookback_bars: int = 20
    testnet_portfolio_donchian_volume_multiplier: float = 1.2
    testnet_entry_post_only_enabled: bool = False
    testnet_entry_maker_offset_bps: float = 1.0
    testnet_entry_book_anchor_enabled: bool = True
    testnet_entry_tolerance_bps: float = 0.0
    testnet_entry_tolerance_min_score: int = 0
    testnet_entry_order_ttl_seconds: int = 15
    testnet_entry_reprice_enabled: bool = True
    testnet_entry_reprice_trigger_bps: float = 2.0
    testnet_entry_reprice_cooldown_seconds: int = 15
    testnet_entry_reprice_max_updates: int = 3
    testnet_entry_market_chase_enabled: bool = False
    testnet_entry_market_chase_min_score: int = 90
    testnet_entry_market_chase_max_signal_age_seconds: int = 20
    testnet_entry_market_chase_max_drift_bps: float = 6.0
    testnet_entry_market_chase_min_reward_pct: float = 0.10
    testnet_stop_loss_maker_enabled: bool = False
    testnet_stop_loss_maker_offset_bps: float = 1.0
    testnet_stop_loss_hard_fallback_enabled: bool = True
    testnet_stop_loss_hard_fallback_offset_bps: float = 6.0
    testnet_take_profit_post_only_enabled: bool = False
    testnet_take_profit_exchange_grace_seconds: int = 15
    testnet_position_watchdog_enabled: bool = True
    testnet_position_watchdog_stale_seconds: int = 180
    testnet_position_watchdog_fail_seconds: int = 120
    testnet_position_watchdog_min_progress_bps: float = 3.0
    testnet_position_watchdog_profit_bps: float = 6.0
    testnet_position_watchdog_break_even_bps: float = 1.0
    testnet_position_watchdog_retrace_bps: float = 5.0
    testnet_position_watchdog_adverse_bps: float = 8.0
    testnet_position_watchdog_flexible_tp_enabled: bool = True
    testnet_position_watchdog_extend_near_tp_bps: float = 1.5
    testnet_position_watchdog_extend_tp_bps: float = 4.0
    testnet_position_watchdog_max_tp_extensions: int = 1
    testnet_position_watchdog_exit_offset_bps: float = 1.0
    testnet_residual_cleanup_enabled: bool = True
    testnet_residual_cleanup_max_qty: float = 0.05
    testnet_residual_cleanup_max_notional_usdc: float = 80.0
    testnet_today_pnl_cache_seconds: int = 30
    testnet_ignore_maker_fees_in_stats: bool = True
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
    mainnet_initial_notional_usdc: float = 200.0
    mainnet_max_cumulative_notional_usdc: float = 800.0
    mainnet_leverage: int = 75
    mainnet_fallback_leverage: int = 100
    mainnet_require_zero_maker_fee: bool = True
    mainnet_expected_taker_fee_rate: float = 0.0004
    mainnet_one_run_entry_scan_interval_seconds: int = 15
    mainnet_one_run_manage_interval_seconds: int = 10
    mainnet_one_run_signal_timeout_minutes: int = 60
    mainnet_entry_order_ttl_seconds: int = 45
    # Conservative entry requote (cancel + replace) settings.  When a
    # maker entry has been on the book for at least
    # mainnet_entry_requote_min_age_seconds and the mark price has
    # drifted by more than mainnet_entry_max_deviation_bps, the bot
    # may cancel and replace the order at a fresh passive price.  It
    # may requote up to mainnet_entry_reprice_max_updates times, with
    # at least mainnet_entry_reprice_interval_seconds between
    # requotes.  All limits are best-effort — the existing TTL
    # (mainnet_entry_order_ttl_seconds) and slippage
    # (mainnet_entry_slippage_bps) still apply.
    mainnet_entry_reprice_interval_seconds: int = 10
    mainnet_entry_reprice_max_updates: int = 3
    mainnet_entry_max_deviation_bps: float = 8.0
    mainnet_entry_requote_min_age_seconds: int = 22
    mainnet_partial_exit_pct: float = 0.40
    mainnet_partial_tp_pct: float = 0.0005
    mainnet_recovery_enabled: bool = True
    # Recovery (DCA) settings — reduced from 3 steps to 1, with
    # tighter trigger (0.09% -> 0.05%) so the first averaging happens
    # closer to entry, yielding a better average price and limiting
    # the maximum notional exposure to 2x the base (400 USDC).
    mainnet_recovery_steps: int = 1
    mainnet_recovery_trigger_pct: float = 0.0005
    mainnet_recovery_tp_shrink: float = 0.45
    mainnet_adverse_exit_bars: int = 10
    mainnet_adverse_exit_loss_pct: float = 0.0007
    mainnet_max_holding_bars: int = 20
    mainnet_client_order_prefix: str = "cry3mn"

    # GTX post-only rejection handling.
    # If a post-only (GTX) order is rejected (-5022), re-quote with a fresh
    # book price and retry up to this many times before giving up.
    mainnet_gtx_retry_attempts: int = 3
    # Maximum slippage (in basis points) from the original signal price that
    # is acceptable when re-quoting after a GTX rejection.
    # Entry: 12 bps = 0.12% — relaxed from 8 bps after observing 3 real
    # ENTRY_REJECTED runs (8.05, 9.89 bps) in June 2026. The cost is ~0.04%
    # of additional slippage per filled entry (max), which is well under
    # the expected per-trade TP target (~0.13-0.22 USDC on 200 USDC
    # notional) and improves the signal-to-fill rate materially.
    mainnet_entry_slippage_bps: float = 12.0
    # TP / DCA: slightly tighter — these are exit/averaging orders, not
    # time-sensitive entries, so we allow less chase.
    mainnet_tp_slippage_bps: float = 5.0
    mainnet_dca_slippage_bps: float = 5.0
    # If a TP order is rejected even after all GTX retries, fall back to a
    # GTC limit order at the best available price.  It is better to pay
    # taker fee on a TP than to miss the exit entirely.
    mainnet_tp_fallback_to_gtc: bool = True
    # If an entry order is rejected even after all GTX retries AND the
    # slippage is within tolerance, fall back to a GTC limit order.
    # This means we accept the taker fee risk to get the position open.
    # Set to False to let the run expire on GTX rejection (old behaviour).
    mainnet_entry_fallback_to_gtc: bool = False
    # Stop-loss maker + market fallback settings.
    # When SL is triggered, first try a reduce-only GTX limit at the
    # stop_loss price for up to mainnet_sl_maker_ttl_seconds.
    # If not filled within TTL, fall back to a market order.
    mainnet_sl_use_maker: bool = True
    mainnet_sl_maker_ttl_seconds: int = 10
    mainnet_sl_fallback_to_market: bool = True

    @property
    def mainnet_effective_entry_notional_usdc(self) -> float:
        """Clamp one-run single-ticket notional to the configured equity cap."""
        ticket = max(0.0, float(self.mainnet_initial_notional_usdc))
        cap = max(0.0, float(self.mainnet_equity_cap_usdc))
        if ticket <= 0:
            return cap
        if cap <= 0:
            return ticket
        return min(ticket, cap)

    @property
    def mainnet_effective_entry_margin_usdc(self) -> float:
        leverage = max(1, int(self.mainnet_leverage))
        return self.mainnet_effective_entry_notional_usdc / leverage

    @property
    def mainnet_effective_max_cumulative_notional_usdc(self) -> float:
        configured = max(0.0, float(self.mainnet_max_cumulative_notional_usdc))
        minimum_required = self.mainnet_effective_entry_notional_usdc * (max(0, int(self.mainnet_recovery_steps)) + 1)
        return max(configured, minimum_required)

    # Risk management
    margin_ratio_warning: float = 0.6    # 60% → send warning
    margin_ratio_critical: float = 0.8   # 80% → urgent alert
    max_leverage: int = 10               # absolute max leverage allowed