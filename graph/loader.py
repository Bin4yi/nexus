"""
graph/loader.py
Neo4j bulk loader â€” MERGE nodes and relationships from UIR objects.
Enterprise-scale: loads all 14 relationship types with proper result consumption
to prevent Neo4j lazy-execution silent drops.

All batch sizes are read from ``config.settings`` â€” no hard-coded magic numbers.
"""
from __future__ import annotations
import json
import logging
from typing import Any
from neo4j import Driver
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential
from neo4j.exceptions import ServiceUnavailable, SessionExpired

from config.settings import settings
from parsers.uir import Project, Module, Component, LogicUnit
from parsers.sql_schema_parser import DatabaseTableInfo, TableQueryEdge
from parsers.config_parser import ConfigurationInfo, ConfigReadEdge

logger = logging.getLogger(__name__)

# ── Confidence tier constants ─────────────────────────────────────────────────
# Every relationship in the graph carries a confidence score (0.0–1.0) and a
# resolution_tier string so queries can filter out low-confidence inferred edges.
TIER_EXPLICIT   = ("explicit",   1.0)   # @Override, direct import, annotation-driven
TIER_STRUCTURAL = ("structural", 0.9)   # AST call expression, direct CALLS
TIER_HEURISTIC  = ("heuristic",  0.7)   # type inference, INJECTS/RETURNS/RECEIVES
TIER_INFERRED   = ("inferred",   0.5)   # cross-file inference, RESOLVES_TO, INSTANTIATES

# Retry decorator for transient Neo4j failures
_loader_retry = retry(
    retry=retry_if_exception_type((ServiceUnavailable, SessionExpired, ConnectionError)),
    stop=stop_after_attempt(settings.retry_max_attempts),
    wait=wait_exponential(multiplier=settings.retry_backoff_seconds, min=1, max=30),
    reraise=True,
)


class Neo4jLoader:
    """
    Loads UIR objects into Neo4j using idempotent MERGE operations.
    All operations are safe to re-run (no duplicates created).
    All session.run() calls consume results to prevent lazy-execution drops.
    """

    def __init__(self, driver: Driver):
        self.driver = driver

    # â”€â”€ Public API â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def load_project(self, project: Project) -> None:
        """Load a full Project hierarchy into Neo4j (Tier 1: skeleton nodes)."""
        with self.driver.session() as session:
            session.run(
                """
                MERGE (p:Project {geid: $geid})
                SET p.name = $name, p.url = $url, p.branch = $branch,
                    p.last_indexed = datetime()
                """,
                geid=project.geid, name=project.name,
                url=project.url, branch=project.branch,
            ).consume()
            for module in project.modules:
                self._load_module(session, project.geid, module)

        logger.info("Loaded project: %s (%d modules)", project.name, len(project.modules))

    def load_dependency_edges(
        self,
        source_module_geid: str,
        deps: list,
    ) -> None:
        """Create DEPENDS_ON edges from a module to its Maven dependencies. (Tier 1)"""
        if not deps:
            return
        with self.driver.session() as session:
            for dep in deps:
                session.run(
                    """
                    MATCH (src:Module {geid: $src_geid})
                    MATCH (tgt:Module)
                    WHERE tgt.artifact_id = $artifact_id
                      AND tgt.group_id = $group_id
                    MERGE (src)-[r:DEPENDS_ON]->(tgt)
                    SET r.scope = $scope, r.version = $version,
                        r.confidence = 1.0, r.resolution_tier = 'explicit'
                    """,
                    src_geid=source_module_geid,
                    artifact_id=dep.target_artifact_id,
                    group_id=dep.target_group_id,
                    scope=dep.scope,
                    version=dep.target_version or "",
                ).consume()

    def load_implements_extends(self, all_components: list[Component]) -> None:
        """
        Two-pass bulk loader for IMPLEMENTS and EXTENDS edges.
        Must be called AFTER all component nodes are loaded so both sides exist.
        (Tier 1: OOP type system)
        """
        implements_pairs = []
        extends_pairs = []

        for comp in all_components:
            for iface_fqn in comp.implements:
                implements_pairs.append({
                    "src_geid": comp.geid,
                    "src_fqn": comp.fqn,
                    "tgt_fqn": iface_fqn,
                })
            if comp.extends:
                extends_pairs.append({
                    "src_geid": comp.geid,
                    "tgt_fqn": comp.extends,
                })

        with self.driver.session() as session:
            if implements_pairs:
                session.run(
                    """
                    CALL apoc.periodic.iterate(
                        'UNWIND $pairs AS pair RETURN pair',
                        'MATCH (src:Component {geid: pair.src_geid})
                         MATCH (tgt:Component {fqn: pair.tgt_fqn})
                         MERGE (src)-[r:IMPLEMENTS]->(tgt)
                         SET r.confidence = 1.0, r.resolution_tier = "explicit"',
                        {batchSize: $batch_size, params: {pairs: $pairs}}
                    )
                    """,
                    pairs=implements_pairs,
                    batch_size=settings.batch_size,
                ).consume()
                logger.info("Loaded %d IMPLEMENTS edges", len(implements_pairs))

            if extends_pairs:
                session.run(
                    """
                    CALL apoc.periodic.iterate(
                        'UNWIND $pairs AS pair RETURN pair',
                        'MATCH (src:Component {geid: pair.src_geid})
                         MATCH (tgt:Component {fqn: pair.tgt_fqn})
                         MERGE (src)-[r:EXTENDS]->(tgt)
                         SET r.confidence = 1.0, r.resolution_tier = "explicit"',
                        {batchSize: $batch_size, params: {pairs: $pairs}}
                    )
                    """,
                    pairs=extends_pairs,
                    batch_size=settings.batch_size,
                ).consume()
                logger.info("Loaded %d EXTENDS edges", len(extends_pairs))

    def load_call_graph(self, logic_units: list[LogicUnit]) -> None:
        """
        Create [:CALLS] edges between LogicUnits using APOC batch processing.
        Matches caller by GEID, target by FQN (best-effort).
        (Tier 1: blast radius)
        """
        with self.driver.session() as session:
            call_pairs = [
                {
                    "caller_geid": lu.geid,
                    "target_fqn": target_fqn,
                    "call_site": lu.file_path,
                }
                for lu in logic_units
                for target_fqn in lu.calls
            ]

            if not call_pairs:
                return

            session.run(
                """
                CALL apoc.periodic.iterate(
                    'UNWIND $pairs AS pair RETURN pair',
                    'MATCH (caller:LogicUnit {geid: pair.caller_geid})
                     OPTIONAL MATCH (target:LogicUnit {fqn: pair.target_fqn})
                     WITH caller, target, pair
                     WHERE target IS NOT NULL
                     MERGE (caller)-[r:CALLS]->(target)
                     SET r.call_site = pair.call_site,
                         r.confidence = 0.9, r.resolution_tier = "structural"',
                    {batchSize: $batch_size, params: {pairs: $pairs}}
                )
                """,
                pairs=call_pairs,
                batch_size=settings.batch_size,
            ).consume()

            # Fallback: match short names (ClassName.method) using ENDS WITH
            short_pairs = [p for p in call_pairs if "." in p["target_fqn"] and p["target_fqn"].count(".") == 1]
            if short_pairs:
                session.run(
                    """
                    CALL apoc.periodic.iterate(
                        'UNWIND $pairs AS pair RETURN pair',
                        'MATCH (caller:LogicUnit {geid: pair.caller_geid})
                         MATCH (target:LogicUnit)
                         WHERE target.fqn ENDS WITH ("." + pair.target_fqn)
                            OR target.fqn = pair.target_fqn
                         WITH caller, target, pair LIMIT 1
                         MERGE (caller)-[r:CALLS]->(target)
                         SET r.call_site = pair.call_site,
                             r.confidence = 0.7, r.resolution_tier = "heuristic"',
                        {batchSize: $batch_size, params: {pairs: $short_pairs}}
                    )
                    """,
                    short_pairs=short_pairs,
                    batch_size=settings.batch_size,
                ).consume()

            actual = session.run("MATCH ()-[r:CALLS]->() RETURN count(r) AS c").single()["c"]
            logger.info("Loaded %d CALLS call-pairs → %d edges in Neo4j", len(call_pairs), actual)

    def load_unresolved_calls(self, logic_units: list[LogicUnit]) -> None:
        """
        Cross-repo CALLS edge resolution — Pass 2.

        After load_call_graph() creates exact-FQN and ENDS-WITH edges, some
        call targets remain unresolved because the callee lives in a different
        repo and its simple name (e.g. "TokenGrantHandler") doesn't form an
        exact match.

        This pass tries suffix matching with at least 3 FQN segments so that
        "org.wso2.carbon.identity.oauth2.token.handlers.TokenGrantHandler"
        matches a call target recorded as "identity.oauth2.token.handlers.TokenGrantHandler".

        Edges created here carry confidence=0.5, resolution_tier="inferred".
        """
        # Collect call targets that contain a dot (qualified) but might be
        # partial — they'll have count > 1 dots and aren't already resolved
        all_pairs = [
            {"caller_geid": lu.geid, "target_fqn": target_fqn, "call_site": lu.file_path}
            for lu in logic_units
            for target_fqn in lu.calls
            if target_fqn.count(".") >= 2   # at least pkg.Class.method
        ]
        if not all_pairs:
            return

        with self.driver.session() as session:
            # Only process pairs that don't already have a CALLS edge
            session.run(
                """
                CALL apoc.periodic.iterate(
                    'UNWIND $pairs AS pair RETURN pair',
                    'MATCH (caller:LogicUnit {geid: pair.caller_geid})
                     WHERE NOT (caller)-[:CALLS]->()
                         OR NOT EXISTS {
                             MATCH (caller)-[:CALLS]->(t:LogicUnit)
                             WHERE t.fqn ENDS WITH pair.target_fqn
                         }
                     MATCH (target:LogicUnit)
                     WHERE target.fqn ENDS WITH ("." + pair.target_fqn)
                        OR target.fqn ENDS WITH pair.target_fqn
                     WITH caller, target, pair
                     WHERE caller <> target
                     MERGE (caller)-[r:CALLS]->(target)
                     ON CREATE SET r.call_site = pair.call_site,
                                   r.confidence = 0.5,
                                   r.resolution_tier = "inferred",
                                   r.cross_repo = true',
                    {batchSize: $batch_size, params: {pairs: $pairs}}
                )
                """,
                pairs=all_pairs,
                batch_size=settings.batch_size,
            ).consume()

        with self.driver.session() as session:
            cross = session.run(
                "MATCH ()-[r:CALLS {cross_repo: true}]->() RETURN count(r) AS c"
            ).single()["c"]
        logger.info(
            "Cross-repo CALLS resolution: %d candidate pairs → %d inferred edges",
            len(all_pairs), cross,
        )

    def load_type_edges(self, logic_units: list[LogicUnit]) -> None:
        """
        Create [:RETURNS] and [:RECEIVES] edges from LogicUnits to type Component nodes.
        (Tier 2: data flow)
        """
        returns_pairs = []
        receives_pairs = []

        for lu in logic_units:
            if lu.return_type and lu.return_type not in (
                "void", "int", "long", "double", "float", "boolean",
                "byte", "char", "short", "String", "Object",
            ):
                returns_pairs.append({
                    "lu_geid": lu.geid,
                    "type_fqn": lu.return_type.split("<")[0].strip(),
                })
            for param in lu.parameters:
                ptype = param.type_name.split("<")[0].strip()
                if ptype not in (
                    "int", "long", "double", "float", "boolean",
                    "byte", "char", "short", "String", "Object",
                ):
                    receives_pairs.append({
                        "lu_geid": lu.geid,
                        "type_fqn": ptype,
                    })

        with self.driver.session() as session:
            if returns_pairs:
                session.run(
                    """
                    CALL apoc.periodic.iterate(
                        'UNWIND $pairs AS pair RETURN pair',
                        'MATCH (lu:LogicUnit {geid: pair.lu_geid})
                         MATCH (t:Component) WHERE t.fqn ENDS WITH pair.type_fqn
                            OR t.fqn = pair.type_fqn
                         MERGE (lu)-[r:RETURNS]->(t)
                         SET r.confidence = 0.7, r.resolution_tier = "heuristic"',
                        {batchSize: $batch_size, params: {pairs: $pairs}}
                    )
                    """,
                    pairs=returns_pairs,
                    batch_size=settings.batch_size,
                ).consume()
                logger.info("Loaded %d RETURNS edges", len(returns_pairs))

            if receives_pairs:
                session.run(
                    """
                    CALL apoc.periodic.iterate(
                        'UNWIND $pairs AS pair RETURN pair',
                        'MATCH (lu:LogicUnit {geid: pair.lu_geid})
                         MATCH (t:Component) WHERE t.fqn ENDS WITH pair.type_fqn
                            OR t.fqn = pair.type_fqn
                         MERGE (lu)-[r:RECEIVES]->(t)
                         SET r.confidence = 0.7, r.resolution_tier = "heuristic"',
                        {batchSize: $batch_size, params: {pairs: $pairs}}
                    )
                    """,
                    pairs=receives_pairs,
                    batch_size=settings.batch_size,
                ).consume()
                logger.info("Loaded %d RECEIVES edges", len(receives_pairs))

    def load_injection_edges(self, all_components: list[Component]) -> None:
        """
        Create [:INJECTS] edges from Component to Component via @Autowired/@Reference fields.
        (Tier 2: DI container graph)
        """
        pairs = []
        for comp in all_components:
            for field in comp.fields:
                if field.is_injected:
                    pairs.append({
                        "src_geid": comp.geid,
                        "tgt_type": field.type_name.split("<")[0].strip(),
                    })
        if not pairs:
            return

        with self.driver.session() as session:
            session.run(
                """
                CALL apoc.periodic.iterate(
                    'UNWIND $pairs AS pair RETURN pair',
                    'MATCH (src:Component {geid: pair.src_geid})
                     MATCH (tgt:Component) WHERE tgt.fqn ENDS WITH pair.tgt_type
                        OR tgt.fqn = pair.tgt_type
                     MERGE (src)-[r:INJECTS]->(tgt)
                     SET r.confidence = 0.7, r.resolution_tier = "heuristic"',
                    {batchSize: $batch_size, params: {pairs: $pairs}}
                )
                """,
                pairs=pairs,
                batch_size=settings.batch_size,
            ).consume()
            logger.info("Loaded %d INJECTS edges", len(pairs))

    def load_annotated_with(self, all_components: list[Component]) -> None:
        """
        Create [:ANNOTATED_WITH] edges from Component/LogicUnit to AnnotationType nodes.
        AnnotationType nodes are created if they don't exist. (Tier 2: framework context)
        """
        comp_pairs = []
        lu_pairs = []

        for comp in all_components:
            for ann in comp.annotations:
                ann_name = ann.get("name", "") if isinstance(ann, dict) else ann.lstrip("@").split("(")[0].strip()
                if ann_name:
                    comp_pairs.append({"src_geid": comp.geid, "ann_name": ann_name})
            for lu in comp.logic_units:
                for ann in lu.annotations:
                    ann_name = ann.get("name", "") if isinstance(ann, dict) else ann.lstrip("@").split("(")[0].strip()
                    if ann_name and ann_name != "Override":
                        lu_pairs.append({"lu_geid": lu.geid, "ann_name": ann_name})

        with self.driver.session() as session:
            if comp_pairs:
                session.run(
                    """
                    CALL apoc.periodic.iterate(
                        'UNWIND $pairs AS pair RETURN pair',
                        'MATCH (src:Component {geid: pair.src_geid})
                         MERGE (ann:AnnotationType {name: pair.ann_name})
                         MERGE (src)-[r:ANNOTATED_WITH]->(ann)
                         SET r.confidence = 1.0, r.resolution_tier = "explicit"',
                        {batchSize: $batch_size, params: {pairs: $pairs}}
                    )
                    """,
                    pairs=comp_pairs,
                    batch_size=settings.batch_size,
                ).consume()
                logger.info("Loaded %d Component ANNOTATED_WITH edges", len(comp_pairs))

            if lu_pairs:
                session.run(
                    """
                    CALL apoc.periodic.iterate(
                        'UNWIND $pairs AS pair RETURN pair',
                        'MATCH (lu:LogicUnit {geid: pair.lu_geid})
                         MERGE (ann:AnnotationType {name: pair.ann_name})
                         MERGE (lu)-[r:ANNOTATED_WITH]->(ann)
                         SET r.confidence = 1.0, r.resolution_tier = "explicit"',
                        {batchSize: $batch_size, params: {pairs: $pairs}}
                    )
                    """,
                    pairs=lu_pairs,
                    batch_size=settings.batch_size,
                ).consume()
                logger.info("Loaded %d LogicUnit ANNOTATED_WITH edges", len(lu_pairs))

    def load_throws_edges(self, logic_units: list[LogicUnit]) -> None:
        """
        Create [:THROWS] edges from LogicUnit to Component/ExternalType.
        (Tier 3: security trace)
        """
        pairs = [
            {"lu_geid": lu.geid, "exc_type": exc}
            for lu in logic_units
            for exc in lu.throws
        ]
        if not pairs:
            return

        with self.driver.session() as session:
            session.run(
                """
                CALL apoc.periodic.iterate(
                    'UNWIND $pairs AS pair RETURN pair',
                    'MATCH (lu:LogicUnit {geid: pair.lu_geid})
                     MERGE (exc:ExceptionType {name: pair.exc_type})
                     MERGE (lu)-[:THROWS]->(exc)',
                    {batchSize: $batch_size, params: {pairs: $pairs}}
                )
                """,
                pairs=pairs,
                batch_size=settings.batch_size,
            ).consume()
            logger.info("Loaded %d THROWS edges", len(pairs))

    def load_overrides_edges(self, logic_units: list[LogicUnit]) -> None:
        """
        Create [:OVERRIDES] edges between LogicUnits (Tier 3).
        """
        pairs = [
            {"lu_geid": lu.geid, "parent_fqn": lu.overrides}
            for lu in logic_units
            if lu.overrides
        ]
        if not pairs:
            return

        with self.driver.session() as session:
            session.run(
                """
                CALL apoc.periodic.iterate(
                    'UNWIND $pairs AS pair RETURN pair',
                    'MATCH (child:LogicUnit {geid: pair.lu_geid})
                     MATCH (parent:LogicUnit) WHERE parent.fqn STARTS WITH pair.parent_fqn
                        OR parent.fqn = pair.parent_fqn
                     MERGE (child)-[r:OVERRIDES]->(parent)
                     SET r.confidence = 1.0, r.resolution_tier = "explicit"',
                    {batchSize: $batch_size, params: {pairs: $pairs}}
                )
                """,
                pairs=pairs,
                batch_size=settings.batch_size,
            ).consume()
            logger.info("Loaded %d OVERRIDES edges", len(pairs))

    def load_instantiates_edges(self, logic_units: list[LogicUnit]) -> None:
        """
        Create [:INSTANTIATES] edges from LogicUnit to Component (new X() calls).
        (Tier 3)
        """
        pairs = [
            {"lu_geid": lu.geid, "cls_type": cls}
            for lu in logic_units
            for cls in lu.instantiates
        ]
        if not pairs:
            return

        with self.driver.session() as session:
            session.run(
                """
                CALL apoc.periodic.iterate(
                    'UNWIND $pairs AS pair RETURN pair',
                    'MATCH (lu:LogicUnit {geid: pair.lu_geid})
                     MATCH (cls:Component) WHERE cls.fqn ENDS WITH pair.cls_type
                        OR cls.fqn = pair.cls_type
                     MERGE (lu)-[r:INSTANTIATES]->(cls)
                     SET r.confidence = 0.5, r.resolution_tier = "inferred"',
                    {batchSize: $batch_size, params: {pairs: $pairs}}
                )
                """,
                pairs=pairs,
                batch_size=settings.batch_size,
            ).consume()
            logger.info("Loaded %d INSTANTIATES edges", len(pairs))

    def load_event_handler_edges(self, all_components: list[Component]) -> None:
        """
        Create [:HANDLES_EVENT] edges for WSO2 event handler components. (Tier 3)
        """
        pairs = [
            {"comp_geid": comp.geid, "base": comp.extends or comp.implements[0]}
            for comp in all_components
            if comp.is_event_handler and (comp.extends or comp.implements)
        ]
        if not pairs:
            return

        with self.driver.session() as session:
            session.run(
                """
                CALL apoc.periodic.iterate(
                    'UNWIND $pairs AS pair RETURN pair',
                    'MATCH (handler:Component {geid: pair.comp_geid})
                     MERGE (event:EventClass {fqn: pair.base})
                     MERGE (handler)-[:HANDLES_EVENT]->(event)',
                    {batchSize: $batch_size, params: {pairs: $pairs}}
                )
                """,
                pairs=pairs,
                batch_size=settings.batch_size,
            ).consume()
            logger.info("Loaded %d HANDLES_EVENT edges", len(pairs))

    def load_database_tables(self, tables: list[DatabaseTableInfo]) -> None:
        """Create (DatabaseTable) nodes in Neo4j."""
        if not tables:
            return
        with self.driver.session() as session:
            for table in tables:
                session.run(
                    """
                    MERGE (t:DatabaseTable {name: $name})
                    SET t.source_file = $source_file,
                        t.repo_name = $repo_name
                    """,
                    name=table.table_name,
                    source_file=table.source_file,
                    repo_name=table.repo_name,
                ).consume()
        logger.info("Loaded %d DatabaseTable nodes", len(tables))

    def load_queries_table_edges(self, edges: list[TableQueryEdge]) -> None:
        """Create [:QUERIES_TABLE] edges from DAO Component → DatabaseTable."""
        if not edges:
            return
        with self.driver.session() as session:
            for edge in edges:
                session.run(
                    """
                    MATCH (c:Component {geid: $geid})
                    MATCH (t:DatabaseTable {name: $table_name})
                    MERGE (c)-[r:QUERIES_TABLE]->(t)
                    SET r.confidence = 0.9, r.resolution_tier = 'structural'
                    """,
                    geid=edge.component_geid,
                    table_name=edge.table_name,
                ).consume()
        logger.info("Loaded %d QUERIES_TABLE edges", len(edges))

    def load_configuration_nodes(self, configs: list[ConfigurationInfo]) -> None:
        """Create (Configuration) nodes in Neo4j."""
        if not configs:
            return
        with self.driver.session() as session:
            for cfg in configs:
                session.run(
                    """
                    MERGE (c:Configuration {config_key: $config_key})
                    SET c.config_type = $config_type,
                        c.source_file = $source_file,
                        c.repo_name = $repo_name
                    """,
                    config_key=cfg.config_key,
                    config_type=cfg.config_type,
                    source_file=cfg.source_file,
                    repo_name=cfg.repo_name,
                ).consume()
        logger.info("Loaded %d Configuration nodes", len(configs))

    def load_reads_config_edges(self, edges: list[ConfigReadEdge]) -> None:
        """Create [:READS_CONFIG] edges from Component → Configuration."""
        if not edges:
            return
        with self.driver.session() as session:
            for edge in edges:
                session.run(
                    """
                    MATCH (c:Component {geid: $geid})
                    MATCH (cfg:Configuration {config_key: $config_key})
                    MERGE (c)-[r:READS_CONFIG]->(cfg)
                    SET r.confidence = 0.7, r.resolution_tier = 'heuristic'
                    """,
                    geid=edge.component_geid,
                    config_key=edge.config_key,
                ).consume()
        logger.info("Loaded %d READS_CONFIG edges", len(edges))

    def load_resolves_to_edges(self, edges: list) -> None:
        """
        Create [:RESOLVES_TO] edges from Interface Component → Implementation Component.
        (Sprint 2: OSGi runtime injection resolution)

        ``edges`` should be a list of ``OSGiResolutionEdge`` objects.
        """
        if not edges:
            return

        pairs = [
            {
                "interface_fqn": e.interface_fqn,
                "implementation_fqn": e.implementation_fqn,
                "reference_field": e.reference_field,
            }
            for e in edges
        ]

        with self.driver.session() as session:
            session.run(
                """
                CALL apoc.periodic.iterate(
                    'UNWIND $pairs AS pair RETURN pair',
                    'MATCH (iface:Component)
                       WHERE iface.fqn = pair.interface_fqn
                          OR iface.fqn ENDS WITH pair.interface_fqn
                     MATCH (impl:Component)
                       WHERE impl.fqn = pair.implementation_fqn
                          OR impl.fqn ENDS WITH pair.implementation_fqn
                     MERGE (iface)-[r:RESOLVES_TO]->(impl)
                     SET r.reference_field = pair.reference_field,
                         r.confidence = 0.5, r.resolution_tier = "inferred"',
                    {batchSize: $batch_size, params: {pairs: $pairs}}
                )
                """,
                pairs=pairs,
                batch_size=settings.batch_size,
            ).consume()
        logger.info("Loaded %d OSGi [:RESOLVES_TO] edges", len(edges))

    def load_specification_nodes(self, specs: list) -> None:
        """
        Create (:Specification) nodes for IETF RFCs.
        (Sprint 4: specification grounding)

        ``specs`` should be a list of ``SpecificationInfo`` objects.
        """
        if not specs:
            return
        with self.driver.session() as session:
            for spec in specs:
                session.run(
                    """
                    MERGE (s:Specification {rfc_number: $rfc_number})
                    SET s.title = $title,
                        s.source_file = $source_file,
                        s.spec_id = 'RFC' + toString($rfc_number)
                    """,
                    rfc_number=spec.rfc_number,
                    title=spec.title,
                    source_file=spec.source_file,
                ).consume()
        logger.info("Loaded %d Specification nodes", len(specs))

    def load_specification_section_nodes(self, sections: list) -> None:
        """
        Create (:SpecSection) nodes for individual RFC sections and link them to
        their parent (:Specification) via [:SECTION_OF].

        This enables section-granular IMPLEMENTS_SPEC queries:
          "Which classes implement RFC 6749 §4.1 Authorization Code Grant?"

        ``sections`` should be a list of ``RFCSection`` objects.
        """
        if not sections:
            return
        bs = settings.batch_size
        rows = [
            {
                "spec_id":        sec.spec_id,
                "rfc_number":     sec.rfc_number,
                "section_number": sec.section_number,
                "section_title":  sec.section_title,
                "body_text":      sec.body_text[:2000],  # cap to avoid huge properties
            }
            for sec in sections
            if sec.spec_id  # skip sections without a spec_id
        ]
        with self.driver.session() as session:
            for i in range(0, len(rows), bs):
                batch = rows[i: i + bs]
                session.run(
                    """
                    UNWIND $rows AS row
                    MERGE (ss:SpecSection {spec_id: row.spec_id})
                    SET ss.rfc_number     = row.rfc_number,
                        ss.section_number = row.section_number,
                        ss.section_title  = row.section_title,
                        ss.body_text      = row.body_text
                    WITH ss, row
                    MATCH (s:Specification {rfc_number: row.rfc_number})
                    MERGE (ss)-[:SECTION_OF]->(s)
                    """,
                    rows=batch,
                ).consume()
        logger.info("Loaded %d SpecSection nodes", len(rows))

    def load_implements_spec_edges(self, edges: list) -> None:
        """
        Create [:IMPLEMENTS_SPEC] edges from Component → Specification and
        (when section_spec_id is present) also → SpecSection.

        Resolves GEIDs for both Component nodes and LogicUnit nodes:
        if the GEID belongs to a LogicUnit, we walk up to its parent Component.
        This handles the case where ChromaDB code_intent vectors were stored
        with LogicUnit GEIDs (from the main ingest pass) rather than Component GEIDs.
        """
        if not edges:
            return

        loaded = 0
        with self.driver.session() as session:
            for edge in edges:
                geid               = edge.component_geid
                rfc_number         = edge.rfc_number
                match_type         = getattr(edge, "match_type", "citation")
                sim                = getattr(edge, "similarity_score", 1.0)
                section_spec_id    = getattr(edge, "section_spec_id", "") or ""
                section_title      = getattr(edge, "section_title", "") or ""
                confidence         = 1.0 if sim >= 0.9 else (0.9 if sim >= 0.7 else 0.7)
                resolution_tier    = "explicit" if match_type == "citation" else "structural"

                # Resolve geid → Component, handling both Component and LogicUnit GEIDs.
                # OPTIONAL MATCH both paths; coalesce picks the first non-null result.
                result = session.run(
                    """
                    OPTIONAL MATCH (c1:Component {geid: $geid})
                    OPTIONAL MATCH (lu:LogicUnit {geid: $geid})<-[:HAS_METHOD]-(c2:Component)
                    WITH coalesce(c1, c2) AS c
                    WHERE c IS NOT NULL
                    MATCH (s:Specification {rfc_number: $rfc_number})
                    MERGE (c)-[r:IMPLEMENTS_SPEC]->(s)
                    SET r.citation_context  = $citation_context,
                        r.match_type        = $match_type,
                        r.similarity_score  = $similarity_score,
                        r.confidence        = $confidence,
                        r.resolution_tier   = $resolution_tier,
                        r.section_spec_id   = $section_spec_id,
                        r.section_title     = $section_title
                    RETURN count(r) AS created
                    """,
                    geid=geid,
                    rfc_number=rfc_number,
                    citation_context=edge.citation_context,
                    match_type=match_type,
                    similarity_score=sim,
                    confidence=confidence,
                    resolution_tier=resolution_tier,
                    section_spec_id=section_spec_id,
                    section_title=section_title,
                )
                rec = result.single()
                if rec and rec["created"]:
                    loaded += 1

                    # If we have a section reference, also draw Component → SpecSection
                    if section_spec_id:
                        session.run(
                            """
                            OPTIONAL MATCH (c1:Component {geid: $geid})
                            OPTIONAL MATCH (lu:LogicUnit {geid: $geid})<-[:HAS_METHOD]-(c2:Component)
                            WITH coalesce(c1, c2) AS c
                            WHERE c IS NOT NULL
                            MATCH (ss:SpecSection {spec_id: $section_spec_id})
                            MERGE (c)-[r:IMPLEMENTS_SPEC]->(ss)
                            SET r.citation_context = $citation_context,
                                r.match_type       = $match_type,
                                r.similarity_score = $similarity_score,
                                r.confidence       = $confidence
                            """,
                            geid=geid,
                            section_spec_id=section_spec_id,
                            citation_context=edge.citation_context,
                            match_type=match_type,
                            similarity_score=sim,
                            confidence=confidence,
                        ).consume()

        logger.info("Loaded %d [:IMPLEMENTS_SPEC] edges (of %d input)", loaded, len(edges))

    def load_remote_calls(self, edges: list[dict[str, Any]]) -> None:
        """Create [:REMOTE_CALLS] edges from API bridge detection results."""
        if not edges:
            return
        with self.driver.session() as session:
            for edge in edges:
                session.run(
                    """
                    MATCH (caller:LogicUnit {geid: $caller_geid})
                    MATCH (callee:LogicUnit {geid: $callee_geid})
                    MERGE (caller)-[r:REMOTE_CALLS]->(callee)
                    SET r.protocol = $protocol,
                        r.http_method = $http_method,
                        r.path = $path,
                        r.confidence = 0.7, r.resolution_tier = 'heuristic'
                    """,
                    **edge,
                ).consume()

    # â”€â”€ Private â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _load_module(self, session, project_geid: str, module: Module) -> None:
        session.run(
            """
            MERGE (m:Module {geid: $geid})
            SET m.name = $name, m.group_id = $group_id,
                m.artifact_id = $artifact_id, m.version = $version,
                m.language = $language
            WITH m
            MATCH (p:Project {geid: $project_geid})
            MERGE (p)-[:CONTAINS]->(m)
            """,
            geid=module.geid, name=module.name,
            group_id=module.group_id, artifact_id=module.artifact_id,
            version=module.version, language=module.language,
            project_geid=project_geid,
        ).consume()
        for component in module.components:
            self._load_component(session, module.geid, component)

    def _load_component(self, session, module_geid: str, comp: Component) -> None:
        # Extract short class name for Neo4j Browser display caption.
        # Browser shows 'name' property over all others â€” without it falls back
        # to file_path ('mirror\identity...') which truncates to 'mirror\id...'.
        short_name = comp.fqn.split(".")[-1] if comp.fqn else comp.fqn
        session.run(
            """
            MERGE (c:Component {geid: $geid})
            SET c.name = $name, c.fqn = $fqn, c.kind = $kind,
                c.file_path = $file_path,
                c.start_line = $start_line, c.end_line = $end_line,
                c.docstring = $docstring,
                c.annotations = $annotations,
                c.is_event_handler = $is_event_handler,
                c.visibility = $visibility,
                c.is_abstract = $is_abstract,
                c.is_final = $is_final
            WITH c
            MATCH (m:Module {geid: $module_geid})
            MERGE (m)-[:DECLARES]->(c)
            """,
            geid=comp.geid, name=short_name, fqn=comp.fqn, kind=comp.kind,
            file_path=comp.file_path, start_line=comp.start_line,
            end_line=comp.end_line, docstring=comp.docstring,
            annotations=json.dumps(comp.annotations),
            is_event_handler=comp.is_event_handler,
            visibility=comp.visibility,
            is_abstract=comp.is_abstract,
            is_final=comp.is_final,
            module_geid=module_geid,
        ).consume()
        for lu in comp.logic_units:
            self._load_logic_unit(session, comp.geid, lu)

    def _load_logic_unit(self, session, comp_geid: str, lu: LogicUnit) -> None:
        # Short name = just the method name (strip package + class prefix and params)
        # e.g. 'org.wso2.foo.JWTTokenIssuer.createJWT(String)' â†’ 'createJWT'
        _fqn_base = lu.fqn.split("(")[0] if lu.fqn else ""
        short_name = _fqn_base.split(".")[-1] if _fqn_base else (lu.fqn or "")
        session.run(
            """
            MERGE (l:LogicUnit {geid: $geid})
            SET l.name = $name, l.fqn = $fqn, l.kind = $kind,
                l.return_type = $return_type,
                l.file_path = $file_path,
                l.start_line = $start_line, l.end_line = $end_line,
                l.docstring = $docstring,
                l.body_text = $body_text,
                l.annotations = $annotations,
                l.deprecated = $deprecated,
                l.throws = $throws,
                l.overrides = $overrides,
                l.visibility = $visibility,
                l.is_static = $is_static,
                l.is_abstract = $is_abstract,
                l.is_final = $is_final,
                l.is_synchronized = $is_synchronized,
                l.lifecycle_role = $lifecycle_role
            WITH l
            MATCH (c:Component {geid: $comp_geid})
            MERGE (c)-[:HAS_METHOD]->(l)
            """,
            geid=lu.geid, name=short_name, fqn=lu.fqn, kind=lu.kind,
            return_type=lu.return_type or "",
            file_path=lu.file_path, start_line=lu.start_line,
            end_line=lu.end_line, docstring=lu.docstring,
            body_text=(lu.body_text or "")[:4000],
            annotations=json.dumps(lu.annotations), deprecated=lu.deprecated,
            throws=lu.throws, overrides=lu.overrides or "",
            visibility=lu.visibility,
            is_static=lu.is_static,
            is_abstract=lu.is_abstract,
            is_final=lu.is_final,
            is_synchronized=lu.is_synchronized,
            lifecycle_role=lu.lifecycle_role or "",
            comp_geid=comp_geid,
        ).consume()
