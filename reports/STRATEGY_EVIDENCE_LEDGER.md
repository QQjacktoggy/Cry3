# Strategy Evidence Ledger

Generated: 2026-07-10 TPE

This is an archive ledger, not a performance report or VM deployment record. Source logs, reports, DB rows, and replay outputs remain authoritative.

## Evidence Labels

| Label | Meaning |
|---|---|
| **LIVE** | Filled/live-loop evidence. Deployment alone is not LIVE evidence. |
| **REPLAY** | Tick/aggTrade, shadow, counterfactual, or offline research; not proof of live profitability. |
| **TEST** | Compile or contract-test result; proves checked code path, not VM state or market result. |
| **HANDOFF** | VM version/service snapshot, deployment archive, checksum, or export provenance; proves artifact/state identity, not a filled-trade outcome. |
| **CONFIG/OPEN** | Checked-in policy, planned behavior, missing evidence, or unresolved caveat. |

## Current Position

- A 2026-07-10 **HANDOFF** snapshot verifies live host `cry3jack`, `cry3.service` active, and VM import `CODEX_V1_VERSION = "_codex_v1.4.56"`.
- v1.4.56 is telemetry-only: it adds the `fill_v1` event/export contract and does not change the v1.4.55 lane, TP/SL, DCA, sizing, or risk policy.
- Focused local verification is **TEST**: `281 passed, 3 warnings` across the live-policy and one-run suites.
- The VM deployment backup is `/home/jack_shih/cry3/.codex_deploy_backups/pre_v1456_20260710_212327/files.tgz`.
- The first VM reconciliation export is **HANDOFF/OPEN**: 1,776 runs and 0 `fill_v1` events. This is expected to include pre-schema history and is not evidence of filled v1.4.56 behavior.
- Runtime observation `has_position=True` with `trades=0` remains unresolved; no new live canary should be inferred from this state.
- The same snapshot preserves separate SHA-256 values for `.codex_deploy/v1455_20260709/files.tgz` and `.codex_deploy_backups/v1455_20260709/files.tgz`; these checksums identify handoff artifacts, not strategy outcomes.
- Local v1.4.55 policy/executor verification is **TEST**: `259 passed, 3 warnings` on 2026-07-10.
- The latest 50-run / 168h review export contains no run confirmed as `_codex_v1.4.55`; deployment is verified, first-run behavior is not.
- Recovery remains a canary: 50 USDC ticket, 2 USDC loop-loss cap, `CNL-WPR-L` allowlist, and 0.50 USDC basket-loss cap.
- DCA/recovery effectiveness is **OPEN** until a reviewed **LIVE** `recovery_entry_filled` event includes the final net-after-fee outcome.

## Archive Ledger

| Version / date | Purpose | Code / policy change | Test / replay evidence | Live evidence | Failure cases / open caveats | Source pointers |
|---|---|---|---|---|---|---|
| Early live V1-V4 / 2026-06-10 | Establish execution loop. | DCA, trailing, notification, order-cleanup iteration. | **CONFIG/OPEN**: no consolidated replay suite. | **LIVE**: operational incidents drove later review. | Execution bugs mean signal backtests alone are inadequate. | `CLAUDE.md`; `docs/maintenance_log_2026-06-10*.md` |
| Codex v1.0.0-v1.0.1 / 2026-06 | Initial handoff/runbook. | Initial lane policy and controls. | **TEST/CONFIG**: runbook/handoff. | **unknown/open**: no retained PnL claim. | Historical defaults are not verified VM state. | `docs/CODEX_V1_0_0_*`; `reports/CODEX_V1_0_*_LIVE_HANDOFF.md` |
| Codex v1.2.1-v1.2.14 / 2026-06-16..19 | Lane-level governance. | Playbooks; W1B audit; extension/veto/W6A/micro-trail changes. | **REPLAY**: attribution and optimization. | **LIVE**: bounded 8/26-run reviews. | Use post-dedup ordered union, not branch WR; sample/no-fill limits. | `reports/CODEX_V1_2_*` |
| Codex v1.3.0-v1.3.19 / 2026-06-19..26 | Live-observation-to-hotfix loop. | VM signal fix; shadow/TP work; W6A; stale-upmove; risk repair. | **REPLAY/TEST**: expired replays and implementation records. | **LIVE**: observation/lane reviews. | Do not conflate shadow, VM state, and profit. | `docs/CODEX_V1_3_0_VM_DEPLOYMENT_AND_SIGNAL_HOTFIX_2026-06-19.md`; `reports/CODEX_V1_3_*` |
| v1.4.0 / 2026-06-25 | Improve fills/W6A risk. | STUP-S offset 3bp to 1bp; TP override; W6A block. | **REPLAY**: 1bp made 8/17 expiry cases TP vs 1/17 baseline. | **LIVE**: preceding 72-run net `-0.2583 USDC`, rationale only. | Fee drag and expiry remained material. | `docs/maintenance_log_2026-06-25_v1.4.0.md` |
| v1.4.1 / 2026-06-27 | Repair W6A TP and BE. | TP hierarchy/partial TP repair; TP1 BE-SL; lane overrides. | **TEST**: compile recorded. | **LIVE**: 8.5h, 23 completed, 20W/3L, net `+0.07187 USDC`; W6A TP confirmed. | Short sample; 23 expired entries. | `docs/maintenance_log_2026-06-27_v1.4.1.md` |
| v1.4.2 / 2026-06-27 | Entry/exit/sizing tune. | STUP-S 3bp; WPR full 11bp maker TP; 100U sizing/caps. | **REPLAY**: 88-run sweep, simulated net `+0.49` to `+0.98 USDC`; **TEST** local/VM. | **unknown/open** post-deploy result. | Fill-model dependence and larger exposure. | `docs/maintenance_log_2026-06-27_v1.4.2.md` |
| v1.4.3-v1.4.14 / 2026-06-28..29 | Market-state exits/profit locks. | Adaptive plan; WPR/STUP-S locks; S1PL; market-state exit. | **REPLAY**: v1.4.6 aggTick, v1.4.14 counterfactual. | **LIVE**: hotfix sources cite routes. | Grouped; individual evidence is unknown/open except cited reports. | `reports/CODEX_V1_4_3_*`; `reports/CODEX_V1_4_5_*`; `reports/CODEX_V1_4_6_*`; `reports/CODEX_V1_4_7_*`; `reports/CODEX_V1_4_9_*`; `reports/CODEX_V1_4_14_*` |
| v1.4.15 / 2026-06-29 | WPR strong-fall and BE attribution. | Deep entry; maker-first `TP1_BE_SL`. | **TEST**: 5 focused, 25 related passed; 142 passed/2 W6A-shadow-off failed. | **LIVE**: loss patterns were rationale. | BE fee leakage remains open. | `docs/maintenance_log_2026-06-29_v1.4.15.md` |
| v1.4.16 / 2026-06-29 | Adaptive TP1 runner. | STUP-S weak-chop/WPR falling-trap runners; quality shadow. | **TEST**: local compile. | **LIVE**: cases `1782720721210`, `1782718210925`, `1782721330163`. | TP1 net-after-fee unknown/open. | `docs/maintenance_log_2026-06-29_v1.4.16.md` |
| v1.4.17 / 2026-06-29 | Relax weak-chop shadow. | Better-entry treatment. | **CONFIG/OPEN**. | **LIVE**: limited prior sample. | No broad PnL claim. | `docs/maintenance_log_2026-06-29_v1.4.17.md` |
| v1.4.18 / 2026-06-29 | Pre-TP protection/hot entry. | CNL-WPR-L protection; STUP-S clean-extension hot entry. | **TEST/CONFIG**. | **LIVE**: cases `1782725568454`, `1782726508340`, `1782727507100`. | Fee-safe realized exits open. | `docs/maintenance_log_2026-06-29_v1.4.18.md` |
| v1.4.19 / 2026-06-29 | Fixed-bucket WPR/STUP repair. | Blocks bad WPR slices; uses CNL/STUP runner and tight-exit profiles. | **REPLAY**: baseline 20 fills/net `-0.405960`; candidate 13 fills/net `+0.480000`; **TEST**: 26 policy and 22 focused maker tests passed. | **LIVE**: VM seeds were the replay basis; no post-change result retained. | Market-mix specific; BE/TP1 net-after-fee and broad falling-WPR shorting remain open. | `docs/maintenance_log_2026-06-29_v1.4.19.md`; `reports/v1418_current_market_fixed_lane_report_2026-06-29.md` |
| v1.4.20 / 2026-06-30 | Replay-aligned fixed profiles. | STUP-S/CNL fixed-bucket filters; S1P-L minimum-notional metadata repair. | **REPLAY**: controlled lanes 83 fills/net `+0.989288`; raw all-family net `+0.701144`; focused tests passed. | **CONFIG**: log says local/backtest-VM only, not deployed. | Fill model, BE fees, and projected family adjustment are not live proof. | `docs/maintenance_log_2026-06-30_v1.4.20.md`; `reports/v1420_profile_explorer_2026-06-28_29.md` |
| v1.4.21 / 2026-06-30 | Three-window adaptive tree canary. | Adds side-overriding CNL/STUP/SFD action tree and slope plumbing; excludes W6A. | **REPLAY**: all three windows cleared stated thresholds; **TEST**: 122 maker and 32 policy tests passed. | **HANDOFF**: log says deployed before v1.4.22; no standalone fill result. | Candle-close proxy slopes differ from replay aggTrade slopes. | `docs/maintenance_log_2026-06-30_v1.4.21.md`; `reports/v1421_decision_tree_three_window_success.md` |
| v1.4.22 / 2026-06-30 | v1.4.21 scope/hold parity hotfix. | Restricts the tree to CNL-WPR-L/STUP-S/SFD-S and honors profile `hold_s`. | **TEST**: 35 policy and 3 hold tests passed; compile passed. | **HANDOFF/LIVE**: deployed service/version verified; three reviewed SLs were entry/state failures, not missed exits. | No aggregate post-hotfix PnL; proxy-slope routing remains open. | `docs/maintenance_log_2026-06-30_v1.4.22.md` |
| v1.4.23 / 2026-07-01 | Four-window conservative-tree candidate. | Candidate tree/overlay research; no version-specific maintenance log exists. | **REPLAY**: report-backed multi-window candidate only. | **unknown/open**: no deployed-state or fill record located. | Candidate/branch metrics are not live-policy proof. | `reports/CODEX_V1_4_23_CONSERVATIVE_TREE_CANDIDATE.md`; `reports/v1423_four_window_conservative_tree_target1p4_summary.md` |
| v1.4.24 / 2026-07-01 | Interim live tree loop. | No standalone v1.4.24 maintenance record exists. | **unknown/open** for implementation validation. | **LIVE**: successor log preserves 4 interrupted-loop rows: 3 completed, 0 wins, net `-0.17879192 USDC`. | Do not reconstruct policy, tests, or deployment state from v1.4.25. | `docs/maintenance_log_2026-07-01_v1.4.25.md` |
| v1.4.25 / 2026-07-01 | Action-level live repair. | Blocks two direct-long E0 slices; rewrites WPR short E0 to 6bp scalp/lock; prevents old-loop rehydration. | **TEST**: 44 policy and 2 guard tests passed; compile/diff check passed. | **HANDOFF**: VM version/service and empty exchange state verified. | Four-row precursor sample is not proof of the new policy. | `docs/maintenance_log_2026-07-01_v1.4.25.md` |
| v1.4.26 / 2026-07-01 | Fifth-window defensive patch. | Blocks STUP weak/mixed shorts and WPR BASE; uses 6bp WPR short scalp. | **REPLAY**: fifth-window completed estimate `-0.37354540` to about `-0.06301160`, still negative. | **LIVE/HANDOFF**: predecessor loop was 14 completed/4 wins/net `-0.37354540`; v1.4.26 version/service verified. | Loss compression is not a positive strategy; S_E2/no-fill risk remains. | `docs/maintenance_log_2026-07-01_v1.4.26.md`; `reports/v1426_fifth_window_tick_replay_2026-07-01.md` |
| v1.4.27 / 2026-07-01 | Five-window compact-tree promotion. | Generated-tree layer, W1D/FDT blocks, TP cap 14bp, profile time lock. | **REPLAY**: selected overlay positive across five normalized windows; **TEST**: 53 policy tests and focused async checks passed. | **CONFIG**: intended live wiring only. | Full maker suite unavailable locally; no-fill/time-lock behavior needs live review. | `docs/maintenance_log_2026-07-01_v1.4.27.md`; `reports/v1427_five_window_final_candidate_2026-07-01.md` |
| v1.4.28 / 2026-07-01 | Legacy-STUP parity shim. | Reopens only known legacy STUP blocks when v1427 returns an explicit action. | **TEST**: 54 policy tests passed. | **LIVE**: one time lock net `+0.0326657`; two stale/side-override reopens SL about `-0.06349` and `-0.06252`. | Reopen freshness and flat-race accounting remained open. | `docs/maintenance_log_2026-07-01_v1.4.28.md` |
| v1.4.29 / 2026-07-01 | Sixth-window stale-override repair. | Blocks stale STUP side overrides; adds 3bp maker fast lock and maker-first time lock. | **REPLAY**: p50 net `+6.017865`; later windows only 66.7% WR; **TEST**: 56 policy and 4 focused maker tests passed. | **LIVE**: 12 v1.4.28 seeds include a reconciled flat-race SL net `-0.21732984`; no v1.4.29 result claimed. | Uses stored live slopes; reconciliation and broad WPR blocking remain open. | `docs/maintenance_log_2026-07-01_v1.4.29.md`; `reports/v1429_six_window_strategy_eval_live_features_2026-07-01.md` |
| v1.4.30 / 2026-07-01 | Fee-aware selective hybrid candidate. | Loss-prune rules for six state keys, fee-aware trails, raw-side preservation. | **REPLAY**: 130 fills/88.46% fee WR/net `+4.95528107`; **TEST**: 63 policy tests and chain smoke. | **CONFIG**: deployment preparation only. | Search multiplicity, thin w5, no-fill, and unavailable full maker suite limit the claim. | `docs/maintenance_log_2026-07-01_v1.4.30.md`; `reports/v1430_selective_hybrid_loss_prune_outcomefee.md` |
| v1.4.31 / 2026-07-02 | Maker-only time-lock repair. | Keeps TP/SL while time-lock maker order rests; prevents market fallback. | **TEST**: compile passed; focused pytest deferred to VM for missing local `binance`. | **LIVE**: two discount-mixed MAX_HOLD_LOSS exits net about `-0.0470` and `-0.0212`. | Short `hold_s=30` fee leakage and maker-fill efficacy remain open. | `docs/maintenance_log_2026-07-02_v1.4.31.md` |
| v1.4.32 / 2026-07-02 | Discount block and full-TP-touch lock. | Blocks discount-mixed long; adds maker-only STUP full-TP-touch lock. | **TEST**: compile and 63 policy tests passed; VM maker tests are named targets, not recorded passes. | **LIVE**: later observations include fee-thin MAX_HOLD_WIN and high-position LONG override SL. | Lock fill/defer and deep-discount repricing effectiveness are open. | `docs/maintenance_log_2026-07-02_v1.4.32.md` |
| v1.4.33 / 2026-07-02 | Forward-loss blocks and maker profit lock. | Blocks weak-chop/deep-stable shorts; 4bp to 2bp deep-stable long entry; side-flip guard. | **TEST**: post-review 65 policy and 135 maker tests passed. | **LIVE**: predecessor loop was 14 completed/10 wins/net about `+0.07696`; log does not claim deployment. | Staged repricing was telemetry only; post-change outcome is open. | `docs/maintenance_log_2026-07-02_v1.4.33.md` |
| v1.4.34 / 2026-07-02 | STUP staged-capture hotfix. | Moves v1430 full exit to 70% TP1 at 6bp with tighter runner; adds fast-floor lock. | **CONFIG**: no test or deployment result is recorded. | **LIVE**: trigger run reached about `+10.20bp` MFE then MAX_HOLD_LOSS. | Trigger motivates the change; partial-fill/lock outcomes remain open. | `docs/maintenance_log_2026-07-02_v1.4.34.md` |
| v1.4.35 / 2026-07-03 | STUP pre-TP1 capture repair. | Adds TP1 floor lock and pre-TP1 runner watch. | **TEST**: 203 tests passed, 3 warnings; VM compile/suite passed. | **HANDOFF**: deployed VM version/service active; no post-change PnL claim. | Maker-only lock can defer and leave later SL risk. | `docs/maintenance_log_2026-07-03_v1.4.35.md` |
| v1.4.36 / 2026-07-03 | Current-market fee/late-entry repair. | Blocks down-slope fast-reclaim longs, fee-unsafe max-hold closes, late STUP shorts after better veto. | **TEST**: 207 tests passed, 3 warnings. | **LIVE**: v1.4.35 trigger cases include fast-reclaim SL net `-0.0791` and gross-flat fee-negative max hold. | Targeted repair; bounded defer can still market-close. | `docs/maintenance_log_2026-07-03_v1.4.36.md` |
| v1.4.37 / 2026-07-03 | Clean-extension late-short/thin-lock repair. | Extends after-veto block; adds 50s/3.5bp maker-only thin lock. | **TEST**: local and VM suites 209 passed, 3 warnings. | **HANDOFF**: deployed version/service and no active run verified; trigger SLs had no-MFE and `+4.91bp` MFE cases. | Entry-quality failure remains distinct from exit capture. | `docs/maintenance_log_2026-07-03_v1.4.37.md` |
| v1.4.38 / 2026-07-03 | Late-fill and STUP thin-lock update. | Closes post-TTL-detected fills; adds 60s/5.5bp maker-only thin lock. | **CONFIG**: three tests added; no result or deployment record. | **LIVE**: trigger review found late detection and 5.66/6.88bp MFE SLs. | Strict-TTL/lock effectiveness and no-MFE loss treatment are open. | `docs/maintenance_log_2026-07-03_v1.4.38.md` |
| v1.4.39 / 2026-07-03 | Shadow selector and mixed/weak-chop capture. | Telemetry-only selector score plus maker-only shadow thin lock. | **CONFIG**: no test/deployment result recorded. | **unknown/open** post-change. | Score does not change admission; deferred-lock outcome needs review. | `docs/maintenance_log_2026-07-03_v1.4.39.md` |
| v1.4.40 / 2026-07-03 | VWAP feature-semantics correction. | Uses only VWAP-distance fields, never raw VWAP price, for shadow score/clean-high guard. | **CONFIG**: documented validation conditions, no pass count. | **unknown/open**. | Telemetry remains non-admission-changing; needs subsequent sample review. | `docs/maintenance_log_2026-07-03_v1.4.40.md` |
| v1.4.41 / 2026-07-03 | Research-selector mixed-state canary. | Selector telemetry plus 45s/3bp maker-only STUP mixed thin lock; other actions observational. | **REPLAY**: 466-run refresh net `-3.773428`; 7/3 holdout net `-0.488832`. | **CONFIG**: no deployment/canary result recorded. | Small holdouts, maker defers, and retained clean winners preclude a broad block. | `docs/maintenance_log_2026-07-03_v1.4.41.md`; `reports/codex_research_dataset_v14_refresh_20260703_133008.md` |
| v1.4.42 / 2026-07-03 | Entry-quality and strict-TTL promotion. | Blocks STUP clean LONG chases; caps CNL strict TTL at 20s; uses 4bp strict-row maker floor. | **CONFIG**: thresholds documented; no test/deployment result. | **LIVE**: trigger rows distinguish chase, stale fill, and max-hold fee failure. | Blocked-row counterfactual and maker/fill review remain open. | `docs/maintenance_log_2026-07-03_v1.4.42.md` |
| v1.4.43 / 2026-07-04 | Mixed-state shutdown and fee-leak scratch. | Blocks STUP mixed; adds near-flat scratches and clean-extension reversal scratch. | **TEST**: 226 tests passed, 3 warnings. | **LIVE**: 20-row review found clean thin-lock rows 4/4 winners, mixed 0/5 filled wins/net about `-0.2173`. | Scratch fallback can add fees; reversal scratch can cap recovery. | `docs/maintenance_log_2026-07-04_v1.4.43.md`; `reports/CODEX_V1_4_42_NEW_METHOD_LIVE_ANALYSIS_2026-07-04.md` |
| v1.4.44 / 2026-07-04 | Deep-discount maker trail lock. | Adds CNL deep 60s/5.5bp/4.5bp maker-only lock and lower deep-only time-lock floor. | **TEST**: 67 policy and 161 maker tests; 6 focused maker tests passed. | **HANDOFF/LIVE**: deployed version/service verified; trigger cases were `+8.19bp` TP and `+6.81bp` MFE then SL. | New lock effectiveness is not yet a filled outcome. | `docs/maintenance_log_2026-07-04_v1.4.44.md` |
| v1.4.45 / 2026-07-04 | Clean-extension short quality block. | Blocks STUP clean shorts with `rsi <= 60.8432` and `slope30 >= 1.26926bp`. | **REPLAY**: 36-row live-VM slice; blocks 8; estimated kept-net lift `+0.380368`. | **LIVE**: two trigger SLs had MFE `+3.47bp`/`+1.54bp`; no post-block result. | Aggressive small-slice gate can overblock winners; blocked rows need replay. | `docs/maintenance_log_2026-07-04_v1.4.45.md` |
| v1.4.46 / 2026-07-04 | Foreign-open-order preflight fix. | Counts only `cry3mn` orders for Codex conflict; emits ignored-order telemetry. | **CONFIG**: regression coverage described; no result/deployment record. | **LIVE**: unrelated `aos_` order caused false `open_entry_order_exists`. | Other account/order-state races are not ruled out. | `docs/maintenance_log_2026-07-04_v1.4.46.md` |
| v1.4.47 / 2026-07-04 | Observe STUP-S reversal. | Precursor to fast lock. | **CONFIG/OPEN**. | **LIVE**: `1783176458326` reached ~+8bp in ~13s, missed ~10bp TP, reversed. | MFE does not prove maker-lock fill; scratch could be fee-negative. | `docs/maintenance_log_2026-07-04_v1.4.47.md`; `docs/maintenance_log_2026-07-04_v1.4.48.md` |
| v1.4.48 / 2026-07-04 | Fee-safe STUP-S early capture. | `CODEX_V1448_STUPS_FAST_SCALP_LOCK`; stale-squeeze-top shadow; fee-safe scratch. | **TEST**: compile/focused tests. | **LIVE**: later record says 5 wins/0 losses, two fast-lock fee-free exits. | Small lane slice; maker deferred/no-fill open. | `docs/maintenance_log_2026-07-04_v1.4.48.md`; `docs/maintenance_log_2026-07-05_v1.4.49.md` |
| v1.4.49 / 2026-07-05 | CNL late-fill/quality repair. | Wider maker-first late-fill exit; falling-trap/weak-reclaim blocks. | **TEST**: compile/focused tests. | **LIVE**: 17 runs/8 fills/27.3% WR/about `-0.1575 USDC`; 3 late-TTL net-negative. | Maker fallback/deep-discount late chase open. | `docs/maintenance_log_2026-07-05_v1.4.49.md` |
| v1.4.50 / 2026-07-05 | Block CNL late chase. | Block weak rebound/upper-window exhaustion. | **TEST/CONFIG**. | **LIVE**: 13 runs, 7 completed, net `-0.1195 USDC`; deep_discount_stable `-0.1232`. | Replay blocked signals before calling avoided losses. | `docs/maintenance_log_2026-07-05_v1.4.50.md` |
| v1.4.51 / 2026-07-05 | STUP-S emergency entry block. | Block hot-short trap/weak-long chase under `SHADOW_REVIEW`. | **TEST/CONFIG**. | **LIVE**: `1783225643617`, `1783226163122` SLs. | Do not block all STUP-S. | `docs/maintenance_log_2026-07-05_v1.4.51.md` |
| v1.4.52 / 2026-07-05 | Stop late-adverse reopen. | Block wait >180s, adverse >=5bp, favorable <=1bp. | **TEST**: focused 5 passed; full policy 80 passed. | **LIVE**: `1783232351425` filled after 360s then SL. | Fresh/favorable reopens remain open. | `docs/maintenance_log_2026-07-05_v1.4.52.md` |
| v1.4.53 / 2026-07-05 | Stop high-range/high-zone reopen. | Block legacy-reopened clean-extension short. | **TEST/CONFIG**; replay required before relaxing. | **LIVE**: `1783237051530`, `1783237610286` motivated it. | Monitor signal count and MFE/MAE. | `docs/maintenance_log_2026-07-05_v1.4.53.md` |
| v1.4.54 / 2026-07-05 | Bounded recovery. | Two 50U maker DCA layers at 0.5%/0.7%; `CNL-WPR-L`/`STUP-S`; 0.50U cap. | **TEST/CONFIG**: contract/handoff. | **unknown/open**: no `recovery_entry_filled`. | Not martingale; average-entry TP sync, not independent layers. | `docs/maintenance_log_2026-07-05_v1.4.54.md`; `reports/CODEX_V1_4_54_LIVE_HANDOFF_2026-07-05.md` |
| v1.4.55 / 2026-07-10 | Archive handoff/narrowed recovery canary. | `BLOCK`, `THIN_SCALP`, `NORMAL`, `RECOVERY_CANARY`, `OBSERVE_ONLY`; block STUP-S TP14; gate-passing TP8/10 thin scalp; default recovery `CNL-WPR-L`. | **TEST**: policy/executor contracts for version, routes, payload, skip reasons, controls. | **unknown/open**: first handoff canary; no reviewed recovery fill. | Defaults are not VM proof; STUP-S recovery default-rejected; fee-safe exits open. | `docs/maintenance_log_2026-07-10_v1.4.55.md`; `src/gridbot/strategy/codex_v1_live.py`; `src/gridbot/mainnet/one_run.py`; `config/settings.py`; `tests/test_codex_v1_live_policy.py`; `tests/test_mainnet_one_run_maker.py` |
| v1.4.56 / 2026-07-10 | Telemetry-only fill evidence contract. | Adds `fill_v1` emission and reconciliation export; retains v1.4.55 strategy and risk behavior. | **TEST**: 281 focused tests passed, 3 warnings. | **HANDOFF/OPEN**: VM active/version verified; 1,776 runs and 0 `fill_v1`; no post-schema filled lifecycle reviewed. | `has_position=True` with `trades=0` is unresolved; no DCA/recovery effectiveness claim. | `src/gridbot/mainnet/fill_telemetry.py`; `scripts/export_fill_reconciliation.py`; `src/gridbot/mainnet/one_run.py`; VM backup `/home/jack_shih/cry3/.codex_deploy_backups/pre_v1456_20260710_212327/files.tgz` |

## Current Claim Register

| Claim | Status | Source pointers | Boundary |
|---|---|---|---|
| Local and VM version is `_codex_v1.4.56`. | **TEST/HANDOFF** | strategy source; policy test; VM runtime snapshot | Version/service health is not filled-trade proof. |
| v1.4.56 records/exports `fill_v1` without changing strategy policy. | **TEST/CONFIG** | `src/gridbot/mainnet/fill_telemetry.py`; exporter and one-run tests | 0 VM events means the live lifecycle is not yet validated. |
| `v1455_stups_clean_extension_tp14_block` blocks STUP-S clean-extension TP14. | **TEST/CONFIG** | strategy source; policy tests | Does not prove PnL of block. |
| Gate-accepted TP8/TP10 STUP-S clean-extension may pass as `THIN_SCALP`. | **TEST/CONFIG** | strategy source; policy tests | Selective policy, not validated module. |
| Runtime payload includes DCA/recovery enable and block metadata. | **TEST/CONFIG** | `src/gridbot/mainnet/one_run.py`; maker tests | Payload is not fill proof. |
| Default recovery allowlist is `CNL-WPR-L`; STUP-S is rejected by default. | **CONFIG/TEST** | `config/settings.py`; tests | VM overrides may differ. |
| Recovery blocks are event/log-visible for cap, drift, partial exit, layers, runtime, allowlist. | **TEST/CONFIG** | executor source/tests | Actual event visibility remains open. |
| `scripts/review_runs.py` prints version/lane/state/action/exit/net PnL/DCA counts. | **CONFIG** | `scripts/review_runs.py` | Requires valid run data. |

## Open Register

| Item | Status | Closure evidence |
|---|---|---|
| DCA/recovery effectiveness | **OPEN** | `recovery_entry_filled`, order events, exit reason, net-after-fee result. |
| Independent per-layer DCA exits | **OPEN** | Implementation plus executor/order-event tests. |
| STUP-S recovery | **OPEN** | Separate LIVE and actual-hold-window REPLAY proof before re-enable. |
| STUP-S mixed cohort | **OPEN** | Larger fee-aware sample; v1.4.42 was 0/5 filled net wins. |
| Fee-safe maker exits | **OPEN** | Fill/cancel/defer events, commissions, net PnL. |
| Checked-in versus running VM config | **OPEN** | Deployed manifest, service health, runtime banner, first-loop review. |
| v1.4.56 live fill contract | **OPEN** | A post-schema `fill_v1` entry/exit lifecycle reconciled to exchange trades and commissions. |
| Runtime position ownership | **OPEN** | Resolve `has_position=True` / `trades=0` to account, environment, symbol, side, quantity, and order/trade identity. |
| Qlib baseline | **REPLAY / REJECTED** | Current baseline failed the research acceptance threshold; new labels/experiment lineage are required before reconsideration. |

## Evidence Rules

1. Every claim needs a code/test line, maintenance log, report, DB query, tick replay, or VM deployment record.
2. A **LIVE** conclusion needs net PnL after fees, exit reason, lane/state/action, and actual fill state.
3. Classify blocked/expired signals only after tick/aggTrade replay: missed win, avoided loss, neutral, or insufficient window.
4. Compare strategies with net PnL, worst loss, MFE/MAE, fees, no-fill rate, and sample size, not win rate alone.
5. Never promote **CONFIG** or **REPLAY** into a live DCA claim without `recovery_entry_filled` and final exit outcome.

## Migration Implication

Use this repository as the historical record. A clean project should first reproduce these fixtures, failure paths, fee accounting, and replay assumptions before searching for new alpha.

## v1.4.56a Telemetry Hardening Verification - 2026-07-10

- **TEST**: 281 focused tests passed with 3 dependency deprecation warnings.
- **DEPLOYED**: incremental and restart-idempotent fill_v1 sync is active on cry3jack; runtime remains _codex_v1.4.56.
- **BACKUP**: .codex_deploy_backups/pre_v1456a_20260710/files.tgz, SHA-256 b21d788c2857c54588d3f4da62c471b5186fd38401572cbc58fc48c88af1a149.
- **RECONCILIATION**: explicit schema cutoff classifies all 1,776 retained runs as PRE_SCHEMA; there are 0 post-schema fill events and no missing/ambiguous claim yet.
- **POSITION OWNERSHIP**: mainnet is FLAT with no active run or open orders. The earlier has_position=True log belongs to testnet: external/manual SHORT 0.043 ETHUSDC with no bot order or recent trade identity.
- **GATE**: do not close or adopt the external testnet position. Testnet lifecycle validation and mainnet canary remain unarmed until a clean symbol/account state is available.

## v1.4.56a Testnet Exchange Lifecycle - 2026-07-10

- **TESTNET EXCHANGE**: clean `BTCUSDC` probe `tprobe_1783696606` completed an actual 0.002 BTC entry and exit and ended flat.
- **FILL CONTRACT**: entry `fill_key=77551836:774923033`; exit `fill_key=77551837:774923064`; roles were `entry` and `exit`.
- **ACCOUNTING**: gross realized PnL `-0.08219999 USDC`, commission `0.11524383 USDC`, observed net `-0.19744382 USDC`.
- **IDEMPOTENCY**: first sync emitted 2 events and immediate second sync emitted 0.
- **BOUNDARY**: this validates telemetry against real testnet exchange fills. It is not mainnet, strategy-profit, or recovery/DCA effectiveness evidence.
- **SOURCE**: `reports/V1456A_FILL_V1_TESTNET_LIFECYCLE_2026-07-10.md`; `scripts/run_fill_v1_testnet_probe.py`.
## v1.4.56b Mainnet Margin Preflight Incident - 2026-07-10

- **LIVE/FAILED-NO-FILL**: `cry3mn_1783697000031` accepted `CNL-WPR-L:deep_discount_stable` at 50 USDC but Binance rejected entry with `-2019 Margin is insufficient`.
- **ACCOUNTING**: no order/fill/position/commission; net PnL `0`; `fill_v1=0`; `recovery_entry_filled=0`.
- **ROOT CAUSE**: 0.4234 USDC could not fund the 150 USDC / 75x recovery basket; arm preflight did not check margin capacity.
- **FIX/TEST**: basket margin preflight with 5% buffer deployed; 264 tests passed, 3 warnings; service active.
- **GATE**: do not re-arm until `USDC availableBalance >= 2.1000`; recommend at least 3 USDC.
- **SOURCE**: `reports/V1456B_MARGIN_PREFLIGHT_INCIDENT_2026-07-10.md`.
## v1.4.56 Mainnet Filled Canary - 2026-07-10

- **LIVE**: `cry3mn_1783699350027`, STUP-S clean-extension `THIN_SCALP` TP10, maker entry/exit, completed `TP`.
- **ACCOUNTING**: 0.028 ETH SHORT, 50.14352 USDC entry quote, realized/net `+0.05012 USDC`, commission `0`.
- **FILL CONTRACT**: two `fill_v1` events; reconciliation v3 `OBSERVED_COMPLETE`; no missing/ambiguous/anomaly rows after cutoff.
- **RECOVERY**: no recovery fill. Trend guard emitted `dca_guard_blocked` and `guard_permanent` skips.
- **CONFIG INCIDENT**: runtime env still allowed STUP-S recovery; corrected and verified as CNL-WPR-L only with 0.50 USDC basket cap.
- **HARDENING**: full-size TP1 now becomes `final_exit`; terminal no-order runs become `OBSERVED_NO_FILL`.
- **SOURCE**: `reports/V1456_MAINNET_CANARY_LIFECYCLE_2026-07-10.md`.