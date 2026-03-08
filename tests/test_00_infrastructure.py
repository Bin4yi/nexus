"""
tests/test_00_infrastructure.py
Gate 0: 9 infrastructure health-check tests.
Requires all Docker services to be running: docker-compose up -d
"""
import pytest
from pathlib import Path


# ── Neo4j tests (tests 1-4) ───────────────────────────────────────────────────

@pytest.mark.integration
def test_neo4j_reachable(neo4j_driver):
    """Test 1: Neo4j is reachable and auth is valid."""
    # verify_connectivity() raises if connection fails
    neo4j_driver.verify_connectivity()


@pytest.mark.integration
def test_gds_plugin_loaded(neo4j_session):
    """Test 2: GDS plugin is installed and callable."""
    result = neo4j_session.run("CALL gds.version() YIELD version RETURN version")
    record = result.single()
    assert record is not None, "gds.version() returned no result"
    assert isinstance(record["version"], str), "GDS version should be a string"
    assert record["version"] != "", "GDS version should not be empty"


@pytest.mark.integration
def test_apoc_plugin_loaded(neo4j_session):
    """Test 3: APOC plugin is installed and callable."""
    result = neo4j_session.run("CALL apoc.version() YIELD version RETURN version")
    record = result.single()
    assert record is not None, "apoc.version() returned no result"
    assert isinstance(record["version"], str)


@pytest.mark.integration
def test_neo4j_auth_works(neo4j_session):
    """Test 4: Authenticated Cypher query succeeds."""
    result = neo4j_session.run("RETURN 1 AS value")
    record = result.single()
    assert record["value"] == 1


# ── ChromaDB tests (tests 5-6) ────────────────────────────────────────────────

@pytest.mark.integration
def test_chromadb_reachable(chroma_client):
    """Test 5: ChromaDB heartbeat returns successfully."""
    # HttpClient raises on connection failure
    # Attempt to list collections as a connectivity check
    collections = chroma_client.list_collections()
    assert isinstance(collections, list)


@pytest.mark.integration
def test_chromadb_collection_crud(chroma_client):
    """Test 6: Can create, use, and delete a ChromaDB collection."""
    name = "_nexus_infra_test"
    # Create
    col = chroma_client.get_or_create_collection(name)
    assert col.name == name
    # Delete
    chroma_client.delete_collection(name)
    remaining = [c.name for c in chroma_client.list_collections()]
    assert name not in remaining


# ── Redis tests (test 7) ──────────────────────────────────────────────────────

@pytest.mark.integration
def test_redis_reachable(redis_client):
    """Test 7: Redis ping returns PONG."""
    assert redis_client.ping() is True


# ── Settings tests (tests 8-9) ────────────────────────────────────────────────

def test_settings_load():
    """Test 8: Settings load and NEO4J_URI is set."""
    from config.settings import settings
    assert settings.neo4j_uri.startswith("bolt://")
    assert settings.chroma_port == 8000
    assert settings.redis_url.startswith("redis://")


def test_max_context_tokens_hardcoded():
    """Test 9: MAX_CONTEXT_TOKENS is exactly 8000 (GraphRAG spec requirement)."""
    from config.settings import settings
    assert settings.max_context_tokens == 8000, (
        f"MAX_CONTEXT_TOKENS must be 8000, got {settings.max_context_tokens}"
    )
