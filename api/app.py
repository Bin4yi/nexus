"""
api/app.py
CodeNexus REST API — FastAPI application.

Exposes the full reasoning pipeline over HTTP so IDE plugins, CI bots,
and developer CLIs can query the knowledge graph without writing Python.

Endpoints:
  POST /api/v1/query          — Natural-language question → rich answer
  GET  /api/v1/explain/{fqn}  — Structured class/method onboarding doc (P1.5)
  GET  /api/v1/stats          — Live graph statistics
  GET  /api/v1/health         — Liveness + readiness check

Auth:
  If API_KEYS is set in .env, every request must include
  Authorization: Bearer <key>  or  X-API-Key: <key>.
  Empty API_KEYS = dev mode (no auth).
"""
from __future__ import annotations
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Header, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from config.settings import settings

logger = logging.getLogger(__name__)

# ── Lifespan: initialise heavy objects once at startup ───────────────────────

_state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialise Neo4j, ChromaDB, and the reasoning pipeline at startup."""
    import chromadb
    from neo4j import GraphDatabase
    from reasoning.router import QueryRouter
    from reasoning.reduce_step import ReduceStep
    from reasoning.map_step import MapStep
    from reasoning.graph_retriever import GraphRetriever

    logger.info("CodeNexus API starting up…")

    chroma = chromadb.HttpClient(host=settings.chroma_host, port=settings.chroma_port)
    neo4j  = GraphDatabase.driver(settings.neo4j_uri, auth=settings.neo4j_auth)

    _state["chroma"]    = chroma
    _state["neo4j"]     = neo4j
    _state["router"]    = QueryRouter(chroma_client=chroma)
    _state["reduce"]    = ReduceStep()
    _state["retriever"] = GraphRetriever(chroma_client=chroma)

    try:
        from reasoning.map_step import MapStep
        _state["map"] = MapStep(chroma_client=chroma)
    except Exception as e:
        logger.warning("MapStep not available: %s", e)
        _state["map"] = None

    logger.info("CodeNexus API ready.")
    yield

    # Shutdown
    _state["router"].close()
    neo4j.close()
    logger.info("CodeNexus API shut down.")


# ── App factory ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="CodeNexus",
    description="GraphRAG knowledge graph API for Java monolith analysis.",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Auth dependency ────────────────────────────────────────────────────────────

async def verify_api_key(
    authorization: Optional[str] = Header(None),
    x_api_key:     Optional[str] = Header(None),
):
    """
    Bearer or X-API-Key authentication.
    Skipped entirely when API_KEYS is empty (dev mode).
    """
    key_set = settings.api_key_set
    if not key_set:
        return   # dev mode — no auth

    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    elif x_api_key:
        token = x_api_key.strip()

    if not token or token not in key_set:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key.",
        )


# ── Request timing middleware ─────────────────────────────────────────────────

@app.middleware("http")
async def add_timing_header(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    ms = (time.time() - start) * 1000
    response.headers["X-Nexus-Latency-Ms"] = str(round(ms, 1))
    return response


# ── POST /api/v1/query ────────────────────────────────────────────────────────

from api.models import QueryRequest, QueryResponse, ExplainResponse, GraphStats, HealthResponse, SourceRef, SpecCitation
from api.response_builder import build_query_response


@app.post("/api/v1/query", response_model=QueryResponse)
async def query(req: QueryRequest, _=Depends(verify_api_key)):
    """
    Answer a natural-language question about the codebase.

    Returns a rich response with:
    - The LLM-synthesised answer
    - Clickable file:line source citations
    - Blast-radius affected nodes
    - Community IDs used for context
    - Route taken (symbolic/exact/semantic/global)
    """
    start = time.time()

    router   = _state["router"]
    reduce   = _state["reduce"]
    map_step = _state.get("map")

    try:
        result = router.route(req.question)
    except Exception as e:
        logger.error("Router failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Router error: {e}")

    # Build Map results for community context
    map_results = []
    if map_step and result.community_summaries:
        try:
            from reasoning.map_step import MapResult
            map_results = [
                MapResult(
                    community_id=s.get("community_id", 0),
                    summary_text=s.get("summary_text", ""),
                    score=100,
                    reason="pre-retrieved",
                )
                for s in result.community_summaries
            ]
        except Exception as e:
            logger.warning("Map step failed: %s", e)

    # Primary targets from grep + seed nodes
    primary_targets = []
    for hit in result.grep_hits:
        if hasattr(hit, "rel_path"):
            class_name = hit.rel_path.replace("\\", "/").split("/")[-1].replace(".java", "")
            primary_targets.append({
                "source": "grep",
                "fqn": class_name,
                "file_path": hit.rel_path,
                "line_number": hit.line_number,
                "text": hit.line_text,
                "edge_type": "",
            })
    for node in result.seed_nodes:
        primary_targets.append({**node, "source": "graph"})
    for node in result.affected_nodes[:10]:
        primary_targets.append({**node, "source": "graph"})

    # Fetch actual source code.
    # ORDERING: graph entity method bodies FIRST, then grep contexts from implementation files only.
    # Constant-definition files (OAuthConstants etc.) are skipped from code snippets —
    # they're already in grep_evidence. This ensures TokenExchangeGrantHandler-type bodies
    # appear before broad grep hits from unrelated flows (e.g. CIBA when asking about token exchange).
    code_snippets = []
    try:
        from reasoning.code_fetcher import CodeFetcher
        fetcher = CodeFetcher()
        # Pass 1: graph/semantic method bodies — graph nodes first (entity lookup), then semantic
        fetchable = [t for t in primary_targets if t.get("file_path") and t.get("start_line")]
        fetchable.sort(key=lambda t: 0 if t.get("source") == "graph" else 1)
        node_snippets = fetcher.fetch_for_nodes(fetchable[:12])
        code_snippets.extend(node_snippets)
        # Pass 2: grep contexts — implementation files only (score >= 2), skip constants + tests
        file_best: dict[str, dict] = {}
        for t in primary_targets:
            if t.get("source") != "grep":
                continue
            fp = t.get("file_path", "")
            if not fp or not t.get("line_number"):
                continue
            txt = t.get("text", "")
            is_test   = "src/test" in fp or "Test.java" in fp
            is_import = txt.strip().startswith("import ")
            is_defn   = "static final" in txt and "=" in txt
            score = 0 if (is_import or is_defn) else (1 if is_test else 2)
            if fp not in file_best or score > file_best[fp]["score"]:
                file_best[fp] = {**t, "score": score}
        for fp, t in list(file_best.items())[:15]:
            if t["score"] < 2:
                continue  # Skip constant definitions and tests — shown in grep_evidence
            snippet = fetcher.fetch_grep_context(fp, t["line_number"], context_lines=20)
            if snippet:
                code_snippets.append(snippet)
    except Exception as e:
        logger.debug("Code fetch failed for query: %s", e)

    try:
        answer = reduce.run(
            map_results=map_results,
            query=req.question,
            primary_targets=primary_targets,
            code_snippets=code_snippets,
            route=result.route,
        )
    except Exception as e:
        logger.error("ReduceStep failed: %s", e)
        raise HTTPException(status_code=500, detail=f"LLM reduce failed: {e}")

    latency_ms = (time.time() - start) * 1000
    return build_query_response(result, answer, latency_ms)


# ── GET /api/v1/explain/{fqn} (P1.5) ─────────────────────────────────────────

@app.get("/api/v1/explain/{fqn:path}", response_model=ExplainResponse)
async def explain(fqn: str, _=Depends(verify_api_key)):
    """
    Generate a structured onboarding document for any class or method.

    Combines:
    - Community architectural summary
    - Blast radius + risk level
    - RFC spec sections this class implements
    - Callers (who calls this) and callees (what this calls)
    - LLM-synthesised explanation grounded in actual source

    This answers: "Explain what OAuthServerConfiguration does" with full context.
    """
    start     = time.time()
    retriever = _state["retriever"]
    reduce    = _state["reduce"]
    neo4j     = _state["neo4j"]

    # 1. Fetch the node from Neo4j
    with neo4j.session() as session:
        rec = session.run(
            """
            MATCH (n {fqn: $fqn})
            RETURN labels(n)[0]            AS node_type,
                   n.geid                  AS geid,
                   n.docstring             AS docstring,
                   n.file_path             AS file_path,
                   n.start_line            AS start_line,
                   n.end_line              AS end_line,
                   n.blast_radius_risk     AS blast_radius_risk,
                   n.entry_point_score     AS entry_point_score,
                   n.community_id          AS community_id
            LIMIT 1
            """,
            fqn=fqn,
        ).single()

    if not rec:
        raise HTTPException(status_code=404, detail=f"Node not found: {fqn}")

    node_type  = rec["node_type"]
    geid       = rec["geid"]
    community_id = rec["community_id"]

    # 2. Callers and callees (1-hop)
    raw_callers = retriever.get_callers(fqn, depth=1)
    raw_callees = retriever.get_callees(fqn, depth=1)

    callers = [SourceRef(
        fqn=c.get("fqn", ""),
        file_path=c.get("file_path", ""),
        line_number=c.get("start_line"),
        source_type="graph",
        edge_type=c.get("edge_type", "CALLS"),
    ) for c in raw_callers[:15]]

    callees = [SourceRef(
        fqn=c.get("fqn", ""),
        file_path=c.get("file_path", ""),
        line_number=c.get("start_line"),
        source_type="graph",
        edge_type="CALLS",
    ) for c in raw_callees[:15]]

    # 3. RFC spec links (IMPLEMENTS_SPEC edges)
    spec_links: list[SpecCitation] = []
    with neo4j.session() as session:
        rows = list(session.run(
            """
            MATCH (n {geid: $geid})-[r:IMPLEMENTS_SPEC]->(ss:SpecSection)
            RETURN ss.spec_id       AS spec_id,
                   ss.rfc_number    AS rfc_number,
                   ss.section_title AS section_title,
                   r.match_type     AS match_type,
                   r.confidence     AS confidence
            ORDER BY r.confidence DESC
            LIMIT 10
            """,
            geid=geid,
        ))
    for row in rows:
        spec_links.append(SpecCitation(
            spec_id=row["spec_id"] or "",
            rfc_number=row["rfc_number"] or 0,
            section_title=row["section_title"] or "",
            match_type=row["match_type"] or "citation",
            confidence=float(row["confidence"] or 1.0),
        ))

    # 4. Fetch code snippets for context
    code_snippets = []
    try:
        from reasoning.code_fetcher import CodeFetcher
        fetcher = CodeFetcher()
        code_snippets = fetcher.fetch_for_nodes([{
            "fqn": fqn,
            "file_path": rec["file_path"] or "",
            "start_line": rec["start_line"] or 0,
            "end_line":   rec["end_line"] or 0,
        }])
    except Exception as e:
        logger.debug("Code fetch failed for %s: %s", fqn, e)

    # 5. LLM explanation (uses existing explain() method)
    try:
        explanation = reduce.explain(
            fqn=fqn,
            code_snippets=code_snippets,
            callers=raw_callers[:10],
            callees=raw_callees[:10],
        )
    except Exception as e:
        logger.error("Explain LLM call failed for %s: %s", fqn, e)
        explanation = f"(LLM explanation unavailable: {e})"

    latency_ms = (time.time() - start) * 1000
    return ExplainResponse(
        fqn=fqn,
        explanation=explanation,
        node_type=node_type,
        blast_radius_risk=rec["blast_radius_risk"],
        entry_point_score=rec["entry_point_score"],
        community_id=community_id,
        callers=callers,
        callees=callees,
        spec_links=spec_links,
        latency_ms=round(latency_ms, 1),
    )


# ── GET /api/v1/stats ─────────────────────────────────────────────────────────

@app.get("/api/v1/stats", response_model=GraphStats)
async def stats(_=Depends(verify_api_key)):
    """Return live graph statistics from Neo4j and ChromaDB."""
    neo4j  = _state["neo4j"]
    chroma = _state["chroma"]

    def _count(session, label_or_rel: str, is_rel: bool = False) -> int:
        try:
            if is_rel:
                return session.run(f"MATCH ()-[r:{label_or_rel}]->() RETURN count(r) AS c").single()["c"]
            return session.run(f"MATCH (n:{label_or_rel}) RETURN count(n) AS c").single()["c"]
        except Exception:
            return 0

    with neo4j.session() as s:
        out = GraphStats(
            components          = _count(s, "Component"),
            logic_units         = _count(s, "LogicUnit"),
            calls_edges         = _count(s, "CALLS", True),
            annotated_with_edges= _count(s, "ANNOTATED_WITH", True),
            implements_spec_edges=_count(s, "IMPLEMENTS_SPEC", True),
            spec_sections       = _count(s, "SpecSection"),
            entry_points        = _count(s, "EntryPoint"),
            data_sinks          = _count(s, "DataSink"),
            communities         = s.run(
                "MATCH (n) WHERE n.community_id IS NOT NULL "
                "RETURN count(DISTINCT n.community_id) AS c"
            ).single()["c"],
        )

    try:
        col = chroma.get_collection("code_intent")
        out.chroma_code_intent = col.count()
    except Exception:
        pass

    try:
        col = chroma.get_collection("code_logic")
        out.chroma_code_logic = col.count()
    except Exception:
        pass

    out.chroma_total_vectors = out.chroma_code_intent + out.chroma_code_logic

    return out


# ── GET /api/v1/health ────────────────────────────────────────────────────────

@app.get("/api/v1/health", response_model=HealthResponse)
async def health():
    """Liveness + readiness probe. Does NOT require authentication."""
    neo4j_status  = "error"
    chroma_status = "error"
    redis_status  = "error"
    pipeline_stage = None

    # Neo4j
    try:
        neo4j = _state.get("neo4j")
        if neo4j:
            with neo4j.session() as s:
                s.run("RETURN 1").consume()
            neo4j_status = "ok"
    except Exception as e:
        neo4j_status = f"error: {e}"

    # ChromaDB
    try:
        chroma = _state.get("chroma")
        if chroma:
            chroma.heartbeat()
            chroma_status = "ok"
    except Exception as e:
        chroma_status = f"error: {e}"

    # Redis
    try:
        import redis as redis_lib
        r = redis_lib.from_url(settings.redis_url, socket_connect_timeout=2)
        r.ping()
        redis_status = "ok"
        pipeline_stage = (r.get("nexus:pipeline:stage") or b"").decode() or None
    except Exception as e:
        redis_status = f"error: {e}"

    overall = "ok" if all(
        s == "ok" for s in [neo4j_status, chroma_status]
    ) else "degraded"

    return HealthResponse(
        status=overall,
        neo4j=neo4j_status,
        chroma=chroma_status,
        redis=redis_status,
        pipeline_stage=pipeline_stage,
    )


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api.app:app", host=settings.api_host, port=settings.api_port, reload=False)
