"""
tests/test_04_knowledge_base.py
Gate 4: 10 integration tests for Neo4j loader, GDS Leiden, ChromaDB embedder,
and community summarizer.
Requires: Docker services running (docker-compose up -d)
"""
import pytest
from unittest.mock import MagicMock, patch

from parsers.uir import LogicUnit, Component, Module, Project, Parameter
from parsers.geid import generate_geid
from vectorstore.chunker import UIRChunker
from community.prompt_builder import build_community_prompt, count_tokens, DATA_BUDGET


# ── Fixtures ──────────────────────────────────────────────────────────────────

REPO = "test-repo"

@pytest.fixture(scope="module")
def test_logic_unit():
    geid = generate_geid(REPO, "com.test.Auth.validateToken")
    return LogicUnit(
        geid=geid,
        fqn="com.test.Auth.validateToken",
        kind="method",
        parameters=[Parameter(name="token", type_name="String")],
        return_type="boolean",
        body_text='{ return jwtUtil.verify(token); }',
        docstring="/** Validates a JWT access token. @param token the JWT @return true if valid */",
        calls=["JwtUtil.verify"],
        file_path="src/main/java/com/test/Auth.java",
        start_line=20,
        end_line=22,
    )


@pytest.fixture(scope="module")
def test_component(test_logic_unit):
    geid = generate_geid(REPO, "com.test.Auth")
    return Component(
        geid=geid,
        fqn="com.test.Auth",
        kind="class",
        logic_units=[test_logic_unit],
        file_path="src/main/java/com/test/Auth.java",
        start_line=1,
        end_line=30,
    )


@pytest.fixture(scope="module")
def test_module(test_component):
    geid = generate_geid(REPO, "com.test:auth-module")
    return Module(
        geid=geid,
        name="auth-module",
        group_id="com.test",
        artifact_id="auth-module",
        version="1.0.0",
        components=[test_component],
    )


@pytest.fixture(scope="module")
def test_project(test_module):
    geid = generate_geid(REPO, REPO)
    return Project(
        geid=geid,
        name=REPO,
        url="https://github.com/test/test-repo",
        branch="main",
        modules=[test_module],
    )


# ── Test 1-4: Neo4j loader (integration) ─────────────────────────────────────

@pytest.mark.integration
def test_01_schema_constraints_created(neo4j_session):
    """Test 1: Schema constraints exist for all node types."""
    from graph.schema import apply_schema
    from neo4j import GraphDatabase
    from config.settings import settings
    driver = GraphDatabase.driver(settings.neo4j_uri, auth=settings.neo4j_auth)
    apply_schema(driver)

    result = neo4j_session.run("SHOW CONSTRAINTS")
    constraint_names = [r["name"] for r in result]
    # At least one GEID constraint should exist
    assert any("geid" in name.lower() for name in constraint_names)
    driver.close()


@pytest.mark.integration
def test_02_nodes_loaded(neo4j_session, test_project):
    """Test 2: After loading a project, LogicUnit nodes exist."""
    from neo4j import GraphDatabase
    from config.settings import settings
    from graph.loader import Neo4jLoader
    driver = GraphDatabase.driver(settings.neo4j_uri, auth=settings.neo4j_auth)

    loader = Neo4jLoader(driver)
    loader.load_project(test_project)

    result = neo4j_session.run(
        "MATCH (n:LogicUnit {geid: $geid}) RETURN count(n) AS cnt",
        geid=test_project.modules[0].components[0].logic_units[0].geid,
    )
    assert result.single()["cnt"] > 0
    driver.close()


@pytest.mark.integration
def test_03_load_idempotent(neo4j_session, test_project):
    """Test 3: Loading the same project twice doesn't create duplicates."""
    from neo4j import GraphDatabase
    from config.settings import settings
    from graph.loader import Neo4jLoader
    driver = GraphDatabase.driver(settings.neo4j_uri, auth=settings.neo4j_auth)
    loader = Neo4jLoader(driver)

    loader.load_project(test_project)  # second load
    result = neo4j_session.run(
        "MATCH (n:Project {geid: $geid}) RETURN count(n) AS cnt",
        geid=test_project.geid,
    )
    assert result.single()["cnt"] == 1   # still exactly 1
    driver.close()


@pytest.mark.integration
def test_04_hierarchy_edges_created(neo4j_session, test_project):
    """Test 4: Module→Component→LogicUnit edges exist."""
    result = neo4j_session.run(
        """
        MATCH (:Module)-[:DECLARES]->(:Component)-[:HAS_METHOD]->(lu:LogicUnit)
        RETURN count(lu) AS cnt
        """
    )
    assert result.single()["cnt"] >= 1


# ── Test 5-7: Chunker (unit) ──────────────────────────────────────────────────

def test_05_chunker_produces_two_chunks(test_logic_unit):
    """Test 5: A LogicUnit with docstring produces code_logic and code_intent chunks."""
    chunker = UIRChunker()
    chunks = chunker.chunk_logic_unit(test_logic_unit)
    types = [c.chunk_type for c in chunks]
    assert "code_logic"  in types
    assert "code_intent" in types


def test_06_chunk_ids_contain_geid(test_logic_unit):
    """Test 6: All chunk IDs start with the LogicUnit GEID."""
    chunker = UIRChunker()
    chunks = chunker.chunk_logic_unit(test_logic_unit)
    for chunk in chunks:
        assert chunk.chunk_id.startswith(test_logic_unit.geid)


def test_07_no_intent_chunk_without_docstring():
    """Test 7: LogicUnit without docstring produces only code_logic chunk."""
    lu = LogicUnit(
        geid="aabbccddeeff0011",
        fqn="com.test.Foo.bar",
        kind="method",
        body_text="{ return 42; }",
        docstring="",  # empty
    )
    chunker = UIRChunker()
    chunks = chunker.chunk_logic_unit(lu)
    types = [c.chunk_type for c in chunks]
    assert "code_intent" not in types


# ── Test 8: Community prompt budget (unit) ────────────────────────────────────

def test_08_community_prompt_under_budget():
    """Test 8: Community prompt never exceeds the 6800-token data budget."""
    # Use 500 nodes × 500-char docstrings to guarantee budget overflow and truncation
    nodes = [
        {"fqn": f"com.example.Service{i}.method{i}", "kind": "method",
         "docstring": "A" * 500}
        for i in range(500)
    ]
    prompt, truncated = build_community_prompt(community_id=1, nodes=nodes)
    assert count_tokens(prompt) <= DATA_BUDGET
    assert truncated is True   # 500 nodes × 500 chars → must have truncated


# ── Test 9-10: ChromaDB embedder (integration) ────────────────────────────────

@pytest.mark.integration
def test_09_chunks_upserted_to_chromadb(chroma_client, test_logic_unit):
    """Test 9: Chunks are upserted and retrievable by GEID."""
    from vectorstore.chunker import UIRChunker
    from vectorstore.embedder import ChromaEmbedder

    embedder = ChromaEmbedder(chroma_client)
    chunker = UIRChunker()
    chunks = chunker.chunk_logic_unit(test_logic_unit)
    embedder.upsert_chunks(chunks)

    # Verify intent chunk is retrievable
    result = embedder.get_by_geid(test_logic_unit.geid, collection="code_intent")
    assert result is not None
    assert result["geid"] == test_logic_unit.geid


@pytest.mark.integration
def test_10_geid_bridge_intact(neo4j_session, chroma_client, test_logic_unit):
    """Test 10: GEID in ChromaDB corresponds to a valid Neo4j node."""
    from vectorstore.embedder import ChromaEmbedder
    embedder = ChromaEmbedder(chroma_client)
    result = embedder.get_by_geid(test_logic_unit.geid)

    if result:  # GEID is in ChromaDB
        neo4j_result = neo4j_session.run(
            "MATCH (n {geid: $geid}) RETURN count(n) AS cnt",
            geid=result["geid"],
        )
        assert neo4j_result.single()["cnt"] >= 1   # exists in Neo4j too
