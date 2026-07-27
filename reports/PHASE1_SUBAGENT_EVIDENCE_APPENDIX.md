# Phase 1 Subagent Evidence Appendix

Date: 2026-07-10
Scope: Old-project archive enrichment. This document preserves the evidence returned by the Phase 1 parallel review. It does not assert that a simulated result occurred in live trading.

## Evidence Status

| Status | Meaning |
| --- | --- |
| LIVE | Observed from a recorded mainnet run or live review. |
| REPLAY | Reconstructed from tick or policy replay; not a live outcome. |
| CONFIG | Checked in source/configuration behavior; current live runtime must be verified separately. |
| OPEN | Evidence gap or a claim that must not be promoted to a result. |

## Version Ledger Findings

- The checked-in implementation identifies itself as `_codex_v1.4.55` in `src/gridbot/strategy/codex_v1_live.py`.
- The repository's prior maintenance and handoff narrative is continuous through v1.4.54. v1.4.55 had no corresponding maintenance or live-handoff document before this appendix.
- No Codex v1.1 maintenance record was found in the archived documentation. This is an archive gap, not evidence that v1.1 never existed.
- The strategy evolution is best understood in four periods: early canary/governance (v1.0-v1.2), W6A and selector repair (v1.3), STUP-S/WPR execution repair (v1.4.0-v1.4.24), and replay-led loss pruning plus bounded recovery (v1.4.25-v1.4.55).

## Live Run Evidence

### W6A

| Run ID | Finding | Status | Source |
| --- | --- | --- | --- |
| `cry3mn_1781886233558` | LONG gross PnL `+0.0054`, net PnL `-0.0149` after fees. | LIVE | `reports/CODEX_V1_3_1_2_LIVE_OBSERVATION_REPORT_2026-06-20.md:35` |
| `cry3mn_1782040648464` | Post-deploy W6A loss; SL exit. | LIVE | `reports/CODEX_V1_3_5_W6A_REVIEW_2026-06-21.md:5` |
| `cry3mn_1782040906797` | Post-deploy W6A loss; SL exit. | LIVE | `reports/CODEX_V1_3_5_W6A_REVIEW_2026-06-21.md:5` |
| `cry3mn_1782041976534` | Post-deploy W6A loss; SL exit. Combined three-run net approximately `-0.21054`; fills were quick, so maker TTL was not the root cause. | LIVE | `reports/CODEX_V1_3_5_W6A_REVIEW_2026-06-21.md:5` |

For replayed W6A losses `1782040906797`, `1782079540185`, and `1782137418170`, DCA was disabled in every sample and causes were not uniform. Do not attribute those losses to missing DCA alone. Source: `reports/W6A_WORST_LIVE_LOSS_TICK_REPLAY_2026-06-24.md:7`.

### CNL-WPR-L

| Run ID or set | Finding | Status | Source |
| --- | --- | --- | --- |
| v1.3.9D seven-seed set | 3bp was overly passive; 0/1bp increased fills but produced a warning loss on `cry3mn_1782242900283`. | REPLAY | `reports/WPR_V139D_7_BACKTEST_VM_REPLAY_2026-06-24.md:20` |
| `cry3mn_1782725568454` | `discount_mixed`; MFE `+4.70bp`, TP1 `5bp`; no pre-TP maker protection, later SL. | LIVE/REPLAY REVIEW | `docs/maintenance_log_2026-06-29_v1.4.18.md:9` |
| `cry3mn_1782737281168` | `falling_discount_trap`; replay policy produced SL `-0.060` despite large MFE/MAE. | REPLAY | `reports/v1418_current_market_fixed_lane_report_2026-06-29.md:35` |

### STUP-S

| Run ID | Finding | Status | Source |
| --- | --- | --- | --- |
| `cry3mn_1782633257565` | `weak_chop`; damage-control exit, realized `-0.0381` plus `0.0202` fee. MFE about `11.12bp`, below TP12 by `1.45bp`. | LIVE | `reports/CODEX_V1_4_6_STUPS_TIME_PROFIT_LOCK_HOTFIX_2026-06-28.md:8` |
| `cry3mn_1782645574102` | `weak_chop`; realized `-0.03264`, MFE about `7.47bp`, below lock threshold. | LIVE | `reports/CODEX_V1_4_7_STUPS_STALL_PROFIT_LOCK_HOTFIX_2026-06-28.md:8` |
| `cry3mn_1782726508340` | `clean_extension` hot 0bp entry accepted then quickly SL; replay preferred deeper maker or no fill. | LIVE/REPLAY REVIEW | `docs/maintenance_log_2026-06-29_v1.4.18.md:9` |
| `cry3mn_1782727507100` | `clean_extension` hot 0bp entry accepted then quickly SL; replay preferred deeper maker or no fill. | LIVE/REPLAY REVIEW | `docs/maintenance_log_2026-06-29_v1.4.18.md:9` |

## Exit and Recovery Evidence

- `cry3mn_1782500195337` exposed a W6A TP hierarchy inversion: partial TP at 8bp while final TP was about 4.43bp. Source: `docs/maintenance_log_2026-06-27_v1.4.1.md:11`.
- `cry3mn_1782501767906` and `cry3mn_1782529446505` support the claim that early fail cuts reduced damage. Source: `docs/maintenance_log_2026-06-27_v1.4.1.md:80`.
- v1.4.15 made BE exits explicit as `TP1_BE_SL`. Source: `docs/maintenance_log_2026-06-29_v1.4.15.md:31`.
- v1.4.54 designed bounded DCA for CNL-WPR-L and STUP-S, with maximum depth 2, 50 USDC layers, and a 0.50 USDC basket-loss cap. This is a CONFIG/design fact, not a live effectiveness result. Source: `docs/maintenance_log_2026-07-05_v1.4.54.md:6`.
- The v1.4.54 handoff reported no observed `dca_preloaded` or `recovery_entry_filled` sample. Therefore DCA/recovery must remain marked OPEN until a filled recovery entry is reviewed. Source: `reports/CODEX_V1_4_54_LIVE_HANDOFF_2026-07-05.md:21`.

## Current v1.4.55 Rule Surface

- Baseline tag: `portfolio_union_21branch_w1s6short_cluster1129`.
- Live adaptive lanes: `CNL-WPR-L`, `STUP-S`, `SFD-S`.
- Route modes: `BLOCK`, `THIN_SCALP`, `NORMAL`, `RECOVERY_CANARY`, and `OBSERVE_ONLY`.
- STUP-S `clean_extension` is blocked when target profit is at least 14bp; at 8bp or 10bp it may use the gated thin-scalp route.
- Recovery canary is restricted to `CNL-WPR-L`; `STUP-S` is not on the v1.4.55 live recovery allowlist.
- Recovery skip reasons include runtime DCA disabled, missing/not-whitelisted lane code, basket cap, max layers, partial exit, drift gate, active preloaded/open DCA order, and order failure.
- Every exit path cancels preplaced DCA. These are CONFIG/source facts and require production-log confirmation after deployment.

## Archive Gaps and Required Follow-up

1. Add v1.4.55 deployment timestamp, VM revision/archive checksum, service restart result, and first-loop logs to the durable handoff record.
2. Export the first v1.4.55 live runs with version, lane, route, exit reason, net PnL, and recovery event counts.
3. Do not claim recovery effectiveness until at least one `recovery_entry_filled` event is inspected end to end.
4. Preserve checked-in defaults separately from live runtime app configuration; they are not interchangeable evidence.
