# v1.4.56a Telemetry Evidence Freeze - 2026-07-10

Evidence state: DEPLOYED / TESTED / POST-SCHEMA LIFECYCLE OPEN.

## Runtime

- Live host: `cry3jack` (`34.80.75.138`)
- Service: `cry3.service` active
- Runtime version: `_codex_v1.4.56`
- Strategy/risk behavior: unchanged from v1.4.55
- Deployment backup: `.codex_deploy_backups/pre_v1456a_20260710/files.tgz`
- Backup SHA-256: `b21d788c2857c54588d3f4da62c471b5186fd38401572cbc58fc48c88af1a149`

## Deployed File Identity

Local and VM SHA-256 values match:

| File | SHA-256 |
| --- | --- |
| `src/gridbot/mainnet/one_run.py` | `18419a33482493816a4f1f3ebd8dab1bfec73d560dfdc136ceb22abc9afab56a` |
| `src/gridbot/mainnet/fill_telemetry.py` | `8847fe8816929b56e35645af45a8b5f33716521504ce9f41101ba5cc7f5ebac2` |
| `scripts/export_fill_reconciliation.py` | `2d19dd1d3022ea6b0b2b525683fe75b1a556521980ce2c31021162d998f1856f` |
| `scripts/reconcile_position_ownership.py` | `bc991af6363aeb1b32332446e14a87b2b56b267bf1e7b1ccfa63471f3961856b` |

## Verification

- Focused suite: `281 passed, 3 warnings`.
- Incremental fill sync: entry detection, every RUNNING poll, terminalization, and entry failure.
- Restart idempotency: existing `fill_key` events are reloaded before emission.
- Explicit schema cutoff reconciliation: `PRE_SCHEMA=1776`, all other statuses `0`.
- Current fill count: `0`; no live fill, DCA, or recovery effectiveness claim is allowed.

## Position Ownership

- Mainnet: `FLAT`, no active run, no open orders.
- Testnet: external/manual `ETHUSDC SHORT 0.043`, no bot-prefixed open order, no active run, and no recent trade identity.
- Operational rule: do not close, adopt, or use the testnet position for lifecycle validation.

## Open Gate

A controlled post-schema lifecycle remains required before promotion: entry and exit `fill_v1`, order identity, commission, exit reason, and final net PnL must reconcile. Mainnet canary remains unarmed.
