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
    # Retire the legacy testnet trader and Telegram notices by default.
    testnet_legacy_enabled: bool = False
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
    mainnet_codex_v1_max_notional_usdc: float = 50.0
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
    # V1.4.31: time-lock is a profit-preservation exit. It should rest a maker
    # close while keeping exchange TP/SL protection alive; if the maker does not
    # fill, defer back to the manage loop instead of paying taker immediately.
    mainnet_codex_v1427_time_lock_maker_only_enabled: bool = True
    mainnet_codex_v1427_time_lock_maker_ttl_seconds: int = 20
    mainnet_codex_v1427_time_lock_adverse_break_bp: float = 1.5
    # V1.4.32: full-exit TP profiles can touch target on mark/book without the
    # post-only TP order filling. Add a maker-only rescue lock so MFE above TP
    # does not round-trip to SL.
    mainnet_codex_full_tp_touch_maker_only_enabled: bool = True
    mainnet_codex_full_tp_touch_maker_ttl_seconds: int = 12
    mainnet_codex_full_tp_touch_min_floor_bp: float = 6.0
    mainnet_codex_full_tp_touch_adverse_break_bp: float = 1.5
    # V1.4.33: positive max-hold exits are profit locks, not emergency stops.
    # Rest a reduce-only maker close first when the trade is above a fee-safe
    # floor, so MAX_HOLD_WIN does not leak most of the edge to taker fees.
    mainnet_codex_max_hold_profit_lock_enabled: bool = True
    mainnet_codex_max_hold_profit_maker_only_enabled: bool = True
    mainnet_codex_max_hold_profit_maker_ttl_seconds: int = 12
    mainnet_codex_max_hold_profit_min_floor_bp: float = 5.0
    mainnet_codex_max_hold_profit_adverse_break_bp: float = 1.5
    mainnet_codex_max_hold_profit_lock_states: str = "STUP-S:mixed,STUP-S:weak_chop,STUP-S:clean_extension,CNL-WPR-L:falling_discount_trap,CNL-WPR-L:fast_reclaim,CNL-WPR-L:deep_discount_stable,CNL-WPR-L:discount_mixed"

    # V1.4.34: STUP-S v1430 full-exit profiles can show a real 5-10bp MFE
    # before the original 11bp TP fills. Let the fast watcher try a maker-only
    # floor lock while keeping TP/SL protection if the maker close cannot fill.
    mainnet_codex_v1434_stups_fast_floor_maker_only_enabled: bool = True
    mainnet_codex_v1434_stups_fast_floor_floor_bp: float = 5.0
    mainnet_codex_v1434_stups_fast_floor_trigger_bp: float = 5.0
    mainnet_codex_v1434_stups_fast_floor_maker_ttl_seconds: int = 6
    mainnet_codex_v1434_stups_fast_floor_adverse_break_bp: float = 1.0
    mainnet_codex_v1434_stups_fast_floor_states: str = "STUP-S:mixed,STUP-S:clean_extension,STUP-S:counter_recoil"
    # V1.4.35: staged STUP-S TP1 protection. Start peak tracking immediately,
    # but only close the runner after TP1 has actually filled. If the 6bp TP1
    # touch does not fill and price falls back toward 5bp, replace TP1 with a
    # reduce-only maker floor order for the TP1 slice; no taker fallback.
    mainnet_codex_v1435_stups_staged_runner_pre_tp1_watch_enabled: bool = True
    mainnet_codex_v1435_stups_tp1_floor_enabled: bool = True
    mainnet_codex_v1435_stups_tp1_floor_floor_bp: float = 5.0
    mainnet_codex_v1435_stups_tp1_floor_trigger_bp: float = 6.0
    mainnet_codex_v1435_stups_tp1_floor_states: str = "STUP-S:mixed,STUP-S:clean_extension,STUP-S:counter_recoil"
    # V1.4.36: live fee/entry repair. Do not turn thin gross-positive
    # MAX_HOLD_WIN into a guaranteed fee-negative market close, and block
    # late STUP-S shorts after a higher short-veto shadow already spent edge.
    mainnet_codex_v1436_max_hold_win_fee_floor_defer_enabled: bool = True
    mainnet_codex_v1436_max_hold_win_fee_floor_defer_extra_bars: int = 2
    mainnet_codex_v1436_late_stups_after_veto_enabled: bool = True
    mainnet_codex_v1436_late_stups_after_veto_edge_spent_bp: float = 10.0
    # V1.4.37: recent STUP-S clean_extension shorts showed a tiny positive MFE
    # window before fast reversal. Capture that with maker-only, no taker
    # fallback, and expand the late-veto edge guard beyond mixed.
    mainnet_codex_v1437_late_stups_after_veto_states: str = "STUP-S:mixed,STUP-S:clean_extension"
    mainnet_codex_v1437_stups_clean_extension_thin_lock_enabled: bool = True
    mainnet_codex_v1437_stups_clean_extension_thin_lock_after_seconds: int = 50
    mainnet_codex_v1437_stups_clean_extension_thin_lock_mfe_bp: float = 3.5
    mainnet_codex_v1437_stups_clean_extension_thin_lock_floor_bp: float = 3.5
    mainnet_codex_v1437_stups_clean_extension_thin_lock_slope_max_bp: float = 1.0
    mainnet_codex_v1437_stups_clean_extension_thin_lock_maker_ttl_seconds: int = 8
    mainnet_codex_v1437_stups_clean_extension_thin_lock_adverse_break_bp: float = 1.0
    # V1.4.38: strict live TTL and STUP-S thin-profit capture. The strict TTL
    # guard rejects maker entries that only become visible after their lane TTL;
    # the thin lock covers 5bp-ish counter_recoil/clean_extension MFE that is too
    # small for the generic 6bp profile time lock but too valuable to let roll to SL.
    mainnet_codex_v1438_strict_entry_ttl_enabled: bool = True
    mainnet_codex_v1438_entry_late_fill_grace_seconds: float = 0.0
    mainnet_codex_v1438_stups_thin_lock_enabled: bool = True
    mainnet_codex_v1438_stups_thin_lock_states: str = "STUP-S:counter_recoil,STUP-S:clean_extension"
    mainnet_codex_v1438_stups_thin_lock_after_seconds: int = 60
    mainnet_codex_v1438_stups_thin_lock_mfe_bp: float = 5.5
    mainnet_codex_v1438_stups_thin_lock_floor_bp: float = 5.0
    mainnet_codex_v1438_stups_thin_lock_slope_max_bp: float = 0.0
    mainnet_codex_v1438_stups_thin_lock_maker_ttl_seconds: int = 8
    mainnet_codex_v1438_stups_thin_lock_adverse_break_bp: float = 1.0
    # V1.4.39: attach a Qlib-style shadow selector score to live decisions
    # without blocking entries, and use that evidence to capture STUP-S mixed /
    # weak_chop 5bp-ish MFE with maker-only exits.
    mainnet_codex_v1439_shadow_score_enabled: bool = True
    mainnet_codex_v1439_shadow_score_review_threshold: int = 35
    mainnet_codex_v1439_shadow_score_thin_lock_threshold: int = 40
    mainnet_codex_v1439_shadow_score_block_candidate_threshold: int = 55
    mainnet_codex_v1439_stups_shadow_thin_lock_enabled: bool = True
    mainnet_codex_v1439_stups_shadow_thin_lock_states: str = "STUP-S:mixed,STUP-S:weak_chop"
    mainnet_codex_v1439_stups_shadow_thin_lock_after_seconds: int = 60
    mainnet_codex_v1439_stups_shadow_thin_lock_mfe_bp: float = 5.5
    mainnet_codex_v1439_stups_shadow_thin_lock_floor_bp: float = 5.0
    mainnet_codex_v1439_stups_shadow_thin_lock_slope_max_bp: float = 0.5
    mainnet_codex_v1439_stups_shadow_thin_lock_lookback_seconds: int = 30
    mainnet_codex_v1439_stups_shadow_thin_lock_maker_ttl_seconds: int = 8
    mainnet_codex_v1439_stups_shadow_thin_lock_adverse_break_bp: float = 1.0
    # V1.4.41/42: research-selector actions from the refreshed live dataset.
    # v1.4.42 promotes the two live-observed issues:
    # - STUP-S clean-extension LONG chase/side-override entries are blocked.
    # - CNL-WPR-L strict rows use shorter entry TTL and thinner maker locks.
    mainnet_codex_v1441_research_selector_enabled: bool = True
    mainnet_codex_v1441_mixed_thin_lock_enabled: bool = True
    mainnet_codex_v1441_mixed_thin_lock_after_seconds: int = 45
    mainnet_codex_v1441_mixed_thin_lock_mfe_bp: float = 3.0
    mainnet_codex_v1441_mixed_thin_lock_floor_bp: float = 2.5
    mainnet_codex_v1441_mixed_thin_lock_slope_max_bp: float = 0.75
    mainnet_codex_v1441_mixed_thin_lock_lookback_seconds: int = 30
    mainnet_codex_v1441_mixed_thin_lock_maker_ttl_seconds: int = 8
    mainnet_codex_v1441_mixed_thin_lock_adverse_break_bp: float = 1.0
    mainnet_codex_v1442_stups_chase_block_enabled: bool = True
    mainnet_codex_v1442_stups_chase_rng15_min_bp: float = 50.0
    mainnet_codex_v1442_stups_chase_d30_min_bp: float = 25.0
    mainnet_codex_v1442_stups_chase_adv3_min_bp: float = 10.0
    mainnet_codex_v1442_stups_chase_vwap_min_bp: float = 8.0
    mainnet_codex_v1442_cnl_wpr_strict_ttl_enabled: bool = True
    mainnet_codex_v1442_cnl_wpr_strict_entry_ttl_seconds: int = 20
    mainnet_codex_v1442_cnl_wpr_max_hold_floor_bp: float = 4.0
    # V1.4.43: live-new-method follow-up. Keep the clean-extension thin-lock
    # edge, remove the failed STUP-S mixed live canary, and try short maker
    # scratches for near-flat fee-leak exits before paying taker.
    mainnet_codex_v1443_stups_mixed_live_block_enabled: bool = True
    mainnet_codex_v1443_max_hold_loss_maker_scratch_enabled: bool = True
    mainnet_codex_v1443_max_hold_loss_scratch_min_bp: float = -2.5
    mainnet_codex_v1443_max_hold_loss_scratch_max_bp: float = 0.75
    mainnet_codex_v1443_max_hold_loss_maker_ttl_seconds: int = 3
    mainnet_codex_v1443_max_hold_loss_adverse_break_bp: float = 0.75
    mainnet_codex_v1443_entry_late_fill_maker_scratch_enabled: bool = True
    mainnet_codex_v1443_entry_late_fill_scratch_min_bp: float = -2.0
    mainnet_codex_v1443_entry_late_fill_scratch_max_bp: float = 1.0
    mainnet_codex_v1443_entry_late_fill_maker_ttl_seconds: int = 3
    mainnet_codex_v1443_entry_late_fill_adverse_break_bp: float = 0.75
    mainnet_codex_v1443_stups_clean_extension_reversal_scratch_enabled: bool = True
    mainnet_codex_v1443_stups_clean_extension_reversal_after_seconds: int = 45
    mainnet_codex_v1443_stups_clean_extension_reversal_mfe_bp: float = 5.0
    mainnet_codex_v1443_stups_clean_extension_reversal_current_min_bp: float = -2.0
    mainnet_codex_v1443_stups_clean_extension_reversal_current_max_bp: float = 1.5
    mainnet_codex_v1443_stups_clean_extension_reversal_giveback_bp: float = 4.0
    mainnet_codex_v1443_stups_clean_extension_reversal_slope_max_bp: float = 0.25
    mainnet_codex_v1443_stups_clean_extension_reversal_maker_ttl_seconds: int = 3
    mainnet_codex_v1443_stups_clean_extension_reversal_adverse_break_bp: float = 0.75
    # V1.4.44: CNL-WPR-L deep-discount full-exit profiles can reach +5~7bp
    # without filling TP. Capture a weakening bounce with maker-only thin lock
    # instead of requiring the old 5bp anchor floor to pass exactly.
    mainnet_codex_v1444_cnl_wpr_deep_trail_lock_enabled: bool = True
    mainnet_codex_v1444_cnl_wpr_deep_trail_after_seconds: int = 60
    mainnet_codex_v1444_cnl_wpr_deep_trail_mfe_bp: float = 5.5
    mainnet_codex_v1444_cnl_wpr_deep_trail_floor_bp: float = 4.5
    mainnet_codex_v1444_cnl_wpr_deep_trail_slope_max_bp: float = 0.0
    mainnet_codex_v1444_cnl_wpr_deep_trail_maker_ttl_seconds: int = 5
    mainnet_codex_v1444_cnl_wpr_deep_trail_adverse_break_bp: float = 0.75
    mainnet_codex_v1444_cnl_wpr_deep_time_lock_floor_bp: float = 4.5
    # V1.4.45: block the STUP-S clean-extension short shape that repeatedly
    # produced low/small MFE live SLs before it reaches the order book.
    mainnet_codex_v1445_stups_clean_short_quality_block_enabled: bool = True
    mainnet_codex_v1445_stups_clean_short_quality_rsi_max: float = 60.8432
    mainnet_codex_v1445_stups_clean_short_quality_slope30_min_bp: float = 1.26926
    # V1.4.47: STUP-S clean_extension SHORT->LONG side override is allowed only
    # while the chase remains fresh/constructive.  The 2026-07-04 live loss
    # showed high VWAP premium + high rng + stale wait + negative slope can
    # produce only ~4bp MFE before SL.
    mainnet_codex_v1447_stups_long_chase_quality_block_enabled: bool = True
    mainnet_codex_v1447_stups_long_chase_vwap_min_bp: float = 30.0
    mainnet_codex_v1447_stups_long_chase_rng15_min_bp: float = 35.0
    mainnet_codex_v1447_stups_long_chase_d30_min_bp: float = 20.0
    mainnet_codex_v1447_stups_long_chase_wait_min_seconds: float = 300.0
    mainnet_codex_v1447_stups_long_chase_slope30_max_bp: float = 0.0
    # V1.4.48: STUP-S clean-extension shorts frequently spike +6~8bp in the
    # first 10-20s, then revert before the older 45-60s thin locks can fire.
    # Capture that window with maker-only / fee-safe behavior, and prevent
    # v1443 clean-reversal scratch from paying taker fees below a fee-safe floor.
    mainnet_codex_v1448_stups_clean_extension_fast_scalp_enabled: bool = True
    mainnet_codex_v1448_stups_clean_extension_fast_scalp_after_seconds: int = 10
    mainnet_codex_v1448_stups_clean_extension_fast_scalp_mfe_bp: float = 6.0
    mainnet_codex_v1448_stups_clean_extension_fast_scalp_floor_bp: float = 5.0
    mainnet_codex_v1448_stups_clean_extension_fast_scalp_maker_ttl_seconds: int = 8
    mainnet_codex_v1448_stups_clean_extension_fast_scalp_adverse_break_bp: float = 1.0
    mainnet_codex_v1448_stups_clean_extension_reversal_fee_floor_enabled: bool = True
    mainnet_codex_v1448_stups_clean_extension_reversal_fee_floor_bp: float = 5.9
    # V1.4.49: CNL-WPR-L live-loss repair. Late fills are not allowed to pay
    # immediate taker fees when they are still within a controllable small-loss
    # window; try a short maker-first exit, then fall back if the book worsens.
    mainnet_codex_v1449_cnl_wpr_late_fill_maker_exit_enabled: bool = True
    mainnet_codex_v1449_cnl_wpr_late_fill_scratch_min_bp: float = -8.0
    mainnet_codex_v1449_cnl_wpr_late_fill_scratch_max_bp: float = 4.0
    mainnet_codex_v1449_cnl_wpr_late_fill_maker_ttl_seconds: int = 6
    mainnet_codex_v1449_cnl_wpr_late_fill_adverse_break_bp: float = 1.0
    # W6A-specific hotfixes (2026-06-17, v1.2.12)
    mainnet_codex_v1_w6a_target_max_gross_loss_usdc: float = 0.16
    mainnet_codex_v1_w6a_no_tp1_early_exit_live: bool = False
    mainnet_codex_v1_w6a_no_tp1_stop_tighten_live: bool = True
    mainnet_codex_v1_w6a_no_tp1_exit_shadow: bool = True
    # V1.3.0 guarded capital restoration: W6A defaults to $50, and only the
    # clean raw-$200 slice can receive a $200 live cap.
    mainnet_codex_v1_w2a_shadow_only_enabled: bool = True
    mainnet_codex_v1_w6a_guarded_200cap_enabled: bool = True
    mainnet_codex_v1_w6a_default_cap_usdc: float = 100.0
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
    mainnet_codex_v137_w6a_no_bounce_after_seconds: float = 120.0
    mainnet_codex_v137_w6a_no_bounce_maker_ttl_seconds: int = 5
    mainnet_codex_v137_w6a_no_bounce_market_fallback_unrealized_r: float = -0.55
    mainnet_codex_v137_w6a_no_bounce_market_fallback_distance_to_sl_r: float = 0.10
    mainnet_codex_v137_w6a_post_tp_probe_shadow: bool = True
    mainnet_codex_v137_w6a_post_tp_probe_giveback_bp: str = "1.5,2.0,2.5"
    mainnet_codex_v137_w6a_fast_trail_enabled: bool = True
    mainnet_codex_v137_w6a_trail_arm_cap_bp: float = 3.5
    mainnet_codex_v137_w6a_trail_watch_interval_seconds: int = 1
    # Codex V1.4.2 W6A live-exit canary: preserve the v1.3.7E risk tree,
    # keep 8bp TP1, and default away from the extra fast-trail cap unless
    # explicitly re-enabled for a canary.
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
    mainnet_codex_v139b_wpr_entry_offset_bp: float = 0.5
    mainnet_codex_v139b_wpr_partial_tp_pct: float = 0.0011
    mainnet_codex_v139b_wpr_partial_exit_pct: float = 1.00
    mainnet_codex_v139b_wpr_max_sl_bp: float = 25.0
    mainnet_codex_v139b_wpr_scratch_mfe_bp: float = 3.0
    mainnet_codex_v139b_wpr_scratch_floor_bp: float = 0.5
    mainnet_codex_v139b_wpr_force_after_seconds: int = 240
    mainnet_codex_v139b_wpr_damage_loss_bp: float = 5.0
    # Codex V1.4.5 WPR exit hotfix: after a reviewed short-rebound WPR trade
    # has shown real MFE, lock/scratch the runner before the delayed damage
    # control path can turn the whole setup into a hard loss.
    mainnet_codex_v145_wpr_profit_lock_mfe_bp: float = 5.0
    mainnet_codex_v145_wpr_profit_lock_floor_bp: float = 2.0
    # Codex V1.4.11 WPR fee-aware profit lock: do not call a WPR exit
    # "profit lock" unless the remaining gross edge can survive taker fallback.
    mainnet_codex_v1411_wpr_profit_lock_fee_floor_enabled: bool = True
    mainnet_codex_v1411_wpr_profit_lock_min_floor_bp: float = 6.0
    # Codex V1.4.7 WPR discount_mixed scratch delay: v1.4.6 scratched one
    # discount_mixed runner after about 60s, then aggTrade touched TP shortly after.
    mainnet_codex_v147_wpr_discount_mixed_scratch_after_seconds: int = 120
    # Codex V1.4.6 STUP-S weak_chop exit hotfix: if a full-exit TP12 trade
    # shows real MFE but stalls/retraces for too long, capture positive maker
    # profit before generic damage control can turn it into a loss.
    mainnet_codex_v146_stups_profit_lock_enabled: bool = True
    mainnet_codex_v146_stups_profit_lock_after_seconds: int = 180
    mainnet_codex_v146_stups_profit_lock_force_after_seconds: int = 300
    mainnet_codex_v146_stups_profit_lock_mfe_bp: float = 8.0
    mainnet_codex_v146_stups_profit_lock_floor_bp: float = 3.0
    mainnet_codex_v146_stups_profit_lock_giveback_bp: float = 4.0
    # Codex V1.4.9 STUP-S fee-aware profit lock: do not close a correct
    # weak_chop direction at a gross profit that is likely negative after the
    # taker fallback fee/slippage. The live floor is max(profile floor, this
    # configured minimum, expected close fee + slippage + net buffer).
    mainnet_codex_v149_stups_profit_lock_fee_floor_enabled: bool = True
    mainnet_codex_v149_stups_profit_lock_min_floor_bp: float = 6.0
    # Codex V1.4.14 STUP-S mixed medium-profit harvest: mixed can show usable
    # MFE without reaching TP8, so capture fee-safe profit earlier than weak_chop.
    mainnet_codex_v1414_stups_mixed_profit_lock_after_seconds: int = 30
    mainnet_codex_v1414_stups_mixed_profit_lock_force_after_seconds: int = 90
    mainnet_codex_v1414_stups_mixed_profit_lock_mfe_bp: float = 6.0
    mainnet_codex_v1414_stups_mixed_profit_lock_floor_bp: float = 6.0
    mainnet_codex_v1414_stups_mixed_profit_lock_giveback_bp: float = 2.0
    mainnet_codex_v1414_stups_mixed_stall_lock_after_seconds: int = 45
    mainnet_codex_v1414_stups_mixed_stall_lock_floor_bp: float = 6.0
    # Codex V1.4.7 STUP-S stall harvest: v1.4.6 only captured near-TP
    # giveback. This catches medium-profit weak_chop trades that sit for too
    # long below TP12 but still have enough edge to survive a maker-first exit.
    mainnet_codex_v147_stups_stall_lock_enabled: bool = True
    mainnet_codex_v147_stups_stall_lock_after_seconds: int = 300
    mainnet_codex_v147_stups_stall_lock_floor_bp: float = 4.5
    mainnet_equity_cap_usdc: float = 400.0
    mainnet_initial_notional_usdc: float = 400.0
    mainnet_max_cumulative_notional_usdc: float = 1600.0
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
    mainnet_recovery_steps: int = 2
    mainnet_recovery_trigger_pct: float = 0.0009
    mainnet_recovery_tp_shrink: float = 1.0
    # After each DCA layer, widen the SL distance by this fraction per layer
    # (sl_pct × (1 + widen × dca_count)) so a freshly averaged position is not
    # immediately swept.  Mirrors backtest_wildcat_s1s5 (0.25/layer).
    mainnet_recovery_sl_widen_per_layer: float = 0.10
    # #25 (2026-06-10): a resting GTX DCA order can partially fill (e.g. 0.001 of
    # an intended 0.124).  The qty-grew detector must NOT treat that as a full
    # layer (widen SL / +1 layer / +full notional / pre-place next) — doing so
    # caused the 21-second double-layer cascade in cry3mn_1781089775237.  A fill
    # is only "a full layer" once filled_qty >= this fraction of the pre-placed
    # order's intended qty; below it we just sync qty tracking and wait.
    mainnet_recovery_trigger_steps_pct: str = "0.005,0.007"
    mainnet_codex_recovery_enabled: bool = False
    mainnet_codex_recovery_lane_codes: str = "CNL-WPR-L"
    mainnet_codex_recovery_max_basket_loss_usdc: float = 0.50
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
    # Hermes runner policy (2026-06-26): after TP1 fills, leave the remainder to
    # TRAIL only; do not rest a fixed TP3, and keep a protective hard SL at 25bp.
    mainnet_trail_require_partial_fill: bool = True
    mainnet_trail_disable_final_tp: bool = True
    mainnet_hard_sl_pct_override: float = 0.0025
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

    # Codex market-state throttle (V1.4.14).
    # Some lanes contain multiple market states with very different forward
    # behavior; after 2 net-loss exits in 1h, block only the configured state
    # for 60m instead of pausing the whole LONG side or whole lane.
    mainnet_codex_state_throttle_enabled: bool = True
    mainnet_codex_state_throttle_states: str = "CNL-WPR-L:falling_discount_trap,CNL-WPR-L:falling_continuation_probe,CNL-WPR-L:discount_mixed,CNL-WPR-L:discount_delayed_reclaim,STUP-S:clean_extension,STUP-S:mixed,SFD-S:strong_down_continuation"
    mainnet_codex_state_throttle_loss_count: int = 2
    mainnet_codex_state_throttle_window_seconds: float = 3600.0
    mainnet_codex_state_throttle_block_minutes: float = 60.0

    # Breakeven Stop Loss (BE-SL) configuration and lane overrides
    mainnet_codex_wpr_use_breakeven_sl: bool = False
    mainnet_codex_wpr_breakeven_offset_bp: float = 2.0
    mainnet_codex_stups_use_breakeven_sl: bool = True
    mainnet_codex_stups_breakeven_offset_bp: float = 1.0
    mainnet_codex_w6a_use_breakeven_sl: bool = False
    mainnet_codex_w6a_breakeven_offset_bp: float = 0.0
    mainnet_codex_s2st_use_breakeven_sl: bool = True
    mainnet_codex_s2st_breakeven_offset_bp: float = 0.0
    # V1.4.15: after TP1, try a very short reduce-only post-only exit before
    # falling back to the protective STOP_MARKET BE order. The STOP remains the
    # safety path; maker-first is only for fee avoidance when the book is still
    # at/through the BE floor.
    mainnet_codex_be_maker_first_enabled: bool = True
    mainnet_codex_be_maker_ttl_seconds: int = 2
    mainnet_codex_be_maker_adverse_break_bp: float = 1.0

    # V1.4.15: WPR falling_discount_trap strong-fall slice waits deeper instead
    # of direct-filling into the first falling-knife tick.
    mainnet_codex_v1415_wpr_strong_fall_deep_entry_enabled: bool = True
    mainnet_codex_v1415_wpr_strong_fall_entry_bp: float = 8.0
    mainnet_codex_v1415_wpr_strong_fall_d30_max_bp: float = -60.0
    mainnet_codex_v1415_wpr_strong_fall_rsi_max: float = 32.0
    mainnet_codex_v1415_wpr_strong_fall_vwap_max_bp: float = -12.0
    mainnet_codex_v1415_wpr_strong_fall_rng15_min_bp: float = 50.0

    mainnet_codex_stups_entry_offset_bp: float = 3.0
    mainnet_codex_stups_partial_tp_pct: float = 0.0011
    mainnet_codex_stups_partial_exit_pct: float = 0.70
    mainnet_codex_stups_max_sl_bp: float = 25.0


    mainnet_codex_w6a_entry_offset_bp: float = 1.5
    mainnet_codex_w6a_partial_exit_pct: float = 1.00
    mainnet_codex_w6a_max_sl_bp: float = 25.0

    # Codex V1.4.3 adaptive execution profiles (2026-06-28): route accepted
    # lanes by feature-only market state, then apply state-specific entry/TP/SL/BE/TTL.
    mainnet_codex_v143_adaptive_exec_enabled: bool = True
    mainnet_codex_v143_w6a_shadow_only_enabled: bool = True

    # Codex V1.4.58 adaptive live canary. Enforcement is deliberately limited
    # to the reviewed CNL-WPR deep no-lane promotion; STUP entry variants stay
    # shadow-only and never submit exchange orders.
    mainnet_codex_v1458_cnl_wpr_deep_gate_enabled: bool = True
    mainnet_codex_v1458_stup_fill_shadow_enabled: bool = True
    mainnet_codex_v1458_stup_fill_shadow_ttl_seconds: int = 90
    mainnet_codex_v1458_stup_fill_shadow_decision_latency_ms: int = 250
    mainnet_codex_v1458_stup_fill_shadow_max_pages: int = 10

    # v1.4.59 continuation evidence runtime. Observation is enabled by default;
    # child flags are dependency-validated before any identity probe or write.
    mainnet_v1459_observation_enabled: bool = True
    mainnet_v1459_observation_persist_session_enabled: bool = True
    mainnet_v1459_observation_record_opportunities_enabled: bool = True
    mainnet_v1459_observation_record_shadow_enabled: bool = True
    mainnet_v1459_observation_record_reconciliation_enabled: bool = True
    mainnet_v1459_account_fingerprint_marker_path: str = ".codex_identity/mainnet_v1459.marker"
    mainnet_v1459_deployment_commit: str = "e4fb23cb20310765dfed401e7acd068302b59b75"

    # v1.4.59 adaptive profile runtime. Candidate selection is shadow-only and
    # is deliberately separate from live enforcement. A child profile set to
    # true must never bypass either the master selector or enforcement flag.
    mainnet_codex_v1459_candidate_selector_enabled: bool = True
    mainnet_codex_v1459_live_enforcement_enabled: bool = False
    mainnet_codex_v1459_runner_enabled: bool = False
    mainnet_codex_v1459_early_fail_enabled: bool = False
    mainnet_codex_v1459_one_step_reprice_enabled: bool = False
    mainnet_codex_v1459_regime_switch_enabled: bool = False
    mainnet_codex_v1459_regime_confirmations: int = 2
    mainnet_codex_v1459_regime_min_dwell_seconds: int = 15
    mainnet_codex_v1459_regime_stale_after_seconds: int = 90
    mainnet_codex_v1459_regime_max_notional_usdc: float = 50.0
    mainnet_codex_v1459_trend_size_mult: float = 1.0
    mainnet_codex_v1459_trend_entry_offset_bp: float = 2.0
    mainnet_codex_v1459_trend_tp1_bp: float = 6.0
    mainnet_codex_v1459_trend_full_tp_bp: float = 16.0
    mainnet_codex_v1459_trend_partial_exit_pct: float = 0.70
    mainnet_codex_v1459_trend_sl_bp: float = 10.0
    mainnet_codex_v1459_trend_be_bp: float = 2.0
    mainnet_codex_v1459_trend_entry_ttl_seconds: int = 60
    mainnet_codex_v1459_trend_hold_seconds: int = 720
    mainnet_codex_v1459_range_size_mult: float = 0.75
    mainnet_codex_v1459_range_entry_offset_bp: float = 1.0
    mainnet_codex_v1459_range_tp1_bp: float = 5.0
    mainnet_codex_v1459_range_full_tp_bp: float = 8.0
    mainnet_codex_v1459_range_partial_exit_pct: float = 1.0
    mainnet_codex_v1459_range_sl_bp: float = 8.0
    mainnet_codex_v1459_range_be_bp: float = 2.0
    mainnet_codex_v1459_range_entry_ttl_seconds: int = 90
    mainnet_codex_v1459_range_hold_seconds: int = 360

    # v1.4.60 lane/state risk-first overlay.  Every switch defaults OFF so a
    # code update is behaviorally identical to v1.4.59 until a reviewed canary
    # configuration explicitly enables candidate observation and enforcement.
    # The overlay may block or reduce an incumbent-approved order; it must
    # never create an order, loosen entry/TTL, or enable a runner.
    mainnet_codex_v1460_candidate_selector_enabled: bool = False
    mainnet_codex_v1460_lane_matrix_enabled: bool = False
    mainnet_codex_v1460_live_enforcement_enabled: bool = False
    mainnet_codex_v1460_shadow_evidence_enabled: bool = False
    mainnet_codex_v1460_runner_enabled: bool = False
    mainnet_codex_v1460_one_step_reprice_enabled: bool = False
    mainnet_codex_v1460_max_notional_usdc: float = 50.0
    mainnet_codex_v1460_probation_notional_usdc: float = 25.0
    mainnet_codex_v1460_weak_min_evaluable_opportunities: int = 8
    mainnet_codex_v1460_weak_min_tp_first: int = 6
    mainnet_codex_v1460_weak_min_ev_per_opportunity_usdc: float = 0.0
    mainnet_codex_v1460_weak_shadow_maker_fee_rate: float = 0.0
    mainnet_codex_v1460_weak_shadow_taker_fee_rate: float = 0.0004
    mainnet_codex_v1460_weak_shadow_max_pages: int = 10
    # Bound public aggTrade work inside one 10-second scheduler cycle.  The
    # durable cursor continues on later cycles, so this limits latency without
    # discarding evidence.
    mainnet_codex_v1464_shadow_aggtrade_pages_per_cycle: int = 1
    mainnet_codex_v1460_weak_shadow_page_limit: int = 1000
    mainnet_codex_v1460_weak_shadow_max_fetch_failures: int = 3
    mainnet_codex_v1460_lane_consecutive_loss_limit: int = 2
    mainnet_codex_v1460_lane_net_loss_cap_usdc: float = 0.12
    mainnet_codex_v1460_session_net_loss_cap_usdc: float = 0.30
    mainnet_codex_v1460_target_paid_closed_fills: int = 20
    mainnet_codex_v1460_max_duration_seconds: int = 72 * 60 * 60
    mainnet_codex_v1460_checkpoint_fills: int = 5

    # v1.4.61 bidirectional regime gate.  All behavior-changing switches stay
    # OFF by default: deploying code alone must preserve v1.4.60B behavior.
    # When reviewed enforcement is enabled, supportive regimes may spend one
    # half-risk probe token for a classified incumbent reject, while adverse
    # regimes may shadow-block an incumbent acceptance.  Safety/integrity
    # rejects are never eligible for promotion.
    mainnet_codex_v1461_candidate_selector_enabled: bool = False
    mainnet_codex_v1461_regime_gate_enabled: bool = False
    mainnet_codex_v1461_live_enforcement_enabled: bool = False
    mainnet_codex_v1461_shadow_all_strategy_rejects_enabled: bool = False
    mainnet_codex_v1461_max_notional_usdc: float = 50.0
    mainnet_codex_v1461_probation_notional_usdc: float = 25.0
    mainnet_codex_v1461_fast_min_evaluable_opportunities: int = 4
    mainnet_codex_v1461_fast_min_tp_first: int = 3
    mainnet_codex_v1461_probation_min_evaluable_opportunities: int = 6
    mainnet_codex_v1461_probation_min_tp_first: int = 4
    mainnet_codex_v1461_min_ev_per_opportunity_usdc: float = 0.0
    mainnet_codex_v1461_evidence_max_age_seconds: int = 6 * 60 * 60
    mainnet_codex_v1461_regime_confirmations: int = 2
    mainnet_codex_v1461_episode_exit_confirmation_seconds: int = 5 * 60
    mainnet_codex_v1461_lane_consecutive_loss_limit: int = 2
    mainnet_codex_v1461_lane_net_loss_cap_usdc: float = 0.12
    mainnet_codex_v1461_session_net_loss_cap_usdc: float = 0.30
    mainnet_codex_v1461_target_paid_closed_fills: int = 20
    mainnet_codex_v1461_max_duration_seconds: int = 72 * 60 * 60
    mainnet_codex_v1461_checkpoint_fills: int = 5
    mainnet_codex_v1461_runner_enabled: bool = False
    mainnet_codex_v1461_one_step_reprice_enabled: bool = False

    # v1.4.63 live admission boundary.  Strict mode is deny-by-default: only
    # registry entries explicitly marked LIVE may reach order placement, while
    # every other classified opportunity is retained as shadow evidence.
    # Promotion enforcement remains a separately reviewed, default-OFF switch.
    mainnet_codex_v1462_strict_live_allowlist_enabled: bool = False
    mainnet_codex_v1462_shadow_all_enabled: bool = False
    mainnet_codex_v1462_promotion_enforcement_enabled: bool = False

    # v1.4.64 automatic Adaptive authority is intentionally separate from the
    # retired v1.4.62 promotion switch above.  It may grant only a short,
    # exact-cohort, regime-matched lease over an incumbent-accepted SHADOW
    # route; it can never reopen an upstream reject or enlarge a ticket.
    mainnet_codex_v1464_auto_promotion_enabled: bool = False
    mainnet_codex_v1464_activation_cutoff_ms: int = 0
    mainnet_codex_v1464_evidence_window_seconds: int = 90 * 60
    mainnet_codex_v1464_evidence_max_age_seconds: int = 90 * 60
    mainnet_codex_v1464_lease_ttl_seconds: int = 15 * 60
    mainnet_codex_v1464_cooldown_seconds: int = 15 * 60
    mainnet_codex_v1464_probation_min_evaluable: int = 4
    mainnet_codex_v1464_probation_min_tp_first: int = 3
    mainnet_codex_v1464_live_min_evaluable: int = 6
    mainnet_codex_v1464_live_min_tp_first: int = 4
    mainnet_codex_v1464_live_min_paid_complete: int = 3
    mainnet_codex_v1464_live_min_paid_wins: int = 2
    mainnet_codex_v1464_retain_min_evaluable: int = 4
    mainnet_codex_v1464_retain_min_tp_first: int = 3
    mainnet_codex_v1464_soft_breach_limit: int = 2
    mainnet_codex_v1464_regime_confirmations: int = 2
    # Promotion must match the market that exists at submit time.  Two
    # consecutive observations must belong to the same exact cohort, fit
    # inside the confirmation window, and the newest one must still be fresh.
    mainnet_codex_v1464_regime_max_age_seconds: int = 60
    mainnet_codex_v1464_regime_confirmation_window_seconds: int = 45
    mainnet_codex_v1464_submit_max_age_seconds: int = 10
    # Shadow terminal labels are produced by the five-minute evaluator.  Rows
    # arriving later than this bound remain diagnostic and cannot authorize.
    mainnet_codex_v1464_max_terminal_latency_seconds: int = 6 * 60
    mainnet_codex_v1464_probation_notional_usdc: float = 25.0
    mainnet_codex_v1464_live_notional_usdc: float = 50.0
    mainnet_codex_v1464_consecutive_paid_loss_limit: int = 2
    mainnet_codex_v1464_lane_net_loss_cap_usdc: float = 0.12
    mainnet_codex_v1464_cohort_net_loss_cap_usdc: float = 0.30

    # v1.4.65 keeps the existing v1.4.60 control lanes live and evaluates
    # three paired W6A execution profiles from the same aggTrade envelope.
    # Collection and winner materialization are enabled by default; paid W6A
    # enforcement remains an explicit rollout switch.
    mainnet_codex_v1465_w6a_profile_shadow_enabled: bool = True
    mainnet_codex_v1465_w6a_profile_selector_enabled: bool = True
    mainnet_codex_v1465_w6a_profile_enforcement_enabled: bool = False
    mainnet_codex_v1465_w6a_profile_lease_ttl_seconds: int = 10 * 60
    mainnet_codex_v1465_w6a_profile_notional_cap_usdc: float = 25.0
    # v1.4.69 builds a match-all adaptive arm ledger.  Every switch defaults
    # closed so a code-only release cannot change paid admission or routing.
    # Observation records compatible lanes; paired shadow evaluates the same
    # immutable tick envelope under a small closed profile menu; the arbiter
    # may materialize read-only lease proposals.  Only live_enforcement may
    # affect paid admission, and it has additional startup invariants below.
    mainnet_codex_v1469_observation_enabled: bool = False
    mainnet_codex_v1469_observation_bucket_seconds: int = 2 * 60
    mainnet_codex_v1469_paired_shadow_enabled: bool = False
    mainnet_codex_v1469_arbiter_enabled: bool = False
    mainnet_codex_v1469_live_enforcement_enabled: bool = False
    mainnet_codex_v1469_safety_window_seconds: int = 15 * 60
    mainnet_codex_v1469_authority_window_seconds: int = 45 * 60
    mainnet_codex_v1469_guard_window_seconds: int = 180 * 60
    mainnet_codex_v1469_probation_min_evaluable: int = 4
    mainnet_codex_v1469_probation_min_tp_first: int = 3
    mainnet_codex_v1469_guard_min_evaluable: int = 6
    mainnet_codex_v1469_challenger_margin_bp: float = 2.0
    mainnet_codex_v1469_challenger_min_paired_wins: int = 3
    mainnet_codex_v1469_regime_confirmations: int = 2
    mainnet_codex_v1469_regime_min_dwell_seconds: int = 15
    mainnet_codex_v1469_regime_max_age_seconds: int = 60
    mainnet_codex_v1469_submit_max_age_seconds: int = 10
    mainnet_codex_v1469_probation_lease_seconds: int = 5 * 60
    mainnet_codex_v1469_live_lease_seconds: int = 10 * 60
    mainnet_codex_v1469_probation_notional_usdc: float = 25.0
    mainnet_codex_v1469_live_notional_usdc: float = 50.0
    mainnet_codex_v1469_global_open_notional_usdc: float = 50.0
    mainnet_codex_v1469_lane_open_notional_usdc: float = 50.0
    mainnet_codex_v1469_per_trade_loss_cap_usdc: float = 0.15
    mainnet_codex_v1469_daily_soft_loss_usdc: float = 0.15
    mainnet_codex_v1469_daily_hard_loss_usdc: float = 0.30
    mainnet_codex_v1469_daily_profit_lock_trigger_usdc: float = 0.15
    mainnet_codex_v1469_daily_profit_lock_giveback_usdc: float = 0.15
    mainnet_codex_v1469_roundtrip_fee_bp: float = 4.0
    mainnet_codex_v1469_slippage_bp: float = 1.0
    # v1.4.67 operational guardrails.  Durable evidence repair is useful but
    # must never consume the 10-second order-management scheduler.  Repair is
    # performed in a single, rate-limited background task while idle.
    mainnet_maintenance_reconcile_interval_seconds: int = 60
    mainnet_maintenance_slow_warning_seconds: float = 5.0
    # Observe storage only: this guard never VACUUMs, deletes, pauses, or
    # changes promotion authority.  It warns Telegram before the evidence DB
    # can exhaust the VM filesystem.
    mainnet_db_capacity_guard_enabled: bool = True
    mainnet_db_capacity_check_interval_seconds: int = 5 * 60
    mainnet_db_capacity_warn_free_mb: int = 1536
    mainnet_db_capacity_critical_free_mb: int = 768
    mainnet_db_capacity_alert_cooldown_seconds: int = 6 * 60 * 60
    mainnet_codex_v142_profile_shadow_enabled: bool = True
    mainnet_codex_v142_no_fill_watch_hours_tpe: str = "05,08,10,13,14,15"

    mainnet_s2_entry_offset_bp: float = 1.0
    mainnet_s2_partial_tp_pct: float = 0.0010
    mainnet_s2_partial_exit_pct: float = 0.40
    mainnet_s2_max_sl_bp: float = 15.0

    @property
    def mainnet_codex_runtime_selected(self) -> bool:
        """Return whether a paid mainnet one-run can use a Codex policy."""

        label = str(self.mainnet_strategy_label or "").strip().lower()
        return bool(
            self.mainnet_codex_v1_enabled
            or label.startswith(("codex_v1", "_codex_v1"))
        )

    def assert_mainnet_v1463_runtime_safety(self) -> None:
        """Fail startup closed when the v1.4.64 live boundary is misconfigured.

        This invariant is intentionally scoped to a paid mainnet Codex one-run.
        Testnet, monitoring-only, and non-Codex processes keep their existing
        startup behavior.
        """

        if not self.mainnet_one_run_enabled or not self.mainnet_codex_runtime_selected:
            return
        strict = bool(self.mainnet_codex_v1462_strict_live_allowlist_enabled)
        shadow = bool(self.mainnet_codex_v1462_shadow_all_enabled)
        promotion = bool(self.mainnet_codex_v1462_promotion_enforcement_enabled)
        adaptive = bool(self.mainnet_codex_v1464_auto_promotion_enabled)
        v1465_profile_shadow = bool(
            self.mainnet_codex_v1465_w6a_profile_shadow_enabled
        )
        v1465_selector = bool(
            self.mainnet_codex_v1465_w6a_profile_selector_enabled
        )
        v1465_enforcement = bool(
            self.mainnet_codex_v1465_w6a_profile_enforcement_enabled
        )
        v1469_observation = bool(
            self.mainnet_codex_v1469_observation_enabled
        )
        v1469_paired_shadow = bool(
            self.mainnet_codex_v1469_paired_shadow_enabled
        )
        v1469_arbiter = bool(self.mainnet_codex_v1469_arbiter_enabled)
        v1469_enforcement = bool(
            self.mainnet_codex_v1469_live_enforcement_enabled
        )
        errors: list[str] = []
        if not strict:
            errors.append("strict_live_allowlist=true")
        if not shadow:
            errors.append("shadow_all=true")
        if promotion:
            errors.append("legacy_promotion_enforcement=false")
        if v1465_enforcement:
            if not v1465_profile_shadow:
                errors.append("v1465_w6a_profile_shadow=true")
            if not v1465_selector:
                errors.append("v1465_w6a_profile_selector=true")
            if not adaptive:
                errors.append("v1464_auto_promotion=true")
            try:
                v1465_lease_ttl = int(
                    self.mainnet_codex_v1465_w6a_profile_lease_ttl_seconds
                )
            except (TypeError, ValueError, OverflowError):
                v1465_lease_ttl = 0
            if v1465_lease_ttl != 10 * 60:
                errors.append("v1465_w6a_profile_lease_ttl_seconds=600")
            try:
                v1465_cap = float(
                    self.mainnet_codex_v1465_w6a_profile_notional_cap_usdc
                )
            except (TypeError, ValueError, OverflowError):
                v1465_cap = 0.0
            if not 0.0 < v1465_cap <= 25.0:
                errors.append("v1465_w6a_profile_notional_cap_usdc in (0,25]")
        if v1469_paired_shadow and not v1469_observation:
            errors.append("v1469_observation=true when paired_shadow=true")
        if v1469_arbiter and not v1469_paired_shadow:
            errors.append("v1469_paired_shadow=true when arbiter=true")
        if v1469_enforcement:
            # v1.4.69 currently has a shadow evaluator and a read-only
            # authority proposal path only.  Until the paid-order adapter
            # consumes an atomic opportunity claim, accepting this flag
            # would create a false safety contract at startup.
            errors.append(
                "v1469_live_enforcement=false until paid claim adapter is available"
            )
            if not v1469_arbiter:
                errors.append("v1469_arbiter=true when live_enforcement=true")
            if adaptive:
                errors.append("v1464_auto_promotion=false under v1469 authority")
            if v1465_enforcement:
                errors.append("v1465_w6a_profile_enforcement=false")
            if bool(self.mainnet_codex_recovery_enabled):
                errors.append("mainnet_codex_recovery_enabled=false")
            for name in (
                "mainnet_codex_v1459_runner_enabled",
                "mainnet_codex_v1459_one_step_reprice_enabled",
                "mainnet_codex_v1460_runner_enabled",
                "mainnet_codex_v1460_one_step_reprice_enabled",
                "mainnet_codex_v1461_runner_enabled",
                "mainnet_codex_v1461_one_step_reprice_enabled",
            ):
                if bool(getattr(self, name, False)):
                    errors.append(f"{name}=false")

            v1469_integer_contract = {
                "mainnet_codex_v1469_observation_bucket_seconds": 30,
                "mainnet_codex_v1469_safety_window_seconds": 1,
                "mainnet_codex_v1469_authority_window_seconds": 1,
                "mainnet_codex_v1469_guard_window_seconds": 1,
                "mainnet_codex_v1469_probation_min_evaluable": 4,
                "mainnet_codex_v1469_probation_min_tp_first": 3,
                "mainnet_codex_v1469_guard_min_evaluable": 6,
                "mainnet_codex_v1469_challenger_min_paired_wins": 3,
                "mainnet_codex_v1469_regime_confirmations": 2,
                "mainnet_codex_v1469_regime_min_dwell_seconds": 1,
                "mainnet_codex_v1469_regime_max_age_seconds": 1,
                "mainnet_codex_v1469_submit_max_age_seconds": 1,
                "mainnet_codex_v1469_probation_lease_seconds": 1,
                "mainnet_codex_v1469_live_lease_seconds": 1,
            }
            v1469_integers: dict[str, int] = {}
            for name, minimum in v1469_integer_contract.items():
                try:
                    value = int(getattr(self, name))
                except (TypeError, ValueError, OverflowError):
                    value = 0
                v1469_integers[name] = value
                if value < minimum:
                    errors.append(f"{name}>={minimum}")
            if not (
                v1469_integers["mainnet_codex_v1469_safety_window_seconds"]
                < v1469_integers[
                    "mainnet_codex_v1469_authority_window_seconds"
                ]
                < v1469_integers["mainnet_codex_v1469_guard_window_seconds"]
            ):
                errors.append("v1469 windows must satisfy safety<authority<guard")
            if (
                v1469_integers[
                    "mainnet_codex_v1469_probation_min_tp_first"
                ]
                > v1469_integers[
                    "mainnet_codex_v1469_probation_min_evaluable"
                ]
            ):
                errors.append("v1469 probation tp_first<=evaluable")
            if (
                v1469_integers["mainnet_codex_v1469_submit_max_age_seconds"]
                > v1469_integers["mainnet_codex_v1469_regime_max_age_seconds"]
            ):
                errors.append("v1469 submit_max_age<=regime_max_age")
            if (
                v1469_integers[
                    "mainnet_codex_v1469_regime_min_dwell_seconds"
                ]
                >= v1469_integers[
                    "mainnet_codex_v1469_probation_lease_seconds"
                ]
            ):
                errors.append("v1469 regime_min_dwell<probation_lease")
            if (
                v1469_integers[
                    "mainnet_codex_v1469_probation_lease_seconds"
                ]
                > v1469_integers["mainnet_codex_v1469_live_lease_seconds"]
            ):
                errors.append("v1469 probation_lease<=live_lease")

            try:
                v1469_margin_bp = float(
                    self.mainnet_codex_v1469_challenger_margin_bp
                )
                probation_cap = float(
                    self.mainnet_codex_v1469_probation_notional_usdc
                )
                live_cap = float(
                    self.mainnet_codex_v1469_live_notional_usdc
                )
                global_cap = float(
                    self.mainnet_codex_v1469_global_open_notional_usdc
                )
                lane_cap = float(
                    self.mainnet_codex_v1469_lane_open_notional_usdc
                )
                trade_loss_cap = float(
                    self.mainnet_codex_v1469_per_trade_loss_cap_usdc
                )
                soft_loss = float(
                    self.mainnet_codex_v1469_daily_soft_loss_usdc
                )
                hard_loss = float(
                    self.mainnet_codex_v1469_daily_hard_loss_usdc
                )
                lock_trigger = float(
                    self.mainnet_codex_v1469_daily_profit_lock_trigger_usdc
                )
                lock_giveback = float(
                    self.mainnet_codex_v1469_daily_profit_lock_giveback_usdc
                )
                fee_bp = float(self.mainnet_codex_v1469_roundtrip_fee_bp)
                slippage_bp = float(self.mainnet_codex_v1469_slippage_bp)
            except (TypeError, ValueError, OverflowError):
                (
                    v1469_margin_bp,
                    probation_cap,
                    live_cap,
                    global_cap,
                    lane_cap,
                    trade_loss_cap,
                    soft_loss,
                    hard_loss,
                    lock_trigger,
                    lock_giveback,
                    fee_bp,
                    slippage_bp,
                ) = (0.0,) * 12
            if v1469_margin_bp < 0.0:
                errors.append("v1469_challenger_margin_bp>=0")
            if not 0.0 < probation_cap <= 25.0:
                errors.append("v1469_probation_notional_usdc in (0,25]")
            if not probation_cap <= live_cap <= 50.0:
                errors.append("v1469_live_notional_usdc in [probation,50]")
            if not 0.0 < global_cap <= 50.0:
                errors.append("v1469_global_open_notional_usdc in (0,50]")
            if not 0.0 < lane_cap <= global_cap:
                errors.append("v1469_lane_open_notional_usdc in (0,global]")
            if not 0.0 < trade_loss_cap <= hard_loss:
                errors.append("v1469_per_trade_loss_cap_usdc in (0,hard_loss]")
            if not 0.0 < soft_loss < hard_loss <= 0.30:
                errors.append("v1469 daily loss must satisfy 0<soft<hard<=0.30")
            if (
                lock_trigger <= 0.0
                or lock_giveback <= 0.0
            ):
                errors.append("v1469 invalid daily profit-lock bounds")
            if fee_bp < 0.0 or slippage_bp < 0.0:
                errors.append("v1469 fee/slippage bp must be non-negative")
        if adaptive:
            if bool(self.mainnet_codex_recovery_enabled):
                errors.append("mainnet_codex_recovery_enabled=false")
            for name in (
                "mainnet_codex_v1459_runner_enabled",
                "mainnet_codex_v1459_one_step_reprice_enabled",
                "mainnet_codex_v1460_runner_enabled",
                "mainnet_codex_v1460_one_step_reprice_enabled",
                "mainnet_codex_v1461_runner_enabled",
                "mainnet_codex_v1461_one_step_reprice_enabled",
            ):
                if bool(getattr(self, name, False)):
                    errors.append(f"{name}=false")
            integer_contract = {
                "mainnet_codex_v1464_evidence_window_seconds": 1,
                "mainnet_codex_v1464_evidence_max_age_seconds": 1,
                "mainnet_codex_v1464_lease_ttl_seconds": 1,
                "mainnet_codex_v1464_cooldown_seconds": 1,
                "mainnet_codex_v1464_probation_min_evaluable": 4,
                "mainnet_codex_v1464_probation_min_tp_first": 3,
                "mainnet_codex_v1464_live_min_evaluable": 6,
                "mainnet_codex_v1464_live_min_tp_first": 4,
                "mainnet_codex_v1464_live_min_paid_complete": 3,
                "mainnet_codex_v1464_live_min_paid_wins": 2,
                "mainnet_codex_v1464_retain_min_evaluable": 4,
                "mainnet_codex_v1464_retain_min_tp_first": 3,
                "mainnet_codex_v1464_soft_breach_limit": 2,
                "mainnet_codex_v1464_regime_confirmations": 2,
                "mainnet_codex_v1464_regime_max_age_seconds": 1,
                "mainnet_codex_v1464_regime_confirmation_window_seconds": 1,
                "mainnet_codex_v1464_submit_max_age_seconds": 1,
                "mainnet_codex_v1464_max_terminal_latency_seconds": 1,
                "mainnet_codex_v1464_shadow_aggtrade_pages_per_cycle": 1,
            }
            integer_values: dict[str, int] = {}
            for name, minimum in integer_contract.items():
                try:
                    value = int(getattr(self, name))
                except (TypeError, ValueError, OverflowError):
                    value = 0
                integer_values[name] = value
                if value < minimum:
                    errors.append(f"{name}>={minimum}")
            try:
                activation_cutoff_ms = int(
                    self.mainnet_codex_v1464_activation_cutoff_ms
                )
            except (TypeError, ValueError, OverflowError):
                activation_cutoff_ms = -1
            if activation_cutoff_ms <= 0:
                errors.append("v1464_activation_cutoff_ms>0")
            threshold_pairs = (
                (
                    "mainnet_codex_v1464_probation_min_tp_first",
                    "mainnet_codex_v1464_probation_min_evaluable",
                ),
                (
                    "mainnet_codex_v1464_live_min_tp_first",
                    "mainnet_codex_v1464_live_min_evaluable",
                ),
                (
                    "mainnet_codex_v1464_live_min_paid_wins",
                    "mainnet_codex_v1464_live_min_paid_complete",
                ),
                (
                    "mainnet_codex_v1464_retain_min_tp_first",
                    "mainnet_codex_v1464_retain_min_evaluable",
                ),
            )
            for numerator_name, denominator_name in threshold_pairs:
                if integer_values[numerator_name] > integer_values[denominator_name]:
                    errors.append(f"{numerator_name}<={denominator_name}")
            if (
                integer_values["mainnet_codex_v1464_evidence_max_age_seconds"]
                > integer_values["mainnet_codex_v1464_evidence_window_seconds"]
            ):
                errors.append("v1464_evidence_max_age<=window")
            if (
                integer_values["mainnet_codex_v1464_lease_ttl_seconds"]
                > integer_values["mainnet_codex_v1464_evidence_max_age_seconds"]
            ):
                errors.append("v1464_lease_ttl<=evidence_max_age")
            if (
                integer_values["mainnet_codex_v1464_regime_max_age_seconds"]
                >= integer_values["mainnet_codex_v1464_lease_ttl_seconds"]
            ):
                errors.append("v1464_regime_max_age<lease_ttl")
            if (
                integer_values[
                    "mainnet_codex_v1464_regime_confirmation_window_seconds"
                ]
                > integer_values["mainnet_codex_v1464_regime_max_age_seconds"]
            ):
                errors.append("v1464_regime_confirmation_window<=regime_max_age")
            if (
                integer_values["mainnet_codex_v1464_submit_max_age_seconds"]
                > integer_values["mainnet_codex_v1464_regime_max_age_seconds"]
            ):
                errors.append("v1464_submit_max_age<=regime_max_age")
            if (
                integer_values["mainnet_codex_v1464_max_terminal_latency_seconds"]
                > integer_values["mainnet_codex_v1464_evidence_max_age_seconds"]
            ):
                errors.append("v1464_terminal_latency<=evidence_max_age")
            try:
                probation_cap = float(
                    self.mainnet_codex_v1464_probation_notional_usdc
                )
                live_cap = float(self.mainnet_codex_v1464_live_notional_usdc)
            except (TypeError, ValueError, OverflowError):
                probation_cap = live_cap = 0.0
            if not 0.0 < probation_cap <= 25.0:
                errors.append("v1464_probation_notional_usdc in (0,25]")
            if not probation_cap <= live_cap <= 50.0:
                errors.append("v1464_live_notional_usdc in [probation,50]")
            for name, upper in (
                ("mainnet_codex_v1464_lane_net_loss_cap_usdc", 0.12),
                ("mainnet_codex_v1464_cohort_net_loss_cap_usdc", 0.30),
            ):
                try:
                    value = float(getattr(self, name))
                except (TypeError, ValueError, OverflowError):
                    value = 0.0
                if not 0.0 < value <= upper:
                    errors.append(f"{name} in (0,{upper}]")
        if not errors:
            return
        raise RuntimeError(
            "unsafe v1.4.64 mainnet Codex configuration: "
            + ", ".join(errors)
            + " "
            + (
                f"(strict={strict}, shadow={shadow}, "
                f"legacy_promotion={promotion}, adaptive={adaptive})"
            )
        )

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
