"""Unit tests for knowledge base setup."""

from unittest.mock import Mock
from pathlib import Path
from types import SimpleNamespace

import pytest

from myloware.knowledge.setup import (
    ingest_documents,
    setup_project_knowledge,
    register_knowledge_base,
    get_existing_vector_store,
)

UPLOAD_CACHE_PATH = Path("data/.kb_upload_cache.json")


def test_get_existing_vector_store_found():
    """Test that existing knowledge base is detected."""
    mock_client = Mock()
    mock_store = Mock()
    mock_store.name = "project_kb_test"  # Function matches by name
    mock_store.id = "vs_12345"  # Returns the ID
    mock_client.vector_stores.list.return_value = [mock_store]

    result = get_existing_vector_store(mock_client, "project_kb_test")

    assert result == "vs_12345"
    mock_client.vector_stores.list.assert_called_once()


def test_get_existing_vector_store_not_found():
    """Test that missing knowledge base returns None."""
    mock_client = Mock()
    mock_client.vector_stores.list.return_value = []

    result = get_existing_vector_store(mock_client, "project_kb_test")

    assert result is None


def test_get_existing_vector_store_error():
    """Test that errors return None."""
    mock_client = Mock()
    mock_client.vector_stores.list.side_effect = Exception("Connection error")

    result = get_existing_vector_store(mock_client, "project_kb_test")

    assert result is None


def test_register_knowledge_base_success():
    """Test successful vector store creation."""
    mock_client = Mock()
    mock_store = Mock()
    mock_store.id = "vs_12345"
    mock_client.vector_stores.create.return_value = mock_store

    vector_store_id = register_knowledge_base(mock_client, "test_project")

    assert vector_store_id == "vs_12345"
    mock_client.vector_stores.create.assert_called_once()
    call_kwargs = mock_client.vector_stores.create.call_args.kwargs
    assert call_kwargs["name"] == "project_kb_test_project"
    assert "chunking_strategy" in call_kwargs


def test_register_knowledge_base_already_exists():
    """Test that already-existing stores are handled gracefully."""
    mock_client = Mock()
    mock_client.vector_stores.create.side_effect = Exception("already exists")

    vector_store_id = register_knowledge_base(mock_client, "existing_project")

    # Returns the expected name even if creation fails with "already exists"
    assert vector_store_id == "project_kb_existing_project"


def test_register_knowledge_base_error():
    """Test that non-idempotent errors are raised."""
    mock_client = Mock()
    mock_client.vector_stores.create.side_effect = Exception("Connection refused")

    with pytest.raises(Exception, match="Connection refused"):
        register_knowledge_base(mock_client, "test_project")


def test_register_knowledge_base_reuses_existing_vector_store():
    """Existing stores are reused without attempting to create a new one."""
    mock_client = Mock()
    mock_store = Mock()
    mock_store.name = "project_kb_proj"
    mock_store.id = "vs_existing"
    mock_client.vector_stores.list.return_value = [mock_store]

    vector_store_id = register_knowledge_base(mock_client, "proj")
    assert vector_store_id == "vs_existing"
    mock_client.vector_stores.create.assert_not_called()


def test_register_knowledge_base_includes_embedding_dimension():
    """Embedding dimension is forwarded via extra_body when provided."""
    mock_client = Mock()
    mock_client.vector_stores.list.return_value = []
    mock_store = Mock()
    mock_store.id = "vs_123"
    mock_client.vector_stores.create.return_value = mock_store

    vector_store_id = register_knowledge_base(
        mock_client,
        "proj",
        provider_id="pgvector",
        embedding_dimension=1234,
    )

    assert vector_store_id == "vs_123"
    call_kwargs = mock_client.vector_stores.create.call_args.kwargs
    assert call_kwargs["extra_body"]["embedding_dimension"] == 1234


def test_register_knowledge_base_falls_back_to_milvus_when_pgvector_missing(monkeypatch):
    """When provider_id is not specified, retry with milvus if pgvector is unavailable."""
    from myloware.knowledge import setup as kb_setup

    mock_client = Mock()
    mock_client.vector_stores.list.return_value = []
    mock_store = Mock()
    mock_store.id = "vs_12345"

    monkeypatch.setattr(kb_setup.settings, "milvus_uri", "")

    def create_side_effect(**kwargs):  # type: ignore[no-untyped-def]
        provider = (kwargs.get("extra_body") or {}).get("provider_id")
        if provider == "pgvector":
            raise Exception("Invalid value: Provider `pgvector` not found")
        assert provider == "milvus"
        return mock_store

    mock_client.vector_stores.create.side_effect = create_side_effect

    vector_store_id = kb_setup.register_knowledge_base(mock_client, "proj", provider_id=None)
    assert vector_store_id == "vs_12345"


def test_setup_project_knowledge_force_reingest_deletes_store_and_recreates(monkeypatch):
    """Force mode deletes the store and recreates it so docs are re-ingested."""
    from myloware.knowledge import setup as kb_setup

    mock_client = Mock()

    calls = {"n": 0}

    def fake_get_existing(_client, _name):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        return "vs_existing_123" if calls["n"] == 1 else None

    monkeypatch.setattr(kb_setup, "get_existing_vector_store", fake_get_existing)
    monkeypatch.setattr(kb_setup, "_upload_documents", lambda *_a, **_k: ["file_1"])
    monkeypatch.setattr(kb_setup, "register_knowledge_base", lambda *_a, **_k: "vs_new")
    monkeypatch.setattr(kb_setup.time, "sleep", lambda *_a, **_k: None)

    vector_store_id = kb_setup.setup_project_knowledge(
        mock_client,
        "proj",
        documents=[{"content": "hello"}],
        force_reingest=True,
    )

    assert vector_store_id == "vs_new"
    mock_client.vector_stores.delete.assert_called_once_with("vs_existing_123")


def test_register_knowledge_base_already_exists_returns_existing_id_if_listable():
    """If create says 'already exists' and list() can find it, return the ID."""
    mock_client = Mock()
    mock_client.vector_stores.create.side_effect = Exception("already exists")

    mock_store = Mock()
    mock_store.name = "project_kb_proj"
    mock_store.id = "vs_existing"
    mock_client.vector_stores.list.side_effect = [[], [mock_store]]

    vector_store_id = register_knowledge_base(mock_client, "proj")
    assert vector_store_id == "vs_existing"


def _build_mock_client() -> Mock:
    # Clear upload cache between tests to avoid reuse masking behavior
    try:
        UPLOAD_CACHE_PATH.unlink()
    except FileNotFoundError:
        pass

    mock_client = Mock()
    mock_store = Mock()
    mock_store.id = "vs_test"
    mock_client.vector_stores.create.return_value = mock_store
    mock_client.vector_stores.list.return_value = []

    mock_file = Mock()
    mock_file.id = "file_12345"
    mock_client.files.create.return_value = mock_file

    return mock_client


def test_ingest_documents_text_content():
    """Test ingesting documents with text content."""
    mock_client = _build_mock_client()

    docs = [
        {"id": "doc1", "content": "Hello world"},
        {"id": "doc2", "content": "Goodbye world"},
    ]

    count = ingest_documents(mock_client, "test_kb", docs)

    assert count == 2
    # Should upload 2 files
    assert mock_client.files.create.call_count == 2
    # Should add files to vector store via batch
    mock_client.vector_stores.file_batches.create.assert_called_once()


def test_ingest_documents_single_file():
    """Test ingesting a single document uses individual file API."""
    mock_client = _build_mock_client()

    docs = [{"id": "doc1", "content": "Hello world"}]

    count = ingest_documents(mock_client, "test_kb", docs)

    assert count == 1
    mock_client.files.create.assert_called_once()
    mock_client.vector_stores.files.create.assert_called_once()


def test_ingest_documents_url_warning():
    """Test that URL documents are skipped with warning."""
    mock_client = _build_mock_client()

    docs = [
        {"id": "url-doc", "url": "https://example.com/doc.txt"},
    ]

    count = ingest_documents(mock_client, "test_kb", docs)

    # URLs are not supported in new API, so count should be 0
    assert count == 0
    mock_client.files.create.assert_not_called()


def test_ingest_documents_with_metadata():
    """Test that metadata filename is used."""
    mock_client = _build_mock_client()

    docs = [
        {
            "id": "doc-meta",
            "content": "Test content",
            "metadata": {"filename": "custom.txt", "source": "test"},
        },
    ]

    ingest_documents(mock_client, "test_kb", docs)

    call_args = mock_client.files.create.call_args
    # The file tuple should include the custom filename
    file_tuple = call_args.kwargs["file"]
    assert file_tuple[0] == "custom.txt"


def test_ingest_documents_auto_id():
    """Test that document IDs are auto-generated if not provided."""
    mock_client = _build_mock_client()

    docs = [{"content": "No ID provided"}]

    count = ingest_documents(mock_client, "test_kb", docs)

    assert count == 1
    mock_client.files.create.assert_called_once()


def test_ingest_documents_missing_content():
    """Test that ValueError is raised when document has no content or url."""
    mock_client = _build_mock_client()

    docs = [
        {"id": "bad-doc", "metadata": {"only": "metadata"}},
    ]

    with pytest.raises(ValueError, match="must have either 'content' or 'url'"):
        ingest_documents(mock_client, "test_kb", docs)


def test_ingest_documents_raises_when_vector_store_add_fails():
    """Errors adding files to vector store are raised."""
    mock_client = _build_mock_client()
    mock_client.vector_stores.file_batches.create.side_effect = RuntimeError("add failed")

    docs = [
        {"id": "d1", "content": "hello"},
        {"id": "d2", "content": "world"},
    ]

    with pytest.raises(RuntimeError, match="add failed"):
        ingest_documents(mock_client, "test_kb", docs)


def test_setup_project_knowledge_creates_and_ingests():
    """Test convenience helper creates store and ingests documents."""
    mock_client = _build_mock_client()

    docs = [{"content": "hello"}]

    vector_store_id = setup_project_knowledge(mock_client, "proj", docs)

    assert vector_store_id == "vs_test"
    mock_client.vector_stores.create.assert_called_once()
    mock_client.files.create.assert_called_once()


def test_setup_project_knowledge_skips_ingest_when_no_docs():
    """Ensure ingestion is skipped when no documents provided."""
    mock_client = _build_mock_client()

    setup_project_knowledge(mock_client, "proj", documents=None)

    mock_client.files.create.assert_not_called()


def test_setup_project_knowledge_skips_when_exists():
    """Test that setup is skipped when knowledge base already exists."""
    mock_client = Mock()
    mock_store = Mock()
    mock_store.name = "project_kb_proj"  # Function matches by name
    mock_store.id = "vs_existing_123"  # Returns the ID
    mock_client.vector_stores.list.return_value = [mock_store]

    # Mock vector_store_has_files to return True (has documents)
    mock_client.vector_stores.files.list.return_value = [Mock()]

    vector_store_id = setup_project_knowledge(mock_client, "proj", documents=[{"content": "test"}])

    # Should return existing ID without creating
    assert vector_store_id == "vs_existing_123"
    mock_client.vector_stores.create.assert_not_called()


def test_setup_project_knowledge_ingests_into_existing_empty_store(monkeypatch):
    """If store exists but is empty, ingest docs into it."""
    mock_client = Mock()
    mock_store = Mock()
    mock_store.name = "project_kb_proj"
    mock_store.id = "vs_existing_123"
    mock_client.vector_stores.list.return_value = [mock_store]
    mock_client.vector_stores.files.list.return_value = []

    ingest_called = {}

    def fake_ingest(_client, vector_store_id: str, documents, chunk_size=512):  # noqa: ARG001
        ingest_called["vector_store_id"] = vector_store_id
        ingest_called["documents"] = documents
        return 1

    monkeypatch.setattr("myloware.knowledge.setup.ingest_documents", fake_ingest)

    vector_store_id = setup_project_knowledge(mock_client, "proj", documents=[{"content": "test"}])
    assert vector_store_id == "vs_existing_123"
    assert ingest_called["vector_store_id"] == "vs_existing_123"


def test_setup_project_knowledge_returns_existing_id_when_empty_and_no_docs():
    """If store exists and no docs are provided, return the existing id."""
    mock_client = Mock()
    mock_store = Mock()
    mock_store.name = "project_kb_proj"
    mock_store.id = "vs_existing_123"
    mock_client.vector_stores.list.return_value = [mock_store]
    mock_client.vector_stores.files.list.return_value = []

    vector_store_id = setup_project_knowledge(mock_client, "proj", documents=None)
    assert vector_store_id == "vs_existing_123"


def test_setup_project_knowledge_documents_but_no_uploads_creates_empty_store(monkeypatch):
    """If documents are provided but none upload, create an empty store."""
    from myloware.knowledge import setup as kb_setup

    mock_client = Mock()
    monkeypatch.setattr(kb_setup, "get_existing_vector_store", lambda _client, _name: None)
    monkeypatch.setattr(kb_setup, "_upload_documents", lambda _client, _docs: [])
    monkeypatch.setattr(kb_setup, "register_knowledge_base", lambda *_a, **_k: "vs_created")

    vector_store_id = kb_setup.setup_project_knowledge(
        mock_client, "proj", documents=[{"url": "https://example.com"}]
    )
    assert vector_store_id == "vs_created"


def test_setup_project_knowledge_returns_store_name_in_fake_provider_mode(monkeypatch):
    """Non-mock clients return the deterministic store name in fake-provider mode."""
    from myloware.knowledge import setup as kb_setup

    class DummyClient:
        pass

    monkeypatch.setattr(kb_setup.settings, "use_fake_providers", True)
    monkeypatch.setattr(kb_setup, "get_existing_vector_store", lambda _client, _name: None)
    monkeypatch.setattr(kb_setup, "_upload_documents", lambda _client, _docs: ["file_1"])
    monkeypatch.setattr(kb_setup, "register_knowledge_base", lambda *_a, **_k: "vs_created")

    vector_store_id = kb_setup.setup_project_knowledge(
        DummyClient(), "proj", documents=[{"content": "hello"}]
    )
    assert vector_store_id == "project_kb_proj"


def test_setup_project_knowledge_returns_created_id_for_real_client_in_real_mode(monkeypatch):
    """Non-mock clients return the created store id when not using fake providers."""
    from myloware.knowledge import setup as kb_setup

    class DummyClient:
        pass

    monkeypatch.setattr(kb_setup.settings, "use_fake_providers", False)
    monkeypatch.setattr(kb_setup, "get_existing_vector_store", lambda _client, _name: None)
    monkeypatch.setattr(kb_setup, "_upload_documents", lambda _client, _docs: ["file_1"])
    monkeypatch.setattr(kb_setup, "register_knowledge_base", lambda *_a, **_k: "vs_created")

    vector_store_id = kb_setup.setup_project_knowledge(
        DummyClient(), "proj", documents=[{"content": "hello"}]
    )
    assert vector_store_id == "vs_created"


def test_private_upload_documents_continues_on_upload_error():
    """A single upload failure should not abort the batch."""
    from myloware.knowledge import setup as kb_setup

    mock_client = Mock()
    mock_client.files.create.side_effect = [
        SimpleNamespace(id="file_ok"),
        Exception("nope"),
    ]

    docs = [
        {"id": "d1", "content": "hello"},
        {"id": "d2", "content": "world"},
        {"id": "url-only", "url": "https://example.com/doc.txt"},
    ]

    file_ids = kb_setup._upload_documents(mock_client, docs)
    assert file_ids == ["file_ok"]


def test_vector_store_has_files_returns_false_on_error():
    from myloware.knowledge import setup as kb_setup

    mock_client = Mock()
    mock_client.vector_stores.files.list.side_effect = RuntimeError("boom")

    assert kb_setup.vector_store_has_files(mock_client, "vs1") is False


def test_log_knowledge_retrieval_logs_top_docs_and_scores(monkeypatch):
    from myloware.knowledge import setup as kb_setup

    class Result:
        def __init__(self, document: str, score: float):
            self.metadata = {"document": document}
            self.score = score

    captured = {}

    def fake_info(event: str, **kwargs):
        captured["event"] = event
        captured["kwargs"] = kwargs

    monkeypatch.setattr(kb_setup.logger, "info", fake_info)

    kb_setup.log_knowledge_retrieval(
        query="q" * 300,
        vector_store_id="vs1",
        results=[Result("doc1", 0.9), "raw"],
        result_count=2,
    )

    assert captured["event"] == "rag_query"
    assert captured["kwargs"]["query"] == "q" * 200
    assert captured["kwargs"]["top_documents"][0] == "doc1"
    assert captured["kwargs"]["top_scores"][0] == 0.9


def test_search_vector_store_passes_kwargs_and_returns_results(monkeypatch):
    from myloware.knowledge import setup as kb_setup

    mock_client = Mock()
    mock_client.vector_stores.search.return_value = SimpleNamespace(data=[1, 2])

    logged = {}

    def fake_log(*, query: str, vector_store_id: str, results, result_count: int):
        logged["query"] = query
        logged["vector_store_id"] = vector_store_id
        logged["result_count"] = result_count

    monkeypatch.setattr(kb_setup, "log_knowledge_retrieval", fake_log)

    results = kb_setup.search_vector_store(mock_client, "vs1", "hello", max_results=3)
    assert results == [1, 2]
    assert logged["vector_store_id"] == "vs1"
    assert logged["result_count"] == 2
    mock_client.vector_stores.search.assert_called_once()
    call_kwargs = mock_client.vector_stores.search.call_args.kwargs
    assert call_kwargs["vector_store_id"] == "vs1"
    assert call_kwargs["query"] == "hello"
    assert call_kwargs["max_num_results"] == 3
    assert "search_mode" not in call_kwargs
    assert "ranking_options" not in call_kwargs


def test_search_vector_store_includes_hybrid_ranking_options(monkeypatch):
    from myloware.knowledge import setup as kb_setup

    mock_client = Mock()
    mock_client.vector_stores.search.return_value = SimpleNamespace(data=[])

    monkeypatch.setattr(kb_setup, "log_knowledge_retrieval", lambda **_kwargs: None)

    kb_setup.search_vector_store(
        mock_client,
        "vs1",
        "hello",
        search_mode="hybrid",
        ranking_options={"ranker": {"type": "rrf", "impact_factor": 60.0}},
    )

    call_kwargs = mock_client.vector_stores.search.call_args.kwargs
    assert call_kwargs["search_mode"] == "hybrid"
    assert call_kwargs["ranking_options"]["ranker"]["type"] == "rrf"


def test_search_vector_store_returns_empty_list_on_error(monkeypatch):
    from myloware.knowledge import setup as kb_setup

    mock_client = Mock()
    mock_client.vector_stores.search.side_effect = RuntimeError("nope")

    results = kb_setup.search_vector_store(mock_client, "vs1", "hello")
    assert results == []
