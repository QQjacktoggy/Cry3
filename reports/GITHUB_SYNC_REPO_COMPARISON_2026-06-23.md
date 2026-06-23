# GitHub Sync And Repo Comparison

Date: 2026-06-23 Asia/Taipei

## Sources Compared

- GitHub `origin/main`: `375a070bbcbd44a07c1f9d87647c540390db7ddc`
- Local `main`: `375a070bbcbd44a07c1f9d87647c540390db7ddc`
- Live VM `/home/jack_shih/cry3`: `df39aba0f2b5bea1a28ab4b9655eab8c0e97ae5a` plus dirty live patches
- Live runtime version: `_codex_v1.3.8_w6a_tp6_conservative_trail`

## Current State

GitHub and local `main` point to the same commit, but local worktree is not clean. Local dirty count is about 430 status entries. Live VM is also not clean, with about 217 status entries.

The live VM currently runs v1.3.8, but that version is not represented as a Git commit. It exists as dirty files on top of an older VM base commit.

## Important Finding

`origin/main` does not contain the Codex v1 live integration surface (`codex_v1` grep returned no tracked hits under `config`, `src`, or `tests`). Therefore, updating GitHub to "current live" is not a small v1.3.8 version bump.

The clean-worktree comparison was built from `origin/main`, then the six v1.3.8 live candidate files were copied from live VM:

- `config/settings.py`
- `src/gridbot/mainnet/one_run.py`
- `src/gridbot/strategy/codex_v1_live.py`
- `tests/test_codex_v1_live_policy.py`
- `tests/test_mainnet_one_run_maker.py`
- `reports/CODEX_V1_3_8_W6A_LIVE_IMPLEMENTATION_PLAN_2026-06-23.md`

The tracked-file diff alone is large:

- `config/settings.py`: `+220 / -2`
- `src/gridbot/mainnet/one_run.py`: `+5082 / -198`
- `tests/test_mainnet_one_run_maker.py`: `+2549 / -9`

New files copied from live:

- `src/gridbot/strategy/codex_v1_live.py`: 1618 lines
- `tests/test_codex_v1_live_policy.py`: 359 lines
- `reports/CODEX_V1_3_8_W6A_LIVE_IMPLEMENTATION_PLAN_2026-06-23.md`: 90 lines

This means a direct live-to-GitHub copy would be roughly 9,900 inserted lines and 209 deleted lines in the current comparison. That is too large to treat as a safe version bump.

## Recommendation

Do not push the current dirty local worktree.

Do not directly overwrite GitHub `main` with live VM files.

Do not open a new repo yet. A new repo would avoid the dirty history, but it would also split deployment docs, tests, reports, VM scripts, and existing issue/commit context. The better first move is to cleanly publish the current live system into the existing repo through a dedicated sync branch and staged PRs.

Recommended path:

1. Create a clean branch from `origin/main`, for example `sync/live-codex-v1-3-8`.
2. PR 1: add the missing Codex v1 live integration files and tests.
3. PR 2: integrate the mainnet one-run/settings changes required by current live.
4. PR 3: add v1.3.8 W6A report/runbook and any deploy notes.
5. After those pass tests and review, fast-forward or merge into `main`.
6. Only after that, evaluate whether a new repo is still needed.

## When A New Repo Would Make Sense

Open a new repo only if the goal is to archive this repo and restart with a smaller production-only surface:

- production bot code only
- no old backtest/report bulk
- no large experimental JSON outputs
- clean deployment docs
- clean CI/test surface

That is a larger migration. It should not be mixed with the urgent task of getting GitHub to reflect the current live bot.

## Practical Decision

Best next action: keep the current GitHub repo, but publish current live through a clean branch/PR sequence. Treat live VM as the source of truth for v1.3.8 behavior, and treat `origin/main` as the commit base that must not be blindly overwritten.

## Execution Result

Implemented through a clean temporary worktree at `C:\tmp\cry3-v138-sync-eval`.

- Branch pushed: `sync/live-codex-v1-3-8`
- Draft PR: https://github.com/QQjacktoggy/Cry3/pull/6
- PR base: `main`
- PR head: `sync/live-codex-v1-3-8`
- Final branch commits:
  - `fb200c6` `sync: add codex v1 live policy`
  - `422d9ca` `sync: integrate live codex v1.3.8 one-run`
  - `3128b3f` `docs: record live v1.3.8 github sync`
  - `b4ee21b` `sync: add codex tp policy shadow`

Validation from the clean worktree:

```powershell
C:\Users\pipi\Desktop\cry3\.venv\Scripts\python.exe -m py_compile config/settings.py src/gridbot/strategy/codex_v1_live.py src/gridbot/mainnet/tp_policy_shadow.py src/gridbot/mainnet/one_run.py tests/test_codex_v1_live_policy.py tests/test_codex_tp_policy_shadow.py tests/test_mainnet_one_run_maker.py
C:\Users\pipi\Desktop\cry3\.venv\Scripts\python.exe -m pytest -q tests/test_codex_v1_live_policy.py tests/test_codex_tp_policy_shadow.py tests/test_mainnet_one_run_maker.py
```

Result: `105 passed, 3 warnings`.
