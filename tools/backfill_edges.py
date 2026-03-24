"""
backfill_edges.py
Backfills three missing edge types by re-scanning the mirror/ source:

  INJECTS   — Component → Component (OSGi @Reference method injection)
  DEPENDS_ON — Component → Component (Maven pom.xml <dependency> blocks)
  OVERRIDES  — LogicUnit → LogicUnit (same method name across EXTENDS hierarchy)

Run from nexus/:
    py backfill_edges.py [--dry-run] [--skip-injects] [--skip-depends] [--skip-overrides]
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

# ── Regex patterns (read from real WSO2 source) ───────────────────────────────

# Method-level @Reference:
#   @Reference(
#       name = "...",
#       service = SomeInterface.class,
#       cardinality = ReferenceCardinality.OPTIONAL, ...)
#   protected void setSomething(SomeInterface arg) { ... }
_REF_BLOCK_RE = re.compile(
    r"@Reference\s*\(([^)]*)\)\s*(?:[\w\s@()]+)void\s+(\w+)\s*\(\s*([\w.<>]+)",
    re.DOTALL,
)
# Extract service = SomeClass.class from @Reference(...) body
_SERVICE_ATTR_RE = re.compile(r"service\s*=\s*([\w.]+)\.class", re.IGNORECASE)
# Method parameter type — fallback when service= not present
_PARAM_TYPE_RE = re.compile(r"void\s+\w+\s*\(\s*([\w.<>]+)")
# Package declaration
_PACKAGE_RE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.MULTILINE)
# Simple class name
_CLASS_NAME_RE = re.compile(
    r"(?:public\s+)?(?:abstract\s+)?(?:final\s+)?(?:class|interface|enum)\s+(\w+)"
)
# @Override annotation on a method
_OVERRIDE_RE = re.compile(
    r"@Override\s+(?:@\w+\s+)*(?:public|protected|private|static|final|synchronized|native|\s)+\s+([\w<>\[\]]+)\s+(\w+)\s*\("
)

# pom.xml dependency block
_DEP_BLOCK_RE = re.compile(
    r"<dependency>(.*?)</dependency>",
    re.DOTALL,
)
_TAG_RE = re.compile(r"<(\w+)>(.*?)</\1>", re.DOTALL)


# ── INJECTS backfill ──────────────────────────────────────────────────────────

def _collect_injects(mirror_root: Path) -> list[dict]:
    """
    Scan all Java files for method-level @Reference annotations.
    Returns list of {source_fqn, target_simple_name, method_name}
    """
    results = []
    for java_file in mirror_root.rglob("*.java"):
        if "test" in java_file.parts or "Test" in java_file.name:
            continue
        try:
            src = java_file.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        if "@Reference" not in src:
            continue

        pkg_m = _PACKAGE_RE.search(src)
        pkg = pkg_m.group(1) if pkg_m else ""
        cls_m = _CLASS_NAME_RE.search(src)
        if not cls_m:
            continue
        source_fqn = f"{pkg}.{cls_m.group(1)}" if pkg else cls_m.group(1)

        for m in _REF_BLOCK_RE.finditer(src):
            ref_body = m.group(1)
            method_name = m.group(2)
            param_type = m.group(3)

            # service= attribute takes priority
            svc_m = _SERVICE_ATTR_RE.search(ref_body)
            target = svc_m.group(1) if svc_m else param_type.split("<")[0].strip()
            target = target.rsplit(".", 1)[-1]  # simple name

            if target and target not in {"void", "Object", "String"}:
                results.append({
                    "source_fqn": source_fqn,
                    "target_simple": target,
                    "method": method_name,
                })
                logger.debug("INJECTS candidate: %s → %s (via %s)", source_fqn, target, method_name)

    logger.info("INJECTS candidates from source scan: %d", len(results))
    return results


def load_injects(session, pairs: list[dict], dry_run: bool) -> int:
    """Write INJECTS edges using batch Cypher."""
    if dry_run:
        logger.info("[DRY RUN] Would write %d INJECTS edges", len(pairs))
        return len(pairs)

    # Batch by source_fqn to keep query size manageable
    loaded = 0
    batch = []
    for p in pairs:
        batch.append(p)
        if len(batch) >= 200:
            loaded += _flush_injects(session, batch)
            batch = []
    if batch:
        loaded += _flush_injects(session, batch)
    return loaded


def _flush_injects(session, batch: list[dict]) -> int:
    result = session.run(
        """
        UNWIND $pairs AS p
        MATCH (src:Component) WHERE src.fqn = p.source_fqn OR src.fqn ENDS WITH ('.' + p.source_fqn)
        MATCH (tgt:Component) WHERE tgt.fqn ENDS WITH ('.' + p.target_simple)
        MERGE (src)-[r:INJECTS]->(tgt)
        SET r.confidence        = 0.8,
            r.resolution_tier   = 'osgi_reference',
            r.via_method        = p.method
        RETURN count(r) AS c
        """,
        pairs=batch,
    ).single()
    count = result["c"] if result else 0
    logger.info("  flushed %d INJECTS edges", count)
    return count


# ── DEPENDS_ON backfill ───────────────────────────────────────────────────────

def _collect_depends_on(mirror_root: Path) -> list[dict]:
    """
    Parse all pom.xml files to extract <dependency> blocks.
    Returns list of {source_artifact, target_group, target_artifact, scope}
    """
    results = []
    for pom in mirror_root.rglob("pom.xml"):
        try:
            xml = pom.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        # Extract this module's artifactId
        src_artifact = ""
        src_group = ""
        # Only read top-level artifactId (not inside <parent> or <dependency>)
        top_artifact_m = re.search(r"<artifactId>([\w.\-]+)</artifactId>", xml)
        top_group_m = re.search(r"<groupId>([\w.\-]+)</groupId>", xml)
        if top_artifact_m:
            src_artifact = top_artifact_m.group(1)
        if top_group_m:
            src_group = top_group_m.group(1)

        if not src_artifact:
            continue

        for dep_m in _DEP_BLOCK_RE.finditer(xml):
            dep_body = dep_m.group(1)
            tags = {m.group(1): m.group(2).strip() for m in _TAG_RE.finditer(dep_body)}
            tgt_group = tags.get("groupId", "")
            tgt_artifact = tags.get("artifactId", "")
            scope = tags.get("scope", "compile")

            # Only track wso2/carbon dependencies (skip apache, junit, etc.)
            if not tgt_group or "wso2" not in tgt_group and "carbon" not in tgt_group:
                continue
            if scope in ("test", "provided"):
                continue
            if tgt_artifact:
                results.append({
                    "src_artifact": src_artifact,
                    "src_group": src_group,
                    "tgt_artifact": tgt_artifact,
                    "tgt_group": tgt_group,
                    "scope": scope,
                })

    logger.info("DEPENDS_ON candidates from pom.xml: %d", len(results))
    return results


def load_depends_on(session, pairs: list[dict], dry_run: bool) -> int:
    """
    Write DEPENDS_ON edges between Component nodes.
    Matches source by artifact_id, target by fqn prefix (artifact_id ≈ package prefix in WSO2).
    """
    if dry_run:
        logger.info("[DRY RUN] Would write %d DEPENDS_ON candidates", len(pairs))
        return len(pairs)

    loaded = 0
    for i, p in enumerate(pairs):
        n = _flush_depends_on(session, p)
        loaded += n
        if (i + 1) % 50 == 0:
            logger.info("  [%d/%d] DEPENDS_ON: %d edges so far", i + 1, len(pairs), loaded)
    return loaded


def _flush_depends_on(session, pair: dict) -> int:
    """One pair at a time to avoid Cartesian product memory explosion."""
    # src: match Component whose file_path lives under the source artifact directory
    # tgt: match Component whose fqn starts with target artifact (WSO2 convention)
    result = session.run(
        """
        MATCH (src:Component)
        WHERE src.fqn STARTS WITH $src_artifact
        WITH src LIMIT 200
        MATCH (tgt:Component)
        WHERE tgt.fqn STARTS WITH $tgt_artifact AND tgt.fqn <> src.fqn
        WITH src, tgt LIMIT 2000
        MERGE (src)-[r:DEPENDS_ON]->(tgt)
        SET r.scope           = $scope,
            r.confidence      = 1.0,
            r.resolution_tier = 'maven_explicit'
        RETURN count(r) AS c
        """,
        src_artifact=pair["src_artifact"],
        tgt_artifact=pair["tgt_artifact"],
        scope=pair["scope"],
    ).single()
    count = result["c"] if result else 0
    return count


# ── OVERRIDES backfill ────────────────────────────────────────────────────────

def load_overrides(session, dry_run: bool) -> int:
    """
    Find OVERRIDES edges using graph structure:
    If LogicUnit A and LogicUnit B have the same method name (last FQN segment
    before the '('), and A's Component EXTENDS or IMPLEMENTS B's Component,
    then A OVERRIDES B.
    """
    if dry_run:
        # Count candidates
        r = session.run(
            """
            MATCH (child:Component)-[:EXTENDS|IMPLEMENTS]->(parent:Component)
            MATCH (child)-[:HAS_METHOD|DECLARES]->(lu_child:LogicUnit)
            MATCH (parent)-[:HAS_METHOD|DECLARES]->(lu_parent:LogicUnit)
            WHERE lu_child.name = lu_parent.name
              AND lu_child.name IS NOT NULL
              AND lu_child.fqn <> lu_parent.fqn
            RETURN count(*) AS c
            """
        ).single()
        count = r["c"] if r else 0
        logger.info("[DRY RUN] Would write ~%d OVERRIDES edges", count)
        return count

    result = session.run(
        """
        MATCH (child:Component)-[:EXTENDS|IMPLEMENTS]->(parent:Component)
        MATCH (child)-[:HAS_METHOD|DECLARES]->(lu_child:LogicUnit)
        MATCH (parent)-[:HAS_METHOD|DECLARES]->(lu_parent:LogicUnit)
        WHERE lu_child.name = lu_parent.name
          AND lu_child.name IS NOT NULL
          AND lu_child.fqn <> lu_parent.fqn
        MERGE (lu_child)-[r:OVERRIDES]->(lu_parent)
        SET r.confidence      = 0.9,
            r.resolution_tier = 'type_hierarchy'
        RETURN count(r) AS c
        """
    ).single()
    count = result["c"] if result else 0
    logger.info("Loaded %d OVERRIDES edges", count)
    return count


# ── Main ──────────────────────────────────────────────────────────────────────

def run(args):
    from neo4j import GraphDatabase
    from config.settings import settings

    mirror_root = Path(__file__).parent / "mirror"
    if not mirror_root.exists():
        logger.error("mirror/ directory not found at %s", mirror_root)
        sys.exit(1)

    driver = GraphDatabase.driver(settings.neo4j_uri, auth=settings.neo4j_auth)

    # ── INJECTS ──────────────────────────────────────────────────────────────
    if not args.skip_injects:
        logger.info("=== Phase 1: INJECTS (OSGi @Reference methods) ===")
        injects_pairs = _collect_injects(mirror_root)
        with driver.session() as s:
            n = load_injects(s, injects_pairs, args.dry_run)
        logger.info("INJECTS: %d edges written", n)

    # ── DEPENDS_ON ───────────────────────────────────────────────────────────
    if not args.skip_depends:
        logger.info("=== Phase 2: DEPENDS_ON (Maven pom.xml) ===")
        dep_pairs = _collect_depends_on(mirror_root)
        with driver.session() as s:
            n = load_depends_on(s, dep_pairs, args.dry_run)
        logger.info("DEPENDS_ON: %d edges written", n)

    # ── OVERRIDES ────────────────────────────────────────────────────────────
    if not args.skip_overrides:
        logger.info("=== Phase 3: OVERRIDES (type hierarchy method matching) ===")
        with driver.session() as s:
            n = load_overrides(s, args.dry_run)
        logger.info("OVERRIDES: %d edges written", n)

    # ── Final summary ─────────────────────────────────────────────────────────
    logger.info("=== Final counts ===")
    with driver.session() as s:
        for rel in ("INJECTS", "DEPENDS_ON", "OVERRIDES", "RESOLVES_TO"):
            r = s.run(f"MATCH ()-[r:{rel}]->() RETURN count(r) AS c").single()
            logger.info("  %-15s %d", rel, r["c"] if r else 0)

    driver.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill INJECTS, DEPENDS_ON, OVERRIDES edges")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-injects", action="store_true")
    parser.add_argument("--skip-depends", action="store_true")
    parser.add_argument("--skip-overrides", action="store_true")
    args = parser.parse_args()

    sys.path.insert(0, os.path.dirname(__file__))
    run(args)
