"""
pipeline/orchestrator.py
Enterprise-scale 4-stage ingestion pipeline orchestrator.
Wires all 14 relationship types across Tier 1/2/3:
  Stage 1 (Mirror)  → Stage 2 (Extract) → Stage 3 (Link) → Stage 4 (Load)
  Post: GDS Leiden + Community Summarization
"""
from __future__ import annotations
import logging
from pathlib import Path
from typing import Optional

import redis
from neo4j import GraphDatabase
import chromadb

from config.settings import settings
from pipeline.mirror import RepositoryMirror
from parsers.java_parser import JavaParser
from parsers.uir import Project, Module, Component, LogicUnit
from parsers.geid import generate_geid
from parsers.sql_schema_parser import SQLSchemaParser
from parsers.config_parser import ConfigurationParser
from parsers.osgi_parser import OSGiParser
from parsers.rfc_parser import RFCParser
from linker.maven_resolver import MavenResolver
from linker.api_bridge import ApiBridgeDetector
from graph.schema import apply_schema
from graph.loader import Neo4jLoader
from graph.gds_client import GDSClient
from graph.tagger import NodeTagger
from graph.flow_extractor import FlowExtractor
from vectorstore.chunker import UIRChunker
from vectorstore.embedder import ChromaEmbedder
from community.summarizer import CommunitySummarizer
from community.global_rollup import GlobalRollup
from reasoning.flow_summarizer import FlowNarrativeSummarizer

logger = logging.getLogger(__name__)


class IngestionPipeline:
    """
    Orchestrates the full 4-stage ingestion pipeline + GraphRAG clustering.

    Stage 1: Mirror  — git clone/pull all repos
    Stage 2: Extract — Tree-sitter → UIR objects
    Stage 3: Link    — Call graph, Maven deps, API bridge, OOP edges (all tiers)
    Stage 4: Load    — Neo4j MERGE all relationship types + ChromaDB embed
    Post:    GDS Leiden + Community Summarization
    """

    def __init__(self):
        # Clients
        self.neo4j_driver = GraphDatabase.driver(
            settings.neo4j_uri, auth=settings.neo4j_auth
        )
        self.chroma = chromadb.HttpClient(
            host=settings.chroma_host, port=settings.chroma_port
        )
        self.redis = redis.from_url(settings.redis_url, decode_responses=True)

        # Components
        self.mirror      = RepositoryMirror()
        self.java_parser = JavaParser()
        self.maven       = MavenResolver()
        self.api_bridge  = ApiBridgeDetector()
        self.sql_parser  = SQLSchemaParser()
        self.config_parser = ConfigurationParser()
        self.osgi_parser = OSGiParser()
        self.rfc_parser  = RFCParser()
        self.loader      = Neo4jLoader(self.neo4j_driver)
        self.gds         = GDSClient(self.neo4j_driver)
        self.tagger      = NodeTagger(self.neo4j_driver)
        self.flow_extractor = FlowExtractor(self.neo4j_driver)
        self.chunker     = UIRChunker()
        self.embedder    = ChromaEmbedder(self.chroma)
        self.summarizer  = CommunitySummarizer(self.gds, self.chroma)
        self.flow_summarizer = FlowNarrativeSummarizer(self.chroma)
        self.global_rollup = GlobalRollup(self.chroma)

    def run(
        self,
        config_path: Optional[Path] = None,
        skip_summarization: bool = False,
    ) -> dict:
        """
        Run the full pipeline from mirror to community summarization.

        Args:
            config_path:         Path to repos.yaml (defaults to settings value)
            skip_summarization:  Skip LLM community summarization (faster, no API key needed)

        Returns:
            Summary dict with counts at each stage
        """
        stats = {
            "repos_mirrored":    0,
            "files_parsed":      0,
            "components":        0,
            "logic_units":       0,
            "call_edges":        0,
            "implements_edges":  0,
            "depends_on_edges":  0,
            "remote_calls":      0,
            "db_tables":         0,
            "config_entries":    0,
            "entry_points":      0,
            "data_sinks":        0,
            "flow_paths":        0,
            "flow_narratives":   0,
            "communities":       0,
            "l2_subsystems":     0,
            "l3_global":         False,
        }

        # ── Apply schema (idempotent) ─────────────────────────────────────────
        apply_schema(self.neo4j_driver)

        # ── Stage 1: Mirror ───────────────────────────────────────────────────
        self._set_status("stage", "mirror")
        local_paths = self.mirror.mirror_all(config_path)
        stats["repos_mirrored"] = len(local_paths)
        logger.info("Stage 1 complete — %d repos mirrored", len(local_paths))

        # ── Stage 2: Extract ─────────────────────────────────────────────────
        self._set_status("stage", "extract")

        all_logic_units: list[LogicUnit] = []
        all_components: list[Component] = []
        fqn_map: dict[str, str] = {}   # file_path → class FQN
        geid_map: dict[str, str] = {}  # method_fqn → geid
        all_java_files: list[Path] = []

        # Per-repo data for Maven/dependency wiring
        repo_module_data: list[tuple[str, list]] = []  # (mod_geid, dep_edges)

        for repo_path in local_paths:
            repo_name = repo_path.name
            java_files = list(repo_path.rglob("*.java"))
            all_java_files.extend(java_files)
            stats["files_parsed"] += len(java_files)

            # Build project hierarchy from pom.xml — parse ALL pom files in the repo
            pom_files = self.maven.find_poms(repo_path)
            module_info, dep_edges = {}, []
            if pom_files:
                for pom_file in pom_files:
                    info, edges = self.maven.parse_pom(pom_file)
                    if not module_info:
                        module_info = info  # use first pom's module info for project identity
                    dep_edges.extend(edges)

            proj_geid = generate_geid(repo_name, repo_name)
            mod_geid  = generate_geid(repo_name, module_info.get("artifact_id", repo_name))

            # Parse all Java files
            components: list[Component] = []
            for jf in java_files:
                try:
                    comps = self.java_parser.parse_file(jf, repo_name)
                    components.extend(comps)
                    for comp in comps:
                        fqn_map[str(jf)] = comp.fqn
                        for lu in comp.logic_units:
                            geid_map[lu.fqn] = lu.geid
                            all_logic_units.append(lu)
                except Exception as e:
                    logger.warning("Failed to parse %s: %s", jf, e)

            all_components.extend(components)
            stats["components"]  += len(components)
            stats["logic_units"] += sum(len(c.logic_units) for c in components)

            module = Module(
                geid=mod_geid,
                name=module_info.get("artifact_id", repo_name),
                group_id=module_info.get("group_id", ""),
                artifact_id=module_info.get("artifact_id", repo_name),
                version=module_info.get("version", "UNKNOWN"),
                components=components,
                dependencies=dep_edges,
            )
            project = Project(
                geid=proj_geid,
                name=repo_name,
                url="",
                modules=[module],
            )

            # ── Stage 4a: Load Neo4j nodes (skeleton) ────────────────────────
            self._set_status("stage", f"load:{repo_name}")
            self.loader.load_project(project)
            repo_module_data.append((mod_geid, dep_edges))

            # ── Stage 4b: Embed into ChromaDB ─────────────────────────────────
            all_chunks = []
            for comp in components:
                all_chunks.extend(self.chunker.chunk_all(comp.logic_units, repo_name))
            self.embedder.upsert_chunks(all_chunks)

            # ── Stage 2b: Scan SQL schemas + Config files (Sprint 2) ─────────
            db_tables = self.sql_parser.scan_sql_scripts(repo_path, repo_name)
            if db_tables:
                self.loader.load_database_tables(db_tables)
                known_table_names = {t.table_name for t in db_tables}
                table_edges = self.sql_parser.detect_table_queries(
                    java_files, components, known_table_names,
                )
                self.loader.load_queries_table_edges(table_edges)
                stats["db_tables"] += len(db_tables)

            config_entries = self.config_parser.scan_config_files(repo_path, repo_name)
            if config_entries:
                self.loader.load_configuration_nodes(config_entries)
                known_config_keys = {c.config_key for c in config_entries}
                config_edges = self.config_parser.detect_config_readers(
                    java_files, components, known_config_keys,
                )
                self.loader.load_reads_config_edges(config_edges)
                stats["config_entries"] += len(config_entries)

        # ── Sprint 2: OSGi resolution (cross-repo, all components known) ─────
        if settings.osgi_enabled:
            self._set_status("stage", "osgi")
            osgi_components = self.osgi_parser.parse_components(
                all_java_files, all_components,
            )
            osgi_edges = self.osgi_parser.build_resolution_edges(
                all_java_files, all_components, osgi_components,
            )
            if osgi_edges:
                self.loader.load_resolves_to_edges(osgi_edges)
            logger.info(
                "OSGi: %d @Component declarations, %d RESOLVES_TO edges",
                len(osgi_components), len(osgi_edges),
            )

        # ── Sprint 4: RFC specification grounding ─────────────────────────────
        rfc_specs = self.rfc_parser.parse_rfc_files(settings.rfc_path)
        if rfc_specs:
            self.loader.load_specification_nodes(rfc_specs)
            rfc_edges = self.rfc_parser.detect_rfc_citations(
                all_java_files, all_components,
            )
            if rfc_edges:
                self.loader.load_implements_spec_edges(rfc_edges)
            logger.info(
                "RFCs: %d specifications, %d IMPLEMENTS_SPEC edges",
                len(rfc_specs), len(rfc_edges) if rfc_specs else 0,
            )

        # ── Stage 3: Link (cross-repo, all nodes loaded first) ───────────────
        self._set_status("stage", "link")

        # Tier 1: Core relationships
        logger.info("Loading Tier 1 edges...")

        # CALLS — method call graph
        self.loader.load_call_graph(all_logic_units)
        stats["call_edges"] = sum(len(lu.calls) for lu in all_logic_units)
        logger.info("Stage 3a — %d CALLS edges", stats["call_edges"])

        # IMPLEMENTS + EXTENDS — OOP type system (2-pass: all nodes exist now)
        self.loader.load_implements_extends(all_components)
        stats["implements_edges"] = sum(
            len(c.implements) + (1 if c.extends else 0)
            for c in all_components
        )
        logger.info("Stage 3b — %d IMPLEMENTS/EXTENDS edges", stats["implements_edges"])

        # DEPENDS_ON — Maven module dependencies
        for mod_geid, dep_edges in repo_module_data:
            self.loader.load_dependency_edges(mod_geid, dep_edges)
        stats["depends_on_edges"] = sum(len(de) for _, de in repo_module_data)
        logger.info("Stage 3c — %d DEPENDS_ON edges", stats["depends_on_edges"])

        # Tier 2: Enterprise context
        logger.info("Loading Tier 2 edges...")

        # INJECTS — DI container graph
        self.loader.load_injection_edges(all_components)

        # ANNOTATED_WITH — framework context (@Service, @Path, @GET, etc.)
        self.loader.load_annotated_with(all_components)

        # RETURNS + RECEIVES — data flow edges
        self.loader.load_type_edges(all_logic_units)

        # REMOTE_CALLS — cross-service REST API bridge
        self._set_status("stage", "api_bridge")
        self.api_bridge.register_endpoints(all_java_files, fqn_map, geid_map)
        remote_edges = self.api_bridge.detect_calls(all_java_files, fqn_map, geid_map)
        if remote_edges:
            edge_dicts = [
                {
                    "caller_geid": e.caller_geid,
                    "callee_geid": e.callee_geid,
                    "protocol": e.protocol,
                    "http_method": e.http_method,
                    "path": e.path,
                }
                for e in remote_edges
            ]
            self.loader.load_remote_calls(edge_dicts)
        stats["remote_calls"] = len(remote_edges)
        logger.info("Stage 3d — %d REMOTE_CALLS edges", stats["remote_calls"])

        # Tier 3: Polish
        logger.info("Loading Tier 3 edges...")

        # THROWS — exception graph
        self.loader.load_throws_edges(all_logic_units)

        # OVERRIDES — method override chain
        self.loader.load_overrides_edges(all_logic_units)

        # INSTANTIATES — new X() calls
        self.loader.load_instantiates_edges(all_logic_units)

        # HANDLES_EVENT — WSO2 observer pattern
        self.loader.load_event_handler_edges(all_components)

        logger.info("Stage 3 complete — all relationship tiers loaded")

        # ── Post: Leiden community detection ──────────────────────────────────
        self._set_status("stage", "leiden")
        leiden_result = self.gds.run_leiden(
            max_levels=settings.leiden_max_levels,
            gamma=settings.leiden_gamma,
            theta=settings.leiden_theta,
            write_property=settings.leiden_write_property,
        )
        stats["communities"] = leiden_result.get("communityCount", 0)
        logger.info("Leiden complete — %d communities", stats["communities"])

        # ── Post: Community summarization (optional) ──────────────────────────
        if not skip_summarization and settings.community_summarization_enabled:
            self._set_status("stage", "summarize")
            self.summarizer.summarize_all()

        # ── Post: EntryPoint / DataSink tagging (TASK 2) ─────────────────────
        self._set_status("stage", "tagging")
        tag_stats = self.tagger.tag_all()
        stats["entry_points"] = tag_stats["entry_points"]
        stats["data_sinks"] = tag_stats["data_sinks"]
        logger.info(
            "Tagging complete — %d EntryPoints, %d DataSinks",
            stats["entry_points"], stats["data_sinks"],
        )

        # ── Post: Execution flow extraction via GDS Dijkstra (TASK 3) ────────
        if stats["entry_points"] > 0 and stats["data_sinks"] > 0:
            self._set_status("stage", "flow_extraction")
            flows = self.flow_extractor.extract_all_flows(
                max_pairs=200, max_path_length=15,
            )
            stats["flow_paths"] = len(flows)
            logger.info("Extracted %d execution flow paths", len(flows))

            # ── Post: Flow narrative generation (TASK 4) ─────────────────────
            if flows and not skip_summarization:
                self._set_status("stage", "flow_narratives")
                narratives = self.flow_summarizer.summarize_flows(
                    flows, max_workers=settings.summarizer_max_workers,
                )
                stats["flow_narratives"] = len(narratives)
                logger.info("Generated %d flow narratives", len(narratives))
        else:
            logger.info("Skipping flow extraction — no EntryPoints or DataSinks tagged")

        # ── Post: Local Ollama micro-drafts for EntryPoint classes (Sprint 4) ─
        if settings.local_drafting_enabled and not skip_summarization:
            self._set_status("stage", "local_drafting")
            try:
                from llm.local_drafting import LocalDraftingEngine
                drafting_engine = LocalDraftingEngine(self.neo4j_driver)
                n_drafts = drafting_engine.run(
                    max_workers=min(2, settings.summarizer_max_workers),
                )
                logger.info("Local micro-drafts generated: %d", n_drafts)
            except Exception as e:
                logger.warning("Local drafting failed (non-fatal): %s", e)

        # ── Post: Global GraphRAG Rollup (Sprint 4) ───────────────────────────
        if not skip_summarization and settings.community_summarization_enabled:
            self._set_status("stage", "global_rollup")
            try:
                l3_summary = self.global_rollup.run_full_rollup()
                stats["l2_subsystems"] = len(l3_summary.l2_domains)
                stats["l3_global"] = bool(l3_summary.summary_text)
                logger.info(
                    "Global rollup complete — %d L2 sub-systems, L3 generated=%s",
                    stats["l2_subsystems"], stats["l3_global"],
                )
            except Exception as e:
                logger.error("Global rollup failed: %s", e)

        self._set_status("stage", "done")
        logger.info("Pipeline complete: %s", stats)
        return stats

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            self.neo4j_driver.close()
        except Exception as e:
            logger.warning("Failed to close Neo4j driver: %s", e)
        try:
            self.redis.close()
        except Exception as e:
            logger.warning("Failed to close Redis client: %s", e)
        return False

    def _set_status(self, key: str, value: str) -> None:
        """Write pipeline state to Redis."""
        try:
            self.redis.set(f"nexus:pipeline:{key}", value, ex=3600)
        except Exception as e:
            logger.warning("Redis state update failed: %s", e)
