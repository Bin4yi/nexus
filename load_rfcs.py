"""
load_rfcs.py
Standalone script — runs RFC specification grounding against existing databases.

Use this when you want to (re-)run only the RFC step without a full re-ingest.
Run from the nexus/ directory:

    py load_rfcs.py

What it does:
    1. Parses all RFC text files from ./rfcs/
    2. Creates (:Specification) nodes in Neo4j
    3. Splits each RFC into numbered sections (~800 sections for 14 RFCs)
    4. For each section: ChromaDB retrieves top-5 candidate Java classes
    5. GPT-4o-mini verifies which candidates implement the requirement
    6. Writes [:IMPLEMENTS_SPEC] edges with match_type + similarity_score
"""
import logging
import sys
from pathlib import Path

# ── Make sure we run from the nexus/ directory ──────────────────────────────
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
)
logger = logging.getLogger("load_rfcs")


def main():
    from config.settings import settings

    # Always resolve rfc_path relative to this script's location
    rfc_path = (HERE / "rfcs").resolve()
    logger.info("RFC directory: %s", rfc_path)

    if not rfc_path.exists():
        logger.error("rfcs/ directory not found at %s", rfc_path)
        sys.exit(1)

    rfc_files = list(rfc_path.glob("*.txt")) + list(rfc_path.glob("*.md"))
    logger.info("Found %d RFC files", len(rfc_files))
    if not rfc_files:
        logger.error("No .txt or .md files found in %s", rfc_path)
        sys.exit(1)

    # ── Connect to Neo4j ─────────────────────────────────────────────────────
    from neo4j import GraphDatabase
    driver = GraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password),
    )
    try:
        driver.verify_connectivity()
        logger.info("Neo4j connected: %s", settings.neo4j_uri)
    except Exception as e:
        logger.error("Cannot connect to Neo4j: %s", e)
        sys.exit(1)

    # ── Connect to ChromaDB ──────────────────────────────────────────────────
    import chromadb
    chroma_client = chromadb.HttpClient(
        host=settings.chroma_host,
        port=settings.chroma_port,
    )
    try:
        chroma_client.heartbeat()
        logger.info("ChromaDB connected: %s:%s", settings.chroma_host, settings.chroma_port)
    except Exception as e:
        logger.error("Cannot connect to ChromaDB: %s", e)
        sys.exit(1)

    # Verify code_intent collection has data
    from vectorstore.embedder import ChromaEmbedder
    embedder = ChromaEmbedder(chroma_client)
    intent_count = embedder._intent_col.count()
    logger.info("code_intent collection: %d chunks available for matching", intent_count)
    if intent_count == 0:
        logger.error(
            "code_intent collection is empty — run full ingestion first so "
            "Java classes are embedded before RFC matching."
        )
        sys.exit(1)

    # ── Parse RFC files ──────────────────────────────────────────────────────
    from parsers.rfc_parser import RFCParser
    parser = RFCParser()

    # Override rfc_path to the absolute path
    specs = parser.parse_rfc_files(rfc_path)
    if not specs:
        logger.error("No RFC specifications parsed from %s", rfc_path)
        sys.exit(1)
    logger.info("Parsed %d RFC specifications", len(specs))

    # ── Load Specification nodes into Neo4j ───────────────────────────────────
    from graph.loader import Neo4jLoader
    loader = Neo4jLoader(driver)
    loader.load_specification_nodes(specs)
    logger.info("Specification nodes loaded into Neo4j")

    # ── Chunk RFC sections ────────────────────────────────────────────────────
    sections = parser.chunk_rfc_sections(specs)
    logger.info("Chunked %d RFC sections across %d RFCs", len(sections), len(specs))

    # ── LLM-verified semantic matching ────────────────────────────────────────
    from parsers.rfc_semantic_matcher import RFCSemanticMatcher
    matcher = RFCSemanticMatcher(embedder)
    llm_edges = matcher.match(sections)

    # ── Also scan for any explicit citations (usually 0, but worth checking) ──
    # We don't have the java_files list here, so skip citation scan
    # (the LLM matcher covers everything anyway)

    # ── Load IMPLEMENTS_SPEC edges ────────────────────────────────────────────
    if llm_edges:
        loader.load_implements_spec_edges(llm_edges)
        logger.info(
            "Loaded %d [:IMPLEMENTS_SPEC] edges into Neo4j", len(llm_edges)
        )
    else:
        logger.warning(
            "No IMPLEMENTS_SPEC edges were produced — check that "
            "code_intent has embeddings and LLM_API_KEY is set in .env"
        )

    driver.close()

    # ── Verification ──────────────────────────────────────────────────────────
    logger.info("Done. Verify in Neo4j Browser:")
    logger.info("  MATCH (n:Specification) RETURN n LIMIT 25")
    logger.info("  MATCH (c:Component)-[r:IMPLEMENTS_SPEC]->(s:Specification)")
    logger.info("  RETURN c.fqn, s.rfc_number, r.similarity_score")
    logger.info("  ORDER BY r.similarity_score DESC LIMIT 20")


if __name__ == "__main__":
    main()
