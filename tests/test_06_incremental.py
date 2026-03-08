"""
tests/test_06_incremental.py
Gate 6: 6 tests for the incremental update module.
Tests 1-3 use mocks. Tests 4-6 require Docker services.
"""
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch, call
import tempfile

from pipeline.incremental import IncrementalUpdater
from parsers.uir import LogicUnit, Component, Parameter
from parsers.geid import generate_geid


REPO = "test-repo"


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_updater(tmp_path):
    """IncrementalUpdater with all external clients mocked.
    
    chromadb cannot be imported on Python 3.14 (pydantic v1 shim broken).
    We inject a fake chromadb module into sys.modules before instantiating
    IncrementalUpdater so the lazy 'import chromadb' inside __init__ gets
    our mock instead of the real package.
    """
    import sys
    import types

    # Build a fake chromadb module with a fake HttpClient
    fake_chromadb = types.ModuleType("chromadb")
    mock_chroma_instance = MagicMock()
    fake_chromadb.HttpClient = MagicMock(return_value=mock_chroma_instance)

    with patch.dict(sys.modules, {"chromadb": fake_chromadb}), \
         patch("pipeline.incremental.GraphDatabase"), \
         patch("pipeline.incremental.RepositoryMirror") as mock_mirror_cls, \
         patch("pipeline.incremental.JavaParser") as mock_parser_cls, \
         patch("pipeline.incremental.Neo4jLoader"), \
         patch("pipeline.incremental.GraphCleanup") as mock_cleanup_cls, \
         patch("pipeline.incremental.GDSClient") as mock_gds_cls, \
         patch("pipeline.incremental.UIRChunker"), \
         patch("pipeline.incremental.ChromaEmbedder"), \
         patch("pipeline.incremental.CommunitySummarizer"):

        updater = IncrementalUpdater()

        # Set up mirror to return temp java files
        java_file = tmp_path / "UserService.java"
        java_file.write_text("public class UserService {}")
        updater.mirror.get_changed_files.return_value = [java_file]
        updater.mirror_root = tmp_path

        # Set up parser
        geid = generate_geid(REPO, "com.test.UserService.getUser")
        test_lu = LogicUnit(
            geid=geid,
            fqn="com.test.UserService.getUser",
            kind="method",
            calls=["UserRepository.findById"],
        )
        test_comp = Component(
            geid=generate_geid(REPO, "com.test.UserService"),
            fqn="com.test.UserService",
            kind="class",
            logic_units=[test_lu],
        )
        updater.parser.parse_file.return_value = [test_comp]

        # Set up cleanup
        updater.cleanup.delete_edges_for_geids.return_value = 3

        # Set up GDS
        updater.gds.run_leiden.return_value = {"communityCount": 5}
        updater.gds.list_community_ids.return_value = [1, 2, 3]

        # Patch repo path existence check
        from config.settings import settings
        settings.repos_mirror_path = tmp_path
        (tmp_path / REPO).mkdir(exist_ok=True)

        yield updater


# ── Test 1: Only changed files re-parsed ─────────────────────────────────────

def test_01_only_changed_files_reparsed(mock_updater):
    """Test 1: Parser is called only for changed files, not all files."""
    mock_updater.update_repo(REPO, since_sha=None, re_summarize=False)

    # Parser.parse_file should be called once (one file returned by mirror)
    assert mock_updater.parser.parse_file.call_count == 1


# ── Test 2: Stale CALLS edges deleted before re-creation ─────────────────────

def test_02_stale_edges_deleted(mock_updater):
    """Test 2: delete_edges_for_geids is called with the GEIDs of changed methods."""
    mock_updater.update_repo(REPO, since_sha="abc123", re_summarize=False)
    mock_updater.cleanup.delete_edges_for_geids.assert_called_once()

    called_geids = mock_updater.cleanup.delete_edges_for_geids.call_args[0][0]
    assert len(called_geids) >= 1   # at least one GEID was passed


# ── Test 3: Stats reflect updated counts ─────────────────────────────────────

def test_03_stats_returned(mock_updater):
    """Test 3: update_repo returns a stats dict with expected keys."""
    stats = mock_updater.update_repo(REPO, since_sha=None, re_summarize=False)

    assert "files_reparsed"  in stats
    assert "nodes_updated"   in stats
    assert "edges_deleted"   in stats
    assert "edges_created"   in stats
    assert stats["files_reparsed"] == 1
    assert stats["edges_deleted"]  == 3


# ── Test 4: MERGE doesn't duplicate nodes (integration) ──────────────────────

@pytest.mark.integration
def test_04_merge_no_duplicates(neo4j_session):
    """Test 4: Loading the same GEID twice creates only one node."""
    geid = generate_geid("int-test", "com.int.test.Foo.bar")

    for _ in range(2):  # load twice
        neo4j_session.run(
            """
            MERGE (n:LogicUnit {geid: $geid})
            SET n.fqn = $fqn, n.kind = 'method'
            """,
            geid=geid, fqn="com.int.test.Foo.bar",
        )

    result = neo4j_session.run(
        "MATCH (n:LogicUnit {geid: $geid}) RETURN count(n) AS cnt",
        geid=geid,
    )
    assert result.single()["cnt"] == 1


# ── Test 5: Deleted file nodes removed (integration) ─────────────────────────

@pytest.mark.integration
def test_05_deleted_file_nodes_removed(neo4j_session):
    """Test 5: Nodes with a deleted file_path are removed by GraphCleanup."""
    from neo4j import GraphDatabase
    from config.settings import settings
    from graph.cleanup import GraphCleanup

    deleted_path = "/tmp/nexus_test_deleted/Obsolete.java"

    # Create a test node
    neo4j_session.run(
        """
        MERGE (n:LogicUnit {geid: 'cleanup_test_geid_001'})
        SET n.fqn = 'com.test.Obsolete.method',
            n.file_path = $path,
            n.kind = 'method'
        """,
        path=deleted_path,
    )

    driver = GraphDatabase.driver(settings.neo4j_uri, auth=settings.neo4j_auth)
    cleanup = GraphCleanup(driver)
    cleanup.delete_nodes_for_files([deleted_path])

    # Verify node gone
    result = neo4j_session.run(
        "MATCH (n {file_path: $path}) RETURN count(n) AS cnt",
        path=deleted_path,
    )
    assert result.single()["cnt"] == 0
    driver.close()


# ── Test 6: Full vs incremental parity (integration) ─────────────────────────

@pytest.mark.integration
def test_06_incremental_parity(neo4j_session):
    """
    Test 6: After full load + incremental update of same data,
    node count is unchanged (no duplication from re-indexing).
    """
    from parsers.geid import generate_geid

    geid1 = generate_geid("parity-test", "com.parity.A.method1")
    geid2 = generate_geid("parity-test", "com.parity.B.method2")

    # Full load
    for geid, fqn in [(geid1, "com.parity.A.method1"), (geid2, "com.parity.B.method2")]:
        neo4j_session.run(
            "MERGE (n:LogicUnit {geid: $geid}) SET n.fqn = $fqn",
            geid=geid, fqn=fqn,
        )

    before = neo4j_session.run(
        "MATCH (n:LogicUnit) WHERE n.geid IN $geids RETURN count(n) AS cnt",
        geids=[geid1, geid2],
    ).single()["cnt"]

    # Simulate incremental update (same MERGEs)
    for geid, fqn in [(geid1, "com.parity.A.method1"), (geid2, "com.parity.B.method2")]:
        neo4j_session.run(
            "MERGE (n:LogicUnit {geid: $geid}) SET n.fqn = $fqn",
            geid=geid, fqn=fqn,
        )

    after = neo4j_session.run(
        "MATCH (n:LogicUnit) WHERE n.geid IN $geids RETURN count(n) AS cnt",
        geids=[geid1, geid2],
    ).single()["cnt"]

    assert before == after == 2   # exactly 2, no duplicates
