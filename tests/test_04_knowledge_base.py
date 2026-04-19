"""
tests/test_04_knowledge_base.py
Gate 4: Integration tests for SQLiteLoader, igraph Leiden, ChromaDB embedder,
and community summarizer.
Requires: Docker services running (docker-compose up -d)
"""
import sqlite3
import tempfile
from pathlib import Path

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


@pytest.fixture(scope="module")
def tmp_db():
    """Temporary SQLite database for loader integration tests."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        yield Path(f.name)


# ── Test 1-4: SQLiteLoader (integration) ──────────────────────────────────────

def test_01_schema_created(tmp_db):
    """Test 1: Schema tables exist after opening SQLiteLoader."""
    from graph.sqlite_loader import SQLiteLoader
    loader = SQLiteLoader(db_path=tmp_db)
    loader.open()
    with sqlite3.connect(str(tmp_db)) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
    assert "nodes" in tables
    assert "calls_edges" in tables
    assert "structure_edges" in tables
    assert "repo_meta" in tables
    loader.close()


def test_02_nodes_loaded(tmp_db, test_project):
    """Test 2: After loading a project, LogicUnit rows exist in nodes table."""
    from graph.sqlite_loader import SQLiteLoader
    loader = SQLiteLoader(db_path=tmp_db)
    loader.open()
    loader.load_project(test_project)
    with sqlite3.connect(str(tmp_db)) as conn:
        cnt = conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE node_type = 'LogicUnit'"
        ).fetchone()[0]
    assert cnt > 0
    loader.close()


def test_03_load_idempotent(tmp_db, test_project):
    """Test 3: Loading the same project twice doesn't create duplicates."""
    from graph.sqlite_loader import SQLiteLoader
    loader = SQLiteLoader(db_path=tmp_db)
    loader.open()
    loader.load_project(test_project)  # second load — INSERT OR REPLACE
    with sqlite3.connect(str(tmp_db)) as conn:
        cnt = conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE fqn = 'com.test.Auth'"
        ).fetchone()[0]
    assert cnt == 1   # still exactly 1
    loader.close()


def test_04_sha_tracking(tmp_db):
    """Test 4: get/set_ingested_sha round-trips correctly."""
    from graph.sqlite_loader import SQLiteLoader
    loader = SQLiteLoader(db_path=tmp_db)
    loader.open()
    loader.set_ingested_sha("my-repo", "abc123def456")
    sha = loader.get_ingested_sha("my-repo")
    assert sha == "abc123def456"
    loader.close()


# ── Test 5-7: Chunker (unit) ──────────────────────────────────────────────────

def test_05_chunker_produces_two_chunks(test_logic_unit):
    """Test 5: A LogicUnit with docstring produces code_logic and code_intent chunks."""
    chunker = UIRChunker()
    chunks = chunker.chunk_logic_unit(test_logic_unit)
    types = [c.chunk_type for c in chunks]
    assert "code_logic"  in types
    assert "code_intent" in types
    assert len(chunks) == 2, f"Expected exactly 2 chunks, got {len(chunks)}: {types}"


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
    nodes = [
        {"fqn": f"com.example.Service{i}.method{i}", "kind": "method",
         "docstring": "A" * 500}
        for i in range(500)
    ]
    prompt, truncated = build_community_prompt(community_id=1, nodes=nodes)
    assert count_tokens(prompt) <= DATA_BUDGET
    assert truncated is True


# ── Test 9-10: ChromaDB embedder (integration) ────────────────────────────────

@pytest.mark.integration
def test_09_chunks_upserted_to_chromadb(chroma_client, test_logic_unit):
    """Test 9: Chunks are upserted and retrievable by GEID."""
    from vectorstore.chunker import UIRChunker
    from vectorstore.embedder import ChromaEmbedder

    embedder = ChromaEmbedder(chroma_client)
    chunker  = UIRChunker()
    chunks   = chunker.chunk_logic_unit(test_logic_unit)
    embedder.upsert_chunks(chunks)

    result = embedder.get_by_geid(test_logic_unit.geid, collection="code_intent")
    assert result is not None
    assert result["geid"] == test_logic_unit.geid


@pytest.mark.integration
def test_10_geid_bridge_intact(chroma_client, test_logic_unit, tmp_db):
    """Test 10: GEID in ChromaDB corresponds to a valid SQLite node."""
    from vectorstore.embedder import ChromaEmbedder
    import sqlite3
    embedder = ChromaEmbedder(chroma_client)
    result   = embedder.get_by_geid(test_logic_unit.geid)

    if result:  # GEID is in ChromaDB
        with sqlite3.connect(str(tmp_db)) as conn:
            cnt = conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE geid = ?", (result["geid"],)
            ).fetchone()[0]
        assert cnt >= 1   # exists in SQLite too
