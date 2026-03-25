"""
tools/export_graph_html.py
Exports the CodeNexus graph as a self-contained interactive HTML file.
No graphviz, no extra software — opens directly in any browser.

Modes:
  community (default) — one node per Leiden community, colour by repo
  repo                — one node per repository
  full                — all nodes up to --limit (default 3000)

Usage (from nexus/):
  py tools/export_graph_html.py
  py tools/export_graph_html.py --mode repo
  py tools/export_graph_html.py --mode full --limit 5000
  py tools/export_graph_html.py --out my_map.html

Or with curl (just data export, no render):
  curl -u neo4j:nexuspassword -H "Content-Type: application/json" ^
       -X POST http://localhost:7474/db/neo4j/tx/commit ^
       -d "{\"statements\":[{\"statement\":\"MATCH (n) RETURN n.community_id, n.repo_name, count(n) LIMIT 100\"}]}"
"""
from __future__ import annotations
import argparse, json, logging, math, os, sys
from collections import defaultdict

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
logger = logging.getLogger(__name__)

_REPO_COLOURS = [
    "#4E79A7","#F28E2B","#E15759","#76B7B2","#59A14F",
    "#EDC948","#B07AA1","#FF9DA7","#9C755F","#BAB0AC",
    "#86BCB6","#D4A6C8","#FABFD2","#A0CBE8","#FFBE7D",
]

def _colour_map(keys):
    return {k: _REPO_COLOURS[i % len(_REPO_COLOURS)] for i, k in enumerate(sorted(keys))}


# ── Data fetchers ──────────────────────────────────────────────────────────────

def fetch_community(driver):
    logger.info("Fetching community nodes…")
    with driver.session() as s:
        rows = list(s.run("""
            MATCH (n) WHERE n.community_id IS NOT NULL
            RETURN n.community_id AS cid, count(n) AS size, n.repo_name AS repo
        """))
        edge_rows = list(s.run("""
            MATCH (a)-[r:CALLS|DEPENDS_ON|EXTENDS|IMPLEMENTS|INJECTS]->(b)
            WHERE a.community_id IS NOT NULL AND b.community_id IS NOT NULL
              AND a.community_id <> b.community_id
            RETURN a.community_id AS src, b.community_id AS dst, count(*) AS cnt
            ORDER BY cnt DESC LIMIT 3000
        """))

    community_size  = defaultdict(int)
    community_repos = defaultdict(lambda: defaultdict(int))
    for r in rows:
        cid = str(r["cid"])
        community_size[cid] += 1
        community_repos[cid][r["repo"] or "unknown"] += 1

    nodes, edges = [], []
    all_repos = set()
    for cid, size in community_size.items():
        dominant = max(community_repos[cid], key=community_repos[cid].get)
        all_repos.add(dominant)
        nodes.append({"id": cid, "label": f"C{cid}", "size": size, "group": dominant})

    colours = _colour_map(list(all_repos))
    for n in nodes:
        n["colour"] = colours.get(n["group"], "#CCCCCC")

    edge_weight = defaultdict(int)
    for e in edge_rows:
        edge_weight[(str(e["src"]), str(e["dst"]))] += int(e["cnt"])
    max_w = max(edge_weight.values()) if edge_weight else 1
    for (src, dst), w in edge_weight.items():
        edges.append({"source": src, "target": dst, "weight": w,
                      "width": round(0.5 + 3 * w / max_w, 2)})

    logger.info("%d community nodes, %d edges", len(nodes), len(edges))
    return nodes, edges, colours, "Community Architecture Map"


def fetch_repo(driver):
    logger.info("Fetching repo nodes…")
    with driver.session() as s:
        node_rows = list(s.run("""
            MATCH (n) WHERE n.repo_name IS NOT NULL
            RETURN n.repo_name AS repo, count(n) AS size
        """))
        edge_rows = list(s.run("""
            MATCH (a)-[r:CALLS|DEPENDS_ON|EXTENDS|IMPLEMENTS|INJECTS|REMOTE_CALLS]->(b)
            WHERE a.repo_name IS NOT NULL AND b.repo_name IS NOT NULL
              AND a.repo_name <> b.repo_name
            RETURN a.repo_name AS src, b.repo_name AS dst, count(*) AS cnt
        """))

    repos = {r["repo"]: int(r["size"]) for r in node_rows if r["repo"]}
    colours = _colour_map(list(repos.keys()))
    max_s = max(repos.values()) if repos else 1
    nodes = [{"id": r, "label": r, "size": s, "group": r,
               "colour": colours.get(r, "#CCCCCC"),
               "display_size": round(10 + 30 * s / max_s, 1)}
             for r, s in repos.items()]

    edge_weight = defaultdict(int)
    for e in edge_rows:
        edge_weight[(e["src"], e["dst"])] += int(e["cnt"])
    max_w = max(edge_weight.values()) if edge_weight else 1
    edges = [{"source": src, "target": dst, "weight": w,
               "width": round(0.5 + 4 * w / max_w, 2)}
             for (src, dst), w in edge_weight.items()]

    logger.info("%d repo nodes, %d edges", len(nodes), len(edges))
    return nodes, edges, colours, "Repository Dependency Map"


def fetch_full(driver, limit):
    """
    Builds a CONNECTED subgraph:
      1. Pick top-K seed nodes (LogicUnit/Component only, with repo_name) by degree.
      2. Fetch all edges between seeds — guaranteed connections exist.
      3. Pull in neighbour nodes that appeared in those edges to fill the set.
    This avoids the "scattered isolated nodes" problem of top-N-by-degree globally.
    """
    seed_count = min(limit, 1500)
    logger.info("Fetching %d seed nodes (LogicUnit + Component, known repo)…", seed_count)
    with driver.session() as s:
        # Seeds: only real code nodes (not AnnotationType / ExceptionType / Field etc.)
        # that have a known repo_name — these are the ones with edges.
        seed_rows = list(s.run(f"""
            MATCH (n)
            WHERE n.geid IS NOT NULL
              AND n.repo_name IS NOT NULL
              AND (n:LogicUnit OR n:Component OR n:EntryPoint OR n:DataSink)
            OPTIONAL MATCH (n)-[r]-()
            WITH n, count(r) AS degree
            ORDER BY degree DESC
            LIMIT {seed_count}
            RETURN n.geid       AS id,
                   n.fqn        AS fqn,
                   n.repo_name  AS repo,
                   n.community_id AS cid,
                   labels(n)[0] AS lbl,
                   degree
        """))

        seed_geids = [r["id"] for r in seed_rows]
        logger.info("Fetching edges between seed nodes…")

        # Edges where BOTH endpoints are seeds (guaranteed to render)
        edge_rows = list(s.run("""
            MATCH (a)-[r:CALLS|DEPENDS_ON|EXTENDS|IMPLEMENTS|INJECTS|OVERRIDES|HAS_METHOD]->(b)
            WHERE a.geid IN $geids AND b.geid IN $geids
            RETURN a.geid AS src, b.geid AS dst, type(r) AS rel
            LIMIT 20000
        """, geids=seed_geids))

    # Keep only nodes that actually appear in at least one edge
    connected_ids = set()
    for e in edge_rows:
        connected_ids.add(str(e["src"]))
        connected_ids.add(str(e["dst"]))

    # If very few connected, fall back to all seeds (community mode is better anyway)
    if len(connected_ids) < 50:
        logger.warning("Few inter-seed edges found (%d). Consider using --mode community instead.", len(connected_ids))
        connected_ids = {str(r["id"]) for r in seed_rows}

    logger.info("Connected subgraph: %d nodes, %d edges", len(connected_ids), len(edge_rows))

    seed_map = {str(r["id"]): r for r in seed_rows}
    repos    = list({r["repo"] for r in seed_rows if r["repo"]})
    colours  = _colour_map(repos)

    _LABEL_COLOURS = {
        "EntryPoint": "#A6E3A1",   # green
        "DataSink":   "#F38BA8",   # red
        "Component":  "#89B4FA",   # blue
        "LogicUnit":  None,        # use repo colour
    }

    nodes = []
    for geid in connected_ids:
        r     = seed_map.get(geid)
        if not r:
            continue
        short = ((r["fqn"] or geid or "").split(".")[-1].split("(")[0])[:28]
        repo  = r["repo"] or "unknown"
        lbl   = r["lbl"] or "LogicUnit"
        # EntryPoints/DataSinks get a fixed highlight colour; others get repo colour
        colour = _LABEL_COLOURS.get(lbl) or colours.get(repo, "#CCCCCC")
        nodes.append({
            "id":     geid,
            "label":  short,
            "size":   max(1, int(r["degree"])),
            "group":  repo,
            "colour": colour,
            "lbl":    lbl,
        })

    edges = [{"source": str(e["src"]), "target": str(e["dst"]), "width": 0.6, "weight": 1}
             for e in edge_rows
             if str(e["src"]) in connected_ids and str(e["dst"]) in connected_ids]

    logger.info("Final: %d nodes, %d edges", len(nodes), len(edges))
    return nodes, edges, colours, f"Connected Subgraph (top {seed_count} seeds)"


# ── HTML template ──────────────────────────────────────────────────────────────

_HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>CodeNexus — {title}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: #1E1E2E; color: #CDD6F4; font-family: 'Segoe UI', sans-serif; overflow: hidden; }}
  #header {{ padding: 10px 16px; background: #181825; border-bottom: 1px solid #313244;
             display: flex; align-items: center; gap: 16px; }}
  #header h1 {{ font-size: 14px; font-weight: 600; color: #CBA6F7; }}
  #stats  {{ font-size: 12px; color: #6C7086; }}
  #controls {{ margin-left: auto; display: flex; gap: 8px; }}
  button {{ background: #313244; border: none; color: #CDD6F4; padding: 4px 10px;
            border-radius: 4px; cursor: pointer; font-size: 12px; }}
  button:hover {{ background: #45475A; }}
  #legend {{ position: absolute; bottom: 16px; left: 16px; background: #181825CC;
             border: 1px solid #313244; border-radius: 6px; padding: 10px 14px;
             font-size: 11px; max-height: 40vh; overflow-y: auto; }}
  .legend-item {{ display: flex; align-items: center; gap: 6px; padding: 2px 0; }}
  .legend-dot  {{ width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }}
  #tooltip {{ position: absolute; background: #181825EE; border: 1px solid #313244;
              border-radius: 6px; padding: 8px 12px; font-size: 12px; pointer-events: none;
              display: none; max-width: 280px; line-height: 1.6; }}
  svg {{ display: block; }}
  .node circle, .node rect, .node polygon {{ cursor: pointer; stroke: #1E1E2E; stroke-width: 1.5px; }}
  .node text {{ fill: #CDD6F4; font-size: 9px; pointer-events: none; }}
  .link {{ stroke: #444466; stroke-opacity: 0.6; fill: none; }}
  #search {{ background: #313244; border: 1px solid #45475A; color: #CDD6F4;
             padding: 4px 8px; border-radius: 4px; font-size: 12px; width: 160px; }}
</style>
</head>
<body>
<div id="header">
  <h1>CodeNexus — {title}</h1>
  <span id="stats"></span>
  <div id="controls">
    <input id="search" type="text" placeholder="Search node…" oninput="filterNodes(this.value)">
    <button onclick="resetZoom()">Reset zoom</button>
    <button onclick="toggleLabels()">Toggle labels</button>
    <button onclick="document.getElementById('legend').style.display = document.getElementById('legend').style.display==='none'?'block':'none'">Legend</button>
  </div>
</div>
<svg id="svg"></svg>
<div id="legend"></div>
<div id="tooltip"></div>

<script src="https://d3js.org/d3.v7.min.js"></script>
<script>
const GRAPH = {graph_json};
const COLOURS = {colours_json};

const w = window.innerWidth, h = window.innerHeight - 42;
const svg = d3.select("#svg").attr("width", w).attr("height", h);
const g   = svg.append("g");
let showLabels = true;

// zoom
const zoom = d3.zoom().scaleExtent([0.05, 8]).on("zoom", e => g.attr("transform", e.transform));
svg.call(zoom);

// stats
document.getElementById("stats").textContent =
  `${{GRAPH.nodes.length}} nodes · ${{GRAPH.links.length}} edges`;

// legend
const leg = document.getElementById("legend");
Object.entries(COLOURS).forEach(([repo, col]) => {{
  leg.innerHTML += `<div class="legend-item"><div class="legend-dot" style="background:${{col}}"></div>${{repo}}</div>`;
}});

// size scale
const sizeExtent = d3.extent(GRAPH.nodes, d => d.size || 1);
const rScale = d3.scaleSqrt().domain(sizeExtent).range([4, 28]);

// simulation
const sim = d3.forceSimulation(GRAPH.nodes)
  .force("link",   d3.forceLink(GRAPH.links).id(d => d.id).distance(60).strength(0.3))
  .force("charge", d3.forceManyBody().strength(-120))
  .force("center", d3.forceCenter(w / 2, h / 2))
  .force("collide", d3.forceCollide(d => rScale(d.size || 1) + 4));

// edges
const link = g.append("g").selectAll("line")
  .data(GRAPH.links).join("line")
  .attr("class", "link")
  .attr("stroke-width", d => d.width || 0.8);

// nodes
const node = g.append("g").selectAll(".node")
  .data(GRAPH.nodes).join("g").attr("class", "node")
  .call(d3.drag()
    .on("start", (e, d) => {{ if (!e.active) sim.alphaTarget(0.3).restart(); d.fx=d.x; d.fy=d.y; }})
    .on("drag",  (e, d) => {{ d.fx=e.x; d.fy=e.y; }})
    .on("end",   (e, d) => {{ if (!e.active) sim.alphaTarget(0); d.fx=null; d.fy=null; }}));

node.append("circle")
  .attr("r", d => rScale(d.size || 1))
  .attr("fill", d => d.colour || "#888");

const label = node.append("text")
  .attr("dy", "0.35em")
  .attr("text-anchor", "middle")
  .text(d => d.label || d.id);

// tooltip
const tip = document.getElementById("tooltip");
node.on("mouseover", (e, d) => {{
  tip.style.display = "block";
  tip.innerHTML = `<strong>${{d.label || d.id}}</strong><br>
    Group: ${{d.group || "—"}}<br>
    Size: ${{d.size || 1}} nodes<br>
    ID: ${{d.id}}`;
}}).on("mousemove", e => {{
  tip.style.left = (e.pageX + 12) + "px";
  tip.style.top  = (e.pageY - 28) + "px";
}}).on("mouseout", () => {{ tip.style.display = "none"; }});

sim.on("tick", () => {{
  link.attr("x1", d => d.source.x).attr("y1", d => d.source.y)
      .attr("x2", d => d.target.x).attr("y2", d => d.target.y);
  node.attr("transform", d => `translate(${{d.x}},${{d.y}})`);
}});

function resetZoom() {{ svg.transition().duration(500).call(zoom.transform, d3.zoomIdentity); }}
function toggleLabels() {{
  showLabels = !showLabels;
  label.style("display", showLabels ? null : "none");
}}
function filterNodes(q) {{
  q = q.toLowerCase();
  node.style("opacity", d => !q || (d.label||"").toLowerCase().includes(q) || (d.id||"").toLowerCase().includes(q) ? 1 : 0.08);
}}
window.addEventListener("resize", () => {{
  const nw = window.innerWidth, nh = window.innerHeight - 42;
  svg.attr("width", nw).attr("height", nh);
  sim.force("center", d3.forceCenter(nw/2, nh/2)).alpha(0.1).restart();
}});
</script>
</body>
</html>"""


def write_html(nodes, edges, colours, title, out_path):
    graph = {"nodes": nodes, "links": edges}
    html  = _HTML.format(
        title=title,
        graph_json=json.dumps(graph),
        colours_json=json.dumps(colours),
    )
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    size_kb = os.path.getsize(out_path) // 1024
    logger.info("Saved → %s  (%d KB) — open in any browser", out_path, size_kb)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Export CodeNexus graph to interactive HTML")
    parser.add_argument("--mode", choices=["community", "repo", "full"], default="community")
    parser.add_argument("--out",   default="")
    parser.add_argument("--limit", type=int, default=3000)
    args = parser.parse_args()

    out = args.out or f"graph_{args.mode}.html"
    if not out.endswith(".html"):
        out += ".html"

    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    from neo4j import GraphDatabase
    from config.settings import settings

    driver = GraphDatabase.driver(settings.neo4j_uri, auth=settings.neo4j_auth)

    if args.mode == "community":
        nodes, edges, colours, title = fetch_community(driver)
    elif args.mode == "repo":
        nodes, edges, colours, title = fetch_repo(driver)
    else:
        nodes, edges, colours, title = fetch_full(driver, args.limit)

    driver.close()
    write_html(nodes, edges, colours, title, out)


if __name__ == "__main__":
    main()
