"""
graph/schema.py
Neo4j schema setup — creates constraints and indexes for all node types.
Extended for enterprise-scale: adds AnnotationType and ExternalType node constraints,
additional property indexes, and a fulltext index for fast code search.
"""
from __future__ import annotations
import logging
from neo4j import Driver

logger = logging.getLogger(__name__)

CONSTRAINTS = [
    "CREATE CONSTRAINT project_geid IF NOT EXISTS FOR (n:Project) REQUIRE n.geid IS UNIQUE",
    "CREATE CONSTRAINT module_geid IF NOT EXISTS FOR (n:Module) REQUIRE n.geid IS UNIQUE",
    "CREATE CONSTRAINT component_geid IF NOT EXISTS FOR (n:Component) REQUIRE n.geid IS UNIQUE",
    "CREATE CONSTRAINT logicunit_geid IF NOT EXISTS FOR (n:LogicUnit) REQUIRE n.geid IS UNIQUE",
    "CREATE CONSTRAINT annotation_type_name IF NOT EXISTS FOR (n:AnnotationType) REQUIRE n.name IS UNIQUE",
    "CREATE CONSTRAINT exception_type_name IF NOT EXISTS FOR (n:ExceptionType) REQUIRE n.name IS UNIQUE",
    "CREATE CONSTRAINT event_class_fqn IF NOT EXISTS FOR (n:EventClass) REQUIRE n.fqn IS UNIQUE",
    "CREATE CONSTRAINT db_table_name IF NOT EXISTS FOR (n:DatabaseTable) REQUIRE n.name IS UNIQUE",
    "CREATE CONSTRAINT config_key IF NOT EXISTS FOR (n:Configuration) REQUIRE n.config_key IS UNIQUE",
    # v2: RFC specification nodes
    "CREATE CONSTRAINT spec_id IF NOT EXISTS FOR (s:Specification) REQUIRE s.spec_id IS UNIQUE",
]

INDEXES = [
    # Core lookup indexes
    "CREATE INDEX component_fqn IF NOT EXISTS FOR (n:Component) ON (n.fqn)",
    "CREATE INDEX logicunit_fqn IF NOT EXISTS FOR (n:LogicUnit) ON (n.fqn)",
    # Community detection indexes
    "CREATE INDEX logicunit_community IF NOT EXISTS FOR (n:LogicUnit) ON (n.community_id)",
    "CREATE INDEX component_community IF NOT EXISTS FOR (n:Component) ON (n.community_id)",
    # Enterprise indexes (Tier 2/3)
    "CREATE INDEX logicunit_return_type IF NOT EXISTS FOR (n:LogicUnit) ON (n.return_type)",
    "CREATE INDEX component_event_handler IF NOT EXISTS FOR (n:Component) ON (n.is_event_handler)",
    "CREATE INDEX component_kind IF NOT EXISTS FOR (n:Component) ON (n.kind)",
    "CREATE INDEX logicunit_kind IF NOT EXISTS FOR (n:LogicUnit) ON (n.kind)",
    # DatabaseTable and Configuration indexes
    "CREATE INDEX dbtable_repo IF NOT EXISTS FOR (n:DatabaseTable) ON (n.repo_name)",
    "CREATE INDEX config_type IF NOT EXISTS FOR (n:Configuration) ON (n.config_type)",
    # v2: visibility/modifier indexes for security analysis queries
    "CREATE INDEX logicunit_visibility IF NOT EXISTS FOR (n:LogicUnit) ON (n.visibility)",
    "CREATE INDEX component_visibility IF NOT EXISTS FOR (n:Component) ON (n.visibility)",
    "CREATE INDEX logicunit_lifecycle IF NOT EXISTS FOR (n:LogicUnit) ON (n.lifecycle_role)",
    # v2: specification index
    "CREATE INDEX spec_rfc IF NOT EXISTS FOR (s:Specification) ON (s.rfc)",
]

# Fulltext index — separate because syntax differs (not via CREATE INDEX)
FULLTEXT_INDEX = """
CREATE FULLTEXT INDEX code_search IF NOT EXISTS
FOR (n:LogicUnit|Component)
ON EACH [n.fqn, n.docstring]
"""


def apply_schema(driver: Driver) -> None:
    """Apply all constraints and indexes. Safe to call multiple times (IF NOT EXISTS)."""
    with driver.session() as session:
        for cypher in CONSTRAINTS:
            session.run(cypher).consume()
            logger.debug("Applied constraint: %s", cypher[:70])
        for cypher in INDEXES:
            session.run(cypher).consume()
            logger.debug("Applied index: %s", cypher[:70])
        try:
            session.run(FULLTEXT_INDEX).consume()
            logger.debug("Applied fulltext index: code_search")
        except Exception as e:
            # Fulltext indexes require GDS or enterprise — graceful degradation
            logger.warning("Fulltext index not created (may need Neo4j 5.x): %s", e)

    logger.info(
        "Schema setup complete — %d constraints, %d indexes",
        len(CONSTRAINTS), len(INDEXES),
    )
