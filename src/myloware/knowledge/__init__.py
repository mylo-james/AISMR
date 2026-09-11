"""AISMR knowledge module - Vector I/O and RAG setup."""

from myloware.knowledge.setup import (
    register_knowledge_base,
    ingest_documents,
    setup_project_knowledge,
    DEFAULT_CHUNK_SIZE,
)
from myloware.knowledge.loader import (
    load_knowledge_documents,
    load_documents_with_manifest,
    load_manifest,
    save_manifest,
    list_knowledge_documents,
    KnowledgeDocument,
    get_knowledge_dir,
)

__all__ = [
    "register_knowledge_base",
    "ingest_documents",
    "setup_project_knowledge",
    "DEFAULT_CHUNK_SIZE",
    "load_knowledge_documents",
    "load_documents_with_manifest",
    "load_manifest",
    "save_manifest",
    "list_knowledge_documents",
    "KnowledgeDocument",
    "get_knowledge_dir",
]
