"""Live Next strategy primitives.

This package is intentionally offline-only until every promotion gate passes.
Importing it must never create an exchange client or mutate order state.
"""

from .contracts import (
    ContractError,
    Decision,
    DecisionAction,
    Opportunity,
    Outcome,
    OutcomeStatus,
    Side,
    canonical_dict,
    canonical_json,
    canonical_sha256,
)

__all__ = [
    "ContractError",
    "Decision",
    "DecisionAction",
    "Opportunity",
    "Outcome",
    "OutcomeStatus",
    "Side",
    "canonical_dict",
    "canonical_json",
    "canonical_sha256",
]
