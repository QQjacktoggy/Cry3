# v1.4.56 Mainnet Canary Lifecycle - 2026-07-10

## Final Outcome

- Run: `cry3mn_1783699350027`
- Version: `_codex_v1.4.56`
- Lane/state: `STUP-S:clean_extension`
- Route/action: `THIN_SCALP` / `S_E2_TP10_SL8_T90_LOCK90_6_0`
- Side: SHORT
- Entry: maker SELL 0.028 ETH at 1790.84, quote 50.14352 USDC
- Exit: maker BUY 0.028 ETH at 1789.05, quote 50.09340 USDC
- Exit reason: `TP`
- Realized PnL: `+0.05012 USDC`
- Commission: `0 USDC`
- Net PnL: `+0.05012 USDC`
- Final exchange state: flat, no open orders, no active run

## fill_v1 Evidence

- Entry fill key: `814618862:74796644238`, role `entry`, maker.
- Exit fill key: `814620751:74796859641`, historically emitted role `partial_exit`, maker. The TP1 quantity equaled the full 0.028 position and terminalized the run; the immutable historical event is retained as-is.
- Reconciliation v3 classifies the run `OBSERVED_COMPLETE` with two fills, net `+0.05012`, and no anomaly.
- Reconciliation artifact SHA-256: `0c4f3016358afd555dac80652c4f780fbcd3273bfe9a41a0dbc680a8e7437b43`.

## Recovery Evidence

- No `recovery_entry_filled` event occurred.
- Two `recovery_skipped` events occurred: `dca_guard_blocked` followed by `guard_permanent`; the guard identified an up-trend against the SHORT.
- The VM environment unexpectedly allowed `STUP-S` recovery even though the checked-in default was CNL-only. No recovery order was placed because the independent trend guard blocked it.
- Runtime override was corrected from `CNL-WPR-L,STUP-S` to `CNL-WPR-L`.
- Post-restart settings: recovery enabled, lane allowlist `CNL-WPR-L`, basket loss cap `0.50 USDC`.
- Environment backup SHA-256: `87757fb3a3d727b8d555feace6095699ac6cbb152ec5392daa2a0b119fe6242f`.

## Follow-up Hardening

- Full-size TP1 telemetry now promotes the final fill to `final_exit` using observed open quantity. Incremental/restart sync rebuilds quantity from all exchange fills before classifying new fills.
- Fill-role deployment SHA-256: `7dd5ebdaeb1bcb9f1a0de2ccfa94f781b44bbc54f5768425497fd392b089b5a3`.
- Fill-role VM backup SHA-256: `4574d489bab85456974b0eb06d7436d2a30e6fdf2f72a5f9048cf7942f7e7751`.
- Reconciliation v3 adds `OBSERVED_NO_FILL`, so the earlier margin-rejected canary is not misreported as missing fill evidence.
- Exporter deployment SHA-256: `b0b753ef77b6ab1caa9d10b1e76019b0d836dae3cf970df749354a73586445ea`.
- Exporter VM backup SHA-256: `b8a9e289408bab653bcdf7203fc1fad84c9bbff86253694827ddd3da2ccb179a`.
- Focused telemetry/reconciliation suite: 19 passed, 3 dependency warnings.

## Claim Boundary

This is one successful TP8/TP10 thin-scalp lifecycle and proves the mainnet fill_v1 entry/exit contract. It does not prove DCA/recovery effectiveness, because no recovery entry filled. Do not widen the recovery allowlist based on this run.