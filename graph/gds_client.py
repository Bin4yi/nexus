"""
graph/gds_client.py
Neo4j Graph Data Science (GDS) client — projects the in-memory graph,
runs the Leiden community detection algorithm, and writes community_id
back to every node in the graph.

Supports HIERARCHICAL Leiden (``includeIntermediateCommunities=true``)
which writes a ``community_id_i`` property for each resolution level *i*.
All Leiden hyper-parameters are read from ``config.settings``.

Dynamic projection: queries the DB for existing relationship types at runtime
to prevent crashes when some relationships haven't been created yet.
"""
from __future__ import annotations
import logging
from neo4j import Driver
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential
from neo4j.exceptions import ServiceUnavailable, SessionExpired

from config.settings import settings

logger = logging.getLogger(__name__)

# Retry decorator for transient Neo4j / GDS failures
_gds_retry = retry(
    retry=retry_if_exception_type((ServiceUnavailable, SessionExpired, ConnectionError)),
    stop=stop_after_attempt(settings.retry_max_attempts),
    wait=wait_exponential(multiplier=settings.retry_backoff_seconds, min=1, max=30),
    reraise=True,
)

# ── Relationship whitelist for GDS Leiden projection ─────────────────────────
# ONLY architectural execution + structural edges are projected.
# High-fan-out utility edges (THROWS, RETURNS, RECEIVES, INSTANTIATES,
# HANDLES_EVENT, ANNOTATED_WITH) cause "God Node" collapse — every method
# pointing to String/Object/Exception collapses into one mega-community.
# These utility edges remain in Neo4j for Cypher queries but MUST NOT
# enter the in-memory GDS graph.
_WHITELISTED_RELATIONSHIPS: list[str] = [
    rel.strip()
    for rel in settings.gds_leiden_relationships.split(",")
    if rel.strip()
]

# Edges that must NEVER be projected — safety net even if a user
# accidentally adds them to the env var.
_BLACKLISTED_RELATIONSHIPS = frozenset({
    "THROWS", "RETURNS", "RECEIVES", "INSTANTIATES",
    "HANDLES_EVENT", "ANNOTATED_WITH",
})

# Node labels to project
_NODE_LABELS = ["Component", "LogicUnit", "Module"]


class GDSClient:
    """
    Wraps the Neo4j GDS Leiden algorithm execution.

    Workflow:
        1. Query DB for existing relationship types
        2. Project all code nodes into GDS in-memory graph (dynamic)
        3. Run gds.leiden.write → writes community_id to each node
        4. Drop the in-memory projection (frees GDS memory)

    The community_id property is then used by the community summarizer
    to group nodes and generate LLM summaries.
    """

    def __init__(self, driver: Driver, graph_name: str | None = None):
        self.driver = driver
        self.graph_name = graph_name or settings.gds_graph_name

    def run_leiden(
        self,
        max_levels: int | None = None,
        gamma: float | None = None,
        theta: float | None = None,
        write_property: str | None = None,
        include_intermediate: bool = False,
    ) -> dict:
        """
        Project the code graph, run Leiden, write community_id to all nodes.

        All hyper-parameters default to their ``settings`` values so callers
        never hard-code magic numbers.

        Args:
            include_intermediate: When *True* the GDS call uses
                ``includeIntermediateCommunities: true`` which writes an
                additional ``<write_property>_i`` property for each level *i*.
                This enables hierarchical community drill-down.

        Returns:
            Dict with communityCount, modularity, ranLevels
        """
        max_levels     = max_levels     or settings.leiden_max_levels
        gamma          = gamma          if gamma is not None else settings.leiden_gamma
        theta          = theta          if theta is not None else settings.leiden_theta
        write_property = write_property or settings.leiden_write_property
        self._drop_if_exists()

        existing_rels = self._get_existing_rel_types()
        if not existing_rels:
            logger.warning("No candidate relationship types found in DB — skipping Leiden")
            return {"communityCount": 0, "modularity": 0.0, "ranLevels": 0}

        self._project_graph(existing_rels)

        result = self._run_leiden(max_levels, gamma, theta, write_property, include_intermediate)
        logger.info(
            "Leiden complete — communities: %d, modularity: %.4f, levels: %d",
            result.get("communityCount", 0),
            result.get("modularity", 0.0),
            result.get("ranLevels", 0),
        )

        self._drop_projection()
        return result

    @_gds_retry
    def get_community_count(self) -> int:
        """Return the number of distinct community_id values in the graph."""
        with self.driver.session() as session:
            result = session.run(
                "MATCH (n) WHERE n.community_id IS NOT NULL "
                "RETURN count(DISTINCT n.community_id) AS cnt"
            )
            record = result.single()
            return record["cnt"] if record else 0

    @_gds_retry
    def get_null_community_count(self) -> int:
        """Return number of nodes missing community_id (should be 0 after Leiden)."""
        with self.driver.session() as session:
            result = session.run(
                "MATCH (n:LogicUnit) WHERE n.community_id IS NULL RETURN count(n) AS cnt"
            )
            record = result.single()
            return record["cnt"] if record else 0

    @_gds_retry
    def get_nodes_by_community(self, community_id: int) -> list[dict]:
        """Fetch all LogicUnit AND Component nodes in a given community.

        Returns both methods *and* classes/interfaces so the community
        summariser has full architectural context (not just method names).
        """
        with self.driver.session() as session:
            result = session.run(
                """
                MATCH (n {community_id: $cid})
                WHERE n:LogicUnit OR n:Component
                RETURN n.geid AS geid, n.fqn AS fqn,
                       null AS docstring, n.kind AS kind,
                       n.file_path AS file_path,
                       labels(n)[0] AS label
                ORDER BY label DESC, n.fqn
                """,
                cid=community_id,
            )
            return [dict(r) for r in result]

    @_gds_retry
    def get_community_boundary_edges(self, community_id: int) -> list[dict]:
        """Return inter-community edges leaving/entering this community.

        Shows how this community connects to the broader architecture —
        critical context for the LLM summariser.
        """
        with self.driver.session() as session:
            result = session.run(
                """
                MATCH (a {community_id: $cid})-[r]->(b)
                WHERE b.community_id IS NOT NULL AND b.community_id <> $cid
                RETURN type(r) AS edge_type,
                       b.community_id AS target_community,
                       count(*) AS cnt
                ORDER BY cnt DESC
                LIMIT 10
                """,
                cid=community_id,
            )
            return [dict(r) for r in result]

    @_gds_retry
    def list_community_ids(self) -> list[int]:
        """Return all distinct community IDs in the graph."""
        with self.driver.session() as session:
            result = session.run(
                "MATCH (n) WHERE n.community_id IS NOT NULL "
                "RETURN DISTINCT n.community_id AS cid ORDER BY cid"
            )
            return [r["cid"] for r in result]

    # ── Private ───────────────────────────────────────────────────────────────

    @_gds_retry
    def _get_existing_rel_types(self) -> list[str]:
        """Intersect the configured whitelist with actually-existing DB rels.

        Steps:
        1. Read the strict whitelist from ``settings.gds_leiden_relationships``.
        2. Remove any blacklisted utility types (safety net).
        3. Query ``db.relationshipTypes()`` to find which ones exist.
        4. Log a WARNING if critical types (CALLS, INJECTS) are missing.
        """
        # Apply blacklist safety net
        safe_whitelist = [
            r for r in _WHITELISTED_RELATIONSHIPS
            if r not in _BLACKLISTED_RELATIONSHIPS
        ]

        # Use a direct data query instead of db.relationshipTypes() schema cache.
        # db.relationshipTypes() can return stale results due to Neo4j's internal
        # schema metadata caching — it may miss relationship types that were just
        # written in the same pipeline run (e.g. CALLS loaded at 09:05 but not
        # visible to the schema cache at 09:08).
        # MATCH ()-[r]->() scans actual stored relationships — always accurate.
        with self.driver.session() as session:
            result = session.run(
                "MATCH ()-[r]->() RETURN DISTINCT type(r) AS relationshipType"
            )
            existing = {r["relationshipType"] for r in result}

        projected = [r for r in safe_whitelist if r in existing]
        excluded_present = existing - set(projected)

        logger.info(
            "GDS Leiden projection whitelist: %s", safe_whitelist,
        )
        logger.info(
            "GDS will project %d relationship types: %s", len(projected), projected,
        )
        if excluded_present:
            logger.info(
                "GDS excluded (present in DB but NOT projected): %s",
                sorted(excluded_present),
            )

        # Critical edge audit
        for critical in ("CALLS", "INJECTS"):
            if critical in safe_whitelist and critical not in existing:
                logger.warning(
                    "CRITICAL: %s is whitelisted but missing from Neo4j! "
                    "Leiden communities will be degraded.", critical,
                )

        return projected

    @_gds_retry
    def _project_graph(self, rel_types: list[str]) -> None:
        """Build a dynamic GDS projection from actually-present relationship types."""
        # Build the relationship projection map dynamically
        rel_map = {rel: {"orientation": "UNDIRECTED"} for rel in rel_types}
        rel_map_cypher = "{" + ", ".join(
            f"{rel}: {{orientation: 'UNDIRECTED'}}" for rel in rel_types
        ) + "}"
        node_labels_cypher = str(_NODE_LABELS)  # e.g. ['Component', 'LogicUnit', ...]

        with self.driver.session() as session:
            result = session.run(
                f"""
                CALL gds.graph.project(
                    $graph_name,
                    {node_labels_cypher},
                    {rel_map_cypher}
                )
                YIELD graphName, nodeCount, relationshipCount
                RETURN graphName, nodeCount, relationshipCount
                """,
                graph_name=self.graph_name,
            )
            record = result.single()
            if record:
                logger.info(
                    "GDS graph projected: %s — nodes: %d, relationships: %d",
                    record["graphName"],
                    record["nodeCount"],
                    record["relationshipCount"],
                )
            else:
                logger.warning("GDS projection returned no result for %s", self.graph_name)

    @_gds_retry
    def _run_leiden(
        self,
        max_levels: int,
        gamma: float,
        theta: float,
        write_property: str,
        include_intermediate: bool = False,
    ) -> dict:
        """Execute ``gds.leiden.write``.

        When *include_intermediate* is True the call adds
        ``includeIntermediateCommunities: true`` which writes
        ``<write_property>_i`` for each hierarchical level *i*.
        """
        intermediate_clause = (
            "includeIntermediateCommunities: true,"
            if include_intermediate
            else ""
        )
        with self.driver.session() as session:
            result = session.run(
                f"""
                CALL gds.leiden.write($graph_name, {{
                    writeProperty: $write_property,
                    maxLevels: $max_levels,
                    gamma: $gamma,
                    theta: $theta,
                    {intermediate_clause}
                    consecutiveIds: true
                }})
                YIELD communityCount, modularity, ranLevels
                RETURN communityCount, modularity, ranLevels
                """,
                graph_name=self.graph_name,
                write_property=write_property,
                max_levels=max_levels,
                gamma=gamma,
                theta=theta,
            )
            record = result.single()
            return dict(record) if record else {}

    @_gds_retry
    def _drop_projection(self) -> None:
        with self.driver.session() as session:
            result = session.run(
                "CALL gds.graph.drop($graph_name, false) YIELD graphName RETURN graphName",
                graph_name=self.graph_name,
            )
            result.consume()
            logger.debug("GDS projection dropped: %s", self.graph_name)

    def _drop_if_exists(self) -> None:
        """Drop existing projection if it already exists (cleanup from failed run)."""
        with self.driver.session() as session:
            result = session.run(
                "CALL gds.graph.exists($graph_name) YIELD exists RETURN exists",
                graph_name=self.graph_name,
            )
            record = result.single()
            if record and record["exists"]:
                self._drop_projection()
                logger.warning("Dropped pre-existing GDS projection: %s", self.graph_name)
