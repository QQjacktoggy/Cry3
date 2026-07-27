# v1.4.56b Margin Preflight Incident - 2026-07-10

## Run Outcome

- Run: `cry3mn_1783697000031`
- Environment: mainnet, `ETHUSDC`
- Config: 50 USDC ticket, 150 USDC maximum recovery basket, 75x, one loop, 2 USDC loop-loss cap.
- Accepted route: `CNL-WPR-L:deep_discount_stable`, LONG, `v1427_base_passthrough`, 2bp post-only entry, requested 50 USDC.
- Terminal status: `FAILED`
- Error: `APIError(code=-2019): Margin is insufficient.`
- Exchange result: no order, no fill, no position, no commission, net PnL 0.
- `fill_v1`: 0
- `recovery_entry_filled`: 0

## Root Cause

The account held 0.4234 USDC. A 50 USDC entry at 75x needs about 0.6667 USDC initial margin. The configured 150 USDC recovery basket needs 2.0000 USDC before buffer. The existing arm preflight checked positions, orders, and maker fee but did not verify that the margin asset could fund the configured basket.

## Fix

- Added a futures margin-asset balance API to `BinanceFuturesClient`.
- Arm and loop re-arm now compare `availableBalance` with `max_cumulative_notional / leverage * 1.05`.
- The current 150 USDC / 75x basket therefore requires 2.1000 USDC available.
- Insufficient margin is now rejected before a run is armed.
- The rejection is logged as `mainnet_one_run_insufficient_margin_preflight`.

## Verification

- Local: 264 arm, live-policy, and maker-executor tests passed; 3 dependency deprecation warnings.
- VM backup: `.codex_deploy_backups/v1456b_margin_preflight_20260710/files.tgz`
- Backup SHA-256: `86939da3851de4c3720a8f77f3049f2bbae36e5ea012ff904f4da20cc3e62fa2`
- Deployed `client.py` SHA-256: `92a62cb7d9085fd7fba979a22ef6a3ec46ddceaa81b72426ecd784fbf3126233`
- Deployed `one_run.py` SHA-256: `974af5cc207c7c2da20bbf282ac8b848edb32e2352b3718432fd403ae60d51ac`
- VM compile passed; `cry3.service` active; strategy version remains `_codex_v1.4.56` because this is execution safety, not a strategy-policy change.

## Restart Gate

Do not arm another run until mainnet `USDC availableBalance >= 2.1000`. A 3 USDC available balance is the operational minimum recommendation. Recheck flat position, no open orders, and no active run immediately before arming.