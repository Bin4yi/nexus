"""
graph/igraph_community.py
Leiden community detection using python-igraph + leidenalg.
Drop-in replacement for GDSClient — same public interface.

Reads structure_edges from SQLite, builds an undirected igraph.Graph,
runs Leiden (RBConfigurationVertexPartition), and writes community_id
back to the nodes table in SQLite.

No JVM required. RAM: ~50 MB for 100 repos (vs. 1-3 GB Neo4j JVM heap).
"""
from __future__ import annotations
import logging
import sqlite3
from pathlib import Path
from typing import Optional

import igraph as ig
import leidenalg

from config.settings import settings

logger = logging.getLogger(__name__)


class IGraphCommunityClient:
    """
    Replaces GDSClient. Exposes the same interface so CommunitySummarizer
    needs zero changes.

    Public API:
        run_leiden()                → dict with communityCount, modularity
        list_community_ids()        → list[int]
        get_nodes_by_community(cid) → list[dict]
        get_community_count()       → int
        get_null_community_count()  → int
    """

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path or settings.sqlite_db_path)
        # Populated by run_leiden(); used to avoid re-querying SQLite for list_community_ids
        self._community_map: dict[str, int] = {}

    # ── Public interface (mirrors GDSClient) ──────────────────────────────────

    def run_leiden(
        self,
        max_levels: int = 10,
        gamma: float = 1.0,
        theta: float = 0.01,
        write_property: str = "community_id",
        **_kwargs,
    ) -> dict:
        """
        Run Leiden community detection.

        Args:
            max_levels:     n_iterations for leidenalg (default 10)
            gamma:          resolution_parameter (higher → more, smaller communities)
            theta:          unused (kept for API parity with GDSClient)
            write_property: ignored (always writes to community_id column)

        Returns:
            dict matching GDS result shape: {"communityCount": int, "modularity": float}
        """
        logger.info("IGraphCommunityClient: loading graph from %s", self.db_path)
        g, fqn_list = self._load_graph()

        if g.vcount() == 0:
            logger.warning("No nodes in structure_edges — Leiden skipped")
            return {"communityCount": 0, "modularity": 0.0}

        logger.info("Running Leiden: %d nodes, %d edges", g.vcount(), g.ecount())

        partition = leidenalg.find_partition(
            g,
            leidenalg.RBConfigurationVertexPartition,
            resolution_parameter=gamma,
            n_iterations=max_levels,
            seed=42,
        )

        membership = partition.membership          # list[int], index == vertex index in fqn_list
        self._community_map = {
            fqn: cid for fqn, cid in zip(fqn_list, membership)
        }

        # Write community_id back to SQLite nodes table
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executemany(
                "UPDATE nodes SET community_id = ? WHERE fqn = ?",
                [(cid, fqn) for fqn, cid in self._community_map.items()],
            )
            conn.commit()

        n_communities = (max(membership) + 1) if membership else 0
        modularity    = partition.quality()
        logger.info(
            "Leiden complete: %d communities, modularity=%.4f",
            n_communities, modularity,
        )
        return {"communityCount": n_communities, "modularity": modularity}

    def list_community_ids(self) -> list[int]:
        """Return sorted list of unique community IDs."""
        if self._community_map:
            return sorted(set(self._community_map.values()))
        with sqlite3.connect(str(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT DISTINCT community_id FROM nodes WHERE community_id IS NOT NULL"
            ).fetchall()
        return sorted(r[0] for r in rows)

    def get_nodes_by_community(self, community_id: int) -> list[dict]:
        """Return a list of node attribute dicts for a given community_id."""
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT fqn, geid, node_type, kind, file_path,
                       start_line, end_line, community_id, visibility,
                       is_entry_point, is_data_sink, entry_point_score
                FROM   nodes
                WHERE  community_id = ?
                ORDER  BY fqn
                """,
                (community_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_community_count(self) -> int:
        """Return the number of distinct non-null community IDs."""
        with sqlite3.connect(str(self.db_path)) as conn:
            return conn.execute(
                "SELECT COUNT(DISTINCT community_id) FROM nodes WHERE community_id IS NOT NULL"
            ).fetchone()[0]

    def get_null_community_count(self) -> int:
        """Return number of nodes without a community assignment."""
        with sqlite3.connect(str(self.db_path)) as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE community_id IS NULL"
            ).fetchone()[0]

    def get_community_boundary_edges(self, community_id: int) -> list[dict]:
        """
        Return inter-community edges originating from this community.
        Used by CommunitySummarizer to show how communities connect.

        Returns list of dicts: {"edge_type": str, "target_community": int, "cnt": int}
        sorted by cnt descending, capped at 10.
        """
        with sqlite3.connect(str(self.db_path)) as conn:
            # Calls edges crossing community boundary
            rows = conn.execute(
                """
                SELECT 'CALLS' AS edge_type,
                       n2.community_id AS target_community,
                       COUNT(*) AS cnt
                FROM   calls_edges ce
                JOIN   nodes n1 ON ce.caller_fqn = n1.fqn
                JOIN   nodes n2 ON ce.callee_fqn = n2.fqn
                WHERE  n1.community_id = ?
                  AND  n2.community_id IS NOT NULL
                  AND  n2.community_id != ?
                GROUP  BY n2.community_id
                UNION ALL
                SELECT se.rel_type,
                       n2.community_id AS target_community,
                       COUNT(*) AS cnt
                FROM   structure_edges se
                JOIN   nodes n1 ON se.src_fqn = n1.fqn
                JOIN   nodes n2 ON se.dst_fqn = n2.fqn
                WHERE  n1.community_id = ?
                  AND  n2.community_id IS NOT NULL
                  AND  n2.community_id != ?
                GROUP  BY se.rel_type, n2.community_id
                ORDER  BY cnt DESC
                LIMIT  10
                """,
                (community_id, community_id, community_id, community_id),
            ).fetchall()
        return [
            {"edge_type": r[0], "target_community": r[1], "cnt": r[2]}
            for r in rows
        ]

    # ── Private ───────────────────────────────────────────────────────────────

    def _load_graph(self) -> tuple[ig.Graph, list[str]]:
        """
        Build an undirected igraph.Graph from structure_edges.

        Returns:
            (graph, fqn_list) where fqn_list[i] is the FQN of vertex i.
        """
        with sqlite3.connect(str(self.db_path)) as conn:
            edge_rows: list[tuple[str, str]] = conn.execute(
                "SELECT src_fqn, dst_fqn FROM structure_edges"
            ).fetchall()

        if not edge_rows:
            logger.warning("structure_edges table is empty — no graph to project")
            return ig.Graph(), []

        # Collect unique FQNs and build index
        fqn_set: set[str] = set()
        for src, dst in edge_rows:
            fqn_set.add(src)
            fqn_set.add(dst)

        fqn_list = sorted(fqn_set)
        fqn_to_idx = {fqn: i for i, fqn in enumerate(fqn_list)}

        edges = [
            (fqn_to_idx[src], fqn_to_idx[dst])
            for src, dst in edge_rows
            if src in fqn_to_idx and dst in fqn_to_idx
        ]

        # Undirected graph — Leiden works on undirected topology
        g = ig.Graph(n=len(fqn_list), edges=edges, directed=False)
        g.vs["name"] = fqn_list
        return g, fqn_list
