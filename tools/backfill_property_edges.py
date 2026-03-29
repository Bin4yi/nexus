"""
tools/backfill_property_edges.py
Standalone backfill — builds READS_PROPERTY and WRITES_PROPERTY edges
from already-mirrored Java files WITHOUT re-running the full pipeline.

Skips: git mirroring, ChromaDB embedding, Leiden, LLM summarization.
Runtime: ~10-15 minutes for 10 repos (vs 5 hours full ingest).

Usage (from nexus/ directory, venv active):
    py tools/backfill_property_edges.py

What it does:
  1. Reads all .java files from mirror/
  2. Re-parses them with JavaParser to extract property_reads/property_writes
  3. Creates PropertyKey nodes + READS_PROPERTY / WRITES_PROPERTY edges in Neo4j

After this runs, questions like "is IMPERSONATED_SUBJECT safe to remove?"
will show exactly which methods read and write that key.
"""
from __future__ import annotations
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from neo4j import GraphDatabase
from config.settings import settings
from parsers.uir import Component, LogicUnit, Parameter, FieldDeclaration
from pipeline.ingest import run_parallel_parse
from graph.loader import Neo4jLoader

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("backfill_property_edges")


def main() -> None:
    driver = GraphDatabase.driver(settings.neo4j_uri, auth=settings.neo4j_auth)
    loader = Neo4jLoader(driver)

    mirror_root = settings.repos_mirror_path
    repo_dirs = sorted(d for d in mirror_root.iterdir() if d.is_dir())
    logger.info(
        "Found %d repos in mirror: %s",
        len(repo_dirs), [d.name for d in repo_dirs],
    )

    all_logic_units: list[LogicUnit] = []

    for repo_path in repo_dirs:
        if sys.platform == "win32":
            repo_path = Path("\\\\?\\" + str(repo_path.resolve()))
        repo_name = repo_path.name

        java_files = [
            f for f in repo_path.rglob("*.java")
            if "src/main/java" in str(f).replace("\\", "/")
            or "src\\main\\java" in str(f)
        ]
        if not java_files:
            logger.info("  %s — no src/main/java files, skipping", repo_name)
            continue

        logger.info("Parsing %d Java files from %s ...", len(java_files), repo_name)
        comp_dicts, _, parse_errors = run_parallel_parse(java_files, repo_name)
        for fp, err in parse_errors:
            logger.warning("Parse error %s: %s", fp, err)

        repo_lus = 0
        for cd in comp_dicts:
            lus = [
                LogicUnit(**{
                    **ld,
                    "parameters": [Parameter(**p) for p in ld.get("parameters", [])],
                })
                for ld in cd.get("logic_units", [])
            ]
            all_logic_units.extend(lus)
            repo_lus += len(lus)

        reads_count  = sum(len(lu.property_reads)  for lu in all_logic_units[-repo_lus:])
        writes_count = sum(len(lu.property_writes) for lu in all_logic_units[-repo_lus:])
        logger.info(
            "  %s — %d logic units, %d read keys, %d write keys",
            repo_name, repo_lus, reads_count, writes_count,
        )

    logger.info(
        "Total: %d logic units — loading READS_PROPERTY / WRITES_PROPERTY edges ...",
        len(all_logic_units),
    )
    loader.load_property_access_edges(all_logic_units)

    # Report stats
    with driver.session() as s:
        for rel in ["READS_PROPERTY", "WRITES_PROPERTY"]:
            c = s.run(f"MATCH ()-[r:{rel}]->() RETURN count(r) AS c").single()["c"]
            logger.info("  %s: %d edges", rel, c)
        pk = s.run("MATCH (k:PropertyKey) RETURN count(k) AS c").single()["c"]
        logger.info("  PropertyKey nodes: %d unique keys", pk)

    driver.close()
    logger.info("Done. Re-ask your 'safe to remove?' questions now.")


if __name__ == "__main__":
    main()
