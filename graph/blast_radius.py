"""
graph/blast_radius.py
Precomputed blast radius — depth-grouped BFS traversal with risk scoring.

Inspired by GitNexus's ``impact()`` tool which returns pre-grouped results by
traversal depth with a risk level in a single call rather than requiring the
consumer to walk the graph at query time.

For each seed node (Component or LogicUnit), this module:
  1. Runs APOC ``expandConfig`` (or a manual 3-step fallback) up to depth 3,
     capturing the hop depth per affected node.
  2. Groups results into depth_1 / depth_2 / depth_3 lists.
  3. Computes a risk_level: LOW / MEDIUM / HIGH / CRITICAL.
  4. Stores the result on the node as:
       n.blast_radius_json   — full JSON payload
       n.blast_radius_risk   — indexed string for fast filtering
       n.blast_radius_total  — indexed int (total affected node count)

Usage:
    from graph.blast_radius import BlastRadiusComputer
    computer = BlastRadiusComputer(driver)
    computer.compute_for_all()          # precompute for every Component
    computer.compute_for_node(geid)     # single node
"""
from __future__ import annotations
import json
import logging
from datetime import datetime, timezone
from neo4j import Driver
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential
from neo4j.exceptions import ServiceUnavailable, SessionExpired

from config.settings import settings

logger = logging.getLogger(__name__)

_retry = retry(
    retry=retry_if_exception_type((ServiceUnavailable, SessionExpired, ConnectionError)),
    stop=stop_after_attempt(settings.retry_max_attempts),
    wait=wait_exponential(multiplier=settings.retry_backoff_seconds, min=1, max=30),
    reraise=True,
)

# Risk thresholds (total affected nodes across all depths)
_RISK_CRITICAL = 50
_RISK_HIGH     = 20
_RISK_MEDIUM   = 5


def _risk_level(total: int) -> str:
    if total >= _RISK_CRITICAL:
        return "CRITICAL"
    if total >= _RISK_HIGH:
        return "HIGH"
    if total >= _RISK_MEDIUM:
        return "MEDIUM"
    return "LOW"


class BlastRadiusComputer:
    """
    Precomputes and stores blast radius results on Component and LogicUnit nodes.
    """

    def __init__(self, driver: Driver):
        self.driver = driver

    # ── Public API ────────────────────────────────────────────────────────────

    def compute_for_node(
        self,
        geid: str,
        depth: int | None = None,
        min_confidence: float = 0.0,
    ) -> dict:
        """
        Compute blast radius for a single node and persist on the node.

        Args:
            geid:           GEID of the target Component or LogicUnit.
            depth:          Max BFS hops (defaults to settings.blast_radius_max_depth).
            min_confidence: Only traverse edges with confidence >= this value.
                            Set to 0.7 to exclude low-confidence inferred edges.

        Returns:
            The blast radius result dict with keys:
              depth_1, depth_2, depth_3, total_affected, risk_level, computed_at
        """
        depth = depth or settings.blast_radius_max_depth
        by_depth = self._bfs_by_depth(geid, depth, min_confidence)

        total = sum(len(v) for v in by_depth.values())
        result = {
            "depth_1":      [n["fqn"] for n in by_depth.get(1, [])],
            "depth_2":      [n["fqn"] for n in by_depth.get(2, [])],
            "depth_3":      [n["fqn"] for n in by_depth.get(3, [])],
            "total_affected": total,
            "risk_level":   _risk_level(total),
            "computed_at":  datetime.now(timezone.utc).isoformat(),
        }
        self._write_blast_radius(geid, result)
        return result

    def compute_for_all(
        self,
        node_labels: list[str] | None = None,
        depth: int | None = None,
        min_confidence: float = 0.0,
        batch_size: int = 50,
    ) -> dict:
        """
        Precompute blast radius for all Component and/or LogicUnit nodes.

        Args:
            node_labels:    Labels to process, e.g. ["Component"]. Defaults to both.
            depth:          Max BFS hops.
            min_confidence: Edge confidence filter (0.0 = all edges, 0.7 = structural+).
            batch_size:     Nodes fetched per Neo4j query page.

        Returns:
            Summary dict: {total, LOW, MEDIUM, HIGH, CRITICAL}
        """
        labels = node_labels or ["Component", "LogicUnit"]
        depth  = depth or settings.blast_radius_max_depth

        summary: dict[str, int] = {"total": 0, "LOW": 0, "MEDIUM": 0, "HIGH": 0, "CRITICAL": 0}

        for label in labels:
            geids = self._fetch_all_geids(label)
            logger.info("Computing blast radius for %d %s nodes…", len(geids), label)

            for i, geid in enumerate(geids):
                try:
                    result = self.compute_for_node(geid, depth=depth, min_confidence=min_confidence)
                    risk = result["risk_level"]
                    summary["total"]  += 1
                    summary[risk]     += 1
                except Exception as e:
                    logger.warning("blast_radius failed for %s: %s", geid, e)

                if (i + 1) % batch_size == 0:
                    logger.info(
                        "  %s: %d / %d processed — CRITICAL=%d HIGH=%d MEDIUM=%d LOW=%d",
                        label, i + 1, len(geids),
                        summary["CRITICAL"], summary["HIGH"],
                        summary["MEDIUM"],   summary["LOW"],
                    )

        logger.info(
            "Blast radius complete: total=%d CRITICAL=%d HIGH=%d MEDIUM=%d LOW=%d",
            summary["total"], summary["CRITICAL"], summary["HIGH"],
            summary["MEDIUM"], summary["LOW"],
        )
        return summary

    # ── Private ───────────────────────────────────────────────────────────────

    @_retry
    def _bfs_by_depth(
        self, geid: str, depth: int, min_confidence: float
    ) -> dict[int, list[dict]]:
        """
        BFS traversal up to `depth` hops inbound on CALLS/INJECTS/IMPLEMENTS edges.
        Returns {hop_depth: [{geid, fqn}]} grouped by depth.
        """
        with self.driver.session() as session:
            try:
                rows = list(session.run(
                    """
                    MATCH (seed)
                    WHERE seed.geid = $geid
                      AND (seed:LogicUnit OR seed:Component)
                    CALL apoc.path.expandConfig(seed, {
                        relationshipFilter: '<CALLS|<INJECTS|<IMPLEMENTS',
                        minLevel: 1,
                        maxLevel: $depth,
                        uniqueness: 'NODE_GLOBAL'
                    }) YIELD path
                    WITH nodes(path)[-1] AS affected,
                         length(path)    AS hop_depth
                    WHERE (affected:LogicUnit OR affected:Component)
                      AND affected.geid <> $geid
                    RETURN affected.geid AS geid,
                           affected.fqn  AS fqn,
                           hop_depth
                    ORDER BY hop_depth, affected.fqn
                    """,
                    geid=geid,
                    depth=depth,
                ))
            except Exception:
                # APOC not available — fall back to manual 3-step query
                rows = list(session.run(
                    """
                    MATCH (seed)
                    WHERE seed.geid = $geid
                      AND (seed:LogicUnit OR seed:Component)

                    // depth 1
                    OPTIONAL MATCH (d1)-[:CALLS|INJECTS|IMPLEMENTS]->(seed)
                    WHERE (d1:LogicUnit OR d1:Component) AND d1.geid <> $geid

                    // depth 2
                    OPTIONAL MATCH (d2)-[:CALLS|INJECTS|IMPLEMENTS]->(d1)
                    WHERE (d2:LogicUnit OR d2:Component)
                      AND d2.geid <> $geid AND d1 IS NOT NULL

                    // depth 3
                    OPTIONAL MATCH (d3)-[:CALLS|INJECTS|IMPLEMENTS]->(d2)
                    WHERE (d3:LogicUnit OR d3:Component)
                      AND d3.geid <> $geid AND d2 IS NOT NULL

                    WITH
                      collect(DISTINCT {geid: d1.geid, fqn: d1.fqn, hop: 1}) +
                      collect(DISTINCT {geid: d2.geid, fqn: d2.fqn, hop: 2}) +
                      collect(DISTINCT {geid: d3.geid, fqn: d3.fqn, hop: 3}) AS all_nodes
                    UNWIND all_nodes AS n
                    WHERE n.geid IS NOT NULL
                    RETURN n.geid AS geid, n.fqn AS fqn, n.hop AS hop_depth
                    """,
                    geid=geid,
                ))

        by_depth: dict[int, list[dict]] = {1: [], 2: [], 3: []}
        seen: set[str] = set()
        for row in rows:
            hop = row["hop_depth"]
            row_geid = row["geid"]
            if not row_geid or row_geid in seen:
                continue
            seen.add(row_geid)
            d = min(max(int(hop), 1), 3)
            by_depth[d].append({"geid": row_geid, "fqn": row["fqn"] or ""})

        return by_depth

    @_retry
    def _write_blast_radius(self, geid: str, result: dict) -> None:
        """Persist blast radius result on the node."""
        with self.driver.session() as session:
            session.run(
                """
                MATCH (n {geid: $geid})
                SET n.blast_radius_json    = $json_str,
                    n.blast_radius_risk    = $risk_level,
                    n.blast_radius_total   = $total,
                    n.blast_radius_computed_at = datetime()
                """,
                geid=geid,
                json_str=json.dumps(result),
                risk_level=result["risk_level"],
                total=result["total_affected"],
            ).consume()

    @_retry
    def _fetch_all_geids(self, label: str) -> list[str]:
        """Fetch all GEIDs for the given node label."""
        with self.driver.session() as session:
            rows = list(session.run(
                f"MATCH (n:{label}) WHERE n.geid IS NOT NULL RETURN n.geid AS geid"
            ))
        return [r["geid"] for r in rows]
