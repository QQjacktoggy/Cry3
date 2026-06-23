# Codex V1.3.8 W6A Live Implementation Plan

Date: 2026-06-23 Asia/Taipei
Target: live VM `34.80.75.138`, repo `/home/jack_shih/cry3`

## Starting Conclusion

The W6A backtest VM run points to the canonical W6A entry path with `entry_bp=0`, `ttl_bars=20`, `partial_tp_pct=0.0006`, strict risk blocks, and no extra fast-trail tightening as the safest live candidate.

Fee drag is real, but the live blocker is not solved by fee tuning alone. The more important mismatch is live exit behavior: v1.3.7E2 uses `partial_tp_pct=0.0005` through the global live TP helper, and W6A fast trail arms earlier through a 3.5bp cap plus a 1s watcher.

## Change Scope

1. Pin live Codex version to `_codex_v1.3.8_w6a_tp6_conservative_trail`.
2. Add v1.3.8 W6A settings:
   - `mainnet_codex_v138_w6a_partial_tp_pct = 0.0006`
   - `mainnet_codex_v138_w6a_fast_trail_enabled = False`
   - keep W6A risk score, stale action, and notional caps unchanged from v1.3.7E.
3. When Codex accepts W6A, override only the decision `partial_tp_pct` to 0.0006 before run payload creation.
4. When TP orders are rebuilt, prefer `signal_json.wildcat.partial_tp_pct` over the global `mainnet_partial_tp_pct`.
5. Make v1.3.8 W6A trail use generic trail arm/watch defaults unless explicitly enabled by the new v1.3.8 setting.
6. Keep 300 USDC sizing out of live for this release. It remains a follow-up canary only after live overlay replay passes.

## Verification Before Restart

Run targeted tests on live VM:

```bash
testnet/.venv/bin/python -m pytest -q tests/test_codex_v1_live_policy.py tests/test_mainnet_one_run_maker.py -k "version_is_pinned or w6a_fast_trail or w6a_partial_tp or run_running_dca_shrinks"
```

Also run a syntax check:

```bash
testnet/.venv/bin/python -m py_compile config/settings.py src/gridbot/strategy/codex_v1_live.py src/gridbot/mainnet/one_run.py
```

## Deployment Steps

1. Backup modified files under `backups/v138_w6a_live_<timestamp>/`.
2. Apply the v1.3.8 patch to settings, live policy, one-run manager, and targeted tests.
3. Run the verification commands above.
4. Restart `cry3.service`.
5. Confirm `CODEX_V1_VERSION`, service status, and recent log lines.

## Rollback

Restore the backup copies and restart `cry3.service`:

```bash
cp backups/v138_w6a_live_<timestamp>/settings.py config/settings.py
cp backups/v138_w6a_live_<timestamp>/codex_v1_live.py src/gridbot/strategy/codex_v1_live.py
cp backups/v138_w6a_live_<timestamp>/one_run.py src/gridbot/mainnet/one_run.py
cp backups/v138_w6a_live_<timestamp>/test_codex_v1_live_policy.py tests/test_codex_v1_live_policy.py
cp backups/v138_w6a_live_<timestamp>/test_mainnet_one_run_maker.py tests/test_mainnet_one_run_maker.py
sudo systemctl restart cry3.service
```

## Result Log

Completed on live VM.

- Backup directory: `/home/jack_shih/cry3/backups/v138_w6a_live_20260623_020404`
- Patched files:
  - `config/settings.py`
  - `src/gridbot/strategy/codex_v1_live.py`
  - `src/gridbot/mainnet/one_run.py`
  - `tests/test_codex_v1_live_policy.py`
  - `tests/test_mainnet_one_run_maker.py`
- Version after patch: `_codex_v1.3.8_w6a_tp6_conservative_trail`
- W6A v1.3.8 settings after import:
  - `mainnet_codex_v138_w6a_partial_tp_pct = 0.0006`
  - `mainnet_codex_v138_w6a_fast_trail_enabled = False`
- Verification:
  - `py_compile` passed for patched Python files.
  - Targeted pytest: `3 passed, 91 deselected`.
  - Explicit TP6 pytest: `2 passed, 78 deselected`.
  - Full relevant suite: `94 passed, 3 warnings`.
- Restart:
  - `sudo systemctl restart cry3.service`
  - service active after restart, PID `1992554`
  - post-restart status check at 57s: active
  - journal since restart had no `traceback`, `exception`, `error`, or `failed` lines.

Decision notes:

- 300 USDC sizing was not enabled in v1.3.8. It remains blocked until live-overlay replay/canary evidence supports it.
- v1.3.7E W6A stale/risk-score/no-bounce risk tree remains active.
- W6A fast trail can still be explicitly enabled by `mainnet_codex_v138_w6a_fast_trail_enabled`, but default live behavior is conservative and uses the generic trail arm/watch path.

