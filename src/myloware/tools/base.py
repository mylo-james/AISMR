"""Base class and utilities for custom Llama Stack tools.

This module follows the Llama Stack v0.3.x ClientTool pattern:
- Extend ClientTool
- Implement get_name(), get_description(), get_input_schema()
- Implement run_impl() (sync) or async_run_impl() (async)

No legacy param shims are provided; tools must define JSON Schema via get_input_schema.

SYNC ↔ ASYNC BOUNDARY:
=====================

The sync Agent (default) always executes client-side tools via:
  ClientTool.run() → run_impl()

It NEVER calls async_run() or async_run_impl() directly.

Our MylowareBaseTool.run_impl() is the ONLY sync→async bridge:
- Detects if subclass has overridden async_run_impl
- If event loop is running: spins new loop in worker thread
- Otherwise: uses asyncio.run()
- ALWAYS awaits the coroutine before returning

Rules:
1. Never call async_run_impl() directly from sync code
2. Never return a coroutine from run_impl() - always await it
3. Use sync Agent with tools that have working sync entrypoint (run_impl)
4. Use AsyncAgent only if you need true async tool execution

The bridge handles: sync → async (creates/awaits coroutine)
The bridge returns: plain dict/JSON (async → sync conversion)
"""

from __future__ import annotations

from abc import abstractmethod
from typing import Any, Dict
import asyncio
from llama_stack_client.lib.agents.client_tool import ClientTool, JSONSchema

from myloware.observability.logging import get_logger

logger = get_logger(__name__)

__all__ = [
    "MylowareBaseTool",
    "format_tool_error",
    "format_tool_success",
    "JSONSchema",
]


def format_tool_error(
    error_type: str,
    message: str,
    details: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Format a tool error response."""
    response: Dict[str, Any] = {
        "error": True,
        "error_type": error_type,
        "message": message,
    }
    if details:
        response["details"] = details
    return response


def format_tool_success(
    data: Dict[str, Any],
    message: str | None = None,
) -> Dict[str, Any]:
    """Format a tool success response."""
    response: Dict[str, Any] = {"success": True, **data}
    if message:
        response["message"] = message
    return response


class MylowareBaseTool(ClientTool):
    """
    Base class for AISMR custom tools using Llama Stack ClientTool.

    Subclasses must implement:
    - get_name() -> str
    - get_description() -> str
    - get_input_schema() -> JSONSchema (JSON Schema format)
    - run_impl(**kwargs) -> Any (sync) or async_run_impl(**kwargs) -> Any (async)
    """

    def run(self, message_history: Any) -> Any:
        """
        Override ClientTool.run() to add instrumentation.

        This is the sync entry point. It should call run_impl() which bridges to async.
        """
        logger.debug(
            "MylowareBaseTool.run() called for %s (type=%s, MRO=%s)",
            self.__class__.__name__,
            type(self),
            self.__class__.mro(),
        )
        try:
            result = super().run(message_history)
            return result
        except Exception as e:
            logger.error("Exception in run(): %s", e, exc_info=True)
            raise

    def run_impl(self, **kwargs: Any) -> Any:
        """
        Sync implementation that bridges to async_run_impl if needed.

        ClientTool.run() calls this synchronously. If there's a running loop,
        we run the async code in a thread with a new loop.
        """
        logger.debug(
            "run_impl() called for %s with kwargs: %s", self.__class__.__name__, list(kwargs.keys())
        )

        # Check if subclass has async_run_impl
        # Use getattr to check if it's overridden (not the base implementation)
        base_async_impl = MylowareBaseTool.async_run_impl
        subclass_async_impl = getattr(type(self), "async_run_impl", None)
        has_async = subclass_async_impl is not None and subclass_async_impl is not base_async_impl
        logger.debug(
            "Has async_run_impl override: %s (base=%s, subclass=%s)",
            has_async,
            base_async_impl,
            subclass_async_impl,
        )

        if has_async:
            import threading

            try:
                asyncio.get_running_loop()
            except RuntimeError:
                logger.debug("No running loop, using asyncio.run()")
                result = asyncio.run(self.async_run_impl(**kwargs))
                logger.debug("asyncio.run() completed, result type: %s", type(result))
                return result

            logger.debug("Detected running event loop, using thread approach")
            result = None
            exception: Exception | None = None

            def run_in_thread() -> None:
                nonlocal result, exception
                logger.debug("Thread started, creating new event loop")
                new_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(new_loop)
                try:
                    logger.debug("Calling async_run_impl in thread")
                    result = new_loop.run_until_complete(self.async_run_impl(**kwargs))
                    logger.debug(
                        "async_run_impl completed in thread, result type: %s", type(result)
                    )
                except Exception as e:
                    logger.error("Exception in thread: %s", e, exc_info=True)
                    exception = e
                finally:
                    new_loop.close()

            thread = threading.Thread(target=run_in_thread)
            thread.start()
            thread.join()

            if exception:
                raise exception
            logger.debug("run_impl() returning result from thread")
            return result

        raise NotImplementedError(
            f"{self.__class__.__name__} must implement run_impl() or async_run_impl()"
        )

    async def async_run_impl(self, **kwargs: Any) -> Any:
        """
        Async implementation of tool logic.

        Override this for async tools. The base implementation calls
        the sync run_impl for backwards compatibility.

        NOTE: This should NEVER be called directly from sync code.
        It should only be called from run_impl() which bridges sync/async.
        """
        # If subclass has overridden async_run_impl, it will be called
        # But this base implementation should not be reached
        # Fallback: try to call sync run_impl (for backwards compatibility)
        try:
            return self.run_impl(**kwargs)
        except NotImplementedError:
            raise

    @abstractmethod
    def get_input_schema(self) -> JSONSchema:
        """Return JSON Schema for tool parameters."""
        raise NotImplementedError("Subclasses must implement get_input_schema")
