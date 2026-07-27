# v1.4.56a fill_v1 Testnet Lifecycle Evidence - 2026-07-10

## Scope

This record validates the `fill_v1` telemetry contract against actual Binance Futures testnet fills. It is execution/telemetry evidence only. It does not prove mainnet profitability, strategy quality, or recovery/DCA effectiveness.

## Safety Preconditions

- Environment was explicitly testnet (`BINANCE_TESTNET=true`).
- Symbol was `BTCUSDC`, selected because testnet `ETHUSDC` had an unrelated external/manual position.
- The probe refused to run unless the selected symbol was flat and had no open orders.
- Cleanup was permitted only when the probe itself opened the position.
- The probe used an in-memory repository and did not modify the live database.

## Lifecycle

| Field | Entry | Exit |
|---|---:|---:|
| Probe run | `tprobe_1783696606` | `tprobe_1783696606` |
| Side | BUY | SELL |
| Quantity | 0.002 BTC | 0.002 BTC |
| Price | 64044.9 | 64003.8 |
| Quote quantity | 128.0898 USDC | 128.0076 USDC |
| Liquidity | taker | taker |
| Commission | 0.05764041 USDC | 0.05760342 USDC |
| Realized PnL | 0 | -0.08219999 USDC |
| fill_key | `77551836:774923033` | `77551837:774923064` |
| role | `entry` | `exit` |

The final exchange position was flat. Gross realized PnL was `-0.08219999` USDC and total commission was `0.11524383` USDC, for an observed lifecycle net of `-0.19744382` USDC.

## Telemetry Assertions

- First synchronization emitted exactly two `fill_v1` events.
- A second synchronization emitted zero events.
- Stable fill keys made the lifecycle idempotent within the repository.
- Entry and exit role classification matched the actual position lifecycle.
- Exchange trade identity, price, quantity, commission, and realized PnL were retained in the emitted payloads.

## Failed Attempt Retained as Evidence

The first `0.001 BTC` attempt was rejected by Binance with error `-4164` (minimum notional). No position was opened. The successful retry used `0.002 BTC` and completed flat.

## Closure Boundary

This closes the clean-testnet-symbol lifecycle requirement for the telemetry contract. The mainnet `fill_v1` lifecycle and recovery/DCA effectiveness remain open until a post-schema mainnet run is reconciled through final net PnL.

## Reproduction Surface

- `scripts/run_fill_v1_testnet_probe.py`
- `src/gridbot/mainnet/fill_telemetry.py`
- `tests/test_fill_telemetry.py`
- `tests/test_mainnet_fill_sync.py`