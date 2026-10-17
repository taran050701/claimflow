"""Domain exceptions. The API layer maps these to HTTP status codes, so the
service layer never has to know about HTTP."""


class ClaimNotFoundError(Exception):
    pass


class IdempotencyKeyConflictError(Exception):
    """The same Idempotency-Key was reused with a different request body."""


class RemittanceNotAvailableError(Exception):
    """Remittances exist only for finalized (PAID or DENIED) claims."""

    def __init__(self, status: str) -> None:
        super().__init__(
            f"No remittance for a claim in status {status}; "
            "remittances are issued once a claim is PAID or DENIED"
        )
