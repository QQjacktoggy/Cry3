# Phase 1 Archive - 2026-07-10

This directory is the Phase 1 preservation snapshot. It combines a limited live-service/run extract with an inventory and compressed copy of selected backtest assets. It is an evidence handoff, not a deployment package, performance report, or complete recovery image.

## Contents

| Artifact | Role | Manifest SHA-256 |
|---|---|---|
| `backtest_asset_manifest.json` | Backtest-VM inventory, tiers, source-size notes, and archive metadata. | `ae228373401a37a2de46d38e717d50b7f792fa9b1ac69ee6f1bed84d62b8ae8a` |
| `phase1_backtest_assets_20260710.tar.gz` | Compressed selected backtest assets; inventory reports 23 files. | `dfab9cdb102c21bf92ccaf92a6ef47451e91ddb20bcb030532acc4f194705fc3` |
| `phase1_review_runs_20260710.txt` | Human-readable reviewed-run export. | `b4799700f3f9d2b5d82e8b84fd88821f6dff9bf4a81cd62db39aa7f944f2a5f4` |
| `phase1_selected_runs_20260710.db` | Selected SQLite subset: 27 runs and 4,451 events. | `5b6c584352667bbfc2eda99fa699dd2430267dcfa7f9b5a42fb4bf2c71014c0a` |
| `phase1_service_tail_20260710.log` | Captured service-log tail. | `6f234b9ec87cef0d33589976b08021ed7c56b498c0169cb2539d4402ad932b6f` |
| `phase1_vm_state_20260710.txt` | VM/service/version state snapshot. | `7e9507314318c7788ac2e566e9ad306bf8496b7a5d7ed0334187c0fbcd39559a` |
| `selected_run_ids.txt` | Run IDs selected for the SQLite subset. | `6f1cc9e876cc3889ca942d3adf34f3f8bd2b30ac52c8e751574f1efac6946d59` |

`phase1_manifest.json` is the authoritative archive manifest. It records the artifact list above, a manifest artifact-list digest of `d3322dfffd129e812791b7975555013efd096ceb9e80cac5adc5622293ad64c8`, and extraction time `2026-07-10T02:15:49Z`. Recompute each file hash before relying on a copied archive.

## Source State

The manifest identifies the live handoff source as host `cry3jack`, service `cry3.service:active`, version `_codex_v1.4.55`. It separately records source DB and deploy/backup archive SHA-256 values. `backtest_asset_manifest.json` describes a different backtest VM inventory (`hermesjacktoggy`); it is not a git-commit provenance record.

## Redaction

Redaction was enabled when the selected SQLite subset was made. The manifest states that recursive JSON key-fragment matching replaced matched values with `[REDACTED]` in:

- `mainnet_run_events.details_json`
- `mainnet_runs.params_json`
- `mainnet_runs.signal_json`

The configured key fragments were `api_key`, `apikey`, `api_secret`, `secret`, `token`, `password`, `chat_id`, and `telegram_chat`. This is a documented transformation rule, not a guarantee that every possible sensitive value or non-JSON field has been removed. Review before wider distribution.

## Integrity And Limits

- The per-artifact hashes and sizes come from `phase1_manifest.json`; this README was added after that manifest and is not covered by its artifact list or digest.
- A matching hash proves byte identity to the manifest record, not completeness, correctness, live deployment, or profitability.
- The selected database is a 27-run subset, not the full production database. The service tail is a bounded capture, not a complete log history.
- The backtest inventory records that its VM workspace had no `.git` directory. Neither archive establishes a reproducible source commit by itself.
- Research assets and replays are not evidence of live net profitability. Use the ledger and retained DB/order evidence for claims about fills, fees, exits, and strategy behavior.
- `/tmp` is explicitly identified as ephemeral in the backtest inventory and must not be the only durable copy of any asset.
