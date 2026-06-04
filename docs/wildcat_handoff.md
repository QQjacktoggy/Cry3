# Wildcat Handoff

Last updated: 2026-06-04

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
