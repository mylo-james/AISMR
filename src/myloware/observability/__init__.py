"""AISMR observability module - Logging and telemetry.

Observability is handled at two levels:
1. **Logging**: Structured JSON logs via structlog
2. **Telemetry**: Llama Stack native telemetry (traces exported to Jaeger)

We do NOT set up our own OpenTelemetry SDK - Llama Stack handles all
trace export via its telemetry API. Query traces via:
    client.telemetry.query_traces()
    client.telemetry.get_trace(trace_id)

Usage:
    from myloware.observability import get_logger

    logger = get_logger(__name__)
    logger.info("Starting workflow", run_id=run_id)
"""

from __future__ import annotations

from myloware.observability.logging import configure_logging, get_logger

__all__ = [
    # Logging
    "configure_logging",
    "get_logger",
    "init_observability",
]

_OBSERVABILITY_INITIALIZED = False


def init_observability() -> None:
    """Initialize logging/telemetry for the process (idempotent).

    This is intentionally *not* executed on import so `myloware` can be used as a
    library without mutating global logging configuration.
    """
    global _OBSERVABILITY_INITIALIZED
    if _OBSERVABILITY_INITIALIZED:
        return
    configure_logging()
    _OBSERVABILITY_INITIALIZED = True
