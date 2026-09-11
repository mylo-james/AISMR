"""Worker exception types."""

from __future__ import annotations


class JobReschedule(RuntimeError):
    """Signal that a job should be rescheduled without treating it as an error.

    Used for polling-style jobs that are expected to run multiple times until an
    external system finishes work (e.g., OpenAI video jobs when webhooks are missing).
    """

    def __init__(self, *, retry_delay_seconds: float, reason: str) -> None:
        super().__init__(reason)
        self.retry_delay_seconds = float(retry_delay_seconds)
        self.reason = reason
