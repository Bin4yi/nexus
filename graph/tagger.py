"""
graph/tagger.py
Tags Neo4j nodes with secondary labels for execution flow analysis.

:EntryPoint — API endpoints, servlet handlers, web framework triggers
:DataSink   — DAOs, repository classes, SQL-executing methods

These labels enable GDS Dijkstra shortest-path queries from any
EntryPoint to any DataSink without brute-force multi-hop explosion.
"""
from __future__ import annotations
import logging
from neo4j import Driver
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential
from neo4j.exceptions import ServiceUnavailable, SessionExpired

from config.settings import settings

logger = logging.getLogger(__name__)

_tagger_retry = retry(
    retry=retry_if_exception_type((ServiceUnavailable, SessionExpired, ConnectionError)),
    stop=stop_after_attempt(settings.retry_max_attempts),
    wait=wait_exponential(multiplier=settings.retry_backoff_seconds, min=1, max=30),
    reraise=True,
)

# ── Annotation / pattern lists ────────────────────────────────────────────────

# Web framework annotations that mark an entry point
ENTRY_POINT_ANNOTATIONS = [
    # JAX-RS
    "Path", "GET", "POST", "PUT", "DELETE", "PATCH",
    # Spring MVC / Web
    "RequestMapping", "GetMapping", "PostMapping", "PutMapping",
    "DeleteMapping", "PatchMapping", "RestController", "Controller",
    # Servlet
    "WebServlet",
]

# Class name / superclass patterns for entry points
ENTRY_POINT_CLASS_PATTERNS = [
    "HttpServlet",
    "AbstractHttpServlet",
    "IdentityServlet",
    "FrameworkServlet",
]

# Patterns indicating a Data Sink (DAO / repository / SQL executor)
DATA_SINK_ANNOTATIONS = [
    "Repository",
]

DATA_SINK_CLASS_PATTERNS = [
    "DAO",
    "DataAccessor",
    "Repository",
    "JDBCPersistenceManager",
    "AbstractDAO",
    "JdbcTemplate",
]

# Method-level patterns: LogicUnits that execute SQL
DATA_SINK_METHOD_INDICATORS = [
    "executeQuery",
    "executeUpdate",
    "prepareStatement",
    "createStatement",
    "prepareCall",
]


class NodeTagger:
    """
    Applies secondary labels `:EntryPoint` and `:DataSink` to Neo4j nodes
    based on annotations, class hierarchy, and naming patterns.
    """

    def __init__(self, driver: Driver):
        self.driver = driver

    def tag_all(self) -> dict:
        """
        Run all tagging passes. Returns counts of tagged nodes.
        """
        stats = {
            "entry_points": 0,
            "data_sinks": 0,
        }

        stats["entry_points"] = self._tag_entry_points()
        stats["data_sinks"] = self._tag_data_sinks()

        logger.info(
            "Tagging complete — %d EntryPoints, %d DataSinks",
            stats["entry_points"], stats["data_sinks"],
        )
        return stats

    # ── Entry Points ──────────────────────────────────────────────────────────

    @_tagger_retry
    def _tag_entry_points(self) -> int:
        """Apply :EntryPoint label to API/servlet nodes."""
        count = 0
        with self.driver.session() as session:
            # 1. Tag Components/LogicUnits annotated with web framework annotations
            for ann in ENTRY_POINT_ANNOTATIONS:
                result = session.run(
                    """
                    MATCH (n)-[:ANNOTATED_WITH]->(a:AnnotationType {name: $ann_name})
                    WHERE NOT n:EntryPoint
                    SET n:EntryPoint
                    RETURN count(n) AS cnt
                    """,
                    ann_name=ann,
                )
                record = result.single()
                cnt = record["cnt"] if record else 0
                count += cnt
                if cnt > 0:
                    logger.debug("Tagged %d nodes with @%s as EntryPoint", cnt, ann)

            # 2. Tag Components extending HTTP servlet classes
            for pattern in ENTRY_POINT_CLASS_PATTERNS:
                result = session.run(
                    """
                    MATCH (n:Component)-[:EXTENDS]->(parent:Component)
                    WHERE parent.fqn CONTAINS $pattern
                      AND NOT n:EntryPoint
                    SET n:EntryPoint
                    RETURN count(n) AS cnt
                    """,
                    pattern=pattern,
                )
                record = result.single()
                cnt = record["cnt"] if record else 0
                count += cnt

            # 3. Tag Components whose class name or FQN contains servlet/controller patterns
            result = session.run(
                """
                MATCH (n:Component)
                WHERE (n.fqn CONTAINS 'Servlet' OR n.fqn CONTAINS 'Controller'
                       OR n.fqn CONTAINS 'Endpoint' OR n.fqn CONTAINS 'Resource')
                  AND NOT n:EntryPoint
                SET n:EntryPoint
                RETURN count(n) AS cnt
                """
            )
            record = result.single()
            cnt = record["cnt"] if record else 0
            count += cnt

            # 4. Also tag LogicUnits (methods) inside EntryPoint components
            result = session.run(
                """
                MATCH (ep:EntryPoint:Component)-[:HAS_METHOD]->(lu:LogicUnit)
                WHERE NOT lu:EntryPoint
                  AND (lu.annotations IS NOT NULL
                       AND any(a IN lu.annotations
                               WHERE a CONTAINS 'Mapping' OR a CONTAINS 'GET'
                                     OR a CONTAINS 'POST' OR a CONTAINS 'PUT'
                                     OR a CONTAINS 'DELETE' OR a CONTAINS 'Path'))
                SET lu:EntryPoint
                RETURN count(lu) AS cnt
                """
            )
            record = result.single()
            cnt = record["cnt"] if record else 0
            count += cnt

        logger.info("Tagged %d EntryPoint nodes", count)
        return count

    # ── Data Sinks ────────────────────────────────────────────────────────────

    @_tagger_retry
    def _tag_data_sinks(self) -> int:
        """Apply :DataSink label to DAO/repository/SQL-executing nodes."""
        count = 0
        with self.driver.session() as session:
            # 1. Tag Components annotated with @Repository
            for ann in DATA_SINK_ANNOTATIONS:
                result = session.run(
                    """
                    MATCH (n:Component)-[:ANNOTATED_WITH]->(a:AnnotationType {name: $ann_name})
                    WHERE NOT n:DataSink
                    SET n:DataSink
                    RETURN count(n) AS cnt
                    """,
                    ann_name=ann,
                )
                record = result.single()
                cnt = record["cnt"] if record else 0
                count += cnt

            # 2. Tag Components whose FQN contains DAO/Repository patterns
            for pattern in DATA_SINK_CLASS_PATTERNS:
                result = session.run(
                    """
                    MATCH (n:Component)
                    WHERE n.fqn CONTAINS $pattern
                      AND NOT n:DataSink
                    SET n:DataSink
                    RETURN count(n) AS cnt
                    """,
                    pattern=pattern,
                )
                record = result.single()
                cnt = record["cnt"] if record else 0
                count += cnt

            # 3. Tag Components that have QUERIES_TABLE edges (from TASK 1)
            result = session.run(
                """
                MATCH (n:Component)-[:QUERIES_TABLE]->(:DatabaseTable)
                WHERE NOT n:DataSink
                SET n:DataSink
                RETURN count(DISTINCT n) AS cnt
                """
            )
            record = result.single()
            cnt = record["cnt"] if record else 0
            count += cnt

            # 4. Tag LogicUnits inside DataSink components
            result = session.run(
                """
                MATCH (ds:DataSink:Component)-[:HAS_METHOD]->(lu:LogicUnit)
                WHERE NOT lu:DataSink
                SET lu:DataSink
                RETURN count(lu) AS cnt
                """
            )
            record = result.single()
            cnt = record["cnt"] if record else 0
            count += cnt

            # 5. Tag LogicUnits whose body_text contains SQL execution patterns
            for indicator in DATA_SINK_METHOD_INDICATORS:
                result = session.run(
                    """
                    MATCH (lu:LogicUnit)
                    WHERE lu.body_text CONTAINS $indicator
                      AND NOT lu:DataSink
                    SET lu:DataSink
                    RETURN count(lu) AS cnt
                    """,
                    indicator=indicator,
                )
                record = result.single()
                cnt = record["cnt"] if record else 0
                count += cnt

        logger.info("Tagged %d DataSink nodes", count)
        return count

    # ── Utility ───────────────────────────────────────────────────────────────

    @_tagger_retry
    def get_entry_point_count(self) -> int:
        """Return the number of EntryPoint nodes."""
        with self.driver.session() as session:
            result = session.run("MATCH (n:EntryPoint) RETURN count(n) AS cnt")
            record = result.single()
            return record["cnt"] if record else 0

    @_tagger_retry
    def get_data_sink_count(self) -> int:
        """Return the number of DataSink nodes."""
        with self.driver.session() as session:
            result = session.run("MATCH (n:DataSink) RETURN count(n) AS cnt")
            record = result.single()
            return record["cnt"] if record else 0
