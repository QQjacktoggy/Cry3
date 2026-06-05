# VM Runtime Export

Updated: 2026-06-05

This folder captures the latest known runtime state from the GCP VM worktree without directly rewriting local source files.

## Files

- `cry3_vm_worktree.patch`
  - Full exported diff of the VM worktree against VM `HEAD`
- `cry3_vm_status.txt`
  - `git status --short` output from the VM repo
- `cry3_vm_diffstat.txt`
  - `git diff --stat` output from the VM repo

## What this represents

At the time of export:

- VM repo HEAD matched `origin/main` at `fae0820`
- The VM had many uncommitted changes and new files
- The runtime behavior on the VM was therefore newer than GitHub `main`

This folder preserves that state for review, cherry-picking, or replay into a new branch.

## Notes

- This export is archival and review-oriented
- It does not guarantee that every VM-local file should be merged as-is
- Secrets were not added to git here; this branch only stores patch and status artifacts
