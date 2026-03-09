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
                    SET r.scope = $scope, r.version = $version
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
                         MERGE (src)-[:IMPLEMENTS]->(tgt)',
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
                         MERGE (src)-[:EXTENDS]->(tgt)',
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
                     MATCH (target:LogicUnit {fqn: pair.target_fqn})
                     MERGE (caller)-[r:CALLS]->(target)
                     SET r.call_site = pair.call_site',
                    {batchSize: $batch_size, params: {pairs: $pairs}}
                )
                """,
                pairs=call_pairs,
                batch_size=settings.batch_size,
            ).consume()
            logger.info("Loaded %d CALLS edges", len(call_pairs))

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
                         MERGE (lu)-[:RETURNS]->(t)',
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
                         MERGE (lu)-[:RECEIVES]->(t)',
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
                     MERGE (src)-[:INJECTS]->(tgt)',
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
                         MERGE (src)-[:ANNOTATED_WITH]->(ann)',
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
                         MERGE (lu)-[:ANNOTATED_WITH]->(ann)',
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
                     MERGE (child)-[:OVERRIDES]->(parent)',
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
                     MERGE (lu)-[:INSTANTIATES]->(cls)',
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
                    MERGE (c)-[:QUERIES_TABLE]->(t)
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
                    MERGE (c)-[:READS_CONFIG]->(cfg)
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
                     SET r.reference_field = pair.reference_field',
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
                        s.source_file = $source_file
                    """,
                    rfc_number=spec.rfc_number,
                    title=spec.title,
                    source_file=spec.source_file,
                ).consume()
        logger.info("Loaded %d Specification nodes", len(specs))

    def load_implements_spec_edges(self, edges: list) -> None:
        """
        Create [:IMPLEMENTS_SPEC] edges from Component → Specification.
        (Sprint 4: specification grounding)

        ``edges`` should be a list of ``SpecImplementsEdge`` objects.
        """
        if not edges:
            return
        with self.driver.session() as session:
            for edge in edges:
                session.run(
                    """
                    MATCH (c:Component {geid: $geid})
                    MATCH (s:Specification {rfc_number: $rfc_number})
                    MERGE (c)-[r:IMPLEMENTS_SPEC]->(s)
                    SET r.citation_context = $citation_context,
                        r.match_type = $match_type,
                        r.similarity_score = $similarity_score
                    """,
                    geid=edge.component_geid,
                    rfc_number=edge.rfc_number,
                    citation_context=edge.citation_context,
                    match_type=getattr(edge, "match_type", "citation"),
                    similarity_score=getattr(edge, "similarity_score", 1.0),
                ).consume()
        logger.info("Loaded %d [:IMPLEMENTS_SPEC] edges", len(edges))

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
                        r.path = $path
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
