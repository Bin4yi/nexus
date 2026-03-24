"""
backfill_quality.py
One-shot quality improvement backfill. Run after backfill_edges.py.

Phases:
  1. cross_calls   — Re-parse source → resolve cross-repo CALLS edges
  2. body_text     — Store body_text on existing LogicUnit nodes in Neo4j
  3. field_nodes   — Create (:Field) nodes for static-final constants
  4. intent        — Re-embed improved code_intent for all methods (uses body signals)
  5. community     — Re-run Louvain community detection with all edge types

Run:
    py backfill_quality.py [--only cross_calls,body_text,field_nodes,intent,community]
    py backfill_quality.py --only cross_calls,body_text
"""
from __future__ import annotations
import argparse
import logging
import re
import sys
import os
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

MIRROR_ROOT = Path(__file__).parent / "mirror"

# ── Phase 1: Cross-repo CALLS ─────────────────────────────────────────────────

def phase_cross_calls(driver, settings):
    """Re-parse all Java files and resolve cross-repo CALLS edges."""
    logger.info("=== Phase 1: Cross-repo CALLS ===")
    from parsers.java_parser import JavaParser

    parser = JavaParser()
    all_pairs = []

    for repo_dir in MIRROR_ROOT.iterdir():
        if not repo_dir.is_dir():
            continue
        repo_name = repo_dir.name
        java_files = [
            f for f in repo_dir.rglob("*.java")
            if "src/main/java" in str(f).replace("\\", "/")
            or "src\\main\\java" in str(f)
        ]
        logger.info("  Parsing %s: %d main java files", repo_name, len(java_files))

        for jf in java_files:
            try:
                components = parser.parse_file(jf, repo_name=repo_name)
            except Exception as e:
                logger.debug("Parse error %s: %s", jf.name, e)
                continue
            for comp in components:
                for lu in comp.logic_units:
                    for target in lu.calls:
                        if target.count(".") >= 2:
                            all_pairs.append({
                                "caller_geid": lu.geid,
                                "target_fqn": target,
                            })

    logger.info("  Total cross-repo call pairs: %d", len(all_pairs))
    if not all_pairs:
        return 0

    # Deduplicate
    seen = set()
    deduped = []
    for p in all_pairs:
        k = (p["caller_geid"], p["target_fqn"])
        if k not in seen:
            seen.add(k)
            deduped.append(p)

    logger.info("  Deduplicated: %d pairs", len(deduped))

    # Write in batches of 500
    total_new = 0
    bs = 500
    with driver.session() as session:
        for i in range(0, len(deduped), bs):
            batch = deduped[i:i + bs]
            result = session.run(
                """
                UNWIND $pairs AS pair
                MATCH (caller:LogicUnit {geid: pair.caller_geid})
                MATCH (target:LogicUnit)
                WHERE (split(target.fqn, '(')[0] ENDS WITH ('.' + pair.target_fqn)
                       OR split(target.fqn, '(')[0] = pair.target_fqn)
                  AND caller <> target
                  AND NOT (caller)-[:CALLS]->(target)
                MERGE (caller)-[r:CALLS]->(target)
                ON CREATE SET r.confidence = 0.5,
                              r.resolution_tier = 'cross_repo_inferred',
                              r.cross_repo = true
                RETURN count(r) AS c
                """,
                pairs=batch,
            ).single()
            n = result["c"] if result else 0
            total_new += n
            if (i // bs + 1) % 5 == 0:
                logger.info("  [%d/%d] new CALLS edges: %d", i + bs, len(deduped), total_new)

    with driver.session() as s:
        total = s.run("MATCH ()-[:CALLS]->() RETURN count(*) AS c").single()["c"]
    logger.info("  Cross-repo CALLS added: %d  |  Total CALLS: %d", total_new, total)
    return total_new


# ── Phase 2: Store body_text on existing nodes ────────────────────────────────

def phase_body_text(driver, settings):
    """Re-parse source files and store body_text on Neo4j LogicUnit nodes."""
    logger.info("=== Phase 2: Storing body_text on LogicUnit nodes ===")
    from parsers.java_parser import JavaParser

    parser = JavaParser()
    updates = []  # {geid, body_text}

    for repo_dir in MIRROR_ROOT.iterdir():
        if not repo_dir.is_dir():
            continue
        java_files = [
            f for f in repo_dir.rglob("*.java")
            if "src/main/java" in str(f).replace("\\", "/")
            or "src\\main\\java" in str(f)
        ]
        for jf in java_files:
            try:
                components = parser.parse_file(jf, repo_name=repo_dir.name)
            except Exception:
                continue
            for comp in components:
                for lu in comp.logic_units:
                    if lu.body_text:
                        updates.append({
                            "geid": lu.geid,
                            "body_text": lu.body_text[:4000],
                        })

    logger.info("  LogicUnits with body_text: %d", len(updates))
    if not updates:
        return 0

    bs = 500
    written = 0
    with driver.session() as session:
        for i in range(0, len(updates), bs):
            batch = updates[i:i + bs]
            result = session.run(
                """
                UNWIND $rows AS row
                MATCH (n:LogicUnit {geid: row.geid})
                SET n.body_text = row.body_text
                RETURN count(n) AS c
                """,
                rows=batch,
            ).single()
            written += result["c"] if result else 0

    logger.info("  body_text written to %d nodes", written)
    return written


# ── Phase 3: Field / constant nodes ──────────────────────────────────────────

_STATIC_FINAL_RE = re.compile(
    r"(?:public|protected|private)?\s*static\s+final\s+"
    r"(?:String|int|long|boolean|double|float|byte|short|char|[\w.<>]+)\s+"
    r"([A-Z][A-Z0-9_]{2,})\s*=\s*([^;]{1,120});",
)
_PACKAGE_RE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.MULTILINE)
_CLASS_NAME_RE = re.compile(
    r"(?:public\s+)?(?:abstract\s+)?(?:final\s+)?(?:class|interface|enum)\s+(\w+)"
)


def phase_field_nodes(driver, settings):
    """
    Extract public static final constants and create (:Field) nodes in Neo4j.
    Links each field to its declaring Component via [:DECLARES] edge.
    """
    logger.info("=== Phase 3: Field / constant nodes ===")
    import hashlib

    fields = []
    for repo_dir in MIRROR_ROOT.iterdir():
        if not repo_dir.is_dir():
            continue
        for jf in repo_dir.rglob("*.java"):
            if "test" in str(jf).lower():
                continue
            try:
                src = jf.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            pkg_m = _PACKAGE_RE.search(src)
            pkg = pkg_m.group(1) if pkg_m else ""
            cls_m = _CLASS_NAME_RE.search(src)
            if not cls_m:
                continue
            class_fqn = f"{pkg}.{cls_m.group(1)}" if pkg else cls_m.group(1)

            for m in _STATIC_FINAL_RE.finditer(src):
                const_name = m.group(1)
                const_value = m.group(2).strip().strip('"\'')
                field_fqn = f"{class_fqn}.{const_name}"
                geid = hashlib.md5(field_fqn.encode()).hexdigest()[:16]
                fields.append({
                    "geid": geid,
                    "fqn": field_fqn,
                    "name": const_name,
                    "value": const_value[:200],
                    "class_fqn": class_fqn,
                    "file_path": str(jf),
                    "repo_name": repo_dir.name,
                })

    logger.info("  Static final constants found: %d", len(fields))
    if not fields:
        return 0

    bs = 300
    created = 0
    with driver.session() as session:
        for i in range(0, len(fields), bs):
            batch = fields[i:i + bs]
            result = session.run(
                """
                UNWIND $fields AS f
                MERGE (field:Field {geid: f.geid})
                SET field.fqn       = f.fqn,
                    field.name      = f.name,
                    field.value     = f.value,
                    field.file_path = f.file_path,
                    field.repo_name = f.repo_name
                WITH field, f
                MATCH (comp:Component) WHERE comp.fqn = f.class_fqn
                  OR comp.fqn ENDS WITH ('.' + f.class_fqn)
                MERGE (comp)-[:DECLARES]->(field)
                RETURN count(field) AS c
                """,
                fields=batch,
            ).single()
            created += result["c"] if result else 0

    logger.info("  Field nodes created/updated: %d", created)
    return created


# ── Phase 4: Re-embed improved intent ────────────────────────────────────────

def phase_intent(driver, settings):
    """
    Re-build code_intent vectors for all methods using the improved
    _synthesize_intent() that includes body signals (called methods,
    string literals, constants). Requires body_text to be stored first.
    """
    logger.info("=== Phase 4: Re-embed improved code_intent ===")
    import chromadb
    from vectorstore.embedder import ChromaEmbedder
    from vectorstore.chunker import EmbeddingChunk, _synthesize_intent, _build_context_prefix
    from parsers.uir import LogicUnit as UIRLogicUnit

    chroma = chromadb.HttpClient(host=settings.chroma_host, port=settings.chroma_port)
    embedder = ChromaEmbedder(chroma)

    # Fetch all LogicUnits with body_text from Neo4j
    logger.info("  Fetching LogicUnits with body_text from Neo4j...")
    with driver.session() as s:
        rows = list(s.run(
            """
            MATCH (n:LogicUnit)
            WHERE n.body_text IS NOT NULL AND n.body_text <> ''
            RETURN n.geid AS geid, n.fqn AS fqn, n.body_text AS body_text,
                   n.return_type AS return_type, n.file_path AS file_path,
                   n.start_line AS start_line, n.end_line AS end_line,
                   n.repo_name AS repo_name
            """
        ))

    logger.info("  Methods with body_text: %d", len(rows))
    if not rows:
        logger.warning("  No body_text found — run phase body_text first")
        return 0

    # Build a minimal LogicUnit-like stub to feed _synthesize_intent
    from dataclasses import dataclass, field as dc_field

    @dataclass
    class _LUStub:
        geid: str
        fqn: str
        body_text: str
        return_type: str = ""
        file_path: str = ""
        start_line: int = 0
        end_line: int = 0
        repo_name: str = ""
        parameters: list = dc_field(default_factory=list)
        docstring: str = ""

    chunks = []
    for row in rows:
        lu = _LUStub(
            geid=row["geid"] or "",
            fqn=row["fqn"] or "",
            body_text=row["body_text"] or "",
            return_type=row["return_type"] or "",
            file_path=row["file_path"] or "",
            start_line=row["start_line"] or 0,
            end_line=row["end_line"] or 0,
            repo_name=row["repo_name"] or "",
        )
        prefix = _build_context_prefix(lu.fqn)
        intent_text = prefix + _synthesize_intent(lu)
        chunks.append(EmbeddingChunk(
            chunk_id=f"{lu.geid}_code_intent",
            geid=lu.geid,
            fqn=lu.fqn,
            text=intent_text,
            chunk_type="code_intent",
            file_path=lu.file_path,
            start_line=lu.start_line,
            end_line=lu.end_line,
            repo_name=lu.repo_name,
        ))

    logger.info("  Upserting %d improved intent chunks...", len(chunks))
    bs = 300
    upserted = 0
    for i in range(0, len(chunks), bs):
        embedder.upsert_chunks(chunks[i:i + bs])
        upserted += len(chunks[i:i + bs])
        logger.info("  [%d/%d] upserted", upserted, len(chunks))

    final = chroma.get_collection("code_intent").count()
    logger.info("  Done. code_intent total: %d", final)
    return upserted


# ── Phase 5: Re-run community detection ──────────────────────────────────────

def phase_community(driver, settings):
    """
    Re-run Louvain community detection using ALL edge types:
    CALLS + INJECTS + OVERRIDES (+ INSTANTIATES).
    Writes community_id back to all Component and LogicUnit nodes.
    """
    logger.info("=== Phase 5: Community detection (Louvain with all edges) ===")

    with driver.session() as session:
        # Drop old projection if it exists
        try:
            session.run("CALL gds.graph.drop('nexus_community', false)").consume()
        except Exception:
            pass

        # Project graph with all relationship types
        logger.info("  Projecting graph with CALLS+INJECTS+OVERRIDES+INSTANTIATES...")
        session.run(
            """
            CALL gds.graph.project(
              'nexus_community',
              ['Component', 'LogicUnit'],
              {
                CALLS:        {orientation: 'UNDIRECTED'},
                INJECTS:      {orientation: 'UNDIRECTED'},
                OVERRIDES:    {orientation: 'UNDIRECTED'},
                INSTANTIATES: {orientation: 'UNDIRECTED'}
              }
            )
            """
        ).consume()
        logger.info("  Graph projected. Running Louvain...")

        # Run Louvain
        result = session.run(
            """
            CALL gds.louvain.write('nexus_community', {
                writeProperty:     'community_id',
                maxIterations:     10,
                maxLevels:         5,
                tolerance:         0.0001
            })
            YIELD communityCount, modularity
            RETURN communityCount, modularity
            """
        ).single()

        community_count = result["communityCount"] if result else 0
        modularity = result["modularity"] if result else 0
        logger.info(
            "  Louvain complete: %d communities, modularity=%.4f",
            community_count, modularity,
        )

        # Clean up projection
        try:
            session.run("CALL gds.graph.drop('nexus_community', false)").consume()
        except Exception:
            pass

    return community_count


# ── Main ──────────────────────────────────────────────────────────────────────

def run(only: set[str]):
    from neo4j import GraphDatabase
    from config.settings import settings

    driver = GraphDatabase.driver(settings.neo4j_uri, auth=settings.neo4j_auth)
    results = {}

    if "cross_calls" in only:
        results["cross_calls"] = phase_cross_calls(driver, settings)

    if "body_text" in only:
        results["body_text"] = phase_body_text(driver, settings)

    if "field_nodes" in only:
        results["field_nodes"] = phase_field_nodes(driver, settings)

    if "intent" in only:
        results["intent"] = phase_intent(driver, settings)

    if "community" in only:
        results["community"] = phase_community(driver, settings)

    logger.info("=== Summary ===")
    for k, v in results.items():
        logger.info("  %-15s %s", k, v)

    driver.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Quality backfill — cross_calls, body_text, field_nodes, intent, community")
    parser.add_argument(
        "--only",
        default="cross_calls,body_text,field_nodes,intent,community",
        help="Comma-separated phases to run",
    )
    args = parser.parse_args()
    only = set(x.strip() for x in args.only.split(","))

    sys.path.insert(0, os.path.dirname(__file__))
    run(only)
