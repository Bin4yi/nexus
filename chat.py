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
    # ORDERING: grep context FIRST (most targeted/precise), then node method bodies.
    # Deduplicate by FILE (not by line) so one snippet per file — prevents OAuthConstants
    # or AccessTokenIssuer from occupying all 8 slots with multiple hits from the same file.
    # The "best" line per file: prefer non-import, non-definition, non-test hits.
    code_snippets = []
    try:
        # Pass 1: pick the most interesting grep hit per file, then fetch context
        from pathlib import Path as _Path
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

        for fp, t in list(file_best.items())[:10]:
            snippet = fetcher.fetch_grep_context(fp, t["line_number"], context_lines=10)
            if snippet:
                code_snippets.append(snippet)

        # Pass 2: graph/semantic nodes with full method bodies (start_line+end_line)
        fetchable = [t for t in primary_targets if t.get("file_path") and t.get("start_line")]
        node_snippets = fetcher.fetch_for_nodes(fetchable[:6])
        code_snippets.extend(node_snippets)
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
            print(dim("  Thinking…"))
            t0 = time.time()
            try:
                result = _run_query(line, conversation_history=conversation_history[-3:])
                last_result = result
                conversation_history.append({"q": line, "a": result["answer"][:300]})
                _print_answer(result, (time.time() - t0) * 1000)
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
