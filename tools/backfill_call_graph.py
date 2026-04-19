"""
backfill_call_graph.py
Standalone script — builds CALLS, INJECTS, ANNOTATED_WITH edges from
already-parsed Java files without re-running the full ingest pipeline.

Useful after a fix to fqn_builder.py or loader.py to rebuild edges
without waiting 90+ minutes for RFC semantic matching.

Usage (from nexus/ directory):
    py backfill_call_graph.py
"""
from __future__ import annotations
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config.settings import settings
from parsers.java_parser import JavaParser
from parsers.uir import Component, LogicUnit, Parameter, FieldDeclaration
from pipeline.ingest import run_parallel_parse
from graph.sqlite_loader import SQLiteLoader

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("backfill_call_graph")


def main() -> None:
    loader = SQLiteLoader()
    loader.open()

    mirror_root = settings.repos_mirror_path
    repo_dirs = [d for d in mirror_root.iterdir() if d.is_dir()]
    logger.info("Found %d repos in mirror: %s", len(repo_dirs), [d.name for d in repo_dirs])

    all_logic_units: list[LogicUnit] = []
    all_components: list[Component] = []

    for repo_path in repo_dirs:
        if sys.platform == "win32":
            repo_path = Path("\\\\?\\" + str(repo_path.resolve()))
        repo_name = repo_path.name
        java_files = list(repo_path.rglob("*.java"))
        logger.info("Parsing %d Java files from %s ...", len(java_files), repo_name)

        comp_dicts, _, parse_errors = run_parallel_parse(java_files, repo_name)
        for fp, err in parse_errors:
            logger.warning("Parse error %s: %s", fp, err)

        for cd in comp_dicts:
            lus = [
                LogicUnit(**{**ld, "parameters": [Parameter(**p) for p in ld.get("parameters", [])]})
                for ld in cd.get("logic_units", [])
            ]
            fields = [FieldDeclaration(**f) for f in cd.get("fields", [])]
            comp = Component(**{**cd, "logic_units": lus, "fields": fields})
            all_components.append(comp)
            all_logic_units.extend(lus)

        logger.info("  %d components, %d logic units so far", len(all_components), len(all_logic_units))

    logger.info("Building CALLS edges (%d logic units)...", len(all_logic_units))
    loader.load_call_graph(all_logic_units)

    logger.info("Building INJECTS edges...")
    loader.load_injection_edges(all_components)

    logger.info("Building ANNOTATED_WITH edges...")
    loader.load_annotated_with(all_components)

    logger.info("Building IMPLEMENTS/EXTENDS edges...")
    loader.load_implements_extends(all_components)

    # Quick stats
    with driver.session() as s:
        for rel in ["CALLS", "INJECTS", "ANNOTATED_WITH", "IMPLEMENTS", "EXTENDS"]:
            c = s.run(f"MATCH ()-[r:{rel}]->() RETURN count(r) AS c").single()["c"]
            logger.info("  %s: %d edges", rel, c)

    driver.close()
    logger.info("Done.")


if __name__ == "__main__":
    main()
