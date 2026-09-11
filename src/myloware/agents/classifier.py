"""Semantic request classification for Supervisor supervisor."""

from __future__ import annotations

from typing import Literal, Optional

from myloware.backends.protocols import AsyncChatBackend, SyncChatBackend
from myloware.observability.logging import get_logger
from pydantic import BaseModel, Field

logger = get_logger("agents.classifier")

ProjectName = Literal["motivational", "aismr", "unknown"]
RequestIntent = Literal[
    "start_run",
    "check_status",
    "approve_gate",
    "list_runs",
    "help",
    "unknown",
]


class ClassificationResult(BaseModel):
    """Result of classifying a user request."""

    intent: RequestIntent = Field(description="What the user wants to do")
    project: Optional[ProjectName] = Field(
        default=None, description="Target project if starting a run"
    )
    run_id: Optional[str] = Field(default=None, description="Run ID if provided")
    gate: Optional[str] = Field(default=None, description="Gate name if approving")
    custom_object: Optional[str] = Field(default=None, description="Custom subject/object")
    skip_steps: list[str] = Field(default_factory=list, description="Steps to skip")
    confidence: float = Field(default=1.0, description="Classification confidence (0-1)")


CLASSIFICATION_PROMPT = """You are a request classifier for AISMR, a video production platform.

Analyze the user's message and classify:
1. intent: What they want to do
   - "start_run": Start a new video production workflow
   - "check_status": Check status of a run
   - "approve_gate": Approve a HITL gate
   - "list_runs": List their runs
   - "help": Need help or information
   - "unknown": Can't determine intent

2. project: Which project (if starting a run)
   - "motivational": Motivational TikTok video (keywords: motivation, inspirational, discipline, mindset)
   - "aismr": Full ASMR production (keywords: asmr, satisfying, calming)
   - null if not starting a run

3. run_id: Extract run ID if mentioned (UUID format like abc-123-def-456)

4. gate: Gate name if approving (ideation, publish)

5. custom_object: Custom subject if specified (e.g., "candles", "books")

6. skip_steps: List of steps to skip if mentioned

Return a JSON object with keys: intent, project, run_id, gate, custom_object, skip_steps, confidence.
"""


def classify_request(
    backend: SyncChatBackend,
    user_message: str,
    model: str = "openai/gpt-4o-mini",
) -> ClassificationResult:
    """Classify a user request using a chat backend with JSON output.

    Raises:
        RuntimeError: If classification fails (fail fast - no fallbacks).
    """
    data = backend.chat_json(
        messages=[
            {"role": "system", "content": CLASSIFICATION_PROMPT},
            {"role": "user", "content": f"Classify this request: {user_message}"},
        ],
        model_id=model,
    )

    # Handle confidence - LLM might return "high", "medium", etc. instead of a number
    raw_confidence = data.get("confidence", 0.8)
    if isinstance(raw_confidence, (int, float)):
        confidence = float(raw_confidence)
    elif isinstance(raw_confidence, str):
        # Map string confidence to numeric values
        confidence_map = {"high": 0.9, "medium": 0.7, "low": 0.5}
        confidence = confidence_map.get(raw_confidence.lower(), 0.8)
    else:
        confidence = 0.8

    result = ClassificationResult(
        intent=data.get("intent", "unknown"),
        project=data.get("project") or None,
        run_id=data.get("run_id") or None,
        gate=data.get("gate") or None,
        custom_object=data.get("custom_object") or None,
        skip_steps=data.get("skip_steps") or [],
        confidence=confidence,
    )

    logger.info(
        "Classified request via LLM",
        extra={
            "intent": result.intent,
            "project": result.project,
            "confidence": result.confidence,
        },
    )
    return result


async def classify_request_async(
    backend: AsyncChatBackend,
    user_message: str,
    model: str = "openai/gpt-4o-mini",
) -> ClassificationResult:
    """Async variant of classify_request."""

    data = await backend.chat_json_async(
        messages=[
            {"role": "system", "content": CLASSIFICATION_PROMPT},
            {"role": "user", "content": f"Classify this request: {user_message}"},
        ],
        model_id=model,
    )

    raw_confidence = data.get("confidence", 0.8)
    if isinstance(raw_confidence, (int, float)):
        confidence = float(raw_confidence)
    elif isinstance(raw_confidence, str):
        confidence_map = {"high": 0.9, "medium": 0.7, "low": 0.5}
        confidence = confidence_map.get(raw_confidence.lower(), 0.8)
    else:
        confidence = 0.8

    result = ClassificationResult(
        intent=data.get("intent", "unknown"),
        project=data.get("project") or None,
        run_id=data.get("run_id") or None,
        gate=data.get("gate") or None,
        custom_object=data.get("custom_object") or None,
        skip_steps=data.get("skip_steps") or [],
        confidence=confidence,
    )
    return result


__all__ = [
    "ClassificationResult",
    "classify_request",
    "ProjectName",
    "RequestIntent",
]
