"""
pipeline/incremental.py
File-scoped incremental re-indexing.
Re-parses only changed .java files, upserts updated nodes, removes stale edges
across all relationship types — no Neo4j required.
"""
from __future__ import annotations
import logging
import sqlite3
from pathlib import Path
from typing import Optional

from config.settings import settings
from pipeline.mirror import RepositoryMirror
from parsers.java_parser import JavaParser
from parsers.uir import Component, LogicUnit, Parameter, FieldDeclaration
from graph.sqlite_loader import SQLiteLoader
from graph.igraph_community import IGraphCommunityClient
from vectorstore.chunker import UIRChunker
from vectorstore.embedder import ChromaEmbedder
from community.summarizer import CommunitySummarizer

logger = logging.getLogger(__name__)

_SQL_EXTENSIONS    = {".sql"}
_CONFIG_EXTENSIONS = {".xml", ".properties", ".yaml", ".yml", ".toml"}


class IncrementalUpdater:
    """
    Handles file-scoped incremental updates when a PR is merged.

    Algorithm:
    1. Compute changed files via git diff
    2. Re-parse changed .java files → fresh UIR objects
    3. Delete ALL stale outgoing edges from changed nodes (all relationship types)
    4. UPSERT updated nodes (idempotent INSERT OR REPLACE)
    5. Re-create ALL edge types
    6. Cross-repo CALLS pass 2 (inferred suffix matching)
    7. Re-run Leiden on the full structure graph
    8. Re-summarize only communities whose membership changed
    """

    def __init__(self):
        import chromadb
        self.chroma = chromadb.HttpClient(
            host=settings.chroma_host, port=settings.chroma_port
        )
        self.loader     = SQLiteLoader()
        self.igraph_comm= IGraphCommunityClient()
        self.mirror     = RepositoryMirror()
        self.parser     = JavaParser()
        self.chunker    = UIRChunker()
        self.embedder   = ChromaEmbedder(self.chroma)
        self.summarizer = CommunitySummarizer(self.igraph_comm, self.chroma)
        self.loader.open()

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
            "files_reparsed":           0,
            "nodes_updated":            0,
            "edges_deleted":            0,
            "edges_created":            0,
            "communities_resummarized": 0,
            "sql_rescanned":            False,
            "config_rescanned":         False,
        }

        all_changed    = self.mirror.get_changed_files(repo_path, since_sha)
        java_files     = [f for f in all_changed if f.suffix == ".java"]
        sql_changed    = any(f.suffix in _SQL_EXTENSIONS    for f in all_changed)
        config_changed = any(f.suffix in _CONFIG_EXTENSIONS for f in all_changed)

        stats["files_reparsed"] = len(java_files)
        logger.info(
            "Incremental update %s: %d Java, sql=%s, config=%s",
            repo_name, len(java_files), sql_changed, config_changed,
        )

        if not java_files and not sql_changed and not config_changed:
            return stats

        all_components:  list[Component]  = []
        all_logic_units: list[LogicUnit]  = []
        all_fqns:        list[str]        = []
        all_chunks = []

        for jf in java_files:
            comps = self.parser.parse_file(jf, repo_name)
            for comp in comps:
                all_components.append(comp)
                all_fqns.append(comp.fqn)
                for lu in comp.logic_units:
                    all_logic_units.append(lu)
                    all_fqns.append(lu.fqn)
                all_chunks.extend(self.chunker.chunk_all(comp.logic_units))
            stats["nodes_updated"] += sum(len(c.logic_units) for c in comps)

        # Delete stale outgoing edges for changed FQNs
        if all_fqns:
            stats["edges_deleted"] = self._delete_stale_edges(all_fqns)

        if all_logic_units:
            self.loader.load_call_graph(all_logic_units)
            self.loader.load_unresolved_calls(all_logic_units)
            stats["edges_created"] += sum(len(lu.calls) for lu in all_logic_units)
            self.loader.load_type_edges(all_logic_units)
            self.loader.load_throws_edges(all_logic_units)
            self.loader.load_overrides_edges(all_logic_units)
            self.loader.load_instantiates_edges(all_logic_units)
            self.loader.load_property_access_edges(all_logic_units)

        if all_components:
            self.loader.load_implements_extends(all_components)
            self.loader.load_injection_edges(all_components)
            self.loader.load_annotated_with(all_components)

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
                config_files  = (
                    list(repo_path.rglob("*.properties"))
                    + list(repo_path.rglob("*.xml"))
                    + list(repo_path.rglob("*.yaml"))
                )
                configs, config_edges = config_parser.scan_config_files(config_files, repo_name)
                self.loader.load_configuration_nodes(configs)
                self.loader.load_reads_config_edges(config_edges)
                stats["config_rescanned"] = True
                logger.info("Re-scanned config: %d nodes, %d read edges", len(configs), len(config_edges))
            except Exception as e:
                logger.warning("Config re-scan failed: %s", e)

        if all_chunks:
            self.embedder.upsert_chunks(all_chunks)

        # Re-run Leiden on the full graph (community shapes may change)
        self.igraph_comm.run_leiden(
            max_levels=settings.leiden_max_levels,
            gamma=settings.leiden_gamma,
        )

        # Re-summarize affected communities
        if re_summarize and settings.community_summarization_enabled and all_fqns:
            affected = self._find_affected_communities(all_fqns)
            for cid in affected:
                self.summarizer.summarize_community(cid)
            stats["communities_resummarized"] = len(affected)

        logger.info("Incremental update complete: %s", stats)
        return stats

    def _delete_stale_edges(self, fqns: list[str]) -> int:
        """Delete all outgoing edges from the given FQNs across all edge tables."""
        total = 0
        with sqlite3.connect(str(self.loader.db_path)) as conn:
            ph = ",".join("?" * len(fqns))
            for table, col in [
                ("calls_edges",     "caller_fqn"),
                ("structure_edges", "src_fqn"),
                ("property_edges",  "lu_fqn"),
            ]:
                cur = conn.execute(
                    f"DELETE FROM {table} WHERE {col} IN ({ph})", fqns
                )
                total += cur.rowcount
            conn.commit()
        logger.info("Deleted %d stale edges for %d FQNs", total, len(fqns))
        return total

    def _find_affected_communities(self, fqns: list[str]) -> list[int]:
        """Find community IDs containing any of the changed FQNs."""
        with sqlite3.connect(str(self.loader.db_path)) as conn:
            ph = ",".join("?" * len(fqns))
            rows = conn.execute(
                f"""
                SELECT DISTINCT community_id FROM nodes
                WHERE fqn IN ({ph}) AND community_id IS NOT NULL
                """,
                fqns,
            ).fetchall()
        return [r[0] for r in rows]
