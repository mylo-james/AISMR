"""Integration tests for RAG functionality.

These tests require a running Llama Stack instance with vector I/O.
Skip in CI with: pytest -m "not integration"
"""

import uuid

import pytest

from myloware.agents.factory import create_persona_agent, create_rag_tool_config
from myloware.knowledge import (
    ingest_documents,
    register_knowledge_base,
    setup_project_knowledge,
)
from myloware.llama_clients import get_sync_client, verify_connection

pytestmark = pytest.mark.live


def _collect_agent_response(response) -> str:
    """Collect content from agent response (handles streaming)."""
    # If it's a generator/iterator, collect all chunks
    if hasattr(response, "__iter__") and not isinstance(response, (str, dict)):
        chunks = []
        final_content = ""
        for event in response:
            if hasattr(event, "event") and event.event:
                payload = event.event.payload

                # Streaming progress - delta.text
                if hasattr(payload, "delta") and payload.delta:
                    delta = payload.delta
                    if hasattr(delta, "text") and delta.text:
                        chunks.append(delta.text)

                # Turn complete - has full output_message
                if hasattr(payload, "turn") and payload.turn:
                    turn = payload.turn
                    if hasattr(turn, "output_message") and turn.output_message:
                        msg = turn.output_message
                        if hasattr(msg, "content") and msg.content:
                            final_content = msg.content

        # Prefer final content if available, otherwise join chunks
        return final_content if final_content else "".join(chunks)

    # Non-streaming response
    if hasattr(response, "completion_message"):
        msg = response.completion_message
        if hasattr(msg, "content"):
            return str(msg.content)

    return str(response)


@pytest.fixture
def client():
    """Provide a cached Llama Stack client."""
    return get_sync_client()


@pytest.fixture
def test_vector_db_id(client):
    """Create a test vector database and ingest seed documents."""
    conn = verify_connection(client)
    if not conn["success"]:
        pytest.skip(f"Llama Stack not available: {conn['error']}")

    project_id = f"test_{uuid.uuid4().hex[:8]}"
    vector_db_id = register_knowledge_base(client, project_id)

    docs = [
        {
            "id": "about-myloware",
            "content": (
                "AISMR is a Llama Stack-native multi-agent video production "
                "platform. It uses four persona agents: Ideator for ideation, Producer "
                "for production, Editor for editing, and Publisher for publishing."
            ),
            "metadata": {"type": "about"},
        },
        {
            "id": "tech-stack",
            "content": (
                "AISMR is built with Python, FastAPI, and Llama Stack. It uses "
                "pgvector for vector storage and supports integrations with "
                "OpenAI Sora, Remotion, and upload-post."
            ),
            "metadata": {"type": "technical"},
        },
    ]

    ingest_documents(client, vector_db_id, docs)

    return vector_db_id


@pytest.mark.integration
def test_setup_project_knowledge(client):
    """Smoke test for setup_project_knowledge helper."""
    conn = verify_connection(client)
    if not conn["success"]:
        pytest.skip(f"Llama Stack not available: {conn['error']}")

    project_id = f"helper_{uuid.uuid4().hex[:8]}"
    vector_db_id = setup_project_knowledge(client, project_id, documents=[])

    assert vector_db_id.startswith("project_kb_")


@pytest.mark.integration
def test_ideator_agent_uses_rag(client, test_vector_db_id):
    """Test the generic native RAG helper without widening a product role."""
    ideator = create_persona_agent(
        client,
        "rag_integration",
        "Use the provided knowledge search tool to answer from its results.",
        tools=[create_rag_tool_config(test_vector_db_id)],
    )

    session_id = ideator.create_session("test-rag-session")

    response = ideator.create_turn(
        messages=[
            {
                "role": "user",
                "content": "What is AISMR? Use the knowledge search to find out.",
            }
        ],
        session_id=session_id,
        stream=False,
    )

    content = _collect_agent_response(response).lower()
    assert any(
        [
            "llama stack" in content,
            "video production" in content,
            "persona" in content,
            "ideator" in content,
            "myloware" in content,
        ]
    ), f"Response should contain RAG-retrieved info: {content[:500]}"


@pytest.mark.integration
def test_knowledge_search_tool_called(client, test_vector_db_id):
    """Test the native knowledge-search tool outside the scoped ideator role."""
    ideator = create_persona_agent(
        client,
        "rag_integration",
        "Use the provided knowledge search tool to answer from its results.",
        tools=[create_rag_tool_config(test_vector_db_id)],
    )
    session_id = ideator.create_session("test-tool-call")

    response = ideator.create_turn(
        messages=[
            {
                "role": "user",
                "content": "Search the knowledge base for information about the tech stack.",
            }
        ],
        session_id=session_id,
        stream=False,
    )

    content = _collect_agent_response(response).lower()
    assert any(
        [
            "python" in content,
            "fastapi" in content,
            "llama stack" in content,
            "myloware" in content,
        ]
    ), f"Should retrieve tech stack info: {content[:500]}"
