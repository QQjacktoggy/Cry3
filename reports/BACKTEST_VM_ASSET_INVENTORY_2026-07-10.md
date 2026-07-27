# Backtest VM Asset Inventory - 2026-07-10

## Verified environment

| Field | Value |
|---|---|
| VM | `hermesjacktoggy` (`34.80.52.35`) |
| User | `jack_shih` |
| Working tree | `/home/jack_shih/cry3` |
| Git metadata | No `.git` directory is present |
| Python | `3.12.3` |
| Report footprint | `reports` reported as `271M` |

The missing `.git` directory means the VM tree does not by itself identify a commit or prove code reproducibility.

## Retention tiers

- **Tier A:** core raw inputs and the integrity-checked preservation archive. Keep at least one durable copy outside the VM.
- **Tier B:** machine-readable research datasets, selectors, and candidates needed to reproduce research decisions.
- **Tier C:** derived narrative summaries and redundant presentation formats that can normally be regenerated from Tier A/B material.

## Tier A - core raw assets

| Asset | Size (bytes) | Classification |
|---|---:|---|
| `codex_v14_subset_current_20260703_133008.db` | 175,800,320 | Canonical core database |
| v1419 aggTrades | 129,410,682 | Raw replay input |
| v1420 three-window data | 138,635,384 | Raw replay input |
| v1430 w1 data | 42,421,894 | Raw replay window |
| v1430 w2 data | 90,067,489 | Raw replay window |
| v1430 w3 data | 9,224,702 | Raw replay window |
| v1422 fourth-window data | 4,151,600 | Raw replay window |
| v1426 fifth-window data | 11,544,338 | Raw replay window |
| v1428 sixth-window data | 6,200,890 | Raw replay window |
| v1429 seventh-window data | 2,649,662 | Raw replay window |

Listed core raw size: **610,106,961 bytes**. This total is not asserted to equal the VM's `reports` footprint because an exact source path was not supplied for every asset.

## Tier B - machine-readable research

| Asset | Size (bytes) | Notes |
|---|---:|---|
| `codex_research_dataset_v14_refresh_20260703_133008.json` | 2,850,245 | v14 refresh research dataset |
| `codex_research_dataset_v14_refresh_20260703_133008.csv` | Not supplied | Tabular companion to the refresh dataset |
| `codex_research_selector_v14_refresh_holdout_v1440_20260703_133008.json` | 996,389 | v1440 holdout selector output |
| `v1430_6win_nonoracle_candidate1.json` | Not supplied | v1430 non-oracle candidate |

Known Tier B JSON size: **3,846,634 bytes**, excluding files whose sizes were not supplied.

## Tier C - derived summaries

| Asset | Size (bytes) | Notes |
|---|---:|---|
| `codex_research_dataset_v14_refresh_20260703_133008.md` | Not supplied | Refresh dataset narrative |
| `codex_research_selector_v14_refresh_holdout_v1440_20260703_133008.md` | Not supplied | Holdout selector narrative |
| `v1430_6win_nonoracle_candidate1.md` | Not supplied | Non-oracle candidate narrative |
| v1423 summary | Not supplied | Exact basename and format were not supplied |

## Preservation archive

| Field | Verified value |
|---|---|
| Local path | `reports/archive/phase1_20260710/phase1_backtest_assets_20260710.tar.gz` |
| Size | 70,669,341 bytes |
| SHA-256 | `dfab9cdb102c21bf92ccaf92a6ef47451e91ddb20bcb030532acc4f194705fc3` |
| Files | 23 |
| Tier | A |

## Handling cautions

- `/tmp` is ephemeral. Any sole copy there must be moved to durable storage before VM restart, recreation, cleanup, or deletion.
- File presence, byte size, and archive checksum establish availability and integrity only. They do **not** establish schema correctness, replay correctness, strategy validity, or valid performance.
- Validate performance separately with pinned code provenance, input-window definitions, fees, execution assumptions, and a reproducible replay.

