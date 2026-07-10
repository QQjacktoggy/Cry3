# Strategy Rulebook Current

Generated: 2026-07-09 TPE

Scope: current local rule surface for Codex v1.4.55 and mainnet one-run recovery behavior. This is not a strategy thesis; it is an implementation contract for operators and future developers.

## Version

- `CODEX_V1_VERSION = "_codex_v1.4.55"`
- Source: `src/gridbot/strategy/codex_v1_live.py`
- Policy test: `tests/test_codex_v1_live_policy.py::test_codex_v1_version_is_pinned`

## v1.4.55 Adaptive Route

Route labels:

- `BLOCK`: reject route before live execution.
- `THIN_SCALP`: allow only the narrow fast scalp profile that passes gate.
- `NORMAL`: ordinary accepted route outside special v1.4.55 handling.
- `RECOVERY_CANARY`: route is eligible for recovery whitelist handling.
- `OBSERVE_ONLY`: not live-accepted; keep telemetry only.

Current route rules:

| Condition | Route | Reason |
|---|---|---|
| `lane_code == STUP-S`, `state == STUP-S:clean_extension`, action TP >= 14bp | `BLOCK` | `stups_clean_extension_tp14_loss_guard` |
| `lane_code == STUP-S`, `state == STUP-S:clean_extension`, action TP <= 10bp | `THIN_SCALP` | `stups_clean_extension_tp8_tp10_gate_pass` |
| `lane_code == CNL-WPR-L` | `RECOVERY_CANARY` | `codex_recovery_lane_whitelist_canary` |
| non-live accepted / shadow | `OBSERVE_ONLY` | `not_live_accepted` |

Main v1.4.55 tags:

- `V1455_STUPS_CLEAN_EXTENSION_TP14_BLOCK_TAG = "v1455_stups_clean_extension_tp14_block"`
- `V1455_ADAPTIVE_ROUTE_TAG = "v1455_adaptive_route"`
- `V1455_THIN_SCALP_ROUTE_TP_MAX_BP = 10.0`
- `V1455_BLOCKED_TP_MIN_BP = 14.0`

Expected metrics on accepted or blocked decisions:

- `v1455_route`
- `v1455_route_reason`
- `v1455_action`
- `v1455_action_tp_bp`
- `v1455_policy_tag`

## STUP-S Contract

Hard block:

- `STUP-S:clean_extension` TP14 route must be blocked with `v1455_stups_clean_extension_tp14_block`.

Allowed narrow path:

- TP8/TP10 clean_extension route can remain live when upstream gates accept it.
- This should be read as "thin scalp canary", not broad STUP-S confidence.

Recovery:

- STUP-S recovery is rejected by default in v1.4.55 because `mainnet_codex_recovery_lane_codes` defaults to `CNL-WPR-L`.
- Expected skip reason: `codex_recovery_lane_not_whitelisted`.

## CNL-WPR-L Contract

- CNL-WPR-L is the initial Codex recovery canary lane.
- It is allowed by `mainnet_codex_recovery_lane_codes = "CNL-WPR-L"`.
- Basket cap remains `mainnet_codex_recovery_max_basket_loss_usdc = 0.50`.
- If recovery fills, review the full sequence: base entry, DCA placement, `recovery_entry_filled`, average entry change, TP sync, final exit, net PnL.

## Runtime Recovery Payload

Before writing the signal payload, `one_run.py` loads runtime config and records:

- `runtime_dca_enabled`
- `codex_recovery_allowed`
- `effective_recovery_enabled`
- `recovery_block_reason`

These fields are written at both the top signal layer and under `codex_v1` / `wildcat` structures where relevant, so review tooling can diagnose whether recovery was disabled by runtime config, settings, or lane whitelist.

Block reasons:

| Reason | Meaning |
|---|---|
| `mainnet_recovery_disabled` | settings-level mainnet recovery is off. |
| `runtime_dca_disabled` | app_config / Telegram runtime DCA is off. |
| `codex_recovery_lane_not_whitelisted` | lane not in `mainnet_codex_recovery_lane_codes`. |
| `None` | no config/whitelist block at payload time. |

## Recovery Skip Events

All recovery skips should emit `recovery_skipped` with details. Important reasons:

- `mainnet_recovery_disabled`
- `runtime_dca_disabled`
- `codex_recovery_lane_not_whitelisted`
- `basket_loss_cap`
- `max_layers_reached`
- `partial_exit`
- `guard_permanent`
- `guard_cooldown`
- `max_cumulative_notional_cap`
- `preloaded_order_active`
- `dca_guard_blocked`
- `drift_gate`
- `open_dca_order_exists`
- `order_failed`

Review rule: a skipped recovery is not automatically a bug. It is a bug only if the skip reason contradicts the configured contract or no event/log appears.

## Loop Guardrails

- Ticket size for first v1.4.55 live batch: 50 USDC.
- Loop loss cap: 2 USDC.
- If `STUP-S:clean_extension` produces two filled net losses first, stop loop review.
- If no `recovery_entry_filled` occurred, write "DCA capability enabled" or "recovery canary armed"; do not write "DCA works".

## Tests

Required focused verification:

```bash
python -m pytest tests/test_codex_v1_live_policy.py tests/test_mainnet_one_run_maker.py -q
```

Current expected coverage:

- Version is `_codex_v1.4.55`.
- Clean-extension TP14 route is blocked.
- TP8/TP10 thin scalp route remains allowed when gate passes.
- DCA runtime config reaches payload.
- CNL-WPR-L can enter recovery canary.
- STUP-S recovery is rejected by default with reason.
- Basket cap, drift gate, partial exit, and max-layer skips are event-visible.

## Operator Review Output

Use:

```bash
python scripts/review_runs.py
```

Expected output fields include:

- version
- lane
- state
- action
- exit reason
- net PnL
- DCA/recovery placed/filled/skip counts
- recovery block reason

## Do Not Assume

- Do not assume VM runtime settings equal local defaults.
- Do not assume an accepted route filled.
- Do not assume DCA helped unless recovery filled and final exit is reviewed.
- Do not assume STUP-S thin scalp evidence transfers to STUP-S TP14 or STUP-S mixed.
- Do not assume old reports are clean git history; many evidence files are currently untracked archive material.
