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
    mainnet_telegram_notice_log_path: str = "testnet/logs/mainnet_telegram_notices.jsonl"

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
    mainnet_codex_v1_enabled: bool = False
    mainnet_codex_v1_max_notional_usdc: float = 800.0
    # Comma-separated live-only lane kill switch. The classifier can still
    # report these lanes for research, but mainnet one-run will reject them
    # before order placement. Anchor is disabled after live 20-run data and a
    # fresh SL showed it was the current loss cluster.
    mainnet_codex_v1_disabled_lanes: str = "anchor_s1_preblock_broad_su6_exitA,W1B"
    # Research-only live gate for the current W6 LONG lane.  Disabled by
    # default: we want the branch available for canary testing without changing
    # the baseline until the live sample is larger.
    mainnet_codex_v1_w6_weak_drift_block_enabled: bool = True
    mainnet_codex_v1_w6_weak_drift_threshold_bp: float = -30.0
    # Live-only W6A tightening block (2026-06-17, v1.2.11). Two back-to-back
    # W6A LONG losses shared the same shape: deep negative drift, weak RSI,
    # far-below-VWAP price, deep pullback, and still-positive short-horizon
    # adverse drift. Keep the research lane, but reject this cluster live.
    mainnet_codex_v1_w6_deep_pullback_block_enabled: bool = True
    mainnet_codex_v1_w6_deep_pullback_d30_max_bp: float = -30.0
    mainnet_codex_v1_w6_deep_pullback_adv3_min_bp: float = 6.5
    mainnet_codex_v1_w6_deep_pullback_rsi_max: float = 39.0
    mainnet_codex_v1_w6_deep_pullback_vwap_dist_max_bp: float = -30.0
    mainnet_codex_v1_w6_deep_pullback_pullback_min_bp: float = 30.0
    # Live-only W2A tightening block. Early live W2A canary trades showed the
    # offline score+rng lane was too broad in practice; keep the base lane for
    # research, but reject live entries outside this narrower bounce envelope.
    mainnet_codex_v1_w2a_tight_block_enabled: bool = True
    mainnet_codex_v1_w2a_d30_low_bp: float = -20.0
    mainnet_codex_v1_w2a_d30_high_bp: float = -5.0
    mainnet_codex_v1_w2a_adv3_low_bp: float = 0.0
    mainnet_codex_v1_w2a_adv3_high_bp: float = 6.0
    mainnet_codex_v1_w2a_bb_lower_dist_low_bp: float = 5.0
    mainnet_codex_v1_w2a_bb_lower_dist_high_bp: float = 20.0
    # Live-only W1B tightening block. The first v1.2.1 canary batch showed
    # that W1B was acting as an over-broad fallback SHORT bucket: large losses
    # clustered around stale late accepts, positive adv3 spikes, and stretched
    # lower-band distance. Keep the base lane for research, but reject live
    # entries outside this tighter envelope.
    mainnet_codex_v1_w1b_tight_block_enabled: bool = True
    mainnet_codex_v1_w1b_d30_low_bp: float = -45.0
    mainnet_codex_v1_w1b_d30_high_bp: float = 5.0
    mainnet_codex_v1_w1b_adv3_high_bp: float = 5.0
    mainnet_codex_v1_w1b_bb_lower_dist_high_bp: float = 20.0
    mainnet_codex_v1_w1b_reprice_wait_max_seconds: float = 60.0
    # Codex-only survival manager. Starts watching after 5m, may close weak
    # positions after 6m, and applies damage control after 7m. This is separate
    # from the generic 10-bar adverse exit because recent Codex SLs occurred
    # before that slower guard could react.
    mainnet_codex_survival_enabled: bool = True
    mainnet_codex_survival_watch_after_seconds: int = 300
    mainnet_codex_survival_exit_after_seconds: int = 420
    mainnet_codex_survival_force_after_seconds: int = 420
    mainnet_codex_survival_min_mfe_bp: float = 4.0
    mainnet_codex_survival_micro_trail_floor_bp: float = 0.5
    mainnet_codex_survival_early_fail_loss_bp: float = 6.0
    mainnet_codex_survival_damage_loss_bp: float = 4.0
    # Survival exits are timed exits, not exchange-side emergency stops. Try a
    # reduce-only maker close first to avoid taker fees, then fall back to market
    # after a short TTL so weak trades are not stranded.
    mainnet_codex_survival_exit_use_maker: bool = True
    mainnet_codex_survival_exit_maker_ttl_seconds: int = 5
    mainnet_codex_survival_exit_adverse_break_bp: float = 1.5
    # W6A-specific hotfixes (2026-06-17, v1.2.12)
    mainnet_codex_v1_w6a_target_max_gross_loss_usdc: float = 0.16
    mainnet_codex_v1_w6a_no_tp1_early_exit_live: bool = False
    mainnet_codex_v1_w6a_no_tp1_stop_tighten_live: bool = True
    mainnet_codex_v1_w6a_no_tp1_exit_shadow: bool = True
    # V1.3.0 guarded capital restoration: W6A defaults to $50, and only the
    # clean raw-$200 slice can receive a $200 live cap.
    mainnet_codex_v1_w2a_shadow_only_enabled: bool = True
    mainnet_codex_v1_w6a_guarded_200cap_enabled: bool = True
    mainnet_codex_v1_w6a_default_cap_usdc: float = 50.0
    mainnet_codex_v1_w6a_clean_cap_usdc: float = 200.0
    mainnet_codex_v1_w6a_raw240_block_min_usdc: float = 240.0
    mainnet_codex_v1_w6a_raw240_block_wait_max_seconds: float = 60.0
    mainnet_codex_v1_w6a_bad_rr_ratio_min: float = 2.60
    mainnet_codex_v1_w6a_bad_rr_early_wait_max_seconds: float = 120.0
    mainnet_codex_v1_w6a_clean_rr_ratio_max: float = 2.20
    mainnet_codex_v1_w6a_clean_wait_min_seconds: float = 60.0
    mainnet_codex_v1_w6a_clean_score_min: float = 70.0
    # Codex V1.3.7E W6A entry-risk shadow/risk action tree. This replaces the
    # older clean-RR W6A 200U promotion policy while keeping the same telemetry
    # path: raw classifier stays visible, effective execution carries live risk.
    mainnet_codex_v137_w6a_risk_shadow_enabled: bool = True
    mainnet_codex_v137_w6a_stale_hard_action: str = "cap50"
    mainnet_codex_v137_w6a_default_cap_usdc: float = 50.0
    mainnet_codex_v137_w6a_max_keep_notional_usdc: float = 200.0
    mainnet_codex_v137_w6a_200_risk_score_max: int = 2
    mainnet_codex_v137_w6a_no_bounce_exit_live: bool = True
    mainnet_codex_v137_w6a_no_bounce_exit_shadow: bool = True
    mainnet_codex_v137_w6a_no_bounce_after_seconds: float = 60.0
    mainnet_codex_v137_w6a_no_bounce_maker_ttl_seconds: int = 5
    mainnet_codex_v137_w6a_no_bounce_market_fallback_unrealized_r: float = -0.55
    mainnet_codex_v137_w6a_no_bounce_market_fallback_distance_to_sl_r: float = 0.10
    mainnet_codex_v137_w6a_post_tp_probe_shadow: bool = True
    mainnet_codex_v137_w6a_post_tp_probe_giveback_bp: str = "1.5,2.0,2.5"
    mainnet_codex_v137_w6a_fast_trail_enabled: bool = True
    mainnet_codex_v137_w6a_trail_arm_cap_bp: float = 3.5
    mainnet_codex_v137_w6a_trail_watch_interval_seconds: int = 1
    # Codex V1.3.8 W6A live-exit alignment: preserve the v1.3.7E risk tree,
    # move TP1 to the backtest-selected 6bp, and default away from the extra
    # fast-trail cap unless explicitly re-enabled for a canary.
    mainnet_codex_v138_w6a_partial_tp_pct: float = 0.0006
    mainnet_codex_v138_w6a_fast_trail_enabled: bool = False
    mainnet_codex_v138_w6a_trail_arm_cap_bp: float = 3.5
    mainnet_codex_v138_w6a_trail_watch_interval_seconds: int = 1
    # Codex V1.3.9: promote only two reviewed no-lane shadow buckets as tiny
    # live canaries, while giving W1B more time before loss-cut survival exits.
    mainnet_codex_v139_reprice_canary_enabled: bool = True
    mainnet_codex_v139_reprice_canary_notional_usdc: float = 50.0
    mainnet_codex_v139_reprice_canary_daily_cap: int = 0
    mainnet_codex_v139_reprice_canary_lanes: str = "SH_WPR_L_S1,SH_L1_ADVERSE_REPRICE_MR_LONG"
    mainnet_codex_v139_reprice_canary_entry_offset_bp: float = 0.0
    mainnet_codex_v139_reprice_canary_dca_enabled: bool = False
    mainnet_codex_v139_w1b_survival_enabled: bool = True
    mainnet_codex_v139_w1b_survival_exit_after_seconds: int = 900
    mainnet_codex_v139_w1b_survival_force_after_seconds: int = 900
    mainnet_codex_v139_w1b_survival_early_fail_loss_bp: float = 14.0
    mainnet_codex_v139_w1b_survival_damage_loss_bp: float = 14.0
    mainnet_codex_v139b_wpr_entry_offset_bp: float = 3.0
    mainnet_codex_v139b_wpr_partial_tp_pct: float = 0.00030
    mainnet_codex_v139b_wpr_partial_exit_pct: float = 0.60
    mainnet_codex_v139b_wpr_max_sl_bp: float = 8.0
    mainnet_codex_v139b_wpr_scratch_mfe_bp: float = 3.0
    mainnet_codex_v139b_wpr_scratch_floor_bp: float = 0.5
    mainnet_codex_v139b_wpr_force_after_seconds: int = 240
    mainnet_codex_v139b_wpr_damage_loss_bp: float = 5.0
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
    mainnet_partial_tp_pct: float = 0.0004  # TP1 = 4bp
    mainnet_mid_tp_pct: float = 0.0        # mid exit disabled — remainder is TRAIL-only
    mainnet_mid_exit_pct: float = 0.0      # mid exit fraction disabled (was 0.50)
    mainnet_recovery_enabled: bool = True
    # Recovery (DCA) settings — aligned to backtest best (wildcat_s1s5_7d):
    # steps=3, trigger 0.09% (increments ×(count+1) per layer), tp_shrink 0.45.
    # steps=0 disables DCA entirely.
    # V6.5 (2026-06-11): steps 3 → 1.  Post-fix live data (n=119): 1-layer runs
    # 19/22=86% WR net +2.24 worst -0.25; 2-layer runs 3/7=43% net -7.14 worst
    # -2.01 — ALL catastrophic tails were layer-2 fills during sustained dumps
    # (pre-placed GTX resting orders fill mid-dump and bypass the placement-time
    # guard).  steps=1 reproduces V3's mechanically-shallow DCA profile (249
    # runs: only 2 ever reached layer 2) with modern execution (GTX 0-fee).
    mainnet_recovery_steps: int = 1
    mainnet_recovery_trigger_pct: float = 0.0009
    mainnet_recovery_tp_shrink: float = 0.55
    # After each DCA layer, widen the SL distance by this fraction per layer
    # (sl_pct × (1 + widen × dca_count)) so a freshly averaged position is not
    # immediately swept.  Mirrors backtest_wildcat_s1s5 (0.25/layer).
    mainnet_recovery_sl_widen_per_layer: float = 0.25
    # #25 (2026-06-10): a resting GTX DCA order can partially fill (e.g. 0.001 of
    # an intended 0.124).  The qty-grew detector must NOT treat that as a full
    # layer (widen SL / +1 layer / +full notional / pre-place next) — doing so
    # caused the 21-second double-layer cascade in cry3mn_1781089775237.  A fill
    # is only "a full layer" once filled_qty >= this fraction of the pre-placed
    # order's intended qty; below it we just sync qty tracking and wait.
    mainnet_dca_min_fill_ratio: float = 0.8
    mainnet_adverse_exit_bars: int = 10
    mainnet_adverse_exit_loss_pct: float = 0.0007
    mainnet_max_holding_bars: int = 24
    # Trailing take-profit / profit-lock (2026-06-08). Mirrors the backtest
    # wildcat_v3_trail_c preset (arm 0.7 / giveback 0.25), which lifted 30d
    # PnL +320->+762 and cut MaxDD 45.8->15.8 by locking runner gains that
    # spike toward TP2 then reverse instead of riding them back to SL.
    # Backtest assumes a 1m-low fill; live samples mark every ~10s so realised
    # gain will be somewhat lower — validate on testnet before trusting fully.
    mainnet_trail_enabled: bool = True
    mainnet_trail_arm_frac: float = 0.5      # arm once peak MFE >= this fraction of tp_pct (was 0.7)
    mainnet_trail_giveback_frac: float = 0.5   # lock-exit after retracing this fraction of the run (was 0.25)
    # TRAIL profit-lock exit fee optimisation (2026-06-08). The runner is in
    # profit and not racing a stop, so the lock-exit can be a reduce-only
    # POST_ONLY (maker, 0 USDC fee on ETHUSDC) instead of a market taker close.
    # We place the maker exit at the passive top-of-book and poll for up to
    # mainnet_trail_exit_maker_ttl_seconds; if it has not filled by then (price
    # ran past), we cancel it and market-close the remainder. SL/ADVERSE/MAX_HOLD
    # keep their guaranteed market close — only TRAIL uses this maker-first path.
    # Within the TTL the maker quote is re-priced every
    # mainnet_trail_exit_reprice_seconds to chase the book, so a moving market
    # does not strand it at a stale price (Run 61139 paid a taker fee that way).
    mainnet_trail_exit_use_maker: bool = True
    mainnet_trail_exit_maker_ttl_seconds: int = 12
    mainnet_trail_exit_reprice_seconds: int = 2
    # Fast trail-trigger watcher (2026-06-10).  While TRAIL is armed, a
    # dedicated asyncio task polls the mark at this interval; the 10s manage
    # cycle is too coarse for sub-minute dumps (run cry3mn_1781048052462
    # peaked 1638.03 with theoretical trigger 1637.73, but the next cycle
    # woke at 1636.9 and the whole trail gain was gone).
    mainnet_trail_watch_interval_seconds: int = 2
    # TRAIL profit floor epsilon (E2, 2026-06-10).  The V3 floor (mark > entry,
    # zero margin) was passed by 0.002 on the 06-10 08:32 loss run — firing AT
    # breakeven means the maker exit then bleeds ticks/fees into a net loss.
    # TRAIL may only fire once mark clears cost basis by at least this many
    # basis points; below the floor, SL/DCA keep ownership of the position.
    # The same epsilon gates the maker-exit anchor (E3) and the chase floor.
    mainnet_trail_profit_floor_bp: float = 1.5

    # Codex V1.3.2 TP policy shadow optimizer. Shadow-only by default:
    # this logs paired baseline-vs-variant TP allocation outcomes and never
    # changes live TP orders unless a later manual version enables override.
    mainnet_codex_tp_policy_shadow_enabled: bool = True
    mainnet_codex_tp_policy_live_override_enabled: bool = False
    mainnet_codex_tp_policy_path_ttl_s: int = 900
    mainnet_codex_tp_policy_max_baseline_drift_bp: float = 3.0

    # Codex V1.3.3 evidence-quality repair. These default to audit/shadow-only;
    # live lane expansion, TP override, and maker-first profit execution remain disabled.
    mainnet_codex_v133_no_lane_miner_enabled: bool = True
    mainnet_codex_v133_shadow_family_quota_enabled: bool = True
    mainnet_codex_v133_shadow_family_active_cap: int = 4
    mainnet_codex_v133_diagnostic_fill_enabled: bool = True
    mainnet_codex_v133_tp_terminalization_enabled: bool = True
    mainnet_codex_v133_fee_gate_audit_only: bool = True
    mainnet_codex_v133_fee_gate_enforce: bool = False
    mainnet_codex_v133_estimated_slippage_bp: float = 0.4
    mainnet_codex_v133_min_net_buffer_bp: float = 1.5
    mainnet_codex_v133_net_floor_audit_only: bool = True
    mainnet_codex_v133_maker_first_profit_exit: bool = False
    mainnet_codex_v133_maker_opportunity_audit_enabled: bool = True

    # Codex V1.3.4 conservative frequency recovery. This is intentionally
    # narrow: weak-drift W6A may route only through the existing guarded $50
    # path, while no-lane near-live buckets get better shadow priority.
    mainnet_codex_v134_w6a_weak_drift_50_canary_enabled: bool = False
    mainnet_codex_v134_w6a_weak_drift_50_canary_max_notional_usdc: float = 50.0
    mainnet_codex_v134_w6a_weak_drift_50_canary_daily_limit: int = 3
    mainnet_codex_v134_nl_near_long_priority_enabled: bool = True
    # Codex V1.3.5 live fill-window recovery. Codex live entries do not use the
    # legacy 3-bar ladder path, so selected live lanes can wait longer than the
    # global maker-entry TTL while still staying post-only.
    mainnet_codex_v135_entry_ttl_by_lane_enabled: bool = True
    mainnet_codex_v135_entry_ttl_seconds_by_lane: str = "SPL_1:180,SCP:180,S1P-L:180,W6A:180,RP1:180,W1D:120,SH_NL_NEAR_W1D_LONG_LIVE200:120"
    # DCA directional guard toggle (2026-06-09).
    # True  = momentum_only: block DCA only when stoch cross signals momentum reversal.
    # False = off: no guard (higher DCA fill rate, validated in backtest_dca_guard_compare).
    # Stable choice: True (momentum_only).
    mainnet_dca_guard_enabled: bool = True
    # #24 (2026-06-10): when the rescue per-candle spike gate skips an entry
    # (sharp adverse move), block NORMAL S1 signals for this many seconds too.
    # The rescue path keeps re-evaluating each candle (so a fast V-bounce rescue
    # entry is still allowed), but S1 — which has no adverse-candle check —
    # blindly caught the falling knife 110s after a spike skip in the same run
    # (cry3mn_1781088625968, -0.71).  Time-boxed so we don't permanently block
    # the post-spike V-bounce that fuels most TRAIL wins; 0 disables the block.
    mainnet_spike_block_seconds: int = 120
    # rng15 entry volatility gate (pre-entry 15m hi-lo range in bp).
    # Live V3-V4 data: WR 89%/81%/88% at 20-35/35-55/55-75bp, cliff at 75+ (WR 33%).
    # 0 disables the respective gate.
    mainnet_rng15_gate_high_bp: float = 75.0
    mainnet_rng15_gate_low_bp: float = 20.0
    # Sweet-zone notional multiplier (applies when sweet_low <= rng15 < sweet_high).
    # Default 1.0 = OFF: the 1.2x sizing has a bookkeeping bug (scaled entry counts
    # toward the unscaled cumulative cap, silently eating the 3rd DCA layer).
    mainnet_rng15_sweet_scale: float = 1.0
    mainnet_rng15_sweet_low_bp: float = 20.0
    mainnet_rng15_sweet_high_bp: float = 55.0
    # Range-regime sizing boost (2026-06-11, default OFF).  Counterpart of the
    # DCA drift gate below: golden-window forensics (06-10) showed the 93%-WR
    # segment (13:00-18:50 TW, +2.67/30 runs) was a near-zero-drift range
    # (+6bp over 5.1h) while the losing V5.5 segment was a −183bp/6.8h
    # downtrend — low-|drift| tape is where this system earns.  When
    # range_scale != 1.0 AND drift_max_bp > 0 AND the |signed net drift| of
    # the last N 1m closes at entry is <= drift_max_bp, the entry notional is
    # multiplied by range_scale (composes with the rng15 sweet-zone multiplier
    # above; the V6.5 scale-aware DCA cumulative cap follows automatically).
    # Defaults keep it OFF until the per-entry drift30 values persisted in
    # signal_json accumulate enough samples to pick the thresholds.
    mainnet_range_scale: float = 1.0
    mainnet_range_drift_max_bp: float = 0.0
    mainnet_range_drift_window_bars: int = 30
    # Ladder / limit-entry offset (2026-06-09).
    # Place maker limit this far BELOW signal price for LONG (ABOVE for SHORT) instead
    # of entering immediately at close. 0 = current behaviour (immediate fill).
    # Backtest sweet-spot 5bp; stable choice 3bp.
    mainnet_entry_limit_offset: float = 0.0003   # 3bp
    # How many 1-minute bars to wait for the limit entry to fill.  If still open
    # after this many bars, cancel and drop the signal (miss).
    mainnet_entry_limit_ttl_bars: int = 3
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
    # SL slippage cap (A3, 2026-06-08).  A plain STOP_MARKET guarantees the
    # exit but fills at market once triggered, so a fast spike can slip well
    # past the trigger (Run 2: trigger 1688.49 -> filled 1690.98, ~0.15%).
    # When this is > 0 the SL is armed as a STOP (stop-limit) whose limit price
    # is the trigger worsened by this fraction, capping the worst fill.
    # TRADE-OFF: in an extreme gap the limit may NOT fill, leaving the position
    # open past the stop until the adverse-exit / max-hold market backstop
    # fires.  Default 0.0 keeps the original guaranteed-exit STOP_MARKET;
    # validate on testnet before enabling on mainnet.
    mainnet_sl_limit_cap_pct: float = 0.0

    # Residual "dust" cleanup threshold.  After partial TP fills, the remaining
    # position may be tiny and its ideal-price TP can sit unfilled until a
    # reverse move triggers the SL.  When the remaining notional is below this
    # threshold, re-quote it as a reduce-only POST-ONLY (maker, 0 USDC fee)
    # order at the top of book so it fills fast without paying taker fees.
    mainnet_residual_cleanup_notional_usdc: float = 20.0

    # Loop cooldown: after a NET-LOSS exit in a loop chain, the same
    # (side, strategy_label) combination is blocked for N minutes so
    # we do not chain into an identical losing setup.  The cooldown escalates
    # with the consecutive-loss streak: base + step*(streak-1) — e.g. 3, 8, 13…
    # minutes for 1, 2, 3 losses in a row.  Any net-win resets the streak.
    mainnet_loop_cooldown_minutes: int = 3
    mainnet_loop_cooldown_step_minutes: int = 5
    # Loop loss protection (2026-06-10): stop chaining new runs once the
    # loop's cumulative NET PnL (realized − commission) falls to −cap USDC.
    # 0 = disabled.  Runtime-adjustable from the Telegram 🛡 buttons and
    # persisted in app_config (key mainnet_loop_loss_cap_usdc); this field
    # is only the cold-start default.
    mainnet_loop_loss_cap_usdc: float = 0.0
    # DCA guard cooldown: after the DCA risk guard blocks a recovery order,
    # hold that block for this many seconds regardless of regime re-classification
    # (prevents a brief range flicker from bypassing the guard within the window).
    # NOTE (2026-06-11): the cooldown alone proved too weak — live DB 06-10~06-11
    # showed 5 runs where a DCA layer still filled 1.1~2.8 min AFTER a
    # dca_guard_blocked event (1W/4L, net −5.58 USDC).  One guard block now also
    # bans DCA for the rest of that run (see MainnetOneRunManager
    # _dca_guard_blocked_runs); this cooldown remains as the short-window brake.
    mainnet_dca_guard_cooldown_seconds: int = 60
    # DCA-only drift gate (P1, 2026-06-11, Codex proposal).  Golden-window
    # forensics: the 93%-WR segment (06-10 13:00-18:50 TW) drifted just +6bp
    # over 5.1h (range) and DCA was a profit assist, while the V5.5 losing
    # segment was a −183bp/6.8h downtrend where DCA was a loss amplifier (both
    # −2.0 tails were DCA layer fills mid-dump).  When > 0, a DCA attempt
    # (poll AND preplace paths) is blocked while the |signed net drift| of the
    # last N 1m closes exceeds this many bp.  Entries are never touched, and
    # the gate re-opens as soon as the drift fades — unlike the permanent
    # per-run momentum-guard ban.  0 = disabled.
    # 30.0 chosen from post-fix live DCA fills (06-11 analysis): losing-run
    # fills sat at |d30| med 28.1bp vs winning-run 19.2bp; X=30 would have
    # blocked 6/13 losing fills but only 6/28 winning ones, and catches the
    # two −1.8/−1.9 tails (runs …111192475 d30=−39.5, …108474752 d30=+45)
    # whose momentum guard never fired (so the permanent ban cannot see them).
    mainnet_dca_drift_gate_bp: float = 30.0
    mainnet_dca_drift_window_bars: int = 30
    # Direction consecutive-loss throttle (Option A, V6.8.5).
    # If >= loss_count net-loss exits for the same direction occur within
    # window_seconds, that direction is blocked for block_minutes.
    # Judges loss on net PnL (realized-commission), same as loop cooldown.
    # A net win resets the loss counter for that direction.  0 on
    # block_minutes disables the feature entirely.
    mainnet_dir_throttle_loss_count: int = 2
    mainnet_dir_throttle_window_seconds: float = 3600.0
    mainnet_dir_throttle_block_minutes: float = 30.0

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
