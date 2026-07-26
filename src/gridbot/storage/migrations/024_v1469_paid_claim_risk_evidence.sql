BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS v1469_paid_claim_risk_evidence (
    claim_id TEXT PRIMARY KEY,
    risk_active_day TEXT NOT NULL CHECK(length(trim(risk_active_day)) > 0),
    risk_evidence_revision TEXT NOT NULL CHECK(
        length(trim(risk_evidence_revision)) > 0
    )
);

CREATE TRIGGER IF NOT EXISTS trg_v1469_paid_claim_risk_evidence_claim_exists
BEFORE INSERT ON v1469_paid_claim_risk_evidence
WHEN NOT EXISTS (
    SELECT 1 FROM v1469_paid_execution_claims AS claim
    WHERE claim.claim_id = NEW.claim_id
)
BEGIN
    SELECT RAISE(ABORT, 'v1469 paid risk evidence requires durable claim');
END;

CREATE TRIGGER IF NOT EXISTS trg_v1469_paid_claim_risk_evidence_no_update
BEFORE UPDATE ON v1469_paid_claim_risk_evidence
BEGIN
    SELECT RAISE(ABORT, 'v1469 paid claim risk evidence is immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_v1469_paid_claim_risk_evidence_no_delete
BEFORE DELETE ON v1469_paid_claim_risk_evidence
BEGIN
    SELECT RAISE(ABORT, 'v1469 paid claim risk evidence cannot be deleted');
END;

COMMIT;
