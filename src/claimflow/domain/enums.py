"""Domain enumerations. Pure Python: no framework or database imports."""

from enum import StrEnum


class ClaimStatus(StrEnum):
    """Lifecycle states of a claim. Legal transitions are enforced in Phase 4."""

    RECEIVED = "RECEIVED"
    VALIDATED = "VALIDATED"
    REJECTED = "REJECTED"  # failed front-end edits; never adjudicated
    PENDED = "PENDED"
    ADJUDICATED = "ADJUDICATED"
    PAID = "PAID"
    DENIED = "DENIED"  # adjudicated and not paid; can be appealed
    APPEALED = "APPEALED"


class AuditEventType(StrEnum):
    CLAIM_RECEIVED = "CLAIM_RECEIVED"
    CLAIM_VALIDATED = "CLAIM_VALIDATED"
    CLAIM_REJECTED = "CLAIM_REJECTED"
    CLAIM_ADJUDICATED = "CLAIM_ADJUDICATED"
    CLAIM_PENDED = "CLAIM_PENDED"
    CLAIM_PAID = "CLAIM_PAID"
    CLAIM_DENIED = "CLAIM_DENIED"
    CLAIM_APPEALED = "CLAIM_APPEALED"
