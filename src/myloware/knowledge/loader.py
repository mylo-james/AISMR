"""Knowledge document loader for Vector I/O ingestion.

Supports:
- Nested directory structures (e.g., video-generation/wan-2.2-fast-prompting.md)
- Enhanced metadata extraction (category, section, document path)
- Both global and project-specific knowledge bases
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, List, Tuple

import yaml  # type: ignore

from myloware.paths import get_repo_root

ROOT = get_repo_root()
MANIFEST_PATH = ROOT / "data" / ".kb_manifest.json"


@dataclass
class KnowledgeDocument:
    """A knowledge document ready for ingestion.

    Attributes:
        id: Unique identifier for the document
        content: Full text content of the document
        filename: Original filename (e.g., "wan-2.2-fast-prompting.md")
        metadata: Additional metadata for retrieval context
    """

    id: str
    content: str
    filename: str
    metadata: dict[str, Any] = field(default_factory=dict)


def parse_document_metadata(content: str) -> tuple[str, dict[str, Any]]:
    """Separate a role declaration from document text; untagged files stay unscoped.

    Roles are source-owned knowledge eligibility, not user-supplied tool arguments.
    Legacy documents remain loadable for maintenance but are not eligible for
    the role knowledge tool until explicitly classified.
    """
    metadata: dict[str, Any] = {"roles": [], "status": "legacy"}
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return content, metadata
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        raise ValueError("Knowledge front matter is missing its closing delimiter")
    header = yaml.safe_load("".join(lines[1:end]))
    if not isinstance(header, dict):
        raise TypeError("Knowledge front matter must be a mapping")
    roles = header.get("roles", [])
    valid_roles = {"ideator", "producer", "editor", "publisher", "supervisor"}
    if not isinstance(roles, list) or any(
        not isinstance(role, str) or role not in valid_roles for role in roles
    ):
        raise ValueError("Knowledge roles must be a list of known agent roles")
    status = header.get("status", "legacy")
    if not isinstance(status, str) or status not in {"active", "legacy", "draft"}:
        raise ValueError("Knowledge status must be active, legacy, or draft")
    if status == "active" and not roles:
        raise ValueError("Active knowledge must declare at least one role")
    metadata.update(roles=list(dict.fromkeys(roles)), status=status)
    if header.get("reviewed"):
        metadata["reviewed"] = str(header["reviewed"])
    return "".join(lines[end + 1 :]).lstrip(), metadata


def get_knowledge_dir() -> Path:
    """Get global knowledge data directory."""
    return ROOT / "data" / "knowledge"


def get_project_knowledge_dir(project_id: str) -> Path:
    """Resolve one project knowledge directory without traversal or symlink redirects."""
    if not isinstance(project_id, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]*", project_id
    ):
        raise ValueError("Knowledge project must be a simple project identifier")
    projects_root = (ROOT / "data" / "projects").resolve()
    project_dir = projects_root / project_id
    knowledge_dir = project_dir / "knowledge"
    if project_dir.resolve() != project_dir or knowledge_dir.resolve() != knowledge_dir:
        raise ValueError("Project knowledge cannot redirect through a symlink")
    return knowledge_dir


def extract_first_heading(content: str) -> str:
    """Extract the first markdown heading from content.

    Returns the heading text without the # prefix, or "Overview" if none found.
    """
    for line in content.split("\n"):
        line = line.strip()
        if line.startswith("#"):
            # Remove all leading # and whitespace
            return re.sub(r"^#+\s*", "", line)
    return "Overview"


def extract_all_headings(content: str) -> list[str]:
    """Extract all markdown headings from content.

    Useful for understanding document structure.
    """
    headings = []
    for line in content.split("\n"):
        line = line.strip()
        if line.startswith("#"):
            headings.append(re.sub(r"^#+\s*", "", line))
    return headings


def load_manifest() -> dict[str, Any] | None:
    """Load the last saved knowledge-base manifest, if present."""
    if not MANIFEST_PATH.exists():
        return None
    try:
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_manifest(manifest: dict[str, Any]) -> None:
    """Persist the current knowledge-base manifest for change detection."""
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")


def _compute_manifest(files: list[Path]) -> dict[str, Any]:
    """Compute a deterministic manifest hash from file paths + stat metadata."""
    h = hashlib.sha256()
    entries: list[dict[str, Any]] = []
    for path in sorted(files):
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        stat = path.stat()
        entry = {"path": rel, "mtime_ns": stat.st_mtime_ns, "size": stat.st_size}
        entries.append(entry)
        h.update(rel.encode("utf-8"))
        h.update(str(stat.st_mtime_ns).encode("utf-8"))
        h.update(str(stat.st_size).encode("utf-8"))
    return {"hash": h.hexdigest(), "files": entries}


def _load_documents_from_dir(
    knowledge_dir: Path,
    kb_type: str = "global",
    read_content: bool = True,
) -> Tuple[List[KnowledgeDocument], list[Path]]:
    """Load all markdown documents from a directory (recursively).

    Args:
        knowledge_dir: Directory to scan for .md files
        kb_type: Type of knowledge base ("global" or "project:{project_id}")
        read_content: Whether to read file contents into KnowledgeDocument.content

    Returns:
        (documents, files_used)
    """
    if not knowledge_dir.exists():
        return [], []

    docs: List[KnowledgeDocument] = []
    files: list[Path] = []

    # Use rglob for recursive directory scanning
    for doc_path in knowledge_dir.rglob("*.md"):
        # Skip index and readme files
        if doc_path.name.lower() in ("index.md", "readme.md"):
            continue

        files.append(doc_path)
        # Refuse symlink escapes before reading any document content.
        if not doc_path.resolve().is_relative_to(knowledge_dir.resolve()):
            raise ValueError(f"Knowledge document escapes its directory: {doc_path.name}")
        raw_content = doc_path.read_text(encoding="utf-8") if read_content else ""
        content, role_metadata = parse_document_metadata(raw_content)

        # Calculate relative path from knowledge dir
        relative_path = doc_path.relative_to(knowledge_dir)

        # Extract category from directory path
        if relative_path.parent != Path("."):
            category = str(relative_path.parent).replace("\\", "/")
        else:
            category = "general"

        # Create unique ID based on path
        doc_id = f"kb_{category}_{doc_path.stem}".replace("/", "_").replace("-", "_")

        # Extract first heading for section context
        first_heading = extract_first_heading(content)

        docs.append(
            KnowledgeDocument(
                id=doc_id,
                content=content,
                filename=doc_path.name,
                metadata={
                    **role_metadata,
                    "source": "knowledge_base",
                    "kb_type": kb_type,
                    "filename": doc_path.name,
                    "document": str(relative_path),
                    "category": category,
                    "section": first_heading if read_content else "Overview",
                },
            )
        )

    return docs, files


def load_documents_with_manifest(
    project_id: str | None,
    include_global: bool = True,
    read_content: bool = True,
) -> tuple[list[KnowledgeDocument], dict[str, Any]]:
    """Load knowledge documents and return a manifest for change detection."""
    all_docs: list[KnowledgeDocument] = []
    files: list[Path] = []

    if include_global:
        docs, used = _load_documents_from_dir(
            get_knowledge_dir(), kb_type="global", read_content=read_content
        )
        all_docs.extend(docs)
        files.extend(used)

    if project_id:
        docs, used = _load_documents_from_dir(
            get_project_knowledge_dir(project_id),
            kb_type=f"project:{project_id}",
            read_content=read_content,
        )
        all_docs.extend(docs)
        files.extend(used)

    manifest = _compute_manifest(files)
    return all_docs, manifest


def load_knowledge_documents(
    include_global: bool = True,
    project_id: str | None = None,
) -> Iterator[KnowledgeDocument]:
    """Load knowledge documents for Vector I/O ingestion.

    Args:
        include_global: Whether to include global knowledge base documents
        project_id: Optional project ID to include project-specific documents

    Returns:
        Iterator of KnowledgeDocument objects

    Example:
        # Load only global KB
        docs = list(load_knowledge_documents())

        # Load global + project KB
        docs = list(load_knowledge_documents(project_id="aismr"))

        # Load only project KB
        docs = list(load_knowledge_documents(include_global=False, project_id="aismr"))
    """
    all_docs: List[KnowledgeDocument] = []

    # Load global knowledge base
    if include_global:
        global_docs, _ = _load_documents_from_dir(
            get_knowledge_dir(),
            kb_type="global",
            read_content=True,
        )
        all_docs.extend(global_docs)

    # Load project-specific knowledge base
    if project_id:
        project_docs, _ = _load_documents_from_dir(
            get_project_knowledge_dir(project_id),
            kb_type=f"project:{project_id}",
            read_content=True,
        )
        all_docs.extend(project_docs)

    return iter(all_docs)


def list_knowledge_documents(
    include_global: bool = True,
    project_id: str | None = None,
) -> list[str]:
    """List available knowledge document paths.

    Returns relative paths like "video-generation/veo3-prompting-guide".
    """
    docs = list(
        load_knowledge_documents(
            include_global=include_global,
            project_id=project_id,
        )
    )
    return [doc.metadata.get("document", doc.filename).replace(".md", "") for doc in docs]


def get_knowledge_stats() -> dict:
    """Get statistics about the knowledge base.

    Useful for debugging and validation.
    """
    global_docs = list(load_knowledge_documents(include_global=True, project_id=None))

    categories = {}
    for doc in global_docs:
        cat = doc.metadata.get("category", "general")
        categories[cat] = categories.get(cat, 0) + 1

    return {
        "total_documents": len(global_docs),
        "categories": categories,
        "documents": [doc.metadata.get("document", doc.filename) for doc in global_docs],
    }


__all__ = [
    "KnowledgeDocument",
    "get_knowledge_dir",
    "get_project_knowledge_dir",
    "load_knowledge_documents",
    "load_documents_with_manifest",
    "load_manifest",
    "save_manifest",
    "list_knowledge_documents",
    "extract_first_heading",
    "get_knowledge_stats",
]
