"""Local, role-scoped knowledge lookup for product agents."""

from __future__ import annotations

import re
from typing import Any

from myloware.agents.roles import is_product_role
from myloware.knowledge.loader import (
    extract_first_heading,
    get_project_knowledge_dir,
    load_knowledge_documents,
)
from myloware.tools.base import JSONSchema, MylowareBaseTool, format_tool_success

_MAX_RESULTS = 3
_MAX_EXCERPT_CHARS = 6000
_TERM_PATTERN = re.compile(r"[a-z0-9]+")


class RoleKnowledgeSearchTool(MylowareBaseTool):
    """Search active local knowledge documents authorized for one fixed role."""

    def __init__(self, *, project: str, role: str) -> None:
        if not is_product_role(role):
            raise ValueError("Knowledge role must be a known product role")
        get_project_knowledge_dir(project)  # Validate the fixed source scope before any lookup.
        self._project = project
        self._role = role

    def get_name(self) -> str:
        return "role_knowledge_search"

    def get_description(self) -> str:
        return "Search current local knowledge authorized for this workflow role."

    def get_input_schema(self) -> JSONSchema:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Terms to find in role-authorized knowledge. Empty lists titles.",
                },
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _MAX_RESULTS,
                    "description": "Maximum documents to return, up to 3.",
                },
            },
            "required": [],
        }

    async def async_run_impl(
        self, query: str = "", max_results: int = _MAX_RESULTS
    ) -> dict[str, Any]:
        """Return matching documents after role and active-status filtering."""
        if not isinstance(query, str):
            raise TypeError("query must be a string")
        if isinstance(max_results, bool) or not isinstance(max_results, int):
            raise TypeError("max_results must be an integer")
        if max_results < 1:
            raise ValueError("max_results must be at least 1")
        limit = min(max_results, _MAX_RESULTS)

        authorized: list[tuple[Any, list[str]]] = []
        for document in load_knowledge_documents(include_global=True, project_id=self._project):
            metadata = document.metadata or {}
            if metadata.get("kb_type") not in {"global", f"project:{self._project}"}:
                continue
            raw_roles = metadata.get("roles", [])
            roles = [raw_roles] if isinstance(raw_roles, str) else list(raw_roles)
            if metadata.get("status") != "active" or self._role not in roles:
                continue
            authorized.append((document, roles))

        terms = _TERM_PATTERN.findall(query.lower())
        if not terms:
            results = [
                self._document_reference(document, roles)
                for document, roles in sorted(authorized, key=lambda item: self._source(item[0]))[
                    :limit
                ]
            ]
            return format_tool_success(
                {"project": self._project, "role": self._role, "results": results},
                message="Listed role-authorized knowledge documents",
            )

        ranked: list[tuple[int, str, Any, list[str], list[str]]] = []
        for document, roles in authorized:
            haystack = f"{document.content}\n{self._source(document)}\n{extract_first_heading(document.content)}".lower()
            matched_terms = [term for term in terms if term in haystack]
            if not matched_terms:
                continue
            score = sum(haystack.count(term) for term in matched_terms)
            ranked.append((score, self._source(document), document, roles, matched_terms))

        results = []
        for score, _source, document, roles, matched_terms in sorted(
            ranked, key=lambda item: (-item[0], item[1])
        )[:limit]:
            result = self._document_reference(document, roles)
            result.update(
                {
                    "matched_terms": matched_terms,
                    "score": score,
                    "excerpt": document.content[:_MAX_EXCERPT_CHARS],
                }
            )
            results.append(result)

        return format_tool_success(
            {"project": self._project, "role": self._role, "results": results},
            message="Searched role-authorized local knowledge",
        )

    @staticmethod
    def _source(document: Any) -> str:
        return str((document.metadata or {}).get("document") or document.filename)

    def _document_reference(self, document: Any, roles: list[str]) -> dict[str, Any]:
        metadata = document.metadata or {}
        return {
            "title": extract_first_heading(document.content),
            "source": self._source(document),
            "kb_type": metadata.get("kb_type", "unknown"),
            "roles": roles,
        }
