"""
backfill_code_intent.py
Standalone script — populates the ChromaDB ``code_intent`` collection for ALL
Component nodes in Neo4j, not just those that happened to have Javadoc during
the main ingest pass.

The main pipeline only creates code_intent vectors for LogicUnits whose
JavaParser extracted a docstring.  This leaves the majority of Component
(class-level) nodes without intent vectors, making semantic search blind to
them.  Running this script after ``nexus ingest`` fills the gap.

Usage (from nexus/ directory):
    py backfill_code_intent.py

Progress is printed every 100 components.  Already-embedded components
(identified by chunk_id in ChromaDB) are skipped, so re-runs are safe.
"""
from __future__ import annotations
import json
import logging
import sys
from pathlib import Path

# ── Add project root to sys.path so relative imports work ────────────────────
sys.path.insert(0, str(Path(__file__).parent))

import chromadb
from neo4j import GraphDatabase

from config.settings import settings
from vectorstore.chunker import EmbeddingChunk
from vectorstore.embedder import ChromaEmbedder

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("backfill_code_intent")


def _annotation_summary(annotations_json: str | None) -> str:
    """Extract annotation names from the JSON string stored in Neo4j."""
    if not annotations_json:
        return ""
    try:
        anns = json.loads(annotations_json)
    except (json.JSONDecodeError, TypeError):
        return ""
    names = []
    for a in anns:
        if isinstance(a, dict):
            n = a.get("name", "").lstrip("@").split("(")[0]
        else:
            n = str(a).lstrip("@").split("(")[0]
        if n:
            names.append(n)
    return ", ".join(names[:5])  # cap at 5 to keep text concise


def _build_text(fqn: str, kind: str, docstring: str | None,
                annotations_json: str | None) -> str:
    """Build a descriptive text string for a Component node."""
    parts = [f"[{kind or 'class'}] {fqn}"]
    ann_str = _annotation_summary(annotations_json)
    if ann_str:
        parts.append(f"[{ann_str}]")
    if docstring and docstring.strip():
        parts.append(docstring.strip()[:800])  # cap Javadoc to 800 chars
    return ": ".join(parts[:1]) + (" " + " ".join(parts[1:])).rstrip()


def main() -> None:
    driver = GraphDatabase.driver(settings.neo4j_uri, auth=settings.neo4j_auth)
    chroma = chromadb.HttpClient(host=settings.chroma_host, port=settings.chroma_port)
    embedder = ChromaEmbedder(chroma)

    # ── 1. Collect already-embedded GEIDs to skip ─────────────────────────────
    logger.info("Checking existing code_intent vectors…")
    try:
        existing = chroma.get_collection("code_intent")
        existing_meta = existing.get(include=["metadatas"])
        existing_geids: set[str] = {
            m.get("geid", "") for m in existing_meta["metadatas"]
        }
        logger.info("Found %d existing code_intent entries to skip", len(existing_geids))
    except Exception as e:
        logger.warning("Could not read existing code_intent entries: %s", e)
        existing_geids = set()

    # ── 2. Fetch all Component nodes from Neo4j ────────────────────────────────
    logger.info("Fetching Component nodes from Neo4j…")
    with driver.session() as session:
        rows = list(session.run(
            """
            MATCH (c:Component)
            RETURN c.geid        AS geid,
                   c.fqn         AS fqn,
                   c.kind        AS kind,
                   c.docstring   AS docstring,
                   c.annotations AS annotations
            ORDER BY c.fqn
            """
        ))
    logger.info("Fetched %d Component nodes", len(rows))

    # ── 3. Build EmbeddingChunk objects for new / updated components ───────────
    chunks: list[EmbeddingChunk] = []
    skipped = 0
    for row in rows:
        geid = row["geid"]
        if not geid:
            continue
        # Skip if this component already has a code_intent vector
        if geid in existing_geids:
            skipped += 1
            continue

        fqn         = row["fqn"] or ""
        kind        = row["kind"] or "class"
        docstring   = row["docstring"]
        annotations = row["annotations"]

        text = _build_text(fqn, kind, docstring, annotations)
        if not text.strip():
            continue

        chunks.append(EmbeddingChunk(
            chunk_id   = f"{geid}_code_intent",
            geid       = geid,
            fqn        = fqn,
            text       = text,
            chunk_type = "code_intent",
            language   = "java",
            file_path  = "",
            start_line = 0,
            end_line   = 0,
            repo_name  = fqn.split(".")[0] if fqn else "",
        ))

    logger.info(
        "Prepared %d new code_intent chunks (%d already existed, skipped)",
        len(chunks), skipped,
    )

    if not chunks:
        logger.info("Nothing to do — all components already have code_intent vectors.")
        driver.close()
        return

    # ── 4. Upsert in batches ───────────────────────────────────────────────────
    bs = settings.embedding_batch_size
    total = len(chunks)
    for i in range(0, total, bs):
        batch = chunks[i: i + bs]
        embedder.upsert_chunks(batch)
        done = min(i + bs, total)
        if done % 100 == 0 or done == total:
            logger.info("  %d / %d components embedded", done, total)

    logger.info("code_intent backfill complete — %d vectors added.", total)
    driver.close()


if __name__ == "__main__":
    main()
