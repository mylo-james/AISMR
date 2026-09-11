from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Any, MutableMapping

import structlog

request_id_var: ContextVar[str] = ContextVar("request_id", default="")


def _add_request_id(
    _logger: logging.Logger,
    _method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    request_id = request_id_var.get("")
    if request_id:
        event_dict["request_id"] = request_id
    return event_dict


def configure_logging() -> None:
    """Configure structlog with JSON output and contextvar support."""

    timestamper = structlog.processors.TimeStamper(fmt="iso")

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            timestamper,
            _add_request_id,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(level=logging.INFO)
    # Request URLs may contain scoped media capabilities. Durable stage events
    # carry safe operational detail without logging those bearer URLs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a configured structlog logger."""

    if name is None:
        return structlog.get_logger()
    return structlog.get_logger(name)


def get_request_id() -> str:
    """Return the current request ID from contextvars."""

    return request_id_var.get("")


logger = get_logger("myloware")
