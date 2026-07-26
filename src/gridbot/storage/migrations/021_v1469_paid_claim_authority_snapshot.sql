-- Immutable authority/risk snapshot attached to every v1.4.69 paid claim.
--
-- Keep this as an extension table instead of rebuilding the claim table:
-- Database._run_migrations() records the migration marker after executescript
-- returns, so the DDL and backfill must be safe to run again after a crash in
-- that small commit/marker window.
BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS v1469_paid_execution_claim_authority (
    claim_id TEXT PRIMARY KEY,
    lease_generation INTEGER NOT NULL CHECK(lease_generation >= 0),
    evidence_revision TEXT NOT NULL CHECK(length(trim(evidence_revision)) > 0),
    regime TEXT NOT NULL CHECK(length(trim(regime)) > 0),
    execution_profile_hash TEXT NOT NULL CHECK(
        length(trim(execution_profile_hash)) > 0
    ),
    risk_policy_hash TEXT NOT NULL CHECK(length(trim(risk_policy_hash)) > 0),
    approved_notional_usdc REAL NOT NULL CHECK(approved_notional_usdc >= 0),
    reserved_loss_usdc REAL NOT NULL CHECK(reserved_loss_usdc >= 0)
);

CREATE TRIGGER IF NOT EXISTS trg_v1469_paid_claim_authority_claim_exists
BEFORE INSERT ON v1469_paid_execution_claim_authority
WHEN NOT EXISTS (
    SELECT 1
    FROM v1469_paid_execution_claims AS claim
    WHERE claim.claim_id = NEW.claim_id
)
BEGIN
    SELECT RAISE(
        ABORT,
        'v1469 paid claim authority requires durable claim'
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_v1469_paid_claim_authority_no_update
BEFORE UPDATE ON v1469_paid_execution_claim_authority
BEGIN
    SELECT RAISE(
        ABORT,
        'v1469 paid claim authority is immutable'
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_v1469_paid_claim_authority_no_delete
BEFORE DELETE ON v1469_paid_execution_claim_authority
BEGIN
    SELECT RAISE(
        ABORT,
        'v1469 paid claim authority cannot be deleted'
    );
END;

-- Pre-021 claims cannot be proven to belong to the lease generation currently
-- stored in v1469_arm_leases.  Preserve and load them, but bind a sentinel
-- snapshot that can never pass CLAIMED -> SUBMITTING revalidation.
INSERT OR IGNORE INTO v1469_paid_execution_claim_authority (
    claim_id,
    lease_generation,
    evidence_revision,
    regime,
    execution_profile_hash,
    risk_policy_hash,
    approved_notional_usdc,
    reserved_loss_usdc
)
SELECT
    claim_id,
    0,
    'LEGACY_UNBOUND',
    'UNCERTAIN',
    'LEGACY_UNBOUND',
    'LEGACY_UNBOUND',
    0,
    0
FROM v1469_paid_execution_claims;

COMMIT;
