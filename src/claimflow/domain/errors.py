"""Domain exceptions. The API layer maps these to HTTP status codes, so the
service layer never has to know about HTTP."""


class ClaimNotFoundError(Exception):
    pass


class IdempotencyKeyConflictError(Exception):
    """The same Idempotency-Key was reused with a different request body."""
