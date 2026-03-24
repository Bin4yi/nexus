"""
backfill_method_intent.py
Backfills code_intent vectors for all LogicUnit nodes that currently
lack them in ChromaDB (i.e. methods with no Javadoc).

Run from nexus/ with:
    py backfill_method_intent.py [--dry-run] [--batch 500]

What it does:
  1. Queries Neo4j for ALL LogicUnit GEIDs + their metadata.
  2. Queries ChromaDB code_intent for GEIDs that already have vectors.
  3. For each missing GEID, reconstructs a _synthesize_intent string
     from the node's Neo4j properties (fqn, file_path, start/end line).
  4. Upserts the new code_intent chunks into ChromaDB.

Expected result for the current graph:
  ~11,587 LogicUnits - ~2,701 already embedded = ~8,886 new vectors added.
"""
from __future__ import annotations
import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass, field

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


@dataclass
class _LUStub:
    geid: str
    fqn: str
    file_path: str
    start_line: int
    end_line: int
    repo_name: str
    body_text: str = ""      # actual source code body stored on the node
    docstring: str = ""      # Javadoc if present


def _build_intent_text(lu: _LUStub) -> str:
    """
    Build a rich intent string for semantic search.

    Priority:
      1. docstring  — if Javadoc was extracted, use it (most descriptive)
      2. body_text  — actual source code; embedder understands code structure
      3. fqn only   — last resort (original behaviour, very weak)

    The [Package:] [Class:] prefix is always included so the vector
    carries class-level context alongside the method signal.
    """
    fqn = lu.fqn or ""
    base = fqn.split("(")[0] if "(" in fqn else fqn
    parts = base.split(".")
    class_name   = parts[-2] if len(parts) >= 2 else ""
    package_name = ".".join(parts[:-2]) if len(parts) >= 3 else ""
    method_name  = fqn.split("(")[0].rsplit(".", 1)[-1] if fqn else "unknown"

    prefix = ""
    if package_name:
        prefix = f"[Package: {package_name}] [Class: {class_name}] "
    elif class_name:
        prefix = f"[Class: {class_name}] "

    if lu.docstring and lu.docstring.strip():
        return f"{prefix}{method_name}: {lu.docstring.strip()}"

    if lu.body_text and lu.body_text.strip():
        # Truncate body to 1 000 chars — enough for semantic signal, not too large
        body = lu.body_text.strip()[:1000]
        return f"{prefix}{method_name}:\n{body}"

    # Fallback: bare method name (original behaviour)
    return f"{prefix}{method_name}()"


def run(dry_run: bool = False, batch_size: int = 500):
    import chromadb
    from neo4j import GraphDatabase
    from config.settings import settings
    from vectorstore.embedder import ChromaEmbedder
    from vectorstore.chunker import EmbeddingChunk

    logger.info("Connecting to Neo4j and ChromaDB…")
    driver = GraphDatabase.driver(settings.neo4j_uri, auth=settings.neo4j_auth)
    chroma = chromadb.HttpClient(host=settings.chroma_host, port=settings.chroma_port)
    embedder = ChromaEmbedder(chroma)

    # ── Step 1: fetch all LogicUnit stubs from Neo4j ────────────────────────
    logger.info("Fetching all LogicUnit nodes from Neo4j…")
    with driver.session() as s:
        rows = list(s.run(
            """
            MATCH (n:LogicUnit)
            RETURN n.geid       AS geid,
                   n.fqn        AS fqn,
                   n.file_path  AS file_path,
                   n.start_line AS start_line,
                   n.end_line   AS end_line,
                   n.repo_name  AS repo_name,
                   n.body_text  AS body_text,
                   n.docstring  AS docstring
            """
        ))
    all_stubs = {
        r["geid"]: _LUStub(
            geid=r["geid"] or "",
            fqn=r["fqn"] or "",
            file_path=r["file_path"] or "",
            start_line=r["start_line"] or 0,
            end_line=r["end_line"] or 0,
            repo_name=r["repo_name"] or "",
            body_text=r["body_text"] or "",
            docstring=r["docstring"] or "",
        )
        for r in rows
        if r["geid"]
    }
    logger.info("Total LogicUnits in Neo4j: %d", len(all_stubs))

    # ── Step 2: find GEIDs already in code_intent ───────────────────────────
    logger.info("Scanning existing code_intent vectors…")
    try:
        col = chroma.get_collection("code_intent")
        total_existing = col.count()
        logger.info("code_intent currently has %d vectors", total_existing)

        # Fetch all existing chunk IDs (only the ones with suffix _code_intent)
        existing_geids: set[str] = set()
        # Paginate — get() can handle large collections but we use include=[] to
        # reduce bandwidth (we only need the IDs)
        offset = 0
        page = 1000
        while True:
            batch = col.get(
                limit=page,
                offset=offset,
                include=[],   # only IDs needed
            )
            if not batch["ids"]:
                break
            for chunk_id in batch["ids"]:
                # chunk_id format: "{geid}_code_intent"
                if chunk_id.endswith("_code_intent"):
                    geid = chunk_id[: -len("_code_intent")]
                    existing_geids.add(geid)
            offset += page
            if len(batch["ids"]) < page:
                break

        logger.info("GEIDs already in code_intent: %d", len(existing_geids))
    except Exception as e:
        logger.warning("Could not read code_intent collection: %s — will embed all", e)
        existing_geids = set()

    # ── Step 3: compute missing set ─────────────────────────────────────────
    missing_geids = set(all_stubs.keys()) - existing_geids
    logger.info("GEIDs missing code_intent vectors: %d", len(missing_geids))

    if not missing_geids:
        logger.info("Nothing to do — all methods already have code_intent vectors.")
        driver.close()
        return

    if dry_run:
        logger.info("[DRY RUN] Would embed %d new code_intent chunks.", len(missing_geids))
        driver.close()
        return

    # ── Step 4: build and upsert chunks in batches ──────────────────────────
    missing_list = [all_stubs[g] for g in missing_geids]
    total = len(missing_list)
    upserted = 0
    start_time = time.time()

    for i in range(0, total, batch_size):
        batch_stubs = missing_list[i : i + batch_size]
        chunks = []
        for lu in batch_stubs:
            text = _build_intent_text(lu)
            if not text.strip():
                continue
            chunks.append(EmbeddingChunk(
                chunk_id=f"{lu.geid}_code_intent",
                geid=lu.geid,
                fqn=lu.fqn,
                text=text,
                chunk_type="code_intent",
                file_path=lu.file_path,
                start_line=lu.start_line,
                end_line=lu.end_line,
                repo_name=lu.repo_name,
            ))

        if not chunks:
            continue

        embedder.upsert_chunks(chunks)
        upserted += len(chunks)
        elapsed = time.time() - start_time
        rate = upserted / elapsed if elapsed > 0 else 0
        logger.info(
            "  [%d/%d]  upserted %d  (%.0f/s)",
            min(i + batch_size, total), total, upserted, rate,
        )

    final_count = chroma.get_collection("code_intent").count()
    logger.info("Done. Upserted %d new intent vectors. code_intent total: %d", upserted, final_count)
    driver.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill code_intent vectors for undocumented methods")
    parser.add_argument("--dry-run", action="store_true", help="Report counts only, no writes")
    parser.add_argument("--batch", type=int, default=500, help="Upsert batch size (default 500)")
    args = parser.parse_args()

    sys.path.insert(0, os.path.dirname(__file__))
    run(dry_run=args.dry_run, batch_size=args.batch)
