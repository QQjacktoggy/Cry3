-- Atomic paid-evidence watermark for v1.4.69 PROBATION -> LIVE promotion.
--
-- An evaluator records the exact terminal-evidence clock it observed. The
-- lease repository compares that snapshot with the current clock inside the
-- same BEGIN IMMEDIATE transaction as the LIVE lease CAS. Any intervening
-- terminal result, including a hard loss, invalidates the stale promotion.
BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS v1469_paid_terminal_evidence_clocks (
    environment TEXT NOT NULL,
    symbol TEXT NOT NULL,
    arm_key TEXT NOT NULL,
    execution_profile_hash TEXT NOT NULL,
    regime TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 0),
    terminal_count INTEGER NOT NULL CHECK(terminal_count >= 0),
    latest_terminal_at_ms INTEGER CHECK(latest_terminal_at_ms >= 0),
    latest_claim_id TEXT,
    updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms >= 0),
    PRIMARY KEY (
        environment, symbol, arm_key, execution_profile_hash, regime
    )
);

INSERT OR IGNORE INTO v1469_paid_terminal_evidence_clocks (
    environment, symbol, arm_key, execution_profile_hash, regime,
    revision, terminal_count, latest_terminal_at_ms, latest_claim_id,
    updated_at_ms
)
SELECT
    claim.environment,
    claim.symbol,
    claim.arm_key,
    authority.execution_profile_hash,
    authority.regime,
    COUNT(*),
    COUNT(*),
    MAX(claim.terminal_at_ms),
    MAX(claim.claim_id),
    MAX(claim.updated_at_ms)
FROM v1469_paid_execution_claims AS claim
JOIN v1469_paid_execution_claim_authority AS authority
  ON authority.claim_id = claim.claim_id
WHERE claim.status = 'TERMINAL'
GROUP BY
    claim.environment,
    claim.symbol,
    claim.arm_key,
    authority.execution_profile_hash,
    authority.regime;

CREATE TABLE IF NOT EXISTS v1469_paid_promotion_evidence_snapshots (
    environment TEXT NOT NULL,
    symbol TEXT NOT NULL,
    arm_key TEXT NOT NULL,
    execution_profile_hash TEXT NOT NULL,
    regime TEXT NOT NULL,
    evidence_revision TEXT NOT NULL CHECK(
        length(trim(evidence_revision)) > 0
    ),
    window_start_ms INTEGER NOT NULL CHECK(window_start_ms >= 0),
    as_of_ms INTEGER NOT NULL CHECK(as_of_ms >= window_start_ms),
    evidence_limit INTEGER NOT NULL CHECK(
        evidence_limit >= 1 AND evidence_limit <= 1000
    ),
    clock_revision INTEGER NOT NULL CHECK(clock_revision >= 0),
    evidence_watermark TEXT NOT NULL CHECK(length(evidence_watermark) = 64),
    terminal_fills INTEGER NOT NULL CHECK(terminal_fills >= 0),
    wins INTEGER NOT NULL CHECK(wins >= 0 AND wins <= terminal_fills),
    fee_net_paid_pnl REAL NOT NULL,
    hard_loss_marker INTEGER NOT NULL CHECK(hard_loss_marker IN (0, 1)),
    latest_terminal_at_ms INTEGER CHECK(latest_terminal_at_ms >= 0),
    truncated INTEGER NOT NULL CHECK(truncated IN (0, 1)),
    created_at_ms INTEGER NOT NULL CHECK(created_at_ms >= 0),
    PRIMARY KEY (
        environment, symbol, arm_key, execution_profile_hash, regime,
        evidence_revision
    )
);

CREATE TRIGGER IF NOT EXISTS trg_v1469_paid_terminal_evidence_clock
AFTER UPDATE OF status, terminal_at_ms, terminal_reason, result_payload_json
ON v1469_paid_execution_claims
WHEN NEW.status = 'TERMINAL'
 AND (
    OLD.status <> 'TERMINAL'
    OR NEW.terminal_at_ms IS NOT OLD.terminal_at_ms
    OR NEW.terminal_reason IS NOT OLD.terminal_reason
    OR NEW.result_payload_json IS NOT OLD.result_payload_json
 )
BEGIN
    INSERT INTO v1469_paid_terminal_evidence_clocks (
        environment, symbol, arm_key, execution_profile_hash, regime,
        revision, terminal_count, latest_terminal_at_ms, latest_claim_id,
        updated_at_ms
    )
    SELECT
        NEW.environment,
        NEW.symbol,
        NEW.arm_key,
        authority.execution_profile_hash,
        authority.regime,
        1,
        1,
        NEW.terminal_at_ms,
        NEW.claim_id,
        NEW.updated_at_ms
    FROM v1469_paid_execution_claim_authority AS authority
    WHERE authority.claim_id = NEW.claim_id
    ON CONFLICT (
        environment, symbol, arm_key, execution_profile_hash, regime
    )
    DO UPDATE SET
        revision = revision + 1,
        terminal_count = terminal_count + (
            CASE WHEN OLD.status <> 'TERMINAL' THEN 1 ELSE 0 END
        ),
        latest_terminal_at_ms = MAX(
            COALESCE(latest_terminal_at_ms, 0), NEW.terminal_at_ms
        ),
        latest_claim_id = CASE
            WHEN latest_terminal_at_ms IS NULL
              OR NEW.terminal_at_ms >= latest_terminal_at_ms
            THEN NEW.claim_id
            ELSE latest_claim_id
        END,
        updated_at_ms = MAX(updated_at_ms, NEW.updated_at_ms);
END;

COMMIT;