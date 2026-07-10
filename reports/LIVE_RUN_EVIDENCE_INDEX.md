# Live Run Evidence Index

Updated: 2026-07-10 TPE

This index records documented evidence, not inferred performance. `LIVE` rows are observed mainnet runs. `REPLAY`, `CONFIG`, and `DEPLOYED` rows are not live outcomes. `unknown` means the cited source did not contain that field.

DB-backed fields below come from the read-only subset `reports/archive/phase1_20260710/phase1_selected_runs_20260710.db` (27 runs, 4451 events). Net PnL is `realized_pnl_usdc + commission_usdc`, matching `scripts/review_runs.py`. Recovery shorthand is `p` placed, `f` filled, `s` skipped, `g` guard-blocked, and `d` drift-blocked.

## Evidence Status

| Status | Meaning |
| --- | --- |
| LIVE | Observed from a recorded mainnet run. |
| REPLAY | Reconstructed tick, policy, or backtest result; not a live outcome. |
| CONFIG | Checked-in source or configuration behavior. |
| DEPLOYED | VM/service/runtime state was verified; this does not prove a filled run. |
| OPEN | Evidence gap; do not treat as a result. |

## Actual Live Runs

| run_id | runtime version | lane | DB state | action / side | net PnL after fees | exit reason | recovery events | evidence | lesson / context |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `cry3mn_1781050335800` | unknown | unknown | `COMPLETED` | action unknown / LONG | `+0.15467000` USDC | `TRAIL` | p0/f1/s0/g0/d0 | LIVE / DB | Older DCA exposure case; `CLAUDE.md`. |
| `cry3mn_1781063317906` | unknown | unknown | `COMPLETED` | action unknown / LONG | `-0.28167000` USDC | `TRAIL` | p0/f0/s0/g0/d0 | LIVE / DB | Older DCA cleanup/DB-state regression case; `CLAUDE.md`. |
| `cry3mn_1781065747854` | unknown | unknown | `COMPLETED` | action unknown / LONG | `-0.07932199` USDC | `TRAIL` | p0/f1/s0/g0/d0 | LIVE / DB | Older preloaded-DCA execution regression case; `CLAUDE.md`. |
| `cry3mn_1781886233558` | `_codex_v1.3.1_shadow_frequency_recovery` | W6A | `COMPLETED`; market state unknown | `v130_w6a_bad_rr_mature_50_cap` / LONG | `-0.01493975` USDC | `TRAIL` | p0/f0/s0/g0/d0 | LIVE / DB | Gross winner became net negative after fees; `reports/CODEX_V1_3_1_2_LIVE_OBSERVATION_REPORT_2026-06-20.md`. |
| `cry3mn_1782040648464` | `_codex_v1.3.4_conservative_frequency_recovery` | W6A | `COMPLETED`; market state unknown | `v130_w6a_default_50_cap` / LONG | `-0.09825490` USDC | `SL` | p0/f0/s0/g0/d0 | LIVE / DB | One of the reviewed W6A losses; `reports/CODEX_V1_3_5_W6A_REVIEW_2026-06-21.md`. |
| `cry3mn_1782040906797` | `_codex_v1.3.4_conservative_frequency_recovery` | W6A | `COMPLETED`; market state unknown | `v134_w6a_weak_drift_50_canary` / LONG | `-0.10031052` USDC | `SL` | p0/f0/s0/g0/d0 | LIVE / DB | Replay sample had DCA disabled; do not attribute the loss to one cause; `reports/W6A_WORST_LIVE_LOSS_TICK_REPLAY_2026-06-24.md`. |
| `cry3mn_1782041976534` | `_codex_v1.3.4_conservative_frequency_recovery` | W6A | `COMPLETED`; market state unknown | `v134_w6a_weak_drift_50_canary` / LONG | `-0.07182140` USDC | `SL` | p0/f0/s0/g0/d0 | LIVE / DB | One of the reviewed W6A losses; `reports/CODEX_V1_3_5_W6A_REVIEW_2026-06-21.md`. |
| `cry3mn_1782079540185` | `_codex_v1.3.6_nl_near_w1d_live200_bad_path_block` | W6A | `COMPLETED`; market state unknown | `v130_w6a_clean_200_cap` / LONG | `-0.22750837` USDC | `w6a_no_tp1_weak_no_bounce_early_exit` | p0/f0/s0/g0/d0 | LIVE / DB | Underlying live row for the replay group below; `reports/W6A_WORST_LIVE_LOSS_TICK_REPLAY_2026-06-24.md`. |
| `cry3mn_1782137418170` | `_codex_v1.3.7D_nl_cooling_pb_short_observer` | W6A | `COMPLETED`; market state unknown | `v130_w6a_clean_200_cap` / LONG | `-0.29317380` USDC | `SL` | p0/f0/s0/g0/d0 | LIVE / DB | Underlying live row for the replay group below; `reports/W6A_WORST_LIVE_LOSS_TICK_REPLAY_2026-06-24.md`. |
| `cry3mn_1782242900283` | `_codex_v1.3.9D_admission_tighten` | CNL-WPR-L | `COMPLETED`; market state unknown | `v139_reprice_tiny_canary` / LONG | `+0.01200000` USDC | `flat_detected` | p0/f0/s0/g0/d0 | LIVE / DB | Actual source run; the separate seven-seed replay warning remains REPLAY below. |
| `cry3mn_1782500195337` | `_codex_v1.4.1` | W6A | `COMPLETED`; market state unknown | `v137_w6a_risk_score_keep50` / LONG | `+0.08192000` USDC | `flat_detected` | p0/f0/s0/g0/d0 | LIVE / DB | Context for the TP hierarchy regression review; `docs/maintenance_log_2026-06-27_v1.4.1.md`. |
| `cry3mn_1782501767906` | `_codex_v1.4.1` | W6A | `COMPLETED`; market state unknown | `v137_w6a_keep_requested` / LONG | `-0.02560000` USDC | `w6a_no_bounce_soft_exit_v2` | p0/f0/s0/g0/d0 | LIVE / DB | Early-fail-cut example; `docs/maintenance_log_2026-06-27_v1.4.1.md`. |
| `cry3mn_1782529446505` | `_codex_v1.4.1` | STUP-S | `COMPLETED`; market state unknown | `v1312_stale_upmove_guarded_canary` / SHORT | `-0.07104000` USDC | `CODEX_EARLY_FAIL` | p0/f0/s0/g0/d0 | LIVE / DB | Early-fail-cut example; `docs/maintenance_log_2026-06-27_v1.4.1.md`. |
| `cry3mn_1782633257565` | `_codex_v1.4.5` | STUP-S | `COMPLETED`; `STUP-S:weak_chop` | `v143_stups_adaptive_exec` / SHORT | `-0.05823718` USDC | `CODEX_DAMAGE_CONTROL` | p0/f0/s0/g0/d0 | LIVE / DB | MFE stayed below TP12; `reports/CODEX_V1_4_6_STUPS_TIME_PROFIT_LOCK_HOTFIX_2026-06-28.md`. |
| `cry3mn_1782645574102` | `_codex_v1.4.6` | STUP-S | `COMPLETED`; `STUP-S:weak_chop` | `v143_stups_adaptive_exec` / SHORT | `-0.05286528` USDC | `CODEX_DAMAGE_CONTROL` | p0/f0/s0/g0/d0 | LIVE / DB | MFE stayed below the lock threshold; `reports/CODEX_V1_4_7_STUPS_STALL_PROFIT_LOCK_HOTFIX_2026-06-28.md`. |
| `cry3mn_1782725568454` | `_codex_v1.4.17` | CNL-WPR-L | `COMPLETED`; `CNL-WPR-L:discount_mixed` | `v145_wpr_profit_lock_exec` / LONG | `-0.11386879` USDC | `SL` | p0/f0/s0/g0/d0 | LIVE / DB | MFE +4.70bp versus TP1 5bp; `docs/maintenance_log_2026-06-29_v1.4.18.md`. |
| `cry3mn_1782726508340` | `_codex_v1.4.17` | STUP-S | `COMPLETED`; `STUP-S:clean_extension` | `v143_stups_adaptive_exec` / SHORT | `-0.06210560` USDC | `SL` | p0/f0/s0/g0/d0 | LIVE / DB | Hot 0bp entry; replay preferred deeper maker entry or no fill; `docs/maintenance_log_2026-06-29_v1.4.18.md`. |
| `cry3mn_1782727507100` | `_codex_v1.4.17` | STUP-S | `COMPLETED`; `STUP-S:clean_extension` | `v143_stups_adaptive_exec` / SHORT | `-0.06052723` USDC | `SL` | p0/f0/s0/g0/d0 | LIVE / DB | Similar hot 0bp entry; `docs/maintenance_log_2026-06-29_v1.4.18.md`. |
| `cry3mn_1782737281168` | `_codex_v1.4.18` | CNL-WPR-L | `COMPLETED`; `CNL-WPR-L:falling_discount_trap` | `v145_wpr_profit_lock_exec` / LONG | `+0.00196730` USDC | `TP1_BE_SL` | p0/f0/s0/g0/d0 | LIVE / DB | Actual source run; the policy counterfactual remains REPLAY below. |
| `cry3mn_1783074079419` | `_codex_v1.4.40` | STUP-S | `COMPLETED`; `STUP-S:clean_extension` | `L_E0_TP10_SL6_T90_LOCK60_5_0` / LONG | `-0.06911058` USDC | `SL` | p0/f0/s0/g0/d0 | LIVE / DB | Immediate adverse fill; `reports/CODEX_V1_4_40_LIVE_TICK_ANALYSIS_2026-07-03.md`. |
| `cry3mn_1783075395374` | `_codex_v1.4.40` | STUP-S | `COMPLETED`; `STUP-S:clean_extension` | `S_E0_TP10_SL6_T90_LOCK60_5_0` / SHORT | `+0.05336000` USDC | `TP` | p0/f0/s0/g0/d0 | LIVE / DB | Winner that a hard shadow-score block would reject; `reports/CODEX_V1_4_40_LIVE_TICK_ANALYSIS_2026-07-03.md`. |
| `cry3mn_1783079155384` | `_codex_v1.4.40` | STUP-S | `COMPLETED`; `STUP-S:mixed` | `S_E2_TP14_SL8_T90_LOCK90_6_0` / SHORT | `-0.02918339` USDC | `MAX_HOLD_LOSS` | p0/f0/s0/g0/d0 | LIVE / DB | MFE only 0.80bp; `reports/CODEX_V1_4_40_LIVE_TICK_ANALYSIS_2026-07-03.md`. |
| `cry3mn_1783079654170` | `_codex_v1.4.40` | STUP-S | `COMPLETED`; `STUP-S:clean_extension` | `L_E0_TP8_SL6_T60_LOCK60_5_0` / LONG | `-0.06630038` USDC | `SL` | p0/f0/s0/g0/d0 | LIVE / DB | Reached 4.71bp but missed 5bp lock; `reports/CODEX_V1_4_40_LIVE_TICK_ANALYSIS_2026-07-03.md`. |
| `cry3mn_1783176458326` | `_codex_v1.4.47` | STUP-S | `COMPLETED`; `STUP-S:clean_extension` | `S_E2_TP10_SL8_T90_LOCK90_6_0` / SHORT | `-0.02152391` USDC | `CODEX_V1443_STUPS_CLEAN_EXTENSION_REVERSAL_SCRATCH` | p0/f0/s0/g0/d0 | LIVE / DB | Runtime row reviewed by the v1.4.48 maintenance record; `docs/maintenance_log_2026-07-04_v1.4.48.md`. |
| `cry3mn_1783232351425` | `_codex_v1.4.51` | STUP-S | `COMPLETED`; `STUP-S:clean_extension` | `S_E2_TP10_SL8_T90_LOCK90_6_0` / SHORT | `-0.05953886` USDC | `SL` | p0/f0/s0/g0/d0 | LIVE / DB | Motivated the v1.4.52 late-adverse reopen block; `docs/maintenance_log_2026-07-05_v1.4.52.md`. |
| `cry3mn_1783237051530` | `_codex_v1.4.52` | STUP-S | `COMPLETED`; `STUP-S:clean_extension` | `S_E2_TP14_SL8_T90_LOCK90_6_0` / SHORT | `-0.06010524` USDC | `SL` | p0/f0/s0/g0/d0 | LIVE / DB | High-range reopen review shape; `docs/maintenance_log_2026-07-05_v1.4.53.md`. |
| `cry3mn_1783237610286` | `_codex_v1.4.52` | STUP-S | `COMPLETED`; `STUP-S:clean_extension` | `S_E0_TP14_SL8_T90_LOCK60_6_0` / SHORT | `-0.07329716` USDC | `SL` | p0/f0/s0/g0/d0 | LIVE / DB | Late high-zone reopen loss; `docs/maintenance_log_2026-07-05_v1.4.53.md`. |

## Replay and Counterfactual Evidence (Not Live Outcomes)

Live DB values for the same run IDs are recorded above. They do not replace replay-specific fields that the replay source did not report.

| run_id | version / source | lane | replay state | action / side | replay net PnL | replay exit | recovery state | evidence | lesson / source |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `cry3mn_1782242900283` | v1.3.9D seven-seed replay | CNL-WPR-L | replayed warning loss | unknown | unknown | unknown | unknown | REPLAY | 3bp was overly passive; `reports/WPR_V139D_7_BACKTEST_VM_REPLAY_2026-06-24.md`. |
| `cry3mn_1782737281168` | v1.4.18 policy counterfactual | CNL-WPR-L / falling_discount_trap | replayed policy outcome | unknown | `-0.060` USDC | `SL` | unknown | REPLAY | Counterfactual, not the actual DB result; `reports/v1418_current_market_fixed_lane_report_2026-06-29.md`. |
| `cry3mn_1782040906797`, `cry3mn_1782079540185`, `cry3mn_1782137418170` | W6A worst-loss tick replay | W6A | replayed losses | unknown | unknown | varies | DCA disabled in every replay sample | REPLAY | Causes were not uniform; `reports/W6A_WORST_LIVE_LOSS_TICK_REPLAY_2026-06-24.md`. |

## Configuration, Deployment, and Open Evidence

| run_id | version / source | lane | state | runtime controls | result | recovery state | evidence | source |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| none | v1.4.54 bounded recovery design | CNL-WPR-L and STUP-S | designed configuration | 50 USDC layers; max depth 2; 0.50 USDC basket cap | not a live result | no confirmed fill at handoff | CONFIG / OPEN | `docs/maintenance_log_2026-07-05_v1.4.54.md`; `reports/CODEX_V1_4_54_LIVE_HANDOFF_2026-07-05.md` |
| none | `_codex_v1.4.55` checked-in implementation | CNL-WPR-L, STUP-S, SFD-S | policy/configuration | recovery canary limited to CNL-WPR-L by default | not a live result | effectiveness open | CONFIG / OPEN | `src/gridbot/strategy/codex_v1_live.py`; `config/settings.py` |
| none | `_codex_v1.4.55` VM runtime | runtime | `cry3jack` active; version verified | ticket 50 USDC; loop cap 2 USDC; DCA true | no confirmed v1.4.55 run in latest review | effectiveness open | DEPLOYED / OPEN | `reports/archive/phase1_20260710/phase1_vm_state_20260710.txt`; maintenance handoff |
| none | `_codex_v1.4.56` telemetry-only VM runtime | runtime | `cry3jack` active; version verified | v1.4.55 strategy/risk controls retained | 1,776 runs; 0 `fill_v1` events | `has_position=True`, `trades=0`; ownership and DCA effectiveness unresolved | HANDOFF / OPEN | VM reconciliation export; backup `/home/jack_shih/cry3/.codex_deploy_backups/pre_v1456_20260710_212327/files.tgz` |

## v1.4.55: Deployment Verified, Run Outcome Open

At `2026-07-10 02:15:49 UTC`, `cry3jack` was active and the loaded strategy source reported `_codex_v1.4.55`. Deployment provenance is recorded as:

- Archive: `.codex_deploy/v1455_20260709/files.tgz`
- Archive SHA-256: `7b0ae48d82140ea6e0039c35b8f7095fb2257da8a42426186856db21e1497ef4`
- Backup: `.codex_deploy_backups/v1455_20260709/files.tgz`
- Backup SHA-256: `b33b2b0f30bc8170a25e15b01cd347abb197ed1c5dc670428a6f888437078cac`
- Source DB SHA-256: `253107e4f149a0d2f4598b56f7f939a998ebbfadd14875f119bee933ba4dc36e`

The latest review covers 50 runs over 168 hours and contains versions through `_codex_v1.4.54`; it contains no confirmed `_codex_v1.4.55` run. This means deployment is verified while v1.4.55 live PnL, exit behavior, and recovery effectiveness remain unconfirmed.

Do not claim v1.4.55 profitability or DCA effectiveness until a DB-backed v1.4.55 run is reviewed. Deployment state, service health, and policy tests are not substitutes for a filled-run outcome.

## v1.4.56: Telemetry Deployed, Fill Outcome Open

On 2026-07-10, `cry3.service` remained active and the VM loaded `_codex_v1.4.56`. The release is telemetry-only: it adds the `fill_v1` evidence/export contract while retaining the v1.4.55 strategy and risk rules. Focused policy and one-run verification was **TEST**: `281 passed, 3 warnings`.

Deployment rollback provenance is preserved at `/home/jack_shih/cry3/.codex_deploy_backups/pre_v1456_20260710_212327/files.tgz`.

The first reconciliation export found 1,776 runs and 0 `fill_v1` events. Treat this as **HANDOFF/OPEN**, not as proof of no fills or proof that telemetry works under a real lifecycle: the run population includes pre-schema history and no post-schema filled lifecycle has been reviewed. Runtime also reported `has_position=True` while the available trade query returned `trades=0`; ownership, environment, and exchange identity remain unresolved.

No v1.4.56 profitability, DCA effectiveness, or recovery effectiveness claim is permitted until a post-schema `fill_v1` lifecycle is reconciled to exchange fills, commissions, exit reason, and final net PnL.

## Refresh Sources

- VM `mainnet_runs`: status, exit reason, side, prices, quantity, realized PnL, commission, signal JSON, and params JSON.
- VM `mainnet_run_events`: entry, TP, partial exit, recovery placed/filled/skipped, DCA guard/drift, trail, survival, and completed events.
- Service log: version, recovery, DCA, basket, drift, traceback, and error markers.
- Durable snapshot: `reports/archive/phase1_20260710/`.

When runtime state matters, VM DB and service-log evidence override checked-in defaults. When outcome state matters, a deployment record alone is insufficient.

## v1.4.56a Runtime Verification - 2026-07-10

The hardening deployment keeps strategy version _codex_v1.4.56 and changes telemetry only. VM-side compilation, service restart, runtime import, and 281 focused tests passed. Backup: .codex_deploy_backups/pre_v1456a_20260710/files.tgz (SHA-256 b21d788c2857c54588d3f4da62c471b5186fd38401572cbc58fc48c88af1a149).

Using the explicit v1.4.56 deployment cutoff, reconciliation reports PRE_SCHEMA=1776, OBSERVED_COMPLETE=0, OBSERVED_PARTIAL=0, MISSING_EXPECTED=0, and AMBIGUOUS=0.

Ownership is now separated by environment: mainnet is FLAT; testnet holds an external/manual ETHUSDC SHORT of 0.043 with no bot-prefixed open order, active run, or recent trade identity. This position is not a Codex run and must not be modified by lifecycle validation.
