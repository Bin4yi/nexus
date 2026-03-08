"""
pipeline/incremental.py
File-scoped incremental re-indexing.
Re-parses only changed .java files, MERGEs updated nodes, and removes stale edges.
chromadb is imported lazily inside __init__.
"""
from __future__ import annotations
import logging
from pathlib import Path
from typing import Optional

from neo4j import GraphDatabase

from config.settings import settings
from pipeline.mirror import RepositoryMirror
from parsers.java_parser import JavaParser
from parsers.geid import generate_geid
from graph.loader import Neo4jLoader
from graph.cleanup import GraphCleanup
from graph.gds_client import GDSClient
from vectorstore.chunker import UIRChunker
from vectorstore.embedder import ChromaEmbedder
from community.summarizer import CommunitySummarizer

logger = logging.getLogger(__name__)


class IncrementalUpdater:
    """
    Handles file-scoped incremental updates when a PR is merged.

    Algorithm:
    1. Compute changed .java files via git diff
    2. Re-parse only those files → new UIR objects
    3. Delete stale CALLS edges from changed LogicUnits
    4. MERGE updated nodes (properties only — no node duplication)
    5. Re-create CALLS edges from fresh AST
    6. Re-run Leiden on affected communities only
    7. Re-summarize only communities whose membership changed
    """

    def __init__(self):
        import chromadb
        self.neo4j_driver = GraphDatabase.driver(
            settings.neo4j_uri, auth=settings.neo4j_auth
        )
        self.chroma = chromadb.HttpClient(
            host=settings.chroma_host, port=settings.chroma_port
        )
        self.mirror    = RepositoryMirror()
        self.parser    = JavaParser()
        self.loader    = Neo4jLoader(self.neo4j_driver)
        self.cleanup   = GraphCleanup(self.neo4j_driver)
        self.gds       = GDSClient(self.neo4j_driver)
        self.chunker   = UIRChunker()
        self.embedder  = ChromaEmbedder(self.chroma)
        self.summarizer = CommunitySummarizer(self.gds, self.chroma)

    def update_repo(
        self,
        repo_name: str,
        since_sha: Optional[str] = None,
        re_summarize: bool = True,
    ) -> dict:
        """
        Incrementally update a single repository.

        Args:
            repo_name:    Short name of the repo (must exist in ./mirror/)
            since_sha:    Git SHA to compute diff from (None = all files)
            re_summarize: Whether to re-summarize affected communities

        Returns:
            Stats dict with files re-parsed, nodes updated, edges refreshed
        """
        repo_path = settings.repos_mirror_path / repo_name
        if not repo_path.exists():
            raise ValueError(f"Repo not mirrored: {repo_path}")

        stats = {
            "files_reparsed":   0,
            "nodes_updated":    0,
            "edges_deleted":    0,
            "edges_created":    0,
            "communities_resummarized": 0,
        }

        # Step 1: Get changed files
        changed_files = self.mirror.get_changed_files(repo_path, since_sha)
        stats["files_reparsed"] = len(changed_files)
        logger.info("Incremental update: %d changed files in %s", len(changed_files), repo_name)

        if not changed_files:
            return stats

        # Step 2: Re-parse changed files → fresh UIR
        all_logic_units = []
        all_geids = []
        all_chunks = []

        for jf in changed_files:
            components = self.parser.parse_file(jf, repo_name)
            for comp in components:
                for lu in comp.logic_units:
                    all_logic_units.append(lu)
                    all_geids.append(lu.geid)
                all_chunks.extend(self.chunker.chunk_all(comp.logic_units))
            stats["nodes_updated"] += sum(len(c.logic_units) for c in components)

        # Step 3: Delete stale outgoing CALLS edges from changed nodes
        stats["edges_deleted"] = self.cleanup.delete_edges_for_geids(all_geids)

        # Step 4 + 5: MERGE updated nodes and re-create CALLS edges
        # Loader uses MERGE — safe to call without duplication
        self.loader.load_call_graph(all_logic_units)
        stats["edges_created"] = sum(len(lu.calls) for lu in all_logic_units)

        # Re-embed changed chunks
        self.embedder.upsert_chunks(all_chunks)

        # Step 6: Re-run Leiden (full graph — community shapes may change)
        self.gds.run_leiden()

        # Step 7: Re-summarize affected communities
        if re_summarize and settings.community_summarization_enabled:
            affected = self._find_affected_communities(all_geids)
            for cid in affected:
                self.summarizer.summarize_community(cid)
            stats["communities_resummarized"] = len(affected)

        logger.info("Incremental update complete: %s", stats)
        return stats

    def _find_affected_communities(self, changed_geids: list[str]) -> list[int]:
        """Find community IDs containing any of the changed GEIDs."""
        with self.neo4j_driver.session() as session:
            result = session.run(
                """
                MATCH (n:LogicUnit)
                WHERE n.geid IN $geids AND n.community_id IS NOT NULL
                RETURN DISTINCT n.community_id AS cid
                """,
                geids=changed_geids,
            )
            return [r["cid"] for r in result]
