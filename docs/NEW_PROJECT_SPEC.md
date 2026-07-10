# New Quant Project: Implementation Specification

Generated: 2026-07-10 TPE

## Purpose and Non-Goals

Build a separately deployed quantitative trading system with reproducible research, tick-accurate replay, immutable strategy versions, and a constrained live executor. The first phase is evidence freezing and architecture foundations, not a live rewrite.

cry3 is an evidence archive. The new project may import selected reports, run records, raw market slices, configurations, and regression fixtures with provenance. It must not copy cry3 runtime modules, deployment scripts, database schema, hidden defaults, or production credentials.

Non-goals:

- Reproduce every legacy strategy, API, or operational behavior.
- Migrate legacy production state, open orders, or credentials.
- Allow notebooks, research jobs, or Qlib workflows to place exchange orders.
- Approve live promotion based on aggregate backtest PnL alone.

## System Boundaries

| Layer | Owns | Must not own |
|---|---|---|
| Ingestion | Raw collection, source cursors, parser results, data-quality events | Strategy or order decisions |
| Canonical data | Normalized market/reference data and versions | Mutable strategy state |
| Feature store | Deterministic features and lineage | Silent raw-field substitution |
| Research/Qlib | Datasets, labels, experiments, evaluation artifacts | Production credentials or exchange access |
| Tick replay | Event-time decision and execution reconstruction | Future-data access at decision time |
| Strategy registry | Immutable manifests and promotions | Exchange state mutation |
| Risk policy | Capital, exposure, DCA, kill-switch decisions | Alpha selection or transport |
| Executor | Idempotent order lifecycle and reconciliation | Research or promotion decisions |
| Observability | Ledger, metrics, alerts, review views | Source of strategy truth |

## Data Ingestion, Storage, and Schemas

Maintain five stores:

1. Raw store: append-only source payloads, source sequence/cursor, retrieval metadata, checksum, and parser version. Raw data is never overwritten.
2. Canonical market store: normalized trades/ticks, candles, funding, fees, instrument specifications, and exchange-status events. Partition by venue, symbol, event date, and dataset version.
3. Feature store: versioned feature rows with input lineage, availability time, and quality flags.
4. Operational ledger: append-only signals, risk decisions, plans, orders, fills, position snapshots, and domain events. Derived views are rebuildable.
5. Evidence store: imported cry3 artifacts, fixtures, experiment reports, replay reports, and promotion evidence.

Every record includes a stable identifier, schema version, UTC event/emission time, and lineage. Persist prices, quantities, rates, and PnL as decimal strings or fixed-point values, not binary floating point. Store event time as UTC epoch milliseconds; local server time is never a market event time.

| Record | Required fields |
|---|---|
| market_trade_v1 | trade_id, venue, symbol, event_time_ms, received_time_ms, price, quantity, aggressor_side, source_sequence, dataset_version, raw_ref |
| market_tick_v1 | venue, symbol, event_time_ms, received_time_ms, bid_price, bid_qty, ask_price, ask_qty, last_price, tick_kind, source_sequence, dataset_version, raw_ref |
| instrument_spec_v1 | venue, symbol, effective_from_ms, tick_size, step_size, min_qty, min_notional, max_leverage, margin_asset, contract_type, maker_fee_rate, taker_fee_rate, funding_interval, source_ref |
| feature_row_v1 | venue, symbol, event_time_ms, available_at_ms, feature_set_version, feature_values, input_dataset_versions, quality_flags, feature_snapshot_id |

A feature row is usable only when available_at_ms is at or before decision time. Historical replay selects the instrument specification effective at the replay time.

Ingestion requirements:

- Persist raw data before normalization.
- Use venue-native IDs for deduplication where available; otherwise use a documented deterministic composite key.
- Emit data-quality events for gaps, duplicate sequences, non-monotonic event time, crossed books, invalid values, clock skew, disconnects, and parser failures.
- Backfills create new dataset versions and do not rewrite versions supporting completed experiments, replay, or live decisions.
- Stale, missing, or invalid data fails closed for new exposure unless an approved policy explicitly permits a reduce-only action.

Open decision: venue/instrument scope, object storage, primary database, stream/broker, retention, backup, and disaster-recovery targets.

## Research and Qlib Layer

Research reads canonical snapshots and versioned features only. It has no production credentials and no order interface. Qlib is a research adapter boundary, not a live dependency: its provider/dataset interface must resolve to feature_row_v1 and export an immutable strategy-candidate manifest.

Every experiment manifest records:

- Dataset, feature-set, and instrument-spec versions; universe; time range; timezone; exclusions.
- Label definition, warm-up policy, decision time, and feature availability policy.
- Rule/model code digest, configuration digest, random seeds, dependency lock digest, and execution image digest.
- Train, validation, and final holdout windows.
- Fee, funding, latency, leverage, fill, and no-fill assumptions.
- Metrics by lane, regime, symbol, and side; worst loss, overlap, concentration, drawdown, and fee slices.

Validation uses staggered, time-separated windows and a final untouched holdout. Adjacent windows such as W1/W2 are not sufficient as the sole in-sample split. Any change after examining final-holdout output creates a new experiment lineage. Promotion review includes standalone lane results and ordered-union results after duplicate/exposure handling.

Open decision: native Qlib, a Qlib-compatible adapter, or another experiment framework. The manifest and availability-time requirements apply in all cases.

Current legacy-research evidence: the first Qlib baseline is **REPLAY / REJECTED**. Its holdout result does not qualify as a strategy candidate or live input. Preserve the experiment and rejection in the evidence store; any retry requires a new label/feature hypothesis and experiment lineage rather than tuning against the examined holdout.

## Tick Replay

Replay reconstructs a decision from frozen event-time inputs and applies a declared execution model only to subsequent ticks. A replay request includes strategy manifest digest, risk-policy digest, feature snapshot ID, decision time, symbol/side, planned orders, source dataset versions, and mode.

| Mode | Required behavior |
|---|---|
| decision | Reproduce the signal and risk decision using only then-available data |
| execution | Simulate acceptance, post-only behavior, partial fills, cancellation, TP/SL, DCA, trailing, fees, funding, and limits |
| actual-hold | Compare a frozen live plan with its realized hold window without entry-time leakage |
| counterfactual | Evaluate a named alternative policy without replacing baseline evidence |

Replay output persists first-touch ordering, MFE/MAE, fill feasibility, fills/rejects/timeouts, gross PnL, fees, funding, net PnL, exit classification, data-quality flags, and source-segment hashes. Missing required source data or effective instrument rules is a replay failure, not a silent approximation.

Open decision: maker queue position and partial-fill model. Until calibrated from venue evidence, maker conclusions are assumption-bound and include sensitivity cases. Price touch alone is not proof of a maker fill.

## Strategy Registry

A strategy is an immutable manifest, not a module name or mutable configuration. Required fields are strategy ID/version/status, code digest, feature-set version, decision-contract version, risk-policy digest, execution-policy digest, allowed universe/routes, data-quality policy, evidence references, approver, and approval time.

Lifecycle: draft, research-approved, shadow-approved, paper-approved, live-canary, retired.

Registry validation checks contract/schema compatibility, feature availability, risk-policy interface, allowed routes, and executor capabilities. Registration cannot overwrite an existing version. Promotion changes manifest status through an auditable record. Rollback activates an earlier approved manifest and emits an event. Runtime code cannot change strategy or execution parameters outside a registered manifest.

## Interfaces and Event Contracts

All messages use a versioned envelope with event_id, event_type, event_version, correlation_id, causation_id, occurred_at_ms, producer, schema_version, and payload.

| Event | Minimum payload |
|---|---|
| SignalProposed | signal ID, strategy digest, symbol, side, decision time, feature snapshot, route, entry/TP/SL intent, rationale codes |
| RiskDecisioned | signal ID, approve/reject/reduce, approved size, policy digest, reason codes, risk snapshot |
| ExecutionPlanned | plan ID, signal/risk IDs, idempotency key, normalized orders, expiry, route, cancel/replace policy |
| OrderSubmitted, OrderUpdate, FillRecorded | plan ID, client ID, venue ID when known, state, quantity, price, venue time, raw reference |
| PositionReconciled | expected/venue positions, discrepancy, action, snapshot ID |
| RunClosed | run ID, exit reason, gross PnL, fees/funding, net PnL, complete lineage |
| KillSwitchChanged, DataQualityChanged | scope, state, actor/source, reason, effective time |

Consumers deduplicate by event ID and exchange requests by an execution-plan-derived idempotency key. Unknown event versions fail closed and alert. Generated validation and compatibility tests are required.

Open decision: JSON Schema, Protobuf, or Avro. The selected serialization must be versioned and decimal-safe.

## Live Executor

The executor accepts only approved execution plans and owns exchange transport. It must validate plan expiry, strategy state, kill switches, instrument rules, risk approval, and duplicate client IDs before submission. Persist intent before external submission and raw exchange acknowledgement before deriving state.

Order lifecycle states: planned, submitted, acknowledged, open, partially filled, filled, cancel requested, cancelled, rejected, expired, unknown, and reconciled. Client order IDs are deterministic and restart-recoverable without exposing strategy secrets. Reconcile orders, fills, and positions on restart and on a scheduled cadence; a discrepancy suspends new entries in affected scope.

TP/SL/DCA/trailing behavior belongs to a registered execution policy, never adapter-specific hidden logic. New exposure is refused during stale/degraded data except an explicit reduce-only path.

Open decision: single process versus services, polling versus streaming order updates, and exchange-native versus local conditional exits. Select and document recovery behavior before paper execution.

## Risk and DCA Policy

Risk is a pure, versioned decision component. Given a signal, account snapshot, exposure, instrument specification, and data health, it returns approval/rejection/reduction and a complete risk snapshot.

Hard controls cover account/venue/symbol/strategy/side/correlated exposure; leverage and margin; per-run/rolling/daily loss; drawdown; consecutive losses; order rate; concurrent runs; unhedged duration; fees/funding/slippage/minimum notional; data staleness; feature quality; reconciliation state; and clock skew. Global and scoped kill switches deny new exposure by default, retaining only explicitly approved reduce-only behavior.

DCA is disabled by default and opt-in per strategy. An approved DCA policy specifies maximum additions, total notional cap, trigger using only available data, spacing/cooldown, invalidation, combined exit, and proof that a fully filled ladder fits portfolio and daily-loss caps. DCA cannot result from retry behavior, missing fill state, or unbounded averaging. Broad DCA expansion is prohibited in live canary.

Open decision: numerical capital, leverage, loss, DCA, and canary limits. Record these in approved policy manifests, not cry3 defaults.

## Deployment and Observability

Deploy immutable artifacts by digest with environment-specific configuration references. Each deployment record links artifact, strategy/risk/execution manifests, migration version, dataset/feature versions, operator, time, and rollback target. Production credentials are absent from local development, CI, and research. Research and live use separate identities, storage namespaces, and databases.

Required telemetry:

- Structured logs with correlation/run/signal/order IDs and no credentials.
- Metrics for ingestion lag/gap, freshness, signal rate, risk decisions, order lifecycle, reconciliation, PnL/fees/funding, exposure, DCA, errors, and kill-switch state.
- Alerts for stale data, normalization failure, consumer lag, unknown/rejected orders, position mismatch, loss caps, restart loops, and missing heartbeat.
- Read-only review views linking each run to data, feature snapshot, signal, risk decision, plan, exchange evidence, replay, and deployed digests.

Production deploys require schema compatibility checks, a migration rollback plan, shadow/paper health checks, and tested rollback to the prior approved artifact/manifests. Migrations are forward-only by default. Destructive retention/backfill requires separate approval.

## Environment Boundaries

| Environment | Exchange access | Permitted activity |
|---|---|---|
| local-dev | None | Unit tests, schema work, fixture replay |
| ci | None | Deterministic contract and replay regression |
| research | Read-only only if required | Ingestion validation, Qlib experiments, historical replay |
| shadow | Read-only market data | Signals/plans only; never submit orders |
| paper | Sandbox or simulator | Full lifecycle without production capital |
| live-canary | Explicitly scoped credentials | One approved small-capital strategy |

Promotion moves immutable manifest digests, never working directories.

## Legacy Evidence Archive

Each imported cry3 artifact has an import manifest with source pointer, content checksum, import time, source environment, applicable strategy/version, claimed behavior, confidence level (confirmed, partial, unproven), and redactions.

Seed fixtures cover known DCA/trailing/order-cleanup failures, hot-entry losses, late adverse reopen behavior, low-MFE fee leakage, actual hold-window replay, thin-scalar TP behavior, and recovery canary/skip reasons. Fixtures contain only enough data to reproduce an evidence claim; they do not import legacy runtime dependencies.

A legacy result that cannot be reproduced remains an observation, not a behavior the new system must emulate.

The cry3 v1.4.56 telemetry handoff is an archive input, not a promotion signal. Preserve the VM version/service snapshot, the 281-focused-test result, deployment backup path, reconciliation population of 1,776 runs, 0 `fill_v1` events, and unresolved `has_position=True` / `trades=0` state with their evidence labels. Pre-schema runs must not be classified as missing post-schema telemetry. No DCA/recovery effectiveness claim may be imported without `recovery_entry_filled`, exchange fills/commissions, exit reason, and final net PnL.

## Migration Stages and Acceptance Gates

| Stage | Scope | Exit gate |
|---|---|---|
| 0 Archive | Import evidence and sanitized fixtures | Every seed claim has source/checksum/confidence; no cry3 runtime dependency |
| 1 Foundations | Ingestion, schemas, quality events | Deterministic fixtures; gaps/duplicates/staleness detected and tested |
| 2 Research | Features, experiments, Qlib adapter | Staggered validation/holdout enforced; selected experiment reproducible |
| 3 Replay | Tick engine and fixture suite | Actual-hold cases reproduce declared classifications within documented tolerance |
| 4 Registry/Risk | Promotion and policy | Unapproved/incompatible versions cannot create executable plans; hard controls tested |
| 5 Shadow | Live input, no submissions | Comparable signals/plans/events complete for an approved observation period |
| 6 Paper | Simulator/sandbox lifecycle | Restart, duplicate, reject, partial fill, cancel, stale-data, reconciliation drills pass |
| 7 Live canary | One constrained strategy | Caps, rollback, alerting, and review work under real lifecycle conditions |

No gate passes solely on win rate or PnL. Review worst losses, adverse overlaps, no-fill behavior, fees, data quality, and unresolved discrepancies. Store immutable gate evidence with each promotion record.

## First Live Canary Definition of Done

- Strategy, feature set, risk policy, execution policy, and artifact are immutable and linked by digest.
- Every submitted order traces to source data, feature snapshot, signal, risk decision, and raw exchange response.
- Tick replay covers intended mechanics and identifies uncalibrated fill assumptions.
- Legacy fixtures and new contract tests pass in CI.
- Shadow and paper gates pass, including restart and reconciliation drills.
- Capital, leverage, DCA, rollback authority, on-call ownership, and alerts are explicitly approved.
- No cry3 value or behavior is assumed without an evidence pointer and a new-project decision.

## Legacy v1.4.56a Import Note - 2026-07-10

The legacy adapter must import fill_v1 by stable fill_key, preserve unknown/algo order identity as ambiguous, and retain the schema deployment cutoff. Current retained history is entirely PRE_SCHEMA (1,776 runs); zero post-schema fills exist.

Position evidence must carry environment ownership. The verified snapshot is mainnet FLAT and testnet external/manual ETHUSDC SHORT 0.043. Neither the position nor historical PnL is promotion evidence. A future observed lifecycle becomes replay-eligible only after entry and exit fills, fees, order identity, and final net PnL reconcile without ambiguity.
