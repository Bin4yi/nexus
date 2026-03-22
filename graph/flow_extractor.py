"""
graph/flow_extractor.py
Execution Flow Extraction — uses Neo4j GDS Dijkstra Shortest Path
to find the primary execution path between EntryPoints and DataSinks.

**CRITICAL DESIGN DECISION:**
We do NOT use brute-force variable-length Cypher (e.g., ``*1..6``)
as this causes combinatorial explosion and OOM on large graphs.
Instead we use the GDS shortest path algorithm which operates on the
projected in-memory graph and terminates in O(E log V) time.

Workflow:
    1. Project the code graph into GDS (reusing execution-flow edges only)
    2. For each (EntryPoint, DataSink) pair, run gds.shortestPath.dijkstra
    3. Collect the linear path of FQNs + any Configuration nodes they touch
    4. Return structured FlowPath objects for the narrative generator
"""
from __future__ import annotations
import logging
from dataclasses import dataclass, field
from typing import Optional

from neo4j import Driver
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential
from neo4j.exceptions import ServiceUnavailable, SessionExpired

from config.settings import settings

logger = logging.getLogger(__name__)

_flow_retry = retry(
    retry=retry_if_exception_type((ServiceUnavailable, SessionExpired, ConnectionError)),
    stop=stop_after_attempt(settings.retry_max_attempts),
    wait=wait_exponential(multiplier=settings.retry_backoff_seconds, min=1, max=30),
    reraise=True,
)

# Relationship types to project for shortest-path analysis.
# Only execution-flow edges — no utility/metadata edges.
_FLOW_RELATIONSHIPS = [
    "CALLS", "INJECTS", "IMPLEMENTS", "EXTENDS",
    "DEPENDS_ON", "HAS_METHOD", "DECLARES", "REMOTE_CALLS",
    "OVERRIDES", "QUERIES_TABLE", "READS_CONFIG", "RESOLVES_TO",
]

# Sprint 3: Edge weights — CALLS = 1 (local), REMOTE_CALLS = 5 (cross-service penalty)
# All other edges default to weight 1.0.
_EDGE_WEIGHTS: dict[str, float] = {
    "CALLS": 1.0,
    "REMOTE_CALLS": 5.0,
    "INJECTS": 1.0,
    "IMPLEMENTS": 1.0,
    "EXTENDS": 1.0,
    "DEPENDS_ON": 1.0,
    "HAS_METHOD": 1.0,
    "DECLARES": 1.0,
    "OVERRIDES": 1.0,
    "QUERIES_TABLE": 1.0,
    "READS_CONFIG": 1.0,
    "RESOLVES_TO": 1.0,
}

# Node labels in the flow graph
_FLOW_NODE_LABELS = [
    "Component", "LogicUnit", "Module",
    "DatabaseTable", "Configuration",
]

# GDS projection name for flow extraction (separate from Leiden)
_FLOW_GRAPH_NAME = "nexus-flow-graph"


@dataclass
class FlowPath:
    """A single API-to-Database execution path."""
    entry_point_fqn: str
    data_sink_fqn: str
    path_fqns: list[str]                 # ordered list of FQNs along the path
    path_node_details: list[dict]        # full node data for each hop
    config_keys: list[str] = field(default_factory=list)   # configs read along the path
    table_names: list[str] = field(default_factory=list)   # tables queried along the path
    path_length: int = 0


class FlowExtractor:
    """
    Uses Neo4j GDS Dijkstra Shortest Path to extract linear execution
    flows from EntryPoint → DataSink without combinatorial explosion.
    """

    def __init__(self, driver: Driver):
        self.driver = driver

    def extract_all_flows(self, max_pairs: int = 200, max_path_length: int = 15) -> list[FlowPath]:
        """
        Extract shortest paths from all EntryPoint → DataSink pairs.

        Args:
            max_pairs:       Cap on the number of (entry, sink) pairs to evaluate.
                             Prevents runaway on very large graphs.
            max_path_length: Discard paths longer than this (likely false positives).

        Returns:
            List of FlowPath objects, sorted by path length.
        """
        # Step 1: Project the flow graph
        self._project_flow_graph()

        try:
            # Step 2: Get all EntryPoint / DataSink pairs
            pairs = self._get_entry_sink_pairs(max_pairs)
            if not pairs:
                logger.warning("No EntryPoint→DataSink pairs found. "
                               "Run NodeTagger.tag_all() first.")
                return []

            logger.info("Extracting shortest paths for %d entry→sink pairs", len(pairs))

            # Step 3: Run Dijkstra for each pair
            flows: list[FlowPath] = []
            for start_id, end_id, start_fqn, end_fqn in pairs:
                flow = self._extract_single_flow(start_id, end_id, start_fqn, end_fqn)
                if flow and flow.path_length <= max_path_length:
                    # Step 4: Enrich with config/table data
                    self._enrich_flow(flow)
                    flows.append(flow)

            logger.info(
                "Extracted %d valid execution flows (of %d pairs evaluated)",
                len(flows), len(pairs),
            )
            return sorted(flows, key=lambda f: f.path_length)

        finally:
            # Always clean up the projection
            self._drop_flow_graph()

    def extract_flow_for_entry(self, entry_fqn: str, max_sinks: int = 10) -> list[FlowPath]:
        """
        Extract shortest paths from a specific EntryPoint to all reachable DataSinks.

        Useful for targeted narrative generation for a specific API endpoint.
        """
        self._project_flow_graph()
        try:
            pairs = self._get_sinks_for_entry(entry_fqn, max_sinks)
            flows = []
            for start_id, end_id, start_fqn, end_fqn in pairs:
                flow = self._extract_single_flow(start_id, end_id, start_fqn, end_fqn)
                if flow:
                    self._enrich_flow(flow)
                    flows.append(flow)
            return sorted(flows, key=lambda f: f.path_length)
        finally:
            self._drop_flow_graph()

    # ── Private: GDS operations ───────────────────────────────────────────────

    @_flow_retry
    def _project_flow_graph(self) -> None:
        """Build the GDS in-memory graph for flow extraction."""
        # Drop if exists (cleanup from failed run)
        with self.driver.session() as session:
            result = session.run(
                "CALL gds.graph.exists($name) YIELD exists RETURN exists",
                name=_FLOW_GRAPH_NAME,
            )
            record = result.single()
            if record and record["exists"]:
                session.run(
                    "CALL gds.graph.drop($name, false) YIELD graphName RETURN graphName",
                    name=_FLOW_GRAPH_NAME,
                ).consume()

        # Check which relationship types actually exist
        with self.driver.session() as session:
            result = session.run(
                "CALL db.relationshipTypes() YIELD relationshipType RETURN relationshipType"
            )
            existing = {r["relationshipType"] for r in result}

        projected_rels = [r for r in _FLOW_RELATIONSHIPS if r in existing]
        if not projected_rels:
            logger.warning("No flow relationship types found in DB")
            return

        # Check which node labels exist
        with self.driver.session() as session:
            result = session.run(
                "CALL db.labels() YIELD label RETURN label"
            )
            existing_labels = {r["label"] for r in result}

        projected_labels = [l for l in _FLOW_NODE_LABELS if l in existing_labels]
        if not projected_labels:
            logger.warning("No flow node labels found in DB")
            return

        # Project relationships without weight properties — relationships in Neo4j
        # don't store a 'weight' property, so we use unweighted projection and
        # BFS-equivalent shortest path (all edges cost 1).
        rel_entries = []
        for rel in projected_rels:
            rel_entries.append(
                f"{rel}: {{orientation: 'UNDIRECTED'}}"
            )
        rel_map = ", ".join(rel_entries)
        label_list = str(projected_labels)

        with self.driver.session() as session:
            result = session.run(
                f"""
                CALL gds.graph.project(
                    $graph_name,
                    {label_list},
                    {{{rel_map}}}
                )
                YIELD graphName, nodeCount, relationshipCount
                RETURN graphName, nodeCount, relationshipCount
                """,
                graph_name=_FLOW_GRAPH_NAME,
            )
            record = result.single()
            if record:
                logger.info(
                    "Flow graph projected: nodes=%d, rels=%d",
                    record["nodeCount"], record["relationshipCount"],
                )

    @_flow_retry
    def _drop_flow_graph(self) -> None:
        """Drop the flow GDS projection."""
        with self.driver.session() as session:
            try:
                session.run(
                    "CALL gds.graph.drop($name, false) YIELD graphName RETURN graphName",
                    name=_FLOW_GRAPH_NAME,
                ).consume()
            except Exception as e:
                logger.debug("GDS projection drop failed (may not exist): %s", e)

    @_flow_retry
    def _get_entry_sink_pairs(self, max_pairs: int) -> list[tuple]:
        """Get (startNodeId, endNodeId, startFqn, endFqn) pairs."""
        with self.driver.session() as session:
            result = session.run(
                """
                MATCH (start:EntryPoint)
                WHERE start.fqn IS NOT NULL
                WITH start ORDER BY start.fqn LIMIT $limit
                MATCH (end:DataSink)
                WHERE end.fqn IS NOT NULL
                  AND start <> end
                WITH start, end LIMIT $limit
                RETURN id(start) AS start_id, id(end) AS end_id,
                       start.fqn AS start_fqn, end.fqn AS end_fqn
                """,
                limit=max_pairs,
            )
            return [
                (r["start_id"], r["end_id"], r["start_fqn"], r["end_fqn"])
                for r in result
            ]

    @_flow_retry
    def _get_sinks_for_entry(self, entry_fqn: str, max_sinks: int) -> list[tuple]:
        """Get sinks reachable from a specific entry point."""
        with self.driver.session() as session:
            result = session.run(
                """
                MATCH (start:EntryPoint {fqn: $fqn})
                MATCH (end:DataSink)
                WHERE end.fqn IS NOT NULL AND start <> end
                RETURN id(start) AS start_id, id(end) AS end_id,
                       start.fqn AS start_fqn, end.fqn AS end_fqn
                LIMIT $limit
                """,
                fqn=entry_fqn,
                limit=max_sinks,
            )
            return [
                (r["start_id"], r["end_id"], r["start_fqn"], r["end_fqn"])
                for r in result
            ]

    @_flow_retry
    def _extract_single_flow(
        self, start_id: int, end_id: int, start_fqn: str, end_fqn: str,
    ) -> Optional[FlowPath]:
        """
        Run GDS Dijkstra shortest path between two node IDs.
        Returns None if no path exists.
        """
        with self.driver.session() as session:
            try:
                result = session.run(
                    """
                    MATCH (start) WHERE id(start) = $start_id
                    MATCH (end) WHERE id(end) = $end_id
                    CALL gds.shortestPath.dijkstra.stream($graph_name, {
                        sourceNode: start,
                        targetNode: end
                    })
                    YIELD index, sourceNode, targetNode, totalCost, nodeIds, costs, path
                    RETURN nodeIds, totalCost
                    """,
                    graph_name=_FLOW_GRAPH_NAME,
                    start_id=start_id,
                    end_id=end_id,
                )
                record = result.single()
            except Exception as e:
                # Path may not exist — this is expected for disconnected pairs
                logger.debug(
                    "No path %s → %s: %s", start_fqn, end_fqn, e,
                )
                return None

            if not record:
                return None

            node_ids = record["nodeIds"]
            total_cost = record["totalCost"]

            # Resolve node IDs to FQNs and details
            path_details = self._resolve_node_ids(session, node_ids)
            path_fqns = [n.get("fqn", f"node:{n.get('id', '?')}") for n in path_details]

            return FlowPath(
                entry_point_fqn=start_fqn,
                data_sink_fqn=end_fqn,
                path_fqns=path_fqns,
                path_node_details=path_details,
                path_length=len(path_fqns),
            )

    @_flow_retry
    def _resolve_node_ids(self, session, node_ids: list[int]) -> list[dict]:
        """Resolve a list of Neo4j internal node IDs to their properties."""
        result = session.run(
            """
            UNWIND $ids AS nid
            MATCH (n) WHERE id(n) = nid
            RETURN id(n) AS id, n.fqn AS fqn, n.name AS name,
                   n.kind AS kind, n.docstring AS docstring,
                   n.config_key AS config_key,
                   labels(n) AS labels
            """,
            ids=list(node_ids),
        )
        return [dict(r) for r in result]

    def _enrich_flow(self, flow: FlowPath) -> None:
        """
        Enrich a FlowPath with Configuration and DatabaseTable data
        touched by nodes along the path.
        """
        with self.driver.session() as session:
            # Find configs read by nodes along the path
            for detail in flow.path_node_details:
                fqn = detail.get("fqn")
                if not fqn:
                    continue

                # Check for config_key (Configuration nodes in the path)
                if detail.get("config_key"):
                    flow.config_keys.append(detail["config_key"])

                labels = detail.get("labels", [])
                if "DatabaseTable" in labels:
                    flow.table_names.append(detail.get("name", fqn))

            # Also check for READS_CONFIG edges from path nodes
            path_fqns = [d.get("fqn") for d in flow.path_node_details if d.get("fqn")]
            if path_fqns:
                result = session.run(
                    """
                    UNWIND $fqns AS fqn
                    MATCH (n {fqn: fqn})-[:READS_CONFIG]->(cfg:Configuration)
                    RETURN DISTINCT cfg.config_key AS config_key
                    """,
                    fqns=path_fqns,
                )
                for r in result:
                    if r["config_key"] not in flow.config_keys:
                        flow.config_keys.append(r["config_key"])

                # Check for QUERIES_TABLE edges
                result = session.run(
                    """
                    UNWIND $fqns AS fqn
                    MATCH (n {fqn: fqn})-[:QUERIES_TABLE]->(t:DatabaseTable)
                    RETURN DISTINCT t.name AS table_name
                    """,
                    fqns=path_fqns,
                )
                for r in result:
                    if r["table_name"] not in flow.table_names:
                        flow.table_names.append(r["table_name"])
