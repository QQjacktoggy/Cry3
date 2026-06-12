# V6.7 Live Analysis - 2026-06-12

> Timezone: Asia/Taipei. Source: VM `/home/jack_shih/cry3/CLAUDE.md`, `cry3.service`, and `/home/jack_shih/testnet/data/gridbot_testnet.db`.

## Current Runtime

- Service: `cry3.service`
- Runtime observed: active since `2026-06-12 00:23:43 UTC` / `2026-06-12 08:23:43 TPE`
- Working tree: VM code contains V6.7 gate logic, but `CLAUDE.md` does not yet have a formal V6.7 release section.
- Main observed V6.7 settings:
  - `mainnet_entry_drift_gate_bp = 50.0`
  - `mainnet_entry_velocity_gate_bp = 15.0`
  - `mainnet_entry_limit_offset = 0.0003`
  - `mainnet_dca_enabled = false`
  - `mainnet_notional_usdc = 300`
  - `mainnet_loop_loss_cap_usdc = 2`

## V6.7 Code Behavior Observed

V6.7 has two entry-level gates before the existing rng15 filter:

1. `entry_drift_skipped`
   - Blocks first entry when `abs(drift30) > mainnet_entry_drift_gate_bp`.
   - Current live gate is `50bp`, not the earlier V6.6.6 candidate value of `30bp`.
   - This means V6.7 is intentionally less aggressive than the candidate note.

2. `entry_velocity_skipped`
   - Blocks entry when the last 3 completed 1m bars moved adversely by at least `15bp`.
   - This is the more active new V6.7 guard in the current sample.

## Trades Since V6.7 Restart

Window: `2026-06-12 08:23:43 TPE` onward.

Summary:

- Total runs: `8`
- Completed: `5`
- Entry expired: `2`
- Current active: `1 ARMED`
- Completed win rate: `5/5 = 100%`
- Gross PnL: `+0.580140 USDC`
- Fees: `0.071551 USDC`
- Net PnL: `+0.508589 USDC`
- Avg net per completed trade: `+0.101718 USDC`

Completed exits:

| Exit | Count | Gross | Fees | Net |
|---|---:|---:|---:|---:|
| TRAIL | 4 | +0.468420 | 0.071551 | +0.396869 |
| flat_detected | 1 | +0.111720 | 0.000000 | +0.111720 |

Run list:

| Run | Status | Side | Exit | Net | rng15 | drift30 | tp_pct | DCA |
|---|---|---|---|---:|---:|---:|---:|---|
| `cry3mn_1781224002321` | COMPLETED | LONG | TRAIL | +0.091440 | 29.72 | -17.55 | 0.000600 | off |
| `cry3mn_1781224079692` | COMPLETED | LONG | TRAIL | +0.055269 | 41.58 | +1.20 | 0.000720 | off |
| `cry3mn_1781224400070` | COMPLETED | LONG | TRAIL | +0.073390 | 37.31 | +24.28 | 0.000600 | off |
| `cry3mn_1781224460120` | COMPLETED | LONG | TRAIL | +0.176770 | 37.30 | +21.70 | 0.000600 | off |
| `cry3mn_1781224669800` | ENTRY_EXPIRED | SHORT | entry_ttl_expired | 0.000000 | 40.12 | -0.90 | 0.000800 | off |
| `cry3mn_1781224939179` | COMPLETED | LONG | flat_detected | +0.111720 | 47.86 | +6.15 | 0.000648 | off |
| `cry3mn_1781225519961` | ENTRY_EXPIRED | LONG | entry_ttl_expired | 0.000000 | 23.55 | +15.36 | 0.000648 | off |
| `cry3mn_1781225759168` | ARMED | pending | pending | 0.000000 | n/a | n/a | n/a | n/a |

Event counts:

| Event | Count |
|---|---:|
| `take_profit_synced` | 10 |
| `armed` | 8 |
| `entry_placed` | 7 |
| `partial_exit` | 6 |
| `completed` | 5 |
| `entry_filled` | 5 |
| `sl_placed` | 5 |
| `tp_ladder_adjusted` | 4 |
| `trail_maker_placed` | 4 |
| `trail_maker_filled` | 3 |
| `entry_trend_skipped` | 2 |
| `entry_rng15_low_skipped` | 1 |
| `entry_velocity_skipped` | 1 |
| `trail_maker_place_failed` | 1 |

## Notable Events

- `cry3mn_1781224079692` had one `trail_maker_place_failed`:
  - Error: `GTX entry retries exhausted (3 attempts, fallback disabled)`.
  - The run still completed positive, but this is worth monitoring because it means the trail maker placement can miss under maker-only constraints.

- Current active run `cry3mn_1781225759168` has already seen guards fire:
  - `entry_rng15_low_skipped`: `rng15 = 17.45`
  - `entry_velocity_skipped`: LONG signal skipped with `adv_vel3_bp = 17.57`, gate `15bp`
  - `entry_trend_skipped`: LONG skipped in downtrend

## Analysis

V6.7 is behaving more conservatively than V6.6.5:

- DCA is off, so the previous tail-loss pattern from DCA expansion is not present.
- Completed trades are all positive so far.
- The new 3-bar adverse velocity gate is active and has already blocked one candidate entry.
- The drift gate is set to `50bp`, so it will only catch extreme directional tape. This is consistent with the setting comment that `30bp` over-blocked good trades in the later sample.

The early read is constructive, but the sample is still too small:

- Only 5 completed trades after restart.
- No SL yet, so we cannot conclude the new gate has solved tail loss.
- The current market tape is favorable for LONG reversion/trailing exits, so results may be regime-dependent.

## Recommendations

1. Keep V6.7 running longer before changing thresholds.
2. Do not lower `mainnet_entry_drift_gate_bp` from `50` back to `30` yet; the VM code comments indicate `30bp` over-blocked.
3. Monitor `entry_velocity_skipped` counterfactuals. This is the real V6.7 behavior to validate.
4. Keep #30 as a P2/P1 observability issue, not a confirmed long TP gap incident.
5. Add a formal V6.7 section to `CLAUDE.md`; right now the file documents V6.6.6 candidate analysis and #30, but not the actual V6.7 release state.

## #30 Correction Summary

The earlier #30 note was too severe in one place.

Confirmed:

- TP post-only `-5022` rejects happen.
- Partial fill TP re-sync can feel slow because client retry waits `2s + 4s`.
- `take_profit_synced` should record actual placed orders more clearly.

Not confirmed:

- `cry3mn_1781168414408` should not be described as a confirmed 73-second no-TP window.
- Event history shows additional partial fills during that period, so TP/re-quote likely succeeded in between.

Corrected severity:

- Treat #30 as a P2/P1 boundary issue around observability and re-sync speed, not as a confirmed P1 long TP-protection outage.
