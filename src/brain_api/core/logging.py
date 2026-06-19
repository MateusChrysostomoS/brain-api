"""Structured logging setup using structlog.

JSON output in non-dev environments, human-readable console output in dev. Never use
`print` in application code — always use a structlog logger.

Includes a `redact_secrets` processor (defense in depth, per the
tenant-secrets-encryption skill): every event is scrubbed before it renders, so a
token, password, or `*_encrypted` value can never leak into a log line even if a
careless call site passes one as a structured field.
"""

import logging
import sys

import structlog

from brain_api.config import get_settings

_configured = False

# Keys whose values must never be logged. Anything ending in `_encrypted`, plus any
# key containing one of these hints, is blanked.
_SECRET_HINTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "authorization",
    "api_key",
    "apikey",
    "refresh_token",
    "access_token",
    "encryption_key",
    "password_hash",
)
_REDACTED = "***REDACTED***"


def redact_secrets(_logger, _method, event_dict):
    """Blank any value whose key ends in `_encrypted` or looks secret-bearing."""
    for key in list(event_dict):
        low = key.lower()
        if low.endswith("_encrypted") or any(hint in low for hint in _SECRET_HINTS):
            event_dict[key] = _REDACTED
    return event_dict


def setup_logging() -> None:
    """Configure structlog + stdlib logging. Safe to call more than once."""
    global _configured
    if _configured:
        return

    settings = get_settings()
    level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)

    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        redact_secrets,  # <-- before any renderer
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    renderer: structlog.types.Processor
    if settings.is_production or settings.APP_ENV.lower() == "staging":
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger."""
    return structlog.get_logger(name)
