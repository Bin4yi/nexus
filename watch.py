"""
watch.py
Incremental file watcher daemon for CodeNexus.

Monitors mirror/ for changed .java files using SHA-256 fingerprinting.
When a file changes, re-parses and re-indexes ONLY that file — no full
pipeline re-run. Community detection, summarization, and global rollup
are NOT re-run (those require the full graph).

Usage:
    py watch.py                           # watch all repos in mirror/
    py watch.py --repo identity-oauth2    # watch one repo only
    py watch.py --interval 10             # check every 10 s (default: 5)
    py watch.py --once                    # one scan then exit (CI mode)
    py watch.py --dry-run                 # detect changes, don't re-index
"""
from __future__ import annotations
import argparse
import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("nexus.watch")

sys.path.insert(0, os.path.dirname(__file__))

MIRROR_ROOT   = Path(__file__).parent / "mirror"
HASH_STORE    = Path(__file__).parent / ".nexus_watch_hashes.json"
POLL_INTERVAL = 5   # seconds


# ── Hashing ────────────────────────────────────────────────────────────────────

def _file_hash(path: Path) -> str:
    """SHA-256 of file contents, first 16 hex chars (sufficient for change detection)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65_536):
            h.update(chunk)
    return h.hexdigest()[:16]


def _load_hashes() -> dict[str, str]:
    if HASH_STORE.exists():
        try:
            return json.loads(HASH_STORE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_hashes(hashes: dict[str, str]) -> None:
    HASH_STORE.write_text(json.dumps(hashes, indent=2), encoding="utf-8")


# ── File scanning ──────────────────────────────────────────────────────────────

def _scan(repo_filter: Optional[str] = None) -> dict[str, str]:
    """Return {rel_path_str: hash} for every .java in mirror/ (prod code only)."""
    result: dict[str, str] = {}
    if not MIRROR_ROOT.exists():
        return result
    for java_file in MIRROR_ROOT.rglob("*.java"):
        rel = java_file.relative_to(MIRROR_ROOT)
        parts = rel.parts
        if repo_filter and parts[0] != repo_filter:
            continue
        # Skip test files to keep re-indexing fast
        rel_str = str(rel).replace("\\", "/")
        if "src/test/java" in rel_str:
            continue
        try:
            result[rel_str] = _file_hash(java_file)
        except OSError:
            pass
    return result


def _diff(old: dict[str, str], new: dict[str, str]) -> tuple[list[str], list[str], list[str]]:
    """Returns (added, modified, deleted) rel-path lists."""
    added    = [k for k in new if k not in old]
    modified = [k for k in new if k in old and old[k] != new[k]]
    deleted  = [k for k in old if k not in new]
    return added, modified, deleted


# ── Incremental re-indexing ────────────────────────────────────────────────────

def _repo_name_from_rel(rel_str: str) -> str:
    """Extract repo name from relative path (first path segment)."""
    return rel_str.split("/")[0]


def _reindex_file(
    rel_str: str,
    driver,
    embedder,
    dry_run: bool = False,
) -> bool:
    """
    Re-parse one changed Java file and update Neo4j + ChromaDB.

    Strategy:
      1. Parse the file → Component + LogicUnit objects
      2. MERGE Component/LogicUnit nodes in Neo4j (updates properties in-place)
      3. Refresh CALLS edges for the changed methods (delete old, add new)
      4. Upsert ChromaDB vectors (same geid-based IDs = automatic replacement)

    Returns True on success.
    """
    abs_path = MIRROR_ROOT / rel_str.replace("/", os.sep)
    repo_name = _repo_name_from_rel(rel_str)

    if dry_run:
        logger.info("  [DRY RUN] Would re-index: %s", rel_str)
        return True

    from pipeline.ingest import run_parallel_parse
    from vectorstore.chunker import EmbeddingChunk
    from parsers.uir import Parameter, FieldDeclaration, Component, LogicUnit

    # ── 1. Parse ──────────────────────────────────────────────────────────────
    try:
        comp_dicts, chunk_dicts, errors = run_parallel_parse(
            [abs_path], repo_name, max_workers=1,
        )
    except Exception as e:
        logger.error("  Parse failed for %s: %s", rel_str, e)
        return False

    for fp, err in errors:
        logger.warning("  Parse error in %s: %s", fp, err)

    if not comp_dicts:
        logger.warning("  No components found in %s — skipping", rel_str)
        return False

    # ── 2. Reconstruct UIR ────────────────────────────────────────────────────
    components: list[Component] = []
    for cd in comp_dicts:
        lus = [
            LogicUnit(**{**ld, "parameters": [Parameter(**p) for p in ld.get("parameters", [])]})
            for ld in cd.get("logic_units", [])
        ]
        fields = [FieldDeclaration(**f) for f in cd.get("fields", [])]
        components.append(Component(**{**cd, "logic_units": lus, "fields": fields}))

    # ── 3. Neo4j MERGE (update existing nodes, create if new) ─────────────────
    try:
        _neo4j_upsert_components(driver, components)
    except Exception as e:
        logger.error("  Neo4j upsert failed for %s: %s", rel_str, e)
        return False

    # ── 4. ChromaDB upsert (same geid IDs = auto-replace vectors) ────────────
    try:
        chunks = [EmbeddingChunk(**cd) for cd in chunk_dicts]
        if chunks:
            embedder.upsert_chunks(chunks)
    except Exception as e:
        logger.error("  ChromaDB upsert failed for %s: %s", rel_str, e)
        return False

    n_lu = sum(len(c.logic_units) for c in components)
    logger.info(
        "  Re-indexed %-60s  %d component(s), %d method(s), %d chunk(s)",
        rel_str[-60:], len(components), n_lu, len(chunk_dicts),
    )
    return True


def _neo4j_upsert_components(driver, components) -> None:
    """
    MERGE each Component + LogicUnit into Neo4j.
    Refreshes outgoing CALLS edges for every updated method.
    """
    with driver.session() as s:
        for comp in components:
            # MERGE Component node
            s.run(
                """
                MERGE (c:Component {geid: $geid})
                SET c.fqn        = $fqn,
                    c.file_path  = $file_path,
                    c.kind       = $kind,
                    c.repo_name  = $repo_name,
                    c.start_line = $start_line,
                    c.end_line   = $end_line,
                    c.docstring  = $docstring
                """,
                geid=comp.geid, fqn=comp.fqn,
                file_path=str(comp.file_path),
                kind=getattr(comp, "kind", "CLASS"),
                repo_name=getattr(comp, "repo_name", ""),
                start_line=getattr(comp, "start_line", 0),
                end_line=getattr(comp, "end_line", 0),
                docstring=getattr(comp, "docstring", ""),
            ).consume()

            for lu in comp.logic_units:
                # MERGE LogicUnit node + CONTAINS edge
                s.run(
                    """
                    MERGE (lu:LogicUnit {geid: $geid})
                    SET lu.fqn         = $fqn,
                        lu.name        = $name,
                        lu.file_path   = $file_path,
                        lu.start_line  = $start_line,
                        lu.end_line    = $end_line,
                        lu.body_text   = $body_text,
                        lu.return_type = $return_type,
                        lu.repo_name   = $repo_name
                    WITH lu
                    MATCH (c:Component {geid: $comp_geid})
                    MERGE (c)-[:CONTAINS]->(lu)
                    """,
                    geid=lu.geid, fqn=lu.fqn,
                    name=lu.name,
                    file_path=str(lu.file_path),
                    start_line=lu.start_line,
                    end_line=lu.end_line,
                    body_text=getattr(lu, "body_text", ""),
                    return_type=getattr(lu, "return_type", ""),
                    repo_name=getattr(lu, "repo_name", ""),
                    comp_geid=comp.geid,
                ).consume()

                # Refresh CALLS edges: delete old outgoing, add new ones
                s.run(
                    "MATCH (lu:LogicUnit {geid: $geid})-[r:CALLS]->() DELETE r",
                    geid=lu.geid,
                ).consume()

                for callee_fqn in getattr(lu, "calls", []):
                    s.run(
                        """
                        MATCH (src:LogicUnit {geid: $src_geid})
                        MATCH (dst) WHERE dst.fqn = $callee_fqn
                            AND (dst:LogicUnit OR dst:Component)
                        MERGE (src)-[:CALLS {confidence: 0.9, tier: 'structural'}]->(dst)
                        """,
                        src_geid=lu.geid, callee_fqn=callee_fqn,
                    ).consume()


# ── Remove deleted file nodes ──────────────────────────────────────────────────

def _remove_deleted(rel_str: str, driver, embedder, dry_run: bool = False) -> None:
    """Remove Component + LogicUnit nodes for a deleted file from Neo4j + ChromaDB."""
    file_path_fragment = rel_str.replace("\\", "/")

    if dry_run:
        logger.info("  [DRY RUN] Would remove nodes for: %s", rel_str)
        return

    with driver.session() as s:
        # Collect geids before deleting so we can purge ChromaDB
        geids = [
            r["geid"] for r in s.run(
                "MATCH (n) WHERE n.file_path CONTAINS $fp AND n.geid IS NOT NULL "
                "RETURN n.geid AS geid",
                fp=file_path_fragment,
            )
        ]
        s.run(
            "MATCH (n) WHERE n.file_path CONTAINS $fp DETACH DELETE n",
            fp=file_path_fragment,
        ).consume()

    # Remove ChromaDB vectors for all affected geids
    for col_name in ("code_intent", "code_logic"):
        try:
            col = embedder._logic_col if col_name == "code_logic" else embedder._intent_col
            ids_to_delete = [f"{g}_{col_name}" for g in geids]
            # Also windowed variants: {geid}_code_logic_0, _1, ...
            for suffix in range(20):
                ids_to_delete.append(f"{geids[0] if geids else ''}_code_logic_{suffix}")
            col.delete(ids=[i for i in ids_to_delete if i])
        except Exception:
            pass

    logger.info("  Removed %d node(s) for deleted file: %s", len(geids), rel_str)


# ── Main loop ──────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="CodeNexus incremental file watcher")
    parser.add_argument("--repo",     help="Watch only this repo (folder name in mirror/)")
    parser.add_argument("--interval", type=float, default=POLL_INTERVAL,
                        help=f"Poll interval in seconds (default: {POLL_INTERVAL})")
    parser.add_argument("--once",     action="store_true",
                        help="Run one scan then exit (useful in CI)")
    parser.add_argument("--dry-run",  action="store_true",
                        help="Detect changes but don't write to Neo4j or ChromaDB")
    args = parser.parse_args()

    # ── Connect ───────────────────────────────────────────────────────────────
    logger.info("Connecting to Neo4j + ChromaDB…")
    import chromadb
    from neo4j import GraphDatabase
    from config.settings import settings
    from vectorstore.embedder import ChromaEmbedder

    driver  = GraphDatabase.driver(settings.neo4j_uri, auth=settings.neo4j_auth)
    chroma  = chromadb.HttpClient(host=settings.chroma_host, port=settings.chroma_port)
    embedder = ChromaEmbedder(chroma)

    # ── Load existing hashes ──────────────────────────────────────────────────
    known = _load_hashes()
    if not known:
        logger.info("No hash store found — doing initial scan (baseline, no re-index)")
        known = _scan(args.repo)
        _save_hashes(known)
        logger.info("Baseline: %d Java files fingerprinted in mirror/", len(known))
        if args.once:
            driver.close()
            return

    logger.info(
        "Watching mirror/%s  (interval: %gs, dry-run: %s)",
        args.repo or "*", args.interval, args.dry_run,
    )
    print("─" * 60)
    print("  CodeNexus File Watcher")
    print(f"  Watching: {MIRROR_ROOT}")
    print(f"  Interval: {args.interval}s | Dry-run: {args.dry_run}")
    print("  Ctrl+C to stop")
    print("─" * 60)

    try:
        while True:
            current = _scan(args.repo)
            added, modified, deleted = _diff(known, current)

            if added or modified or deleted:
                ts = time.strftime("%H:%M:%S")
                logger.info(
                    "[%s] Changes: +%d added, ~%d modified, -%d deleted",
                    ts, len(added), len(modified), len(deleted),
                )

                changed = added + modified
                ok = failed = 0
                for rel in changed:
                    if _reindex_file(rel, driver, embedder, dry_run=args.dry_run):
                        ok += 1
                    else:
                        failed += 1

                for rel in deleted:
                    _remove_deleted(rel, driver, embedder, dry_run=args.dry_run)

                if not args.dry_run:
                    known.update({k: current[k] for k in changed})
                    for k in deleted:
                        known.pop(k, None)
                    _save_hashes(known)

                logger.info("Done. %d succeeded, %d failed", ok, failed)
            else:
                logger.debug("No changes detected")

            if args.once:
                break

            time.sleep(args.interval)

    except KeyboardInterrupt:
        print("\nWatcher stopped.")
    finally:
        driver.close()


if __name__ == "__main__":
    main()
