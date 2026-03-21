"""
pipeline/incremental.py
File-scoped incremental re-indexing.
Re-parses only changed .java files, MERGEs updated nodes, and removes stale edges
across ALL 14 relationship types — not just CALLS.
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
from parsers.uir import Component, LogicUnit, Parameter, FieldDeclaration
from graph.loader import Neo4jLoader
from graph.cleanup import GraphCleanup
from graph.gds_client import GDSClient
from vectorstore.chunker import UIRChunker
from vectorstore.embedder import ChromaEmbedder
from community.summarizer import CommunitySummarizer

logger = logging.getLogger(__name__)

# File extensions that trigger SQL/config re-scanning
_SQL_EXTENSIONS    = {".sql"}
_CONFIG_EXTENSIONS = {".xml", ".properties", ".yaml", ".yml", ".toml"}


class IncrementalUpdater:
    """
    Handles file-scoped incremental updates when a PR is merged.

    Algorithm:
    1. Compute changed files via git diff
    2. Re-parse changed .java files → fresh UIR objects
    3. Delete ALL stale outgoing edges from changed nodes (all relationship types)
    4. MERGE updated nodes (properties only — no node duplication)
    5. Re-create ALL edge types:
       CALLS, INJECTS, ANNOTATED_WITH, IMPLEMENTS/EXTENDS,
       RETURNS/RECEIVES, THROWS, OVERRIDES, INSTANTIATES
       (+ SQL/config edges if .sql/.xml files changed)
    6. Cross-repo CALLS pass 2 (inferred suffix matching)
    7. Re-run Leiden on affected communities only
    8. Re-summarize only communities whose membership changed
    """

    def __init__(self):
        import chromadb
        self.neo4j_driver = GraphDatabase.driver(
            settings.neo4j_uri, auth=settings.neo4j_auth
        )
        self.chroma = chromadb.HttpClient(
            host=settings.chroma_host, port=settings.chroma_port
        )
        self.mirror     = RepositoryMirror()
        self.parser     = JavaParser()
        self.loader     = Neo4jLoader(self.neo4j_driver)
        self.cleanup    = GraphCleanup(self.neo4j_driver)
        self.gds        = GDSClient(self.neo4j_driver)
        self.chunker    = UIRChunker()
        self.embedder   = ChromaEmbedder(self.chroma)
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

        stats: dict = {
            "files_reparsed":          0,
            "nodes_updated":           0,
            "edges_deleted":           0,
            "edges_created":           0,
            "communities_resummarized": 0,
            "sql_rescanned":           False,
            "config_rescanned":        False,
        }

        # Step 1: Get changed files (all types, not just .java)
        all_changed = self.mirror.get_changed_files(repo_path, since_sha)
        java_files    = [f for f in all_changed if f.suffix == ".java"]
        sql_changed   = any(f.suffix in _SQL_EXTENSIONS    for f in all_changed)
        config_changed = any(f.suffix in _CONFIG_EXTENSIONS for f in all_changed)

        stats["files_reparsed"] = len(java_files)
        logger.info(
            "Incremental update %s: %d Java, sql=%s, config=%s",
            repo_name, len(java_files), sql_changed, config_changed,
        )

        if not java_files and not sql_changed and not config_changed:
            return stats

        # Step 2: Re-parse changed .java files → fresh UIR
        all_components: list[Component] = []
        all_logic_units: list[LogicUnit] = []
        all_node_geids: list[str] = []
        all_chunks = []

        for jf in java_files:
            comps = self.parser.parse_file(jf, repo_name)
            for comp in comps:
                all_components.append(comp)
                for lu in comp.logic_units:
                    all_logic_units.append(lu)
                    all_node_geids.append(lu.geid)
                all_node_geids.append(comp.geid)
                all_chunks.extend(self.chunker.chunk_all(comp.logic_units))
            stats["nodes_updated"] += sum(len(c.logic_units) for c in comps)

        # Step 3: Delete ALL stale outgoing edges from changed nodes
        if all_node_geids:
            stats["edges_deleted"] = self.cleanup.delete_all_edges_for_geids(all_node_geids)

        # Step 4: Re-load nodes via MERGE (idempotent — no duplicates)
        # The main loader handles node upsert; edges are cleared above so
        # re-running the loaders re-creates them fresh.

        if all_logic_units:
            # ── Tier 1 edges ──────────────────────────────────────────────────
            self.loader.load_call_graph(all_logic_units)
            # Cross-repo CALLS pass 2
            self.loader.load_unresolved_calls(all_logic_units)
            stats["edges_created"] += sum(len(lu.calls) for lu in all_logic_units)

            # ── Tier 2 edges ──────────────────────────────────────────────────
            self.loader.load_type_edges(all_logic_units)
            self.loader.load_throws_edges(all_logic_units)
            self.loader.load_overrides_edges(all_logic_units)
            self.loader.load_instantiates_edges(all_logic_units)

        if all_components:
            self.loader.load_implements_extends(all_components)
            self.loader.load_injection_edges(all_components)
            self.loader.load_annotated_with(all_components)

        # ── SQL and config re-scan (repo-scoped) ──────────────────────────────
        if sql_changed:
            try:
                from parsers.sql_schema_parser import SQLSchemaParser
                sql_parser = SQLSchemaParser()
                sql_files  = list(repo_path.rglob("*.sql"))
                tables, query_edges = sql_parser.scan_sql_scripts(sql_files, repo_name)
                self.loader.load_database_tables(tables)
                self.loader.load_queries_table_edges(query_edges)
                stats["sql_rescanned"] = True
                logger.info("Re-scanned SQL: %d tables, %d query edges", len(tables), len(query_edges))
            except Exception as e:
                logger.warning("SQL re-scan failed: %s", e)

        if config_changed:
            try:
                from parsers.config_parser import ConfigurationParser
                config_parser = ConfigurationParser()
                config_files  = list(repo_path.rglob("*.properties")) + \
                                list(repo_path.rglob("*.xml")) + \
                                list(repo_path.rglob("*.yaml"))
                configs, config_edges = config_parser.scan_config_files(config_files, repo_name)
                self.loader.load_configuration_nodes(configs)
                self.loader.load_config_read_edges(config_edges)
                stats["config_rescanned"] = True
                logger.info("Re-scanned config: %d nodes, %d read edges", len(configs), len(config_edges))
            except Exception as e:
                logger.warning("Config re-scan failed: %s", e)

        # Re-embed changed chunks
        if all_chunks:
            self.embedder.upsert_chunks(all_chunks)

        # Step 7: Re-run Leiden (full graph — community shapes may change)
        self.gds.run_leiden()

        # Step 8: Re-summarize affected communities
        if re_summarize and settings.community_summarization_enabled and all_node_geids:
            affected = self._find_affected_communities(all_node_geids)
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
                MATCH (n)
                WHERE n.geid IN $geids AND n.community_id IS NOT NULL
                RETURN DISTINCT n.community_id AS cid
                """,
                geids=changed_geids,
            )
            return [r["cid"] for r in result]
