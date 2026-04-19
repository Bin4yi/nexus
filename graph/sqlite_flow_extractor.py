"""
graph/sqlite_flow_extractor.py
Execution Flow Extraction using igraph weighted Dijkstra Shortest Path.

Reads directly from SQLite (structure_edges + calls_edges + nodes).
Drop-in replacement for FlowExtractor — same public interface and
same FlowPath dataclass (re-exported from graph.flow_extractor for
compatibility with FlowNarrativeSummarizer and downstream consumers).

Why no GDS / Neo4j:
  igraph.get_shortest_paths() runs in O(E log V) — same algorithmic
  complexity as Neo4j GDS Dijkstra, zero JVM overhead, ~5 MB of RAM
  for 100 repos worth of CALLS edges.
"""
from __future__ import annotations
import logging
import sqlite3
from pathlib import Path
from typing import Optional

import igraph as ig

from config.settings import settings
# Re-export FlowPath so callers that do `from graph.sqlite_flow_extractor import FlowPath`
# continue to work — but the canonical definition stays in flow_extractor.py.
from graph.flow_extractor import FlowPath  # noqa: F401

logger = logging.getLogger(__name__)

# Dijkstra edge weights — REMOTE_CALLS penalised as cross-service hops
_EDGE_WEIGHTS: dict[str, float] = {
    "CALLS":       1.0,
    "REMOTE_CALLS": 5.0,
    "INJECTS":     1.0,
    "IMPLEMENTS":  1.0,
    "EXTENDS":     1.0,
    "RESOLVES_TO": 1.0,
    "OVERRIDES":   1.0,
}


class SQLiteFlowExtractor:
    """
    Uses igraph Dijkstra Shortest Path to extract linear execution flows
    from EntryPoint → DataSink nodes.

    Drop-in replacement for FlowExtractor.
    Same public methods: extract_all_flows(), extract_flow_for_entry().
    """

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path or settings.sqlite_db_path)
        self._g: Optional[ig.Graph] = None
        self._fqn_to_vid: dict[str, int] = {}
        self._vid_to_fqn: dict[int, str] = {}
        self._vid_to_meta: dict[int, dict] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    def extract_all_flows(
        self,
        max_pairs: int = 200,
        max_path_length: int = 15,
    ) -> list[FlowPath]:
        """
        Extract shortest paths for all EntryPoint→DataSink pairs.

        Args:
            max_pairs:       Cap on (entry, sink) pairs evaluated.
            max_path_length: Discard paths longer than this.

        Returns:
            List of FlowPath objects sorted by path_length.
        """
        self._build_graph()
        pairs = self._get_entry_sink_pairs(max_pairs)
        if not pairs:
            logger.warning(
                "No EntryPoint→DataSink pairs found. "
                "Check that is_entry_point / is_data_sink flags are set in nodes table."
            )
            return []

        logger.info("Extracting shortest paths for %d entry→sink pairs", len(pairs))
        flows: list[FlowPath] = []
        for start_fqn, end_fqn in pairs:
            flow = self._extract_single_flow(start_fqn, end_fqn)
            if flow and flow.path_length <= max_path_length:
                self._enrich_flow(flow)
                flows.append(flow)

        logger.info(
            "Extracted %d valid execution flows (of %d pairs evaluated)",
            len(flows), len(pairs),
        )
        return sorted(flows, key=lambda f: f.path_length)

    def extract_flow_for_entry(
        self,
        entry_fqn: str,
        max_sinks: int = 10,
    ) -> list[FlowPath]:
        """
        Extract shortest paths from a specific EntryPoint to all reachable DataSinks.

        Args:
            entry_fqn: FQN of the entry point node.
            max_sinks: Maximum number of DataSinks to evaluate.

        Returns:
            List of FlowPath objects sorted by path_length.
        """
        self._build_graph()
        with sqlite3.connect(str(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT fqn FROM nodes WHERE is_data_sink = 1 ORDER BY fqn LIMIT ?",
                (max_sinks,),
            ).fetchall()
        sinks = [r[0] for r in rows if r[0] != entry_fqn]

        flows: list[FlowPath] = []
        for sink_fqn in sinks:
            flow = self._extract_single_flow(entry_fqn, sink_fqn)
            if flow:
                self._enrich_flow(flow)
                flows.append(flow)
        return sorted(flows, key=lambda f: f.path_length)

    # ── Private ───────────────────────────────────────────────────────────────

    def _build_graph(self) -> None:
        """
        Load structure_edges and calls_edges from SQLite into an igraph directed graph.
        Vertices are indexed by FQN; edges carry float weights.
        """
        with sqlite3.connect(str(self.db_path)) as conn:
            struct_rows: list[tuple] = conn.execute(
                "SELECT src_fqn, dst_fqn, rel_type FROM structure_edges"
            ).fetchall()
            calls_rows: list[tuple] = conn.execute(
                "SELECT caller_fqn, callee_fqn FROM calls_edges"
            ).fetchall()
            node_rows: list[tuple] = conn.execute(
                """
                SELECT fqn, kind, file_path, is_entry_point, is_data_sink
                FROM   nodes
                """
            ).fetchall()

        # Build vertex list from known nodes table (authoritative source)
        fqn_list = [r[0] for r in node_rows]
        self._fqn_to_vid = {fqn: i for i, fqn in enumerate(fqn_list)}
        self._vid_to_fqn = {i: fqn for i, fqn in enumerate(fqn_list)}
        self._vid_to_meta = {
            i: {
                "fqn":            r[0],
                "kind":           r[1],
                "file_path":      r[2],
                "is_entry_point": r[3],
                "is_data_sink":   r[4],
            }
            for i, r in enumerate(node_rows)
        }

        edges:   list[tuple[int, int]] = []
        weights: list[float]           = []

        def _add(src_fqn: str, dst_fqn: str, weight: float) -> None:
            sv = self._fqn_to_vid.get(src_fqn)
            dv = self._fqn_to_vid.get(dst_fqn)
            if sv is not None and dv is not None and sv != dv:
                edges.append((sv, dv))
                weights.append(weight)

        for src, dst, rel in struct_rows:
            _add(src, dst, _EDGE_WEIGHTS.get(rel, 1.0))

        for caller, callee in calls_rows:
            _add(caller, callee, 1.0)

        self._g = ig.Graph(
            n=len(fqn_list),
            edges=edges,
            directed=True,
            edge_attrs={"weight": weights},
        )
        logger.info(
            "Flow graph built: %d nodes, %d edges", len(fqn_list), len(edges)
        )

    def _get_entry_sink_pairs(self, max_pairs: int) -> list[tuple[str, str]]:
        """Return up to max_pairs (entry_fqn, sink_fqn) tuples."""
        with sqlite3.connect(str(self.db_path)) as conn:
            entries = [
                r[0] for r in conn.execute(
                    "SELECT fqn FROM nodes WHERE is_entry_point = 1 ORDER BY fqn LIMIT ?",
                    (max_pairs,),
                ).fetchall()
            ]
            sinks = [
                r[0] for r in conn.execute(
                    "SELECT fqn FROM nodes WHERE is_data_sink = 1 ORDER BY fqn LIMIT ?",
                    (max_pairs,),
                ).fetchall()
            ]

        pairs: list[tuple[str, str]] = []
        for e in entries:
            for s in sinks:
                if e != s:
                    pairs.append((e, s))
                    if len(pairs) >= max_pairs:
                        return pairs
        return pairs

    def _extract_single_flow(
        self,
        start_fqn: str,
        end_fqn: str,
    ) -> Optional[FlowPath]:
        """
        Run igraph Dijkstra from start_fqn to end_fqn.
        Returns None if no path exists or vertices not in graph.
        """
        if self._g is None:
            return None

        start_vid = self._fqn_to_vid.get(start_fqn)
        end_vid   = self._fqn_to_vid.get(end_fqn)
        if start_vid is None or end_vid is None:
            return None

        try:
            # get_shortest_paths returns a list of vertex-id lists (one per target)
            result = self._g.get_shortest_paths(
                start_vid,
                to=end_vid,
                weights="weight",
                mode="out",
            )
            path_vids: list[int] = result[0] if result else []
        except Exception as e:
            logger.debug("No path %s → %s: %s", start_fqn, end_fqn, e)
            return None

        if not path_vids:
            return None

        path_fqns = [self._vid_to_fqn.get(v, f"v:{v}") for v in path_vids]
        path_node_details = [
            {**self._vid_to_meta.get(v, {}), "id": v}
            for v in path_vids
        ]
        return FlowPath(
            entry_point_fqn  = start_fqn,
            data_sink_fqn    = end_fqn,
            path_fqns        = path_fqns,
            path_node_details= path_node_details,
            path_length      = len(path_fqns),
        )

    def _enrich_flow(self, flow: FlowPath) -> None:
        """Add config keys and table names touched by nodes along the path."""
        path_fqns = [d.get("fqn") for d in flow.path_node_details if d.get("fqn")]
        if not path_fqns:
            return

        with sqlite3.connect(str(self.db_path)) as conn:
            ph = ",".join("?" * len(path_fqns))

            # Config keys read along the path
            prop_rows = conn.execute(
                f"""
                SELECT lu_fqn, prop_key FROM property_edges
                WHERE  lu_fqn IN ({ph}) AND direction = 'read'
                """,
                path_fqns,
            ).fetchall()
            for _, key in prop_rows:
                if key not in flow.config_keys:
                    flow.config_keys.append(key)

            # Database tables touched along the path
            table_rows = conn.execute(
                f"""
                SELECT component_fqn, table_name FROM datasink_tables
                WHERE  component_fqn IN ({ph})
                """,
                path_fqns,
            ).fetchall()
            for _, tname in table_rows:
                if tname not in flow.table_names:
                    flow.table_names.append(tname)
