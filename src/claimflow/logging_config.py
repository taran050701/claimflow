"""Structured logging with structlog: one JSON object per line on stdout.

Logging to stdout (not files) is a 12-factor rule: the platform (Render, Docker,
Kubernetes) collects the stream. Context such as request_id and correlation_id is
bound once with contextvars and merged into every log line automatically.
"""

import logging

import structlog


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    renderer = (
        structlog.dev.ConsoleRenderer() if fmt == "console" else structlog.processors.JSONRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,  # request_id, correlation_id, ...
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.dict_tracebacks,  # exceptions as structured JSON
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
    )
