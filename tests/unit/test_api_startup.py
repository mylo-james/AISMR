"""Tests for API server startup and lifespan."""

from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from myloware.knowledge.loader import KnowledgeDocument


def test_load_knowledge_documents_empty_dir():
    """Test loading documents when none are present."""
    with (
        patch("myloware.api.server.load_documents_with_manifest") as load_mock,
        patch("myloware.api.server.load_manifest", return_value=None),
        patch("myloware.api.server.save_manifest") as save_mock,
    ):
        load_mock.return_value = ([], {"hash": "h0", "files": []})

        from myloware.api.server import _load_knowledge_documents

        docs, manifest = _load_knowledge_documents(project_id="proj")
        assert docs == []
        assert manifest["hash"] == "h0"
        save_mock.assert_called_once()


def test_load_knowledge_documents_with_files():
    """Test converting KnowledgeDocument objects to ingestable dicts."""
    doc1 = KnowledgeDocument(
        id="kb_general_test1",
        content="Content here.",
        filename="test1.md",
        metadata={"document": "test1", "kb_type": "global", "chunk_index": 0},
    )
    doc2 = KnowledgeDocument(
        id="kb_general_test2",
        content="More content.",
        filename="test2.md",
        metadata={"document": "test2", "kb_type": "global", "chunk_index": 0},
    )

    with (
        patch(
            "myloware.api.server.load_documents_with_manifest",
            return_value=([doc1, doc2], {"hash": "h1"}),
        ),
        patch("myloware.api.server.save_manifest"),
    ):
        from myloware.api.server import _load_knowledge_documents

        docs, manifest = _load_knowledge_documents(project_id="proj")

        assert len(docs) == 2
        assert {d["id"] for d in docs} == {"kb_general_test1", "kb_general_test2"}
        assert all("content" in doc for doc in docs)
        assert all(doc["metadata"]["type"] == "knowledge" for doc in docs)
        assert manifest["hash"] == "h1"


def test_health_endpoint_public():
    """Test that health endpoint doesn't require authentication."""
    # Use fake Llama Stack to skip knowledge base setup
    with patch("myloware.api.server.settings") as mock_settings:
        mock_settings.llama_stack_provider = "fake"
        mock_settings.fail_fast_on_startup = False

        from myloware.api.server import app

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/health")
            # Should not be 401/403 (auth required)
            assert response.status_code in [200, 500]


def test_authenticated_endpoints_require_key():
    """Test that protected endpoints require API key."""
    with patch("myloware.api.server.settings") as mock_settings:
        mock_settings.llama_stack_provider = "fake"
        mock_settings.fail_fast_on_startup = False
        mock_settings.api_key = "test-key"

        from myloware.api.server import app

        with TestClient(app, raise_server_exceptions=False) as client:
            # Runs endpoint requires auth
            response = client.get("/v1/runs/some-id")
            assert response.status_code == 401

            # Chat supervisor endpoint requires auth
            response = client.post("/v1/chat/supervisor", json={"message": "test"})
            assert response.status_code == 401


def test_vector_db_id_from_app_state():
    """Test that vector_db_id dependency uses app state."""
    from fastapi import FastAPI, Request

    from myloware.api.dependencies import get_vector_db_id

    # Create mock request with app state
    mock_request = Mock(spec=Request)
    mock_request.app = Mock(spec=FastAPI)
    mock_request.app.state = Mock()
    mock_request.app.state.vector_db_id = "project_kb_test"

    result = get_vector_db_id(mock_request)
    assert result == "project_kb_test"


def test_vector_db_id_default():
    """Test that vector_db_id dependency has a sensible default."""
    from fastapi import FastAPI, Request

    from myloware.api.dependencies import get_vector_db_id

    # Create mock request without vector_db_id in state
    mock_request = Mock(spec=Request)
    mock_request.app = Mock(spec=FastAPI)
    mock_request.app.state = Mock(spec=[])  # Empty state

    result = get_vector_db_id(mock_request)
    assert result == "project_kb_myloware"
