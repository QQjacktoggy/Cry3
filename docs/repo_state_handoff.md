# Repo State Handoff

Updated: 2026-06-05

This repo is now organized into separate handoff lines so GitHub, local work, and the GCP VM runtime do not get mixed together.

## Branch layout

- `origin/main`
  - Current shared baseline.
  - Head: `fae0820 Add testnet order clearing script`
- `wildcat/research-handoff`
  - Latest local wildcat research line.
  - Head: `4cc9b01 Document wildcat strategy handoff`
  - Includes [docs/wildcat_handoff.md](/C:/Users/jack_shih/Desktop/cry3/docs/wildcat_handoff.md)
- `vm-runtime-handoff`
  - New branch created from `origin/main`.
  - Stores the latest exported VM runtime state as patch artifacts and notes.
  - Does not merge wildcat research into the VM handoff by default.

## Why it is split this way

The GCP VM was newer at runtime than GitHub `main`, but those VM changes were not committed in the VM worktree. The safest way to preserve that state was to export the VM diff and track it in a separate branch without forcing an unreviewed merge into `main`.

## VM runtime export

The latest VM runtime export is checked in under:

- [handoff/vm_runtime/cry3_vm_worktree.patch](/C:/Users/jack_shih/Desktop/cry3/handoff/vm_runtime/cry3_vm_worktree.patch)
- [handoff/vm_runtime/cry3_vm_status.txt](/C:/Users/jack_shih/Desktop/cry3/handoff/vm_runtime/cry3_vm_status.txt)
- [handoff/vm_runtime/cry3_vm_diffstat.txt](/C:/Users/jack_shih/Desktop/cry3/handoff/vm_runtime/cry3_vm_diffstat.txt)
- [handoff/vm_runtime/README.md](/C:/Users/jack_shih/Desktop/cry3/handoff/vm_runtime/README.md)

These files were exported from the VM on 2026-06-05.

## Snapshot summary

- VM instance observed: `cry3jack`
- VM external IP at inspection time: `34.80.75.138`
- VM repo HEAD at inspection time: `fae0820`
- VM had significant uncommitted runtime changes across Binance, strategy, Telegram, testnet, and tests
- Local `main` is ahead of `origin/main` because it includes the wildcat handoff commit
- `tmp_remote/` remains untracked locally on the wildcat branch and was intentionally left out of commits

## Recommended use

Use the branches like this:

- For wildcat strategy research: start from `wildcat/research-handoff`
- For recovering or reviewing the latest VM runtime behavior: start from `vm-runtime-handoff`
- Do not merge either branch into `main` until the contents are reviewed and intentionally reconciled

## Suggested next step

If we want a true unified branch later, the clean path is:

1. Review or selectively apply `handoff/vm_runtime/cry3_vm_worktree.patch`
2. Reconcile conflicts with `wildcat/research-handoff`
3. Commit the integrated runtime branch
4. Only then consider merging to `main`
