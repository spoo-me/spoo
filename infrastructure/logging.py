"""
Logging utilities

Provides:
- get_logger(): Get a configured logger instance
- should_sample(): Determine if an event should be logged based on sampling rate
- hash_ip(): Hash IP addresses for privacy
- setup_logging(): Initialize the logging system
- configure_structlog(): Configure structlog processors
- SAMPLING_RATES: Sampling rate configuration
"""

import hashlib
import logging
import os
import random
import sys

import structlog
from structlog.stdlib import BoundLogger
from structlog.types import EventDict, Processor

# ---------------------------------------------------------------------------
# Environment configuration
# ---------------------------------------------------------------------------
ENV = os.getenv("ENV", "development")
IS_PRODUCTION = ENV == "production"
IS_DEVELOPMENT = ENV == "development"

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO" if IS_PRODUCTION else "DEBUG")
LOG_FORMAT = os.getenv("LOG_FORMAT", "json" if IS_PRODUCTION else "console")

# ---------------------------------------------------------------------------
# Sampling rates for high-frequency events
# ---------------------------------------------------------------------------
SAMPLING_RATES = {
    "url_redirect": float(os.getenv("SAMPLE_RATE_REDIRECT", "0.05")),  # 5%
    "stats_query": float(os.getenv("SAMPLE_RATE_STATS", "0.20")),  # 20%
    "cache_operation": float(os.getenv("SAMPLE_RATE_CACHE", "0.01")),  # 1%
    "stats_export": float(os.getenv("SAMPLE_RATE_EXPORT", "0.80")),  # 80%
}

# Sensitive fields to redact from logs
REDACTED_FIELDS = {
    "password",
    "password_hash",
    "token",
    "api_key",
    "authorization",
    "cookie",
    "refresh_token",
    "access_token",
    "secret",
    "key",
}

# Field names that trip the substring heuristic in redact_sensitive_fields
# but carry no secret material: derived booleans, display prefixes, and
# identifiers. These pass through unredacted.
SAFE_FIELDS = {
    "has_password",
    "password_protected",
    "key_id",
    "key_prefix",
    "token_prefix",
    "query_keys",
    # LLM cost accounting: token COUNTS, not token material. Without
    # these the substring heuristic redacts the spend telemetry and
    # Axiom cannot answer what a task cost.
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "max_tokens",
}


# ---------------------------------------------------------------------------
# IP hashing
# ---------------------------------------------------------------------------
def hash_ip(ip_address: str | None) -> str | None:
    """
    Hash IP address for privacy in production.

    In production: Returns SHA-256 hash (first 16 chars) for GDPR compliance.
    In development: Returns the original IP for easier debugging.
    """
    if ip_address is None:
        return None
    if IS_PRODUCTION and ip_address:
        return hashlib.sha256(ip_address.encode()).hexdigest()[:16]
    return ip_address


# ---------------------------------------------------------------------------
# Logger factory
# ---------------------------------------------------------------------------
def get_logger(name: str) -> BoundLogger:
    """Get a configured structlog logger instance."""
    return structlog.get_logger(name)


def should_sample(event_type: str) -> bool:
    """
    Determine if an event should be logged based on sampling rate.

    Uses random sampling to reduce log volume for high-frequency events.
    """
    sample_rate = SAMPLING_RATES.get(event_type, 1.0)
    if sample_rate >= 1.0:
        return True
    if sample_rate <= 0.0:
        return False
    return random.random() < sample_rate


def redact_sensitive_fields(
    logger: logging.Logger, method_name: str, event_dict: EventDict
) -> EventDict:
    """Redact sensitive fields from logs."""
    for key in list(event_dict.keys()):
        if key.lower() in SAFE_FIELDS:
            continue
        if (
            key.lower() in REDACTED_FIELDS
            or any(
                sensitive in key.lower()
                for sensitive in ["password", "token", "key", "secret"]
            )
        ) and key not in ["level", "event", "timestamp", "logger"]:
            event_dict[key] = "***REDACTED***"
    return event_dict


def filter_exceptions(
    logger: logging.Logger, method_name: str, event_dict: EventDict
) -> EventDict:
    """Format exceptions properly for logging."""
    exc_info = event_dict.pop("exc_info", None)
    if exc_info:
        event_dict["exception"] = structlog.processors.format_exc_info(
            logger, method_name, {"exc_info": exc_info}
        )["exception"]
    return event_dict


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def configure_structlog() -> None:
    """
    Configure structlog with appropriate processors for the environment.

    Production: JSON formatting for easy parsing.
    Development: Pretty console formatting with colors.
    """
    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.StackInfoRenderer(),
        redact_sensitive_fields,
        filter_exceptions,
    ]

    if LOG_FORMAT == "json":
        structlog.configure(
            processors=[
                *shared_processors,
                structlog.processors.format_exc_info,
                structlog.processors.JSONRenderer(),
            ],
            wrapper_class=structlog.stdlib.BoundLogger,
            context_class=dict,
            logger_factory=structlog.stdlib.LoggerFactory(),
            cache_logger_on_first_use=True,
        )
    else:
        _level_styles = structlog.dev.ConsoleRenderer.get_default_level_styles()
        _level_styles["debug"] = "\x1b[36m"  # cyan, distinct from info (green)
        structlog.configure(
            processors=[
                *shared_processors,
                structlog.processors.format_exc_info,
                structlog.dev.ConsoleRenderer(
                    colors=True,
                    pad_event=15,
                    sort_keys=False,
                    level_styles=_level_styles,
                ),
            ],
            wrapper_class=structlog.stdlib.BoundLogger,
            context_class=dict,
            logger_factory=structlog.stdlib.LoggerFactory(),
            cache_logger_on_first_use=True,
        )


def configure_stdlib_logging() -> None:
    """Configure standard library logging to work with structlog."""
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, LOG_LEVEL.upper()),
    )

    # Reduce noise from third-party libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    # httpx logs full request URLs at INFO; those must not land in the log sink.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("boto3").setLevel(logging.WARNING)

    # Silence pymongo debug logs
    logging.getLogger("pymongo").setLevel(logging.WARNING)
    logging.getLogger("pymongo.connection").setLevel(logging.WARNING)
    logging.getLogger("pymongo.serverSelection").setLevel(logging.WARNING)
    logging.getLogger("pymongo.command").setLevel(logging.WARNING)
    logging.getLogger("pymongo.topology").setLevel(logging.WARNING)


def configure_sentry_logging() -> None:
    """Configure Sentry integration for error tracking."""
    sentry_dsn = os.getenv("SENTRY_DSN")
    if not sentry_dsn:
        return

    try:
        from sentry_sdk.integrations.logging import LoggingIntegration

        sentry_logging = LoggingIntegration(  # noqa: F841
            level=logging.INFO,
            event_level=logging.ERROR,
        )
    except ImportError:
        pass


def setup_logging() -> None:
    """
    Initialize logging system for the application.

    This is the main entry point for logging configuration.
    Should be called early in application startup.
    """
    configure_stdlib_logging()
    configure_structlog()
    configure_sentry_logging()

    logger = structlog.get_logger(__name__)
    logger.info(
        "logging_initialized",
        env=ENV,
        log_level=LOG_LEVEL,
        log_format=LOG_FORMAT,
        sentry_enabled=bool(os.getenv("SENTRY_DSN")),
    )


__all__ = [
    "SAMPLING_RATES",
    "configure_structlog",
    "get_logger",
    "hash_ip",
    "setup_logging",
    "should_sample",
]
