"""Supervisor tools for workflow control."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, Iterable
from uuid import UUID

from myloware.config.projects import list_projects, load_project
from myloware.llama_clients import get_sync_client
from myloware.observability.logging import get_logger
from myloware.storage.database import get_session
from myloware.storage.models import Run, RunStatus
from myloware.storage.repositories import ArtifactRepository, RunRepository
from myloware.tools.base import JSONSchema, MylowareBaseTool, format_tool_error

logger = get_logger(__name__)


def _run_repo_factory() -> RunRepository:
    with get_session() as session:
        return RunRepository(session)


def _artifact_repo_factory() -> ArtifactRepository:
    with get_session() as session:
        return ArtifactRepository(session)


def _validate_workflow_project(project: object) -> Dict[str, Any] | None:
    """Return a structured error unless a project is a configured local project."""
    if not isinstance(project, str):
        return format_tool_error("invalid_project", "Project must be a configured project name")
    if not project or project in {".", ".."} or Path(project).name != project:
        return format_tool_error("invalid_project", "Project must be a configured project name")

    try:
        if project not in list_projects():
            return format_tool_error("invalid_project", "Project must be a configured project name")
        load_project(project)
    except (FileNotFoundError, ValueError, OSError):
        return format_tool_error("invalid_project", "Project must be a configured project name")
    return None


class StartWorkflowTool(MylowareBaseTool):
    """Start a video production workflow."""

    def __init__(
        self,
        client_factory: Callable[[], Any] = get_sync_client,
        run_repo_factory: Callable[[], RunRepository] | None = None,
        artifact_repo_factory: Callable[[], ArtifactRepository] | None = None,
        vector_db_id: str = "project_kb",
        orchestrator: Callable[..., Any] | None = None,
        enable_dedupe: bool = True,
    ):
        self.client_factory = client_factory
        self.run_repo_factory = run_repo_factory
        self.artifact_repo_factory = artifact_repo_factory
        self.vector_db_id = vector_db_id
        self.orchestrator = orchestrator
        self.enable_dedupe = enable_dedupe

    def get_name(self) -> str:
        return "start_workflow"

    def get_description(self) -> str:
        return "Start a video production workflow for a project. Returns run_id and status."

    def get_input_schema(self) -> JSONSchema:
        return {
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": "Project name",
                },
                "brief": {
                    "type": "string",
                    "description": "Short brief for the run",
                },
                "user_id": {
                    "type": "string",
                    "description": "User identifier (e.g., telegram_<chat_id>)",
                },
                "telegram_chat_id": {
                    "type": "string",
                    "description": "Telegram chat id for notifications",
                },
            },
            "required": ["project", "brief"],
        }

    def _execute_sync(
        self,
        project: str,
        brief: str,
        user_id: str | None = None,
        telegram_chat_id: str | None = None,
    ) -> Dict[str, Any]:
        validation_error = _validate_workflow_project(project)
        if validation_error:
            return validation_error

        client = self.client_factory()

        orchestrator = self.orchestrator
        if orchestrator is None:
            from myloware.workflows.langgraph.workflow import run_workflow as _run_workflow

            orchestrator = _run_workflow

        # Helper to optionally de-dupe by brief/user/project
        def _dedupe(run_repo: RunRepository) -> Run | None:
            if not self.enable_dedupe:
                return None
            try:
                statuses = {
                    RunStatus.PENDING.value,
                    RunStatus.RUNNING.value,
                    RunStatus.AWAITING_IDEATION_APPROVAL.value,
                    RunStatus.AWAITING_VIDEO_GENERATION.value,
                    RunStatus.AWAITING_RENDER.value,
                    RunStatus.AWAITING_PUBLISH_APPROVAL.value,
                    RunStatus.COMPLETED.value,
                }
                q = (
                    run_repo.session.query(Run)
                    .filter(
                        Run.workflow_name == project,
                        Run.input == brief,
                        Run.status.in_(statuses),
                    )
                    .order_by(Run.created_at.desc())
                )
                if user_id:
                    q = q.filter(Run.user_id == user_id)
                return q.first()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("StartWorkflowTool dedupe check failed: %s", exc)
                return None

        def _normalize_status(val: Any) -> str:
            if isinstance(val, RunStatus):
                return val.value
            return str(val)

        try:
            if self.run_repo_factory and self.artifact_repo_factory:
                run_repo = self.run_repo_factory()
                artifact_repo = self.artifact_repo_factory()

                existing = _dedupe(run_repo)
                if existing:
                    return {
                        "run_id": str(existing.id),
                        "status": _normalize_status(existing.status),
                        "current_step": existing.current_step,
                        "deduped": True,
                    }

                result = orchestrator(
                    client=client,
                    brief=brief,
                    vector_db_id=self.vector_db_id,
                    run_repo=run_repo,
                    artifact_repo=artifact_repo,
                    workflow_name=project,
                    user_id=user_id,
                    telegram_chat_id=telegram_chat_id,
                )
                status = _normalize_status(getattr(result, "status", RunStatus.RUNNING.value))
                return {
                    "run_id": str(result.run_id),
                    "status": status,
                    "current_step": getattr(result, "current_step", None),
                    "deduped": False,
                }

            # Default path uses DB session context
            with get_session() as session:
                run_repo = RunRepository(session)
                artifact_repo = ArtifactRepository(session)

                existing = _dedupe(run_repo)
                if existing:
                    return {
                        "run_id": str(existing.id),
                        "status": _normalize_status(existing.status),
                        "current_step": existing.current_step,
                        "deduped": True,
                    }

                result = orchestrator(
                    client=client,
                    brief=brief,
                    vector_db_id=self.vector_db_id,
                    run_repo=run_repo,
                    artifact_repo=artifact_repo,
                    workflow_name=project,
                    user_id=user_id,
                    telegram_chat_id=telegram_chat_id,
                )
                status = _normalize_status(getattr(result, "status", RunStatus.RUNNING.value))
                return {
                    "run_id": str(result.run_id),
                    "status": status,
                    "current_step": getattr(result, "current_step", None),
                    "deduped": False,
                }

        except Exception as exc:
            logger.exception("StartWorkflowTool failed: %s", exc)
            return format_tool_error("start_workflow_failed", str(exc))

    async def async_run_impl(
        self,
        project: str,
        brief: str,
        user_id: str | None = None,
        telegram_chat_id: str | None = None,
    ) -> Dict[str, Any]:
        # Not used by Llama Stack tool runner (expects sync). Keep for completeness.
        return self._execute_sync(project, brief, user_id, telegram_chat_id)

    def run_impl(
        self,
        project: str,
        brief: str,
        user_id: str | None = None,
        telegram_chat_id: str | None = None,
    ) -> Dict[str, Any]:
        """Sync entrypoint expected by Llama Stack tool runner.

        If we're already inside an event loop, offload the blocking workflow
        start to a dedicated thread to avoid 'Already running asyncio' errors.
        """
        try:
            asyncio.get_running_loop()
            # Running loop: offload blocking start to a worker thread
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    self._execute_sync,
                    project,
                    brief,
                    user_id,
                    telegram_chat_id,
                )
                return future.result()
        except RuntimeError:
            # No running loop; safe to run synchronously
            return self._execute_sync(project, brief, user_id, telegram_chat_id)


class GetRunStatusTool(MylowareBaseTool):
    """Get status of a run."""

    def __init__(
        self,
        run_repo_factory: Callable[[], RunRepository] | None = None,
    ):
        self.run_repo_factory = run_repo_factory

    def get_name(self) -> str:
        return "get_run_status"

    def get_description(self) -> str:
        return "Retrieve status, current_step, and artifacts for a run."

    def get_input_schema(self) -> JSONSchema:
        return {
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "description": "Run identifier",
                },
            },
            "required": ["run_id"],
        }

    def run_impl(self, run_id: str) -> Dict[str, Any]:
        if self.run_repo_factory:
            repo = self.run_repo_factory()
            run = repo.get(UUID(run_id)) if run_id else None
            if run is None:
                raise ValueError("Run not found")
            return {
                "run_id": str(run.id),
                "workflow_name": run.workflow_name,
                "status": run.status,
                "current_step": run.current_step,
                "artifacts": run.artifacts or {},
            }

        with get_session() as session:
            repo = RunRepository(session)
            run = repo.get(UUID(run_id))
            if run is None:
                raise ValueError("Run not found")
            return {
                "run_id": str(run.id),
                "workflow_name": run.workflow_name,
                "status": run.status,
                "current_step": run.current_step,
                "artifacts": run.artifacts or {},
            }


class ListRunsTool(MylowareBaseTool):
    """List recent runs."""

    def __init__(
        self,
        run_repo_factory: Callable[[], RunRepository] | None = None,
    ):
        self.run_repo_factory = run_repo_factory

    def get_name(self) -> str:
        return "list_runs"

    def get_description(self) -> str:
        return "List recent workflow runs."

    def get_input_schema(self) -> JSONSchema:
        return {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Max runs to return",
                    "default": 10,
                },
            },
            "required": [],
        }

    def run_impl(self, limit: int = 10) -> Dict[str, Iterable[Dict[str, Any]]]:
        limit = int(limit)
        if self.run_repo_factory:
            repo = self.run_repo_factory()
            runs = repo.list(limit=limit)
        else:
            with get_session() as session:
                repo = RunRepository(session)
                runs = repo.list(limit=limit)

        def _serialize(run: Any) -> dict[str, Any]:
            if hasattr(run, "__dict__"):
                return {
                    "run_id": str(getattr(run, "id", None)) if run else None,
                    "workflow_name": getattr(run, "workflow_name", None),
                    "status": getattr(run, "status", None),
                    "current_step": getattr(run, "current_step", None),
                }
            if isinstance(run, dict):
                return {
                    "run_id": str(run.get("id")),
                    "workflow_name": run.get("workflow_name"),
                    "status": run.get("status"),
                    "current_step": run.get("current_step"),
                }
            return {}

        return {"runs": [_serialize(r) for r in runs]}


class ApproveGateTool(MylowareBaseTool):
    """Approve a HITL gate for a run."""

    def __init__(
        self,
        client_factory: Callable[[], Any] = get_sync_client,
        run_repo_factory: Callable[[], RunRepository] | None = None,
        artifact_repo_factory: Callable[[], ArtifactRepository] | None = None,
        vector_db_id: str = "project_kb",
        gate_approver: Callable[..., Any] | None = None,
    ):
        self.client_factory = client_factory
        self.run_repo_factory = run_repo_factory
        self.artifact_repo_factory = artifact_repo_factory
        self.vector_db_id = vector_db_id
        self.gate_approver = gate_approver

    def get_name(self) -> str:
        return "approve_gate"

    def get_description(self) -> str:
        return "Approve a HITL gate for a run."

    def get_input_schema(self) -> JSONSchema:
        return {
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "description": "Run identifier",
                },
                "gate": {
                    "type": "string",
                    "description": "Gate name (ideation|publish)",
                },
                "content_override": {
                    "type": "string",
                    "description": "Optional content override",
                },
            },
            "required": ["run_id", "gate"],
        }

    def run_impl(
        self, run_id: str, gate: str, content_override: str | None = None
    ) -> dict[str, Any]:
        gate_approver = self.gate_approver
        if gate_approver is None:
            from myloware.workflows.hitl import approve_gate as _approve_gate

            gate_approver = _approve_gate

        if self.run_repo_factory and self.artifact_repo_factory:
            run_repo = self.run_repo_factory()
            artifact_repo = self.artifact_repo_factory()
            run = run_repo.get(UUID(run_id))
            if run is not None and run.workflow_name == "monthly":
                return {
                    "run_id": str(run.id),
                    "status": "pending_visitor_action",
                    "current_step": "visitor_decision",
                }
            client = self.client_factory()
            result = gate_approver(
                client=client,
                run_id=UUID(run_id),
                gate=gate,
                run_repo=run_repo,
                artifact_repo=artifact_repo,
                vector_db_id=self.vector_db_id,
                content_override=content_override,
            )
            # Handle status as either Enum or string
            status = result.status.value if hasattr(result.status, "value") else result.status
            return {
                "run_id": str(result.run_id),
                "status": status,
                "current_step": result.current_step,
            }

        with get_session() as session:
            run_repo = RunRepository(session)
            artifact_repo = ArtifactRepository(session)
            run = run_repo.get(UUID(run_id)) if hasattr(run_repo, "get") else None
            if run is not None and run.workflow_name == "monthly":
                return {
                    "run_id": str(run.id),
                    "status": "pending_visitor_action",
                    "current_step": "visitor_decision",
                }
            client = self.client_factory()
            result = gate_approver(
                client=client,
                run_id=UUID(run_id),
                gate=gate,
                run_repo=run_repo,
                artifact_repo=artifact_repo,
                vector_db_id=self.vector_db_id,
                content_override=content_override,
            )
            # Handle status as either Enum or string
            status = result.status.value if hasattr(result.status, "value") else result.status
            return {
                "run_id": str(result.run_id),
                "status": status,
                "current_step": result.current_step,
            }


__all__ = [
    "StartWorkflowTool",
    "GetRunStatusTool",
    "ListRunsTool",
    "ApproveGateTool",
]
