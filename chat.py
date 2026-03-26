"""
chat.py
Interactive CLI chat for CodeNexus.

Ask natural-language questions about the codebase directly in your terminal.
Connects to the running CodeNexus pipeline (no API server needed).

Usage:
    py chat.py                          # start chat
    py chat.py --port 8082              # if API server is running, use HTTP mode
    py chat.py --no-color               # plain output (for piping)

Commands inside the chat:
    /explain <fqn>    Explain a class or method in detail
    /stats            Show graph + vector store statistics
    /route            Show which route the last query took
    /help             Show this help
    /clear            Clear the screen
    /quit  or  exit   Exit
"""
from __future__ import annotations
import argparse
import logging
import os
import sys
import time
import textwrap

logger = logging.getLogger(__name__)

sys.path.insert(0, os.path.dirname(__file__))


# ── ANSI colours ───────────────────────────────────────────────────────────────

USE_COLOR = sys.stdout.isatty()

def _c(text: str, code: str) -> str:
    if not USE_COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"

def bold(t):    return _c(t, "1")
def dim(t):     return _c(t, "2")
def green(t):   return _c(t, "32")
def cyan(t):    return _c(t, "36")
def yellow(t):  return _c(t, "33")
def red(t):     return _c(t, "31")
def blue(t):    return _c(t, "34")
def magenta(t): return _c(t, "35")

ROUTE_COLOR = {
    "exact":                        cyan,
    "symbolic":                     yellow,
    "symbolic_fallback":            yellow,
    "semantic":                     green,
    "hybrid_grep+graph+semantic":   yellow,
    "hybrid_graph+semantic":        cyan,
    "global":                       magenta,
    "global_l2":         magenta,
    "global_fallback":   dim,
}


# ── Pipeline initialisation ────────────────────────────────────────────────────

_pipeline = {}

def _init_pipeline() -> bool:
    """Lazily initialise the reasoning pipeline (heavy imports)."""
    if _pipeline:
        return True
    try:
        print(dim("  Connecting to Neo4j, ChromaDB…"))
        import chromadb
        from neo4j import GraphDatabase
        from config.settings import settings
        from reasoning.router import QueryRouter
        from reasoning.reduce_step import ReduceStep
        from reasoning.graph_retriever import GraphRetriever
        from reasoning.code_fetcher import CodeFetcher

        chroma    = chromadb.HttpClient(host=settings.chroma_host, port=settings.chroma_port)
        driver    = GraphDatabase.driver(settings.neo4j_uri, auth=settings.neo4j_auth)
        router    = QueryRouter(chroma_client=chroma)
        reduce    = ReduceStep()
        retriever = GraphRetriever(chroma_client=chroma)
        fetcher   = CodeFetcher()

        _pipeline["chroma"]    = chroma
        _pipeline["driver"]    = driver
        _pipeline["router"]    = router
        _pipeline["reduce"]    = reduce
        _pipeline["retriever"] = retriever
        _pipeline["fetcher"]   = fetcher
        return True
    except Exception as e:
        print(red(f"  Failed to connect: {e}"))
        return False


# ── Query runner ──────────────────────────────────────────────────────────────

def _run_query(question: str, conversation_history: list[dict] | None = None) -> dict:
    from reasoning.map_step import MapResult
    from api.response_builder import build_query_response

    router  = _pipeline["router"]
    reduce  = _pipeline["reduce"]
    fetcher = _pipeline["fetcher"]

    # Inject prior conversation context into the query so follow-up questions
    # like "what files should I change" can be resolved against the prior topic.
    effective_question = question
    if conversation_history:
        last = conversation_history[-1]
        ctx = f"[Prior context: {last['q']}]\n{question}"
        effective_question = ctx

    result = router.route(effective_question)

    map_results = []
    if result.community_summaries:
        try:
            map_results = [
                MapResult(
                    community_id=s.get("community_id", 0),
                    summary_text=s.get("summary_text", ""),
                    score=100,
                    reason="pre-retrieved",
                )
                for s in result.community_summaries
            ]
        except Exception:
            pass

    primary_targets = []
    for hit in result.grep_hits:
        if hasattr(hit, "rel_path"):
            # Use the class name (filename without .java) as fqn for display
            class_name = hit.rel_path.replace("\\", "/").split("/")[-1].replace(".java", "")
            primary_targets.append({
                "source": "grep", "fqn": class_name,
                "file_path": hit.rel_path, "line_number": hit.line_number,
                "text": hit.line_text, "edge_type": "",
            })
    for node in result.seed_nodes:
        primary_targets.append({**node, "source": "graph"})
    for node in result.affected_nodes[:10]:
        primary_targets.append({**node, "source": "graph"})

    # Fetch actual source code.
    # ORDERING: graph entity method bodies FIRST (most targeted — entity names from LLM parser),
    # then grep contexts from implementation files (skip constant-definition files like OAuthConstants
    # and test files — they waste budget and push real implementation code off the screen).
    code_snippets = []
    try:
        # Expand graph seed nodes with sibling methods from the same class
        graph_seed_fqns = [
            t["fqn"] for t in primary_targets
            if t.get("source") == "graph" and t.get("fqn")
        ]
        if graph_seed_fqns:
            try:
                siblings = router.retriever.get_class_methods(graph_seed_fqns[:8])
                seen_fqns = {t["fqn"] for t in primary_targets if t.get("fqn")}
                for sib in siblings:
                    if sib["fqn"] not in seen_fqns:
                        primary_targets.append({**sib, "source": "graph"})
                        seen_fqns.add(sib["fqn"])
            except Exception as e:
                logger.debug("Sibling expansion failed: %s", e)

            # ── Feature 1: Multi-hop neighbor expansion ──────────────────────
            # For each top seed node, add direct callers and callees so the LLM
            # sees WHERE each method is called from and WHAT it calls.
            # This is the core value of having Neo4j — zero extra RAM cost.
            try:
                seen_fqns = {t["fqn"] for t in primary_targets if t.get("fqn")}
                for fqn in graph_seed_fqns[:5]:
                    for neighbor in (
                        router.retriever.get_callers(fqn)[:5]
                        + router.retriever.get_callees(fqn)[:5]
                    ):
                        nfqn = neighbor.get("fqn")
                        if nfqn and nfqn not in seen_fqns:
                            primary_targets.append({**neighbor, "source": "graph"})
                            seen_fqns.add(nfqn)
            except Exception as e:
                logger.debug("Neighbor expansion failed: %s", e)

        # ── Feature 8: DataSink schema injection ─────────────────────────────
        # If any retrieved Component is a DAO/repository class, fetch the
        # CREATE TABLE DDL for the tables it queries and inject into code_snippets.
        try:
            all_fqns = [t["fqn"] for t in primary_targets if t.get("fqn")]
            table_rows = router.retriever.get_datasink_tables(all_fqns)
            seen_tables: set[str] = set()
            for row in table_rows:
                tname = row.get("table_name", "")
                src   = row.get("source_file", "")
                if tname and src and tname not in seen_tables:
                    schema_snippet = fetcher.fetch_table_schema(tname, src)
                    if schema_snippet:
                        code_snippets.append(schema_snippet)
                        seen_tables.add(tname)
        except Exception as e:
            logger.debug("DataSink schema injection failed: %s", e)

        # Pass 1: graph/semantic nodes — full method bodies (start_line+end_line).
        fetchable = [t for t in primary_targets if t.get("file_path") and t.get("start_line")]
        # Graph nodes first, then semantic hits
        fetchable.sort(key=lambda t: 0 if t.get("source") == "graph" else 1)
        node_snippets = fetcher.fetch_for_nodes(fetchable[:20])
        code_snippets.extend(node_snippets)

        # Pass 2: grep contexts — one best hit per file, skip constant-definitions and tests.
        # Constant-def files (OAuthConstants, ErrorConstants, etc.) are already shown in the
        # grep_evidence section of the prompt — no need to expand them with ±20 lines of context.
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

        # Only expand grep context for implementation files (score >= 2).
        # Tests (score=1) and constant definitions (score=0) are skipped from code snippets.
        for fp, t in list(file_best.items())[:15]:
            if t["score"] < 2:
                continue  # Constant definitions and tests → shown in grep_evidence only
            snippet = fetcher.fetch_grep_context(fp, t["line_number"], context_lines=20)
            if snippet:
                code_snippets.append(snippet)
    except Exception as e:
        logger.debug("Code fetch failed: %s", e)

    answer = reduce.run(
        map_results=map_results,
        query=question,
        primary_targets=primary_targets,
        code_snippets=code_snippets,
        route=result.route,
    )

    resp = build_query_response(result, answer, 0)
    return {
        "answer":   answer,
        "route":    result.route,
        "sources":  resp.sources,
        "affected": resp.affected,
        "confidence": resp.confidence,
    }


def _run_query_streaming(question: str, conversation_history: list[dict] | None = None) -> dict:
    """
    Streaming variant of _run_query.

    Phase 1 (retrieval): router.route() — silent, ~5-15s.
    Phase 2 (display):   print route header immediately so user sees progress.
    Phase 3 (generate):  stream answer tokens to terminal as gpt-5 produces them.
    Returns the same dict as _run_query (answer is the full assembled string).
    """
    from reasoning.map_step import MapResult
    from api.response_builder import build_query_response

    router  = _pipeline["router"]
    reduce  = _pipeline["reduce"]
    fetcher = _pipeline["fetcher"]

    effective_question = question
    if conversation_history:
        last = conversation_history[-1]
        ctx = f"[Prior context: {last['q']}]\n{question}"
        effective_question = ctx

    # ── Phase 1: retrieval (silent) ────────────────────────────────────────
    print(dim("  Retrieving…"), flush=True)
    result = router.route(effective_question)

    map_results = []
    if result.community_summaries:
        try:
            map_results = [
                MapResult(
                    community_id=s.get("community_id", 0),
                    summary_text=s.get("summary_text", ""),
                    score=100,
                    reason="pre-retrieved",
                )
                for s in result.community_summaries
            ]
        except Exception:
            pass

    primary_targets = []
    for hit in result.grep_hits:
        if hasattr(hit, "rel_path"):
            class_name = hit.rel_path.replace("\\", "/").split("/")[-1].replace(".java", "")
            primary_targets.append({
                "source": "grep", "fqn": class_name,
                "file_path": hit.rel_path, "line_number": hit.line_number,
                "text": hit.line_text, "edge_type": "",
            })
    for node in result.seed_nodes:
        primary_targets.append({**node, "source": "graph"})
    for node in result.affected_nodes[:10]:
        primary_targets.append({**node, "source": "graph"})

    code_snippets = []
    try:
        # Expand graph seed nodes with sibling methods from the same class.
        # Without this, finding validateSubjectToken misses validateActorToken,
        # setSubjectAsAuthorizedUser, etc. which are private helpers with weak embeddings.
        graph_seed_fqns = [
            t["fqn"] for t in primary_targets
            if t.get("source") == "graph" and t.get("fqn")
        ]
        if graph_seed_fqns:
            try:
                siblings = router.retriever.get_class_methods(graph_seed_fqns[:8])
                seen_fqns = {t["fqn"] for t in primary_targets if t.get("fqn")}
                for sib in siblings:
                    if sib["fqn"] not in seen_fqns:
                        primary_targets.append({**sib, "source": "graph"})
                        seen_fqns.add(sib["fqn"])
            except Exception as e:
                logger.debug("Sibling expansion failed: %s", e)

            # ── Feature 1: Multi-hop neighbor expansion ──────────────────────
            try:
                seen_fqns = {t["fqn"] for t in primary_targets if t.get("fqn")}
                for fqn in graph_seed_fqns[:5]:
                    for neighbor in (
                        router.retriever.get_callers(fqn)[:5]
                        + router.retriever.get_callees(fqn)[:5]
                    ):
                        nfqn = neighbor.get("fqn")
                        if nfqn and nfqn not in seen_fqns:
                            primary_targets.append({**neighbor, "source": "graph"})
                            seen_fqns.add(nfqn)
            except Exception as e:
                logger.debug("Neighbor expansion failed: %s", e)

        # ── Feature 8: DataSink schema injection ─────────────────────────────
        try:
            all_fqns = [t["fqn"] for t in primary_targets if t.get("fqn")]
            table_rows = router.retriever.get_datasink_tables(all_fqns)
            seen_tables: set[str] = set()
            for row in table_rows:
                tname = row.get("table_name", "")
                src   = row.get("source_file", "")
                if tname and src and tname not in seen_tables:
                    schema_snippet = fetcher.fetch_table_schema(tname, src)
                    if schema_snippet:
                        code_snippets.append(schema_snippet)
                        seen_tables.add(tname)
        except Exception as e:
            logger.debug("DataSink schema injection failed: %s", e)

        fetchable = [t for t in primary_targets if t.get("file_path") and t.get("start_line")]
        fetchable.sort(key=lambda t: 0 if t.get("source") == "graph" else 1)
        node_snippets = fetcher.fetch_for_nodes(fetchable[:20])
        code_snippets.extend(node_snippets)

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
                continue
            snippet = fetcher.fetch_grep_context(fp, t["line_number"], context_lines=20)
            if snippet:
                code_snippets.append(snippet)
    except Exception as e:
        logger.debug("Code fetch failed: %s", e)

    # ── Phase 2: show route immediately (user sees this before LLM starts) ──
    route_col = {
        "symbolic": "\033[33m", "exact": "\033[36m",
        "semantic": "\033[35m", "global": "\033[32m",
    }
    route_label = result.route.upper()
    col_code = route_col.get(result.route.split("_")[0], "\033[37m") if USE_COLOR else ""
    reset = "\033[0m" if USE_COLOR else ""
    src_count = len(primary_targets) + len(map_results)
    print(f"\n  {bold('Route')}: {col_code}{route_label}{reset}  |  "
          f"{bold('Sources')}: {src_count}", flush=True)
    print("  " + "-" * 70, flush=True)
    print(f"  ", end="", flush=True)   # indent for streamed answer

    # ── Phase 3: stream answer ─────────────────────────────────────────────
    _first_token = [True]

    def _on_token(delta: str) -> None:
        # Indent continuation lines to match the leading "  "
        text = delta.replace("\n", "\n  ")
        if _first_token[0] and text.startswith("  "):
            text = text.lstrip()
        _first_token[0] = False
        print(text, end="", flush=True)

    answer = reduce.run(
        map_results=map_results,
        query=question,
        primary_targets=primary_targets,
        code_snippets=code_snippets,
        route=result.route,
        on_token=_on_token,
    )
    print()  # newline after streamed answer

    resp = build_query_response(result, answer, 0)
    return {
        "answer":   answer,
        "route":    result.route,
        "sources":  resp.sources,
        "affected": resp.affected,
        "confidence": resp.confidence,
    }


def _print_answer_footer(result: dict, latency_ms: float) -> None:
    """Print just the latency + top sources footer (answer was already streamed)."""
    print(f"\n  {dim('Latency')}: {latency_ms:.0f}ms", flush=True)
    sources = result.get("sources", [])
    if sources:
        print()
        print(f"  {dim('Top sources:')}")
        seen: set[str] = set()
        for src in sources[:5]:
            fqn      = getattr(src, "fqn", "") or str(src)
            rel_path = getattr(src, "rel_path", "") or ""
            if fqn in seen:
                continue
            seen.add(fqn)
            short_fqn = fqn.split(".")[-1] if "." in fqn else fqn
            line_no   = getattr(src, "line_number", None)
            loc       = f"[{rel_path.split('/')[-1]}:{line_no}]" if line_no else f"[{rel_path.split('/')[-1]}]"
            print(f"    {cyan(short_fqn):<52} {dim(loc)}")
    print()


def _run_explain(fqn: str) -> dict:
    from config.settings import settings
    retriever = _pipeline["retriever"]
    reduce    = _pipeline["reduce"]
    driver    = _pipeline["driver"]

    with driver.session() as s:
        rec = s.run(
            "MATCH (n {fqn: $fqn}) "
            "RETURN labels(n)[0] AS node_type, n.geid AS geid, "
            "n.blast_radius_risk AS blast_radius_risk, "
            "n.entry_point_score AS entry_point_score, "
            "n.community_id AS community_id, "
            "n.file_path AS file_path, n.start_line AS start_line, n.end_line AS end_line "
            "LIMIT 1",
            fqn=fqn,
        ).single()

    if not rec:
        return {"error": f"Not found in graph: {fqn}"}

    raw_callers = retriever.get_callers(fqn, depth=1)
    raw_callees = retriever.get_callees(fqn, depth=1)

    try:
        explanation = reduce.explain(
            fqn=fqn,
            code_snippets=[],
            callers=raw_callers[:10],
            callees=raw_callees[:10],
        )
    except Exception as e:
        explanation = f"(LLM unavailable: {e})"

    return {
        "fqn":               fqn,
        "node_type":         rec["node_type"],
        "blast_radius_risk": rec["blast_radius_risk"],
        "entry_point_score": rec["entry_point_score"],
        "community_id":      rec["community_id"],
        "callers":           raw_callers[:15],
        "callees":           raw_callees[:15],
        "explanation":       explanation,
    }


def _run_stats() -> dict:
    import chromadb
    from config.settings import settings

    driver = _pipeline["driver"]
    chroma = _pipeline["chroma"]

    def _count(s, label, is_rel=False):
        try:
            if is_rel:
                return s.run(f"MATCH ()-[r:{label}]->() RETURN count(r) AS c").single()["c"]
            return s.run(f"MATCH (n:{label}) RETURN count(n) AS c").single()["c"]
        except Exception:
            return 0

    out = {}
    with driver.session() as s:
        out["Components"]    = _count(s, "Component")
        out["LogicUnits"]    = _count(s, "LogicUnit")
        out["Fields"]        = _count(s, "Field")
        out["SpecSections"]  = _count(s, "SpecSection")
        out["CALLS edges"]   = _count(s, "CALLS", True)
        out["IMPLEMENTS_SPEC"] = _count(s, "IMPLEMENTS_SPEC", True)
        out["EntryPoints"]   = _count(s, "EntryPoint")
        out["DataSinks"]     = _count(s, "DataSink")
        out["Communities"]   = s.run(
            "MATCH (n) WHERE n.community_id IS NOT NULL "
            "RETURN count(DISTINCT n.community_id) AS c"
        ).single()["c"]

    for col_name in ("code_intent", "code_logic", "community_summaries"):
        try:
            out[f"chroma:{col_name}"] = chroma.get_collection(col_name).count()
        except Exception:
            out[f"chroma:{col_name}"] = 0

    return out


# ── Output formatters ─────────────────────────────────────────────────────────

def _wrap(text: str, width: int = 100, indent: str = "  ") -> str:
    lines = text.strip().split("\n")
    out = []
    for line in lines:
        if len(line) <= width:
            out.append(indent + line)
        else:
            for wrapped in textwrap.wrap(line, width=width - len(indent)):
                out.append(indent + wrapped)
    return "\n".join(out)


def _print_answer(result: dict, latency_ms: float) -> None:
    route    = result["route"]
    answer   = result["answer"]
    sources  = result["sources"]
    affected = result["affected"]
    conf     = result.get("confidence", 0)
    col      = ROUTE_COLOR.get(route, dim)

    print()
    print(f"  {bold('Route')}:  {col(route.upper())}  |  "
          f"{bold('Confidence')}: {conf:.0%}  |  "
          f"{bold('Latency')}: {latency_ms:.0f}ms  |  "
          f"{bold('Sources')}: {len(sources)}")
    print("  " + "-" * 70)
    print(_wrap(answer))

    if sources:
        print()
        print(f"  {dim('Top sources:')}")
        for src in sources[:5]:
            fqn  = src.fqn if hasattr(src, "fqn") else src.get("fqn", "")
            fp   = src.file_path if hasattr(src, "file_path") else src.get("file_path", "")
            line = src.line_number if hasattr(src, "line_number") else src.get("line_number", "")
            short_fqn = fqn.split(".")[-1][:50] if fqn else ""
            short_fp  = fp.split("/")[-1] if fp else ""
            loc = f"[{short_fp}:{line}]" if line else f"[{short_fp}]"
            print(f"    {cyan(short_fqn):<52} {dim(loc)}")
    print()


def _print_explain(result: dict, latency_ms: float) -> None:
    if "error" in result:
        print(red(f"  Error: {result['error']}"))
        return

    print()
    print(f"  {bold('FQN')}:       {cyan(result['fqn'])}")
    print(f"  {bold('Type')}:      {result.get('node_type', '?')}")
    print(f"  {bold('Risk')}:      {yellow(str(result.get('blast_radius_risk', 'unknown')))}")
    print(f"  {bold('Community')}: {result.get('community_id', '?')}")
    print(f"  {bold('Callers')}:   {len(result.get('callers', []))}  |  "
          f"{bold('Callees')}: {len(result.get('callees', []))}")
    print(f"  {bold('Latency')}:   {latency_ms:.0f}ms")
    print("  " + "-" * 70)

    explanation = result.get("explanation", "")
    print(_wrap(explanation[:3000]))

    callers = result.get("callers", [])[:5]
    if callers:
        print()
        print(f"  {dim('Called by:')}")
        for c in callers:
            fqn = c.get("fqn", "")
            print(f"    {dim('<-')} {cyan(fqn.split('.')[-1])}")
    print()


def _print_stats(stats: dict) -> None:
    print()
    print(f"  {bold('=== CodeNexus Graph Statistics ===')}")
    print()
    neo4j_keys  = [k for k in stats if not k.startswith("chroma:")]
    chroma_keys = [k for k in stats if k.startswith("chroma:")]

    print(f"  {bold('Neo4j nodes & edges:')}")
    for k in neo4j_keys:
        print(f"    {k:<22} {green(str(stats[k])):>8}")
    print()
    print(f"  {bold('ChromaDB vectors:')}")
    for k in chroma_keys:
        label = k.replace("chroma:", "")
        total = sum(stats[ck] for ck in chroma_keys)
        print(f"    {label:<22} {cyan(str(stats[k])):>8}")
    print(f"    {'TOTAL vectors':<22} {cyan(str(total)):>8}")
    print()


def _print_help() -> None:
    print(f"""
  {bold('CodeNexus Chat — Commands')}

  {yellow('Natural-language questions')} (just type them):
    What does TokenExchangeGrantHandler do?
    How does the authorization code grant flow work?
    Can I safely remove IMPERSONATED_SUBJECT?
    What is the overall architecture?

  {yellow('Slash commands')}:
    {cyan('/explain <fqn>')}    Deep-dive on a class or method
    {cyan('/stats')}            Graph + vector store statistics
    {cyan('/route')}            Show the route taken by the last query
    {cyan('/history')}          Show recent questions
    {cyan('/clear')}            Clear the screen
    {cyan('/help')}             Show this help
    {cyan('/quit')}             Exit

  {dim('Examples:')}
    /explain org.wso2.carbon.identity.oauth2.internal.OAuth2ServiceComponent
    /explain org.wso2.carbon.identity.oauth2.token.handlers.grant.AuthorizationCodeGrantHandler
""")


# ── Main REPL ─────────────────────────────────────────────────────────────────

def _banner() -> None:
    print()
    print(bold("  ======================================================"))
    print(bold("  ") + cyan("  CodeNexus Chat"))
    print(bold("  ") + dim("  GraphRAG  |  Neo4j + ChromaDB + GPT-4o-mini"))
    print(bold("  ") + dim("  Type a question, /help for commands, /quit to exit"))
    print(bold("  ======================================================"))
    print()


def main() -> None:
    global USE_COLOR

    arg_parser = argparse.ArgumentParser(description="CodeNexus CLI chat")
    arg_parser.add_argument("--no-color", action="store_true", help="Disable colour output")
    args = arg_parser.parse_args()

    if args.no_color:
        USE_COLOR = False

    _banner()

    if not _init_pipeline():
        print(red("  Could not connect to services. Check Neo4j and ChromaDB are running."))
        sys.exit(1)

    print(green("  Connected.") + dim("  Ready for questions.\n"))

    history: list[str] = []
    conversation_history: list[dict] = []  # [{q, a}] rolling context for follow-ups
    last_result: dict | None = None

    try:
        while True:
            try:
                line = input(bold("You: ")).strip()
            except (EOFError, KeyboardInterrupt):
                print("\n" + dim("  Goodbye."))
                break

            if not line:
                continue

            # ── Slash commands ────────────────────────────────────────────────
            if line.lower() in ("/quit", "/exit", "exit", "quit"):
                print(dim("  Goodbye."))
                break

            if line.lower() == "/clear":
                os.system("cls" if os.name == "nt" else "clear")
                _banner()
                continue

            if line.lower() == "/help":
                _print_help()
                continue

            if line.lower() == "/stats":
                print(dim("  Fetching stats…"))
                try:
                    stats = _run_stats()
                    _print_stats(stats)
                except Exception as e:
                    print(red(f"  Stats failed: {e}"))
                continue

            if line.lower() == "/route":
                if last_result:
                    route = last_result.get("route", "unknown")
                    col = ROUTE_COLOR.get(route, dim)
                    print(f"  Last route: {col(route.upper())}")
                else:
                    print(dim("  No query yet."))
                continue

            if line.lower() == "/history":
                if history:
                    print()
                    for i, q in enumerate(history[-10:], 1):
                        print(f"  {dim(str(i)+'.')} {q}")
                    print()
                else:
                    print(dim("  No history yet."))
                continue

            if line.lower().startswith("/explain "):
                fqn = line[9:].strip()
                if not fqn:
                    print(red("  Usage: /explain <fully.qualified.ClassName>"))
                    continue
                print(dim(f"  Explaining {fqn}…"))
                t0 = time.time()
                try:
                    result = _run_explain(fqn)
                    _print_explain(result, (time.time() - t0) * 1000)
                except Exception as e:
                    print(red(f"  Explain failed: {e}"))
                continue

            if line.startswith("/"):
                print(red(f"  Unknown command: {line}"))
                print(dim("  Type /help for available commands."))
                continue

            # ── Natural-language query ─────────────────────────────────────────
            history.append(line)
            t0 = time.time()
            try:
                result = _run_query_streaming(line, conversation_history=conversation_history[-3:])
                last_result = result
                conversation_history.append({"q": line, "a": result["answer"][:300]})
                _print_answer_footer(result, (time.time() - t0) * 1000)
            except Exception as e:
                print(red(f"  Query failed: {e}"))
                import traceback
                traceback.print_exc()

    finally:
        # Cleanup
        if "router" in _pipeline:
            try:
                _pipeline["router"].close()
            except Exception:
                pass
        if "driver" in _pipeline:
            try:
                _pipeline["driver"].close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
