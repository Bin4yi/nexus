"""
backfill_rfc_specs.py
Standalone script — re-runs RFC specification grounding without a full ingest.

Rebuilds:
  - (:Specification) nodes (RFC-level)
  - (:SpecSection) nodes (section-granular, linked via [:SECTION_OF])
  - [:IMPLEMENTS_SPEC] edges (Component → Specification AND Component → SpecSection)
    from both citation detection and LLM-verified semantic matching.

Fixes applied vs the original ingest run:
  - GEIDs are now resolved for both Component AND LogicUnit nodes
    (the original loader only matched Component nodes, silently dropping
    all LLM edges whose ChromaDB hit was a LogicUnit GEID).
  - Matching is now section-granular (de-dup key = geid + section, not geid + RFC).
  - top_k increased from 5 → 10 so more candidates reach the LLM.

Usage (from nexus/ directory):
    py backfill_rfc_specs.py
"""
from __future__ import annotations
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from neo4j import GraphDatabase
from config.settings import settings
from graph.loader import Neo4jLoader
from graph.schema import apply_schema
from parsers.rfc_parser import RFCParser
from parsers.rfc_semantic_matcher import RFCSemanticMatcher
from vectorstore.embedder import ChromaEmbedder
import chromadb

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("backfill_rfc_specs")


def main() -> None:
    driver = GraphDatabase.driver(settings.neo4j_uri, auth=settings.neo4j_auth)
    loader = Neo4jLoader(driver)

    # Ensure SpecSection constraint exists
    apply_schema(driver)

    rfc_path = settings.rfc_path
    if not rfc_path.exists():
        logger.error("RFC directory not found: %s", rfc_path)
        logger.error("Set RFC_PATH in .env or create the directory and add RFC .txt/.md files.")
        driver.close()
        return

    parser = RFCParser()

    # ── 1. Parse RFC files ────────────────────────────────────────────────────
    rfc_specs = parser.parse_rfc_files(rfc_path)
    if not rfc_specs:
        logger.warning("No RFC files found in %s", rfc_path)
        driver.close()
        return
    logger.info("Found %d RFC specifications", len(rfc_specs))

    # ── 2. Load Specification nodes ───────────────────────────────────────────
    loader.load_specification_nodes(rfc_specs)

    # ── 3. Chunk into sections & load SpecSection nodes ───────────────────────
    rfc_sections = parser.chunk_rfc_sections(rfc_specs)
    logger.info("Chunked %d RFC sections", len(rfc_sections))
    loader.load_specification_section_nodes(rfc_sections)

    # ── 4. Citation-based edges (fast, no LLM) ────────────────────────────────
    mirror_root = settings.repos_mirror_path
    all_java_files: list[Path] = []
    for repo_dir in mirror_root.iterdir():
        if repo_dir.is_dir():
            all_java_files.extend(repo_dir.rglob("*.java"))
    logger.info("Scanning %d Java files for RFC citations...", len(all_java_files))

    # Build minimal component stubs from Neo4j for citation detection
    with driver.session() as session:
        rows = list(session.run(
            "MATCH (c:Component) RETURN c.geid AS geid, c.fqn AS fqn, c.file_path AS file_path"
        ))

    class _CompStub:
        def __init__(self, geid, fqn, file_path):
            self.geid = geid
            self.fqn = fqn
            self.file_path = file_path

    components = [_CompStub(r["geid"], r["fqn"], r["file_path"]) for r in rows if r["geid"]]
    logger.info("Loaded %d Component stubs for citation scan", len(components))

    citation_edges = parser.detect_rfc_citations(all_java_files, components)
    logger.info("Found %d citation edges", len(citation_edges))

    # ── 5. LLM-verified semantic matching ────────────────────────────────────
    chroma_client = chromadb.HttpClient(host=settings.chroma_host, port=settings.chroma_port)
    embedder = ChromaEmbedder(chroma_client)
    llm_matcher = RFCSemanticMatcher(embedder)
    llm_edges = llm_matcher.match(rfc_sections)
    logger.info("Found %d LLM-verified edges", len(llm_edges))

    # ── 6. Load all edges ─────────────────────────────────────────────────────
    all_edges = citation_edges + llm_edges
    if all_edges:
        loader.load_implements_spec_edges(all_edges)
    else:
        logger.warning("No IMPLEMENTS_SPEC edges to load.")

    # ── Quick stats ───────────────────────────────────────────────────────────
    with driver.session() as s:
        spec_count = s.run("MATCH (n:Specification) RETURN count(n) AS c").single()["c"]
        sec_count  = s.run("MATCH (n:SpecSection) RETURN count(n) AS c").single()["c"]
        edge_count = s.run("MATCH ()-[r:IMPLEMENTS_SPEC]->() RETURN count(r) AS c").single()["c"]
        logger.info(
            "Done — %d Specification nodes, %d SpecSection nodes, %d IMPLEMENTS_SPEC edges",
            spec_count, sec_count, edge_count,
        )

    driver.close()


if __name__ == "__main__":
    main()
