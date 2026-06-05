# Wildcat Handoff

Last updated: 2026-06-05

## Current Goal

Optimize `wildcat` for ETHUSDC with:

- Each 7-day backtest day `>= 20 USDC`
- 7-day average `> 30 USDC/day`
- Max drawdown `< 20 USDC`
- Per setup notional remains `1000 USDC`
- Leverage reporting includes `75x / 100x`

User later allowed approximate convergence around 10%, but the active strict goal is still not fully proven.

## Completed

- Added wildcat research script: `scripts/backtest_wildcat_s1s5.py`
- Added `--align-taipei-days` so 7-day validation can run on complete Taiwan calendar days instead of partial boundary days
- Added `--focused-preset wildcat_converged_v1` for local DD/weak-day refinement sweeps
- Added S1/S5-heavy wildcat model with:
  - daily catch-up mode
  - late-day rescue mode
  - DCA-style recovery steps
  - partial take-profit
  - duplicate setup layers
  - daily profit target / floor lock / giveback controls
  - strict and near-10pct objective reporting
- Added fixed preset:
  - `--preset wildcat_converged_v1`
- Added supporting manual/S1-S5 analysis:
  - `scripts/analyze_s1s5_manual_follow.py`
  - `reports/s1s5_manual_follow_cumulative.json`
- Updated Telegram/live-side diagnostics:
  - S1~S5 status list
  - score/level display
  - signal-only safe path
  - manual signal button/audit binding

## Important Reports

- `reports/wildcat_s1s5_7d.json`
  - Latest search result.
- `reports/wildcat_converged_v1_7d.json`
  - Fixed preset replay.
- `reports/wildcat_s1s5_7d_fee_sensitivity.json`
  - Maker TP / taker SL sensitivity from the earlier daily-20 version.
- `reports/wildcat_s1s5_7d_all_variants.json`
  - Earlier full variant artifact.

## Best Current Preset

Command:

```powershell
python scripts\backtest_wildcat_s1s5.py --preset wildcat_converged_v1 --days 7 --target-daily-usdc 20 --json-output reports\wildcat_converged_v1_7d.json
```

Latest replay result:

- PnL: `+274.0450 USDC`
- Avg: `+39.1493 USDC/day`
- Daily target hits: `7/8`
- Weak day: `2026-05-30 +19.0769 USDC`
- MaxDD: `23.4663 USDC`
- WR: `89.04%`
- PF: `2.0621`

Interpretation:

- Avg target is met.
- Daily target is short by one day, only `0.9231 USDC`.
- MaxDD is above strict `<20`; also above the near `22` line.
- This is not strict-complete.

## Latest Aligned 7-Day Baseline

Command:

```powershell
python scripts\backtest_wildcat_s1s5.py --preset wildcat_converged_v1 --align-taipei-days --days 7 --target-daily-usdc 20 --json-output reports\wildcat_converged_v1_7d_aligned.json
```

Latest replay result on the last 7 complete Taiwan days:

- PnL: `+231.4232 USDC`
- Avg: `+33.0605 USDC/day`
- Daily target hits: `6/7`
- Weak day: `2026-05-30 +19.0769 USDC`
- MaxDD: `22.4889 USDC`

Interpretation:

- Aligning to complete days removed the partial-day distortion.
- `wildcat_converged_v1` still remains the best local candidate among the focused refinement set.
- The current gap is now clearer: roughly `+0.9231 USDC` on the weak day and `2.4889 USDC` on MaxDD.

## Latest Aligned 30-Day Baseline

Command:

```powershell
python scripts\backtest_wildcat_s1s5.py --preset wildcat_converged_v1 --align-taipei-days --days 30 --target-daily-usdc 20 --json-output reports\wildcat_converged_v1_30d_aligned.json
```

Latest replay result on the last 30 complete Taiwan days:

- PnL: `+800.3099 USDC`
- Avg: `+26.6770 USDC/day`
- Daily target hits: `21/30`
- Worst day: `2026-05-16 -7.6721 USDC`
- MaxDD: `54.3828 USDC`

Interpretation:

- The current preset is profitable over 30 days but not robust enough.
- The main instability is not average return; it is drawdown and weak-day clustering.
- Weak days cluster around `2026-05-12` to `2026-05-18`, plus `2026-05-23`, `2026-05-25`, `2026-05-26`, and `2026-05-30`.

## 30-Day Weak-Day Notes

From full trade dump analysis:

- `S1_BB_RSI` is still the main engine and also the main weak-day drag.
- On weak days, the recurring failure mode is many `MAX_HOLD_LOSS` exits from `S1_BB_RSI`.
- `S5_Stoch` is not the main weak-day cause, but over the full 30-day window it is slightly negative overall in the current preset.
- Fully removing `S5_Stoch` did not improve the profile enough; it helps on some weak days.

## Best 30-Day Candidates So Far

Two useful tradeoff candidates emerged:

1. Yield-forward 30-day candidate

- Label: `hold20_floor24_s5med`
- Construction:
  - `max_holding_bars=20`
  - `cooldown_bars=4`
  - `daily_profit_target_usdc=38`
  - `daily_floor_lock_usdc=24`
  - `daily_giveback_usdc=4`
  - `S5` slightly tighter: `long_d_max=32`, `short_d_min=68`, `range_edge_atr_margin=0.20`
- Result:
  - Avg: `26.7748`
  - MaxDD: `46.4701`
  - Hits: `20/30`
  - Worst day: `-9.0148`

2. Risk-compressed 30-day candidate

- Label: `dup1_target40`
- Construction:
  - no duplicate same-side layers
  - `max_holding_bars=20`
  - `cooldown_bars=4`
  - `daily_profit_target_usdc=40`
  - `daily_floor_lock_usdc=24`
  - `daily_giveback_usdc=4`
- Result:
  - Avg: `20.3340`
  - MaxDD: `33.0373`
  - Hits: `19/30`
  - Worst day: `-21.9620`

Interpretation:

- `hold20_floor24_s5med` is the best "still earns well" candidate.
- `dup1_target40` is the best "compress DD hard" candidate found so far, but avg falls close to the floor.

## Current 30-Day Balanced Preset

New preset:

- `wildcat_30d_balanced_v1`

Command:

```powershell
python scripts\backtest_wildcat_s1s5.py --preset wildcat_30d_balanced_v1 --align-taipei-days --days 30 --target-daily-usdc 20 --json-output reports\wildcat_30d_balanced_v1_30d_aligned.json
```

Result:

- Avg: `21.0515 USDC/day`
- MaxDD: `27.2349 USDC`
- Total PnL: `+631.5448 USDC`
- Hit days `>=20`: `16/30`

Construction summary:

- `max_holding_bars=20`
- `cooldown_bars=5`
- no duplicate same-side layers
- `partial_exit_pct=0.40`
- `partial_tp_pct=0.0005`
- `daily_profit_target_usdc=40`
- `daily_floor_lock_usdc=24`
- `daily_giveback_usdc=4`
- tighter S5 range:
  - `s5_long_d_max=31`
  - `s5_short_d_min=69`
  - `range_edge_atr_margin=0.20`

Interpretation:

- This preset satisfies the relaxed 30-day goals of:
  - avg daily pnl `>= 20 USDC`
  - MaxDD `<= 30 USDC`
- It does **not** satisfy the earlier stricter daily-hit expectations.

## Rolling Validation Status

Rolling report:

- [reports/wildcat_30d_balanced_v1_rolling.json](/C:/Users/jack_shih/Desktop/cry3/reports/wildcat_30d_balanced_v1_rolling.json)

Using the last 60 complete Taiwan days:

- Rolling 7d windows: `55`
  - avg of avg daily pnl: `19.5013`
  - avg max drawdown: `30.9792`
  - windows meeting `avg>=20 and MaxDD<=30`: `24 / 55`
- Rolling 30d windows: `32`
  - avg of avg daily pnl: `18.8100`
  - avg max drawdown: `45.4400`
  - windows meeting `avg>=20 and MaxDD<=30`: `5 / 32`

Interpretation:

- `wildcat_30d_balanced_v1` passes the latest single 30-day validation window.
- It does **not** yet generalize well across rolling windows.
- Treat it as the current best balanced milestone, not as a fully robust final strategy.

## Preset Parameters

`wildcat_converged_v1` uses:

- Strategies: `S1_BB_RSI`, `S5_Stoch`
- Notional: `1000 USDC`
- Max open positions: `2`
- Duplicate layers: enabled, max `2`
- Recovery steps: `3`
- Recovery trigger: `0.0009`
- Recovery TP shrink: `0.45`
- Partial exit: `0.35`
- Partial TP: `0.0006`
- Daily profit target: `40 USDC`
- Daily floor lock: `22 USDC`
- Daily giveback: `6 USDC`
- Catch-up starts: hour `12`
- Rescue starts: hour `14`

## Not Finished

Next Codex should continue from here:

1. Find a risk-balanced preset that keeps avg `>30` while reducing MaxDD below `20`.
2. Fix the one weak day around `2026-05-30` from `19.0769` to `>=20`.
3. Re-run maker/taker fee sensitivity on the converged/risk-balanced preset.
4. Run 30-day and rolling 7-day validation.
5. Only after validation, port to live Telegram signal mode. Do not auto-trade mainnet.

## Useful Commands

Quick search:

```powershell
python scripts\backtest_wildcat_s1s5.py --days 7 --quick --top-n 20 --target-daily-usdc 20 --json-output reports\wildcat_s1s5_7d.json
```

Preset replay:

```powershell
python scripts\backtest_wildcat_s1s5.py --preset wildcat_converged_v1 --days 7 --target-daily-usdc 20 --json-output reports\wildcat_converged_v1_7d.json
```

Syntax check:

```powershell
python -m py_compile scripts\backtest_wildcat_s1s5.py src\gridbot\testnet\auto_trader.py src\gridbot\strategy\winrate_optimized_portfolio.py src\gridbot\telegram\handlers.py
```

## Safety Notes

- Do not commit API keys or `.env`.
- `tmp_remote/` is temporary and should not be committed.
- The active strict goal is not complete yet.
