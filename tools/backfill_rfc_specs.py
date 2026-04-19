"""
backfill_rfc_specs.py
Standalone script — re-runs RFC specification grounding without a full ingest.

Rebuilds:
  - Specification nodes (RFC-level) in SQLite
  - SpecSection nodes (section-granular)
  - IMPLEMENTS_SPEC edges (from citation detection + LLM-verified semantic matching)

Usage (from nexus/ directory):
    py backfill_rfc_specs.py
"""
from __future__ import annotations
import logging
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config.settings import settings
from graph.sqlite_loader import SQLiteLoader
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
    loader = SQLiteLoader()
    loader.open()

    rfc_path = settings.rfc_path
    if not rfc_path.exists():
        logger.error("RFC directory not found: %s", rfc_path)
        logger.error("Set RFC_PATH in .env or create the directory and add RFC .txt/.md files.")
        loader.close()
        return

    parser = RFCParser()

    # ── 1. Parse RFC files ────────────────────────────────────────────────────
    rfc_specs = parser.parse_rfc_files(rfc_path)
    if not rfc_specs:
        logger.warning("No RFC files found in %s", rfc_path)
        loader.close()
        return
    logger.info("Found %d RFC specifications", len(rfc_specs))

    # ── 2. Load Specification nodes ───────────────────────────────────────────
    loader.load_specification_nodes(rfc_specs)

    # ── 3. Chunk into sections & load SpecSection nodes ──────────────────────
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

    # Build minimal component stubs from SQLite for citation detection
    with sqlite3.connect(str(settings.sqlite_db_path)) as conn:
        rows = conn.execute(
            "SELECT geid, fqn, file_path FROM nodes WHERE node_type = 'Component'"
        ).fetchall()

    class _CompStub:
        def __init__(self, geid, fqn, file_path):
            self.geid      = geid
            self.fqn       = fqn
            self.file_path = file_path

    components = [_CompStub(r[0], r[1], r[2]) for r in rows if r[0]]
    logger.info("Loaded %d Component stubs for citation scan", len(components))

    citation_edges = parser.detect_rfc_citations(all_java_files, components)
    logger.info("Found %d citation edges", len(citation_edges))

    # ── 5. LLM-verified semantic matching ─────────────────────────────────────
    chroma_client = chromadb.HttpClient(host=settings.chroma_host, port=settings.chroma_port)
    embedder      = ChromaEmbedder(chroma_client)
    llm_matcher   = RFCSemanticMatcher(embedder)
    llm_edges     = llm_matcher.match(rfc_sections)
    logger.info("Found %d LLM-verified edges", len(llm_edges))

    # ── 6. Load all edges ─────────────────────────────────────────────────────
    all_edges = citation_edges + llm_edges
    if all_edges:
        loader.load_implements_spec_edges(all_edges)
    else:
        logger.warning("No IMPLEMENTS_SPEC edges to load.")

    # ── Quick stats ───────────────────────────────────────────────────────────
    with sqlite3.connect(str(settings.sqlite_db_path)) as conn:
        spec_count = conn.execute("SELECT COUNT(*) FROM rfc_specs").fetchone()[0]
        sec_count  = conn.execute("SELECT COUNT(*) FROM rfc_sections").fetchone()[0]
        edge_count = conn.execute("SELECT COUNT(*) FROM implements_spec").fetchone()[0]
    logger.info(
        "Done — %d Specification nodes, %d SpecSection nodes, %d IMPLEMENTS_SPEC edges",
        spec_count, sec_count, edge_count,
    )

    loader.close()


if __name__ == "__main__":
    main()
