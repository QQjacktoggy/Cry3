# Maintenance Log: Codex v1.4.55 Live Handoff

Date: 2026-07-10
Status: `deployed_verified`; live outcome evidence remains open

## Deployment Verification

The 2026-07-10 VM snapshot records the following verified runtime state:

- Host: `cry3jack`.
- Service: `cry3.service` active.
- Loaded strategy version: `_codex_v1.4.55`.
- Verification timestamp: `2026-07-10 02:15:49 UTC` (`2026-07-10 10:15:49 TPE`).
- Deployment archive: `.codex_deploy/v1455_20260709/files.tgz`.
- Deployment archive SHA-256: `7b0ae48d82140ea6e0039c35b8f7095fb2257da8a42426186856db21e1497ef4`.
- Rollback backup: `.codex_deploy_backups/v1455_20260709/files.tgz`.
- Rollback backup SHA-256: `b33b2b0f30bc8170a25e15b01cd347abb197ed1c5dc670428a6f888437078cac`.
- Source DB: `/home/jack_shih/testnet/data/gridbot_testnet.db`.
- Source DB SHA-256: `253107e4f149a0d2f4598b56f7f939a998ebbfadd14875f119bee933ba4dc36e`.

The runtime controls recorded for this handoff are a `50 USDC` ticket, `2 USDC` loop-loss cap, and DCA enabled (`true`). These controls describe runtime state; they are not evidence that a DCA or recovery fill occurred.

## Verification Results

- Test result: `259 passed`, `3 warnings`.
- Latest review scope: `50` runs over a `168h` lookback.
- The review artifact contains runtime versions through `_codex_v1.4.54` and no confirmed `_codex_v1.4.55` run.
- Therefore deployment/version verification is complete, but no v1.4.55 trade outcome is confirmed by the current review.

## Sanitized Evidence Subset

- Read-only subset: `reports/archive/phase1_20260710/phase1_selected_runs_20260710.db`.
- Contents: `27` selected runs and `4451` events.
- The subset was used to fill lane, state, action, side, net PnL, exit reason, and recovery counts in `reports/LIVE_RUN_EVIDENCE_INDEX.md` only when the DB contained the field.
- A missing DB value remains `unknown`; replay outcomes are not replaced with the source run's live outcome.

## Current Policy Record

- Version: `_codex_v1.4.55`.
- Adaptive outcomes: `BLOCK`, `THIN_SCALP`, `NORMAL`, `RECOVERY_CANARY`, `OBSERVE_ONLY`.
- High-risk STUP-S `clean_extension` with TP14 is blocked.
- Gate-passing TP8/TP10 STUP-S `clean_extension` remains eligible for thin scalp.
- Runtime DCA capability is represented in the signal payload.
- Recovery is canary-only and initially allowlisted to `CNL-WPR-L`.
- Basket loss cap remains `0.50 USDC`.

## Explicit Non-Claims

- No live profitability, win-rate, or safety conclusion is claimed for v1.4.55.
- DCA is not yet proven effective in live trading.
- Recovery is not considered effective until an inspected v1.4.55 run contains `recovery_entry_filled` and its final net PnL is reviewed.
- Deployment verification does not imply that a v1.4.55 order filled.
- Historical replay results are not live profitability results.

## Evidence Artifacts

- `reports/archive/phase1_20260710/phase1_vm_state_20260710.txt`
- `reports/archive/phase1_20260710/phase1_service_tail_20260710.log`
- `reports/archive/phase1_20260710/phase1_review_runs_20260710.txt`
- `reports/archive/phase1_20260710/phase1_selected_runs_20260710.db`
- `reports/LIVE_RUN_EVIDENCE_INDEX.md`
- `reports/strategy_evidence_index.jsonl`

## Next Evidence Gate

Capture the first confirmed v1.4.55 run with version, lane, state, action, side, net PnL after fees, exit reason, and all recovery placed/filled/skipped events. Until then, keep v1.4.55 outcome status open.
