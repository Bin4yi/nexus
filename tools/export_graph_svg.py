"""
tools/export_graph_svg.py
Exports the CodeNexus Neo4j graph as SVG.

Three modes:
  community (default) — one node per Leiden community, edges = cross-community
                        calls. ~200 nodes, colour-coded by dominant repo.
                        Best for understanding overall architecture.

  repo                — one node per repository, edges = cross-repo dependency
                        counts. 5-15 nodes. Quick ecosystem overview.

  full                — all nodes (WARNING: 35k+ nodes is slow).
                        Use --limit to cap at a manageable number.

Requirements:
  pip install graphviz          (Python binding)
  winget install graphviz       (or https://graphviz.org/download/)
  -- Graphviz executables must be on PATH --

Usage (run from nexus/):
  py tools/export_graph_svg.py                              # community map
  py tools/export_graph_svg.py --mode repo                  # repo map
  py tools/export_graph_svg.py --mode full --limit 3000     # top 3k nodes
  py tools/export_graph_svg.py --mode community --out arch  # custom filename
"""
from __future__ import annotations
import argparse
import logging
import math
import os
import sys
from collections import defaultdict

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
logger = logging.getLogger(__name__)

# ── Colour palette — one colour per repo ──────────────────────────────────────
_REPO_COLOURS = [
    "#4E79A7", "#F28E2B", "#E15759", "#76B7B2", "#59A14F",
    "#EDC948", "#B07AA1", "#FF9DA7", "#9C755F", "#BAB0AC",
    "#86BCB6", "#D4A6C8", "#FABFD2", "#A0CBE8", "#FFBE7D",
]
_UNKNOWN_COLOUR = "#CCCCCC"


def _repo_colour_map(repos: list[str]) -> dict[str, str]:
    return {r: _REPO_COLOURS[i % len(_REPO_COLOURS)] for i, r in enumerate(sorted(repos))}


# ══════════════════════════════════════════════════════════════════════════════
# MODE 1 — COMMUNITY MAP
# ══════════════════════════════════════════════════════════════════════════════

def export_community(driver, out_path: str) -> None:
    """
    One node per Leiden community.
    Node size  ∝ sqrt(member count).
    Node colour = dominant repo in that community.
    Edges = cross-community CALLS (thickness ∝ call count, capped).
    """
    import graphviz

    logger.info("Querying community membership…")
    with driver.session() as s:
        # Community nodes: id, size, dominant repo
        rows = list(s.run("""
            MATCH (n)
            WHERE n.community_id IS NOT NULL
            RETURN n.community_id        AS cid,
                   count(n)              AS size,
                   n.repo_name           AS repo
            ORDER BY cid
        """))

    # Aggregate per community: total size + dominant repo
    community_size: dict[int, int] = defaultdict(int)
    community_repo_votes: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in rows:
        cid = int(r["cid"])
        community_size[cid] += 1
        repo = r["repo"] or "unknown"
        community_repo_votes[cid][repo] += 1

    community_dominant_repo: dict[int, str] = {
        cid: max(votes, key=votes.get)
        for cid, votes in community_repo_votes.items()
    }

    all_repos = set(community_dominant_repo.values())
    colour_map = _repo_colour_map(list(all_repos))

    logger.info("Found %d communities across %d repos", len(community_size), len(all_repos))

    # Cross-community edges
    logger.info("Querying cross-community relationships…")
    with driver.session() as s:
        edge_rows = list(s.run("""
            MATCH (a)-[r:CALLS|DEPENDS_ON|EXTENDS|IMPLEMENTS|INJECTS]->(b)
            WHERE a.community_id IS NOT NULL
              AND b.community_id IS NOT NULL
              AND a.community_id <> b.community_id
            RETURN a.community_id AS src,
                   b.community_id AS dst,
                   type(r)        AS rel,
                   count(*)       AS cnt
            ORDER BY cnt DESC
            LIMIT 2000
        """))

    # Aggregate edge weights between community pairs
    edge_weight: dict[tuple[int, int], int] = defaultdict(int)
    for e in edge_rows:
        src, dst = int(e["src"]), int(e["dst"])
        edge_weight[(src, dst)] += int(e["cnt"])

    logger.info("Building SVG (community mode)…")
    dot = graphviz.Digraph(
        name="CodeNexus Communities",
        engine="sfdp",
        graph_attr={
            "overlap":     "prism",
            "splines":     "curved",
            "bgcolor":     "#1E1E2E",
            "fontcolor":   "#CDD6F4",
            "fontname":    "Helvetica",
            "label":       "CodeNexus — Community Architecture Map",
            "labelloc":    "t",
            "fontsize":    "24",
            "pad":         "0.5",
        },
        node_attr={
            "style":     "filled",
            "fontname":  "Helvetica",
            "fontcolor": "#1E1E2E",
            "shape":     "circle",
        },
        edge_attr={
            "color":    "#555577",
            "arrowsize":"0.5",
        },
    )

    # Add nodes
    for cid, size in community_size.items():
        repo   = community_dominant_repo.get(cid, "unknown")
        colour = colour_map.get(repo, _UNKNOWN_COLOUR)
        # Scale node: min 0.3", max 2.0"
        width  = str(round(min(2.0, max(0.3, 0.3 * math.sqrt(size))), 2))
        label  = f"C{cid}\n({size})"
        dot.node(
            str(cid),
            label=label,
            fillcolor=colour,
            width=width,
            height=width,
            fontsize=str(max(6, min(14, size // 5))),
        )

    # Add edges (cap thickness)
    max_w = max(edge_weight.values()) if edge_weight else 1
    for (src, dst), w in edge_weight.items():
        penwidth = str(round(1 + 4 * w / max_w, 2))
        dot.edge(str(src), str(dst), penwidth=penwidth)

    # Legend
    with dot.subgraph(name="cluster_legend") as leg:
        leg.attr(label="Repositories", fontcolor="#CDD6F4", bgcolor="#2A2A3E",
                 style="filled", fontname="Helvetica")
        for repo, colour in sorted(colour_map.items()):
            leg.node(
                f"legend_{repo}", label=repo,
                shape="box", style="filled", fillcolor=colour,
                fontcolor="#1E1E2E", fontname="Helvetica", fontsize="10",
            )

    _render(dot, out_path)


# ══════════════════════════════════════════════════════════════════════════════
# MODE 2 — REPO MAP
# ══════════════════════════════════════════════════════════════════════════════

def export_repo(driver, out_path: str) -> None:
    """One node per repository. Edges = cross-repo call/dependency counts."""
    import graphviz

    logger.info("Querying repo-level cross-dependencies…")
    with driver.session() as s:
        node_rows = list(s.run("""
            MATCH (n) WHERE n.repo_name IS NOT NULL
            RETURN n.repo_name AS repo, count(n) AS size
        """))
        edge_rows = list(s.run("""
            MATCH (a)-[r:CALLS|DEPENDS_ON|EXTENDS|IMPLEMENTS|INJECTS|REMOTE_CALLS]->(b)
            WHERE a.repo_name IS NOT NULL AND b.repo_name IS NOT NULL
              AND a.repo_name <> b.repo_name
            RETURN a.repo_name AS src, b.repo_name AS dst,
                   type(r) AS rel, count(*) AS cnt
            ORDER BY cnt DESC
        """))

    repos = {r["repo"]: int(r["size"]) for r in node_rows if r["repo"]}
    colour_map = _repo_colour_map(list(repos.keys()))

    edge_weight: dict[tuple[str, str], int] = defaultdict(int)
    for e in edge_rows:
        edge_weight[(e["src"], e["dst"])] += int(e["cnt"])

    logger.info("Building SVG (%d repos, %d cross-repo edges)…",
                len(repos), len(edge_weight))

    dot = graphviz.Digraph(
        name="CodeNexus Repos",
        engine="neato",
        graph_attr={
            "overlap":   "false",
            "splines":   "curved",
            "bgcolor":   "#1E1E2E",
            "fontcolor": "#CDD6F4",
            "fontname":  "Helvetica",
            "label":     "CodeNexus — Repository Dependency Map",
            "labelloc":  "t",
            "fontsize":  "20",
            "pad":       "1.0",
        },
        node_attr={
            "style":    "filled",
            "fontname": "Helvetica",
            "fontcolor":"#1E1E2E",
            "shape":    "box",
            "style":    "filled,rounded",
        },
        edge_attr={"fontname": "Helvetica", "fontsize": "9"},
    )

    max_size = max(repos.values()) if repos else 1
    for repo, size in repos.items():
        width = str(round(1.5 + 2.5 * size / max_size, 2))
        dot.node(
            repo, label=f"{repo}\n({size:,} nodes)",
            fillcolor=colour_map.get(repo, _UNKNOWN_COLOUR),
            width=width, height="0.8",
        )

    max_w = max(edge_weight.values()) if edge_weight else 1
    for (src, dst), w in edge_weight.items():
        penwidth = str(round(0.5 + 4 * w / max_w, 2))
        dot.edge(src, dst, label=str(w), penwidth=penwidth,
                 color="#AAAACC", fontcolor="#CDD6F4")

    _render(dot, out_path)


# ══════════════════════════════════════════════════════════════════════════════
# MODE 3 — FULL NODE GRAPH (with limit)
# ══════════════════════════════════════════════════════════════════════════════

def export_full(driver, out_path: str, limit: int = 2000) -> None:
    """
    All nodes up to `limit`, ordered by relationship degree (most connected first).
    Uses sfdp layout which handles large graphs better than fdp.
    """
    import graphviz

    logger.info("Fetching top %d nodes by degree…", limit)
    with driver.session() as s:
        node_rows = list(s.run(f"""
            MATCH (n)
            WHERE n.geid IS NOT NULL
            OPTIONAL MATCH (n)-[r]-()
            WITH n, count(r) AS degree
            ORDER BY degree DESC
            LIMIT {limit}
            RETURN n.geid       AS geid,
                   n.fqn        AS fqn,
                   n.repo_name  AS repo,
                   n.community_id AS cid,
                   labels(n)[0] AS label,
                   degree
        """))

    geids = {r["geid"] for r in node_rows}
    logger.info("Fetching edges between selected nodes…")
    with driver.session() as s:
        edge_rows = list(s.run("""
            MATCH (a)-[r]->(b)
            WHERE a.geid IN $geids AND b.geid IN $geids
            RETURN a.geid AS src, b.geid AS dst, type(r) AS rel
            LIMIT 10000
        """, geids=list(geids)))

    repos = list({r["repo"] or "unknown" for r in node_rows})
    colour_map = _repo_colour_map(repos)

    _LABEL_SHAPES = {
        "LogicUnit":   "ellipse",
        "Component":   "box",
        "EntryPoint":  "diamond",
        "DataSink":    "hexagon",
        "Module":      "parallelogram",
        "Project":     "rectangle",
    }

    logger.info("Building SVG (%d nodes, %d edges)…", len(node_rows), len(edge_rows))
    dot = graphviz.Digraph(
        name="CodeNexus Full",
        engine="sfdp",
        graph_attr={
            "overlap":   "prism",
            "splines":   "line",
            "bgcolor":   "#1E1E2E",
            "fontcolor": "#CDD6F4",
            "label":     f"CodeNexus — Full Graph (top {limit} nodes by degree)",
            "labelloc":  "t",
            "fontsize":  "18",
        },
        node_attr={
            "style":     "filled",
            "fontname":  "Helvetica",
            "fontcolor": "#1E1E2E",
            "fontsize":  "7",
            "width":     "0.2",
            "height":    "0.2",
            "fixedsize": "true",
        },
        edge_attr={"color": "#44445566", "arrowsize": "0.3"},
    )

    for r in node_rows:
        repo   = r["repo"] or "unknown"
        label  = r["label"] or "Node"
        short  = (r["fqn"] or r["geid"] or "").split(".")[-1].split("(")[0][:20]
        shape  = _LABEL_SHAPES.get(label, "ellipse")
        colour = colour_map.get(repo, _UNKNOWN_COLOUR)
        dot.node(str(r["geid"]), label=short, fillcolor=colour, shape=shape)

    for e in edge_rows:
        dot.edge(str(e["src"]), str(e["dst"]))

    _render(dot, out_path)


# ── Shared render helper ───────────────────────────────────────────────────────

def _render(dot, out_path: str) -> None:
    svg_path = out_path if out_path.endswith(".svg") else out_path + ".svg"
    logger.info("Rendering SVG → %s  (this may take 30–120s for large graphs)…", svg_path)
    try:
        rendered = dot.render(
            filename=os.path.splitext(svg_path)[0],
            format="svg",
            cleanup=True,
        )
        logger.info("Done. SVG saved: %s", rendered)
    except Exception as e:
        # Save the dot source for manual rendering
        dot_path = out_path.replace(".svg", "") + ".dot"
        dot.save(dot_path)
        logger.error("Render failed: %s", e)
        logger.error("Graphviz executables not found on PATH.")
        logger.error("Install: winget install graphviz  OR  https://graphviz.org/download/")
        logger.error("Then run manually:  dot -Tsvg %s -o %s", dot_path, svg_path)
        sys.exit(1)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Export CodeNexus graph to SVG")
    parser.add_argument("--mode", choices=["community", "repo", "full"],
                        default="community",
                        help="community = Leiden community map (default); "
                             "repo = repository dependency map; "
                             "full = all nodes (use --limit)")
    parser.add_argument("--out", default="",
                        help="Output filename without extension (default: graph_<mode>)")
    parser.add_argument("--limit", type=int, default=2000,
                        help="Max nodes for --mode full (default 2000)")
    args = parser.parse_args()

    out = args.out or f"graph_{args.mode}"

    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    from neo4j import GraphDatabase
    from config.settings import settings

    logger.info("Connecting to Neo4j at %s…", settings.neo4j_uri)
    driver = GraphDatabase.driver(settings.neo4j_uri, auth=settings.neo4j_auth)

    if args.mode == "community":
        export_community(driver, out)
    elif args.mode == "repo":
        export_repo(driver, out)
    else:
        export_full(driver, out, limit=args.limit)

    driver.close()


if __name__ == "__main__":
    main()
