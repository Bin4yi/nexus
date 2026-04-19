"""
main.py
CLI entry point for CodeNexus GraphRAG ingestion and querying.

Query pipeline uses a THREE-TIER DETERMINISTIC ROUTER:
  ROUTE A — SYMBOLIC:  grep → graph bridge → blast radius → reduce (100 % deterministic)
  ROUTE B — EXACT:     Neo4j entity lookup → blast radius → reduce (100 % deterministic)
  ROUTE C — CONCEPTUAL: ChromaDB vector search → map → reduce  (probabilistic, for open-ended Qs)

All configuration is centralised in ``config/settings.py`` which reads from ``.env``.
"""
from __future__ import annotations
import argparse, re, sys, logging, time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from config.settings import settings
from pipeline.orchestrator import IngestionPipeline

# ── Logging — driven entirely by settings ────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.WARNING),
    format=settings.log_format,
)
# Silence noisy third-party HTTP/SDK loggers regardless of LOG_LEVEL
for _noisy in (
    "httpcore", "httpx", "openai", "openai._base_client",
    "urllib3", "requests", "chromadb", "sentence_transformers",
    "huggingface_hub", "huggingface_hub.utils._http", "transformers",
    "filelock", "torch",
):
    logging.getLogger(_noisy).setLevel(logging.ERROR)
logger = logging.getLogger(__name__)


def ingest():
    logging.getLogger().setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))
    logger.info("Starting ingestion pipeline...")
    pipeline = IngestionPipeline()
    stats = pipeline.run()
    logger.info("Ingestion complete. Stats:")
    for k, v in stats.items():
        logger.info(f"  {k}: {v}")
    if stats.get("failed_repos"):
        sys.exit(1)


def _semantic_code_search(
    chroma_client, question: str, n: int = 10, repo_filter: str | None = None
) -> list[dict]:
    """ChromaDB code_intent search — finds methods semantically close to query."""
    try:
        col = chroma_client.get_collection("code_intent")
        where = {"repo_name": {"$eq": repo_filter}} if repo_filter else None
        kwargs: dict = dict(query_texts=[question], n_results=min(n, col.count()))
        if where:
            kwargs["where"] = where
        results = col.query(**kwargs)
    except Exception:
        return []
    hits = []
    if results["ids"]:
        for i, chunk_id in enumerate(results["ids"][0]):
            meta = results["metadatas"][0][i]
            fp = meta.get("file_path", "")
            if "mirror" in fp:
                fp = fp[fp.find("mirror"):]
            hits.append({"fqn": meta.get("fqn", ""), "file_path": fp, "source": "semantic", "distance": results["distances"][0][i]})
    return hits


def _format_fp(fp: str) -> str:
    if "mirror" in fp:
        return fp[fp.find("mirror"):]
    return fp


_SEARCH_TERM_SYSTEM = """\
You are a Java/OAuth2 codebase search assistant for the WSO2 identity server.
Given a developer's question, extract the exact terms that should be grep-searched
in a Java source tree to locate the relevant files.

Return ONLY a JSON array of strings — no explanation, no markdown, no wrapper object.

Rules (in priority order):
1. camelCase compound terms FIRST — e.g. "tokenExchange", "audienceValidation",
   "multipleAudience".  These hit specific class/method names.
2. CamelCase class or interface names if directly implied — e.g. "TokenExchangeGrantHandler",
   "JWTTokenIssuer".
3. Plain domain keywords — specific technical nouns, not generic verbs.
   e.g. "audience", "exchange", "issuer".
   DO NOT include: token, claim, support, implement, change, file, method, class,
   single, multiple, want, need, introduce, currently.
4. Quoted short literals (2-4 chars) for JWT claim names — e.g. '"aud"', '"sub"'.
   Only if the question clearly involves JWT claim field names.

Maximum 8 terms total.  Order them from most specific to least specific.\
"""


def _llm_extract_search_terms(question: str, entity_names: list[str]) -> list[str]:
    """Use the LLM to extract grep-ready search terms from the question.

    Falls back to a minimal regex heuristic if the API call fails so the
    query pipeline never hard-errors on keyword extraction.
    """
    from openai import OpenAI
    import json as _json

    # Always include any CamelCase entities spotted by the router — they are
    # already precise and cost nothing to add.
    router_entities = list(entity_names[:3])

    try:
        client = settings.make_llm_client()
        resp = client.chat.completions.create(
            model=settings.llm_model,
            messages=[
                {"role": "system", "content": _SEARCH_TERM_SYSTEM},
                {"role": "user", "content": f'Question: "{question}"'},
            ],
            max_completion_tokens=3000,
        )
        raw = resp.choices[0].message.content.strip()
        # Strip accidental markdown fences
        raw = re.sub(r"^```[a-z]*\n?", "", raw).rstrip("` \n")
        terms: list[str] = _json.loads(raw)
        if not isinstance(terms, list):
            raise ValueError("LLM returned non-list")
        terms = [t for t in terms if isinstance(t, str) and len(t) >= 2]
        logger.info("LLM search terms for query: %s", terms)
        # Merge: LLM terms first (most relevant), then router entities (deduped)
        seen: set[str] = set(terms)
        for e in router_entities:
            if e not in seen:
                terms.append(e)
                seen.add(e)
        return terms[:10]
    except Exception as exc:
        logger.warning("LLM term extraction failed (%s) — falling back to regex", exc)
        # Fallback: extract CamelCase tokens and words ≥5 chars not in a tiny noise set
        _noise = {"token","claim","which","files","should","change","currently",
                  "introduce","implement","single","multiple","support","method"}
        words = re.findall(r"[A-Za-z][a-z0-9]+(?:[A-Z][a-z0-9]+)+", question)  # CamelCase
        words += [w for w in re.findall(r"[a-z]{5,}", question.lower()) if w not in _noise]
        return (router_entities + list(dict.fromkeys(words)))[:10]


def query(question: str, candidates: int = 20, threshold: int = 70,
          depth: int | None = None, repo: str | None = None):
    """
    Hybrid GraphRAG query with deterministic routing:
      - Tries to find entity in Neo4j first
      - DETERMINISTIC: graph traversal → community IDs → fetch summaries by ID → reduce
      - SEMANTIC fallback: ChromaDB vector search → map step → reduce

    ``depth`` defaults to ``settings.blast_radius_depth`` when not overridden.
    """
    if depth is None:
        depth = settings.blast_radius_depth
    import chromadb
    from openai import OpenAI
    from reasoning.router import QueryRouter
    from reasoning.reduce_step import ReduceStep
    from reasoning.map_step import MapStep

    print(f"\n{'━' * 72}")
    print(f"  🔍  {question}")
    print(f"{'━' * 72}\n")

    chroma = chromadb.HttpClient(host=settings.chroma_host, port=settings.chroma_port)
    llm    = settings.make_llm_client()
    t0     = time.perf_counter()

    # ── STEP 1: Route the query ───────────────────────────────────────────────
    router = QueryRouter(chroma)
    try:
        route_result = router.route(question)
    finally:
        router.close()

    t_route = time.perf_counter() - t0

    route_label = {
        "symbolic":          "⚡ SYMBOLIC  — grep + graph bridge",
        "symbolic_fallback": "⚡ SYMBOLIC  — grep found nothing, semantic fallback",
        "symbolic_no_bridge":"⚡ SYMBOLIC  — grep hit, but no graph node for that line",
        "exact":             "🔗 EXACT-ENTITY — direct Neo4j lookup",
        "semantic":          "🔍 SEMANTIC — ChromaDB vector search",
    }
    print(f"  ⚙️  Route: {route_label.get(route_result.route, route_result.route)}")
    if route_result.symbols:
        print(f"  🎯  Symbols grepped: {', '.join(route_result.symbols)}")
    if route_result.grep_hits:
        print(f"  📎  Grep hits: {len(route_result.grep_hits)} production files")
        for h in route_result.grep_hits[:3]:
            print(f"       ↳ {h.rel_path}:{h.line_number}  {h.line_text[:60]}")
    if route_result.entity_names:
        print(f"  📌  Entities: {', '.join(route_result.entity_names[:5])}")
    if route_result.seed_nodes:
        print(f"  🌱  Seed nodes: {len(route_result.seed_nodes)}")
        for n in route_result.seed_nodes[:3]:
            print(f"       ↳ {n['fqn']}")
    print(f"  📊  {len(route_result.community_ids)} communities to analyse\n")


    # ── STEP 2: Semantic code search (always, for 'FILES TO CHANGE' section) ──
    semantic_hits = _semantic_code_search(chroma, question, n=10, repo_filter=repo)

    # ── STEP 3: Map step (SEMANTIC route only) or deterministic summaries ─────
    map_results = []
    # All multi-route results (hybrid_* labels) and pure semantic/fallback routes
    # use ChromaDB vector search and need the map step + code expansion.
    # Only purely symbolic/exact/global routes skip it.
    _NON_SEMANTIC_ROUTES = {"symbolic", "exact", "global", "global_l2", "failed"}
    is_semantic = route_result.route not in _NON_SEMANTIC_ROUTES

    if is_semantic:
        map_step    = MapStep(chroma, llm, n_candidates=candidates,
                              max_workers=settings.summarizer_max_workers)
        map_results = map_step.run(question, mode="question")
        summaries_for_reduce = [
            {"community_id": r.community_id, "summary_text": r.summary_text}
            for r in map_results if r.score >= threshold
        ]
    else:
        # Deterministic (symbolic / exact): communities are mathematically proven
        summaries_for_reduce = route_result.community_summaries

    t_retrieval = time.perf_counter() - t0

    # ── STEP 4: Build grounded context for the reduce call ────────────────────
    grep_evidence = [
        {
            "fqn":       f"{h.rel_path}:{h.line_number}",
            "file_path": h.rel_path,
            "source":    "grep",
            "edge_type": f"literal '{h.symbol}' at line {h.line_number}",
            "text":      h.line_text[:150],
        }
        for h in (route_result.grep_hits or [])
    ]

    primary_targets = (
        grep_evidence
        + semantic_hits
        + [{"fqn": n["fqn"], "file_path": _format_fp(n.get("file_path", "")), "source": "graph"}
           for n in route_result.seed_nodes]
        + [{"fqn": n["fqn"], "file_path": _format_fp(n.get("file_path", "")), "source": "graph",
            "edge_type": n.get("edge_type")}
           for n in route_result.affected_nodes[:20]]
    )

    # ── STEP 4b: Fetch real source code when the query asks for it ────────────
    from reasoning.code_fetcher import CodeFetcher
    from reasoning.reduce_step import detect_query_intent
    query_intent = detect_query_intent(question)

    code_snippets = []
    fetcher = CodeFetcher()

    # ── STEP 4c: Keyword grep (capability/general) + semantic snippet fetch ──────
    # Design principle:
    #   Keyword grep is MORE reliable than semantic search for capability queries:
    #   semantic finds "methods about claims generally";
    #   grep finds the EXACT code for the specific term being asked about.
    #   For "act claim" → grep '"act"' → directly hits ImpersonatedAccessTokenClaimProvider.
    #   So: run grep FIRST for capability/general, then augment with semantic snippets.

    # ── Keyword extraction — LLM-driven ───────────────────────────────────
    # Instead of a fragile hardcoded stop-list, ask the LLM to extract exactly
    # the search terms that should be grep'd in a Java codebase for this question.
    # The LLM understands intent/meta-words vs real technical terms natively.
    # A minimal regex fallback is used if the LLM call fails.
    _kw_search_terms = _llm_extract_search_terms(question, route_result.entity_names)

    if is_semantic and query_intent in ("capability", "general"):
        # ── Phase A: Keyword grep ─────────────────────────────────────────────
        # Run unconditionally — this is the primary evidence source for capability
        # queries, not a fallback.  Grep hits are inserted BEFORE semantic snippets
        # so the LLM sees them first.
        from reasoning.lexical_search import LexicalSearcher
        _lex_kw = LexicalSearcher()
        _seen_kw_terms: set[str] = set()
        for term in _kw_search_terms:
            if term in _seen_kw_terms or len(term) < 3:
                continue
            _seen_kw_terms.add(term)
            # Quoted literals (e.g. '"act"') target small constant declaration files
            # like claim providers.  Use a wide window so we capture the FULL class
            # body (these files are typically 50-100 lines).  For other terms the
            # default window is sufficient.
            _ctx_lines = 40 if (term.startswith('"') and term.endswith('"')) else 15
            _ghits = _lex_kw.search(term, max_hits=10)
            for hit in _ghits:
                _norm_rp = hit.rel_path.replace("\\", "/")
                if "src/main/java" in _norm_rp and "Test" not in _norm_rp:
                    s = fetcher.fetch_grep_context(hit.rel_path, hit.line_number, context_lines=_ctx_lines)
                    if s:
                        code_snippets.append(s)
            if len(code_snippets) >= 6:
                break

        # ── Phase A-chain: generic constant-following ─────────────────────────
        # If Phase A grep snippets reference UPPER_SNAKE_CASE constants, grep for
        # those constants to discover the SETTER/CONSUMER side of the implementation.
        # This is fully generic: it follows any constant, not a hardcoded list.
        if code_snippets and len(code_snippets) < 10:
            _all_snippet_text = "\n".join(s.code or "" for s in code_snippets)
            _referenced_constants = set(re.findall(r'\b[A-Z][A-Z0-9_]{4,}\b', _all_snippet_text))
            # Remove common Java noise constants
            _noise_constants = {
                "AUTHORIZATION", "OAUTH", "STRING", "OBJECT", "EXCEPTION",
                "NULL", "TRUE", "FALSE", "RETURN", "STATIC", "FINAL",
                "PUBLIC", "PRIVATE", "PROTECTED", "CLASS", "VOID",
                "OVERRIDE", "IMPORT", "PACKAGE", "THROWS", "ERROR",
            }
            _referenced_constants -= _noise_constants
            # Limit to 3 most interesting constants (longest names = most specific)
            _sorted_consts = sorted(_referenced_constants, key=len, reverse=True)[:3]
            _seen_chain_files: set[str] = set(s.file_path or "" for s in code_snippets)
            for _const in _sorted_consts:
                if len(code_snippets) >= 10:
                    break
                _chain_hits = _lex_kw.search(_const, max_hits=10)
                for _ch in _chain_hits:
                    _cnorm = _ch.rel_path.replace("\\", "/")
                    if ("src/main/java" in _cnorm and "Test" not in _cnorm
                            and _cnorm not in _seen_chain_files):
                        _cs = fetcher.fetch_grep_context(
                            _ch.rel_path, _ch.line_number, context_lines=15,
                        )
                        if _cs:
                            _cs.context_note = f"constant reference: {_const}"
                            code_snippets.append(_cs)
                            _seen_chain_files.add(_cnorm)
                            break  # one hit per constant is enough

        # ── Phase A-graph: expand evidence via Neo4j call graph ─────────────
        # Bridge Phase A grep hits to Neo4j nodes, then expand to their 1-hop
        # callees and callers.  The LLM sees the FULL call tree of the
        # implementation — allowing it to reason about structural properties
        # (nesting, recursion, propagation) without any hardcoded domain rules:
        # if none of the callees reads/propagates an existing value, that absence
        # is visible directly in the evidence.
        if code_snippets:
            from graph.sqlite_retriever import SqliteRetriever as _GR
            _gexp = _GR(db_path=settings.sqlite_db_path, chroma_client=chroma)
            try:
                _seed_fqns: list[str] = []

                # Priority 1: use graph seed_nodes already identified by the router
                # (entity lookup hits like TokenExchangeGrantHandler) — these are
                # more precise than grep→class-name derivation from Constants files.
                for _rn in route_result.seed_nodes:
                    if _rn.get("fqn") and _rn["fqn"] not in _seed_fqns:
                        # Skip pure Constants/utility classes — they're not implementation seeds
                        _rn_cls = _rn["fqn"].split(".")[-1] if "." in _rn["fqn"] else _rn["fqn"]
                        if not _rn_cls.endswith("Constants") and not _rn_cls.endswith("Utils"):
                            _seed_fqns.append(_rn["fqn"])

                # Priority 2: derive class names from keyword-grep snippet file paths
                # (catches implementation classes that the entity lookup missed)
                _seen_class_names: set[str] = set()
                for _gs in code_snippets[:8]:
                    fp = _gs.file_path or ""
                    _basename = fp.replace("\\", "/").split("/")[-1]
                    _cls = _basename[:-5] if _basename.endswith(".java") else _basename
                    if (_cls and _cls not in _seen_class_names
                            and not _cls.startswith("[")
                            and not _cls.endswith("Constants")
                            and not _cls.endswith("Utils")):
                        _seen_class_names.add(_cls)
                        _class_nodes = _gexp.find_nodes(_cls)
                        for _cn in _class_nodes:
                            if _cn.get("fqn") and _cn["fqn"] not in _seed_fqns:
                                _seed_fqns.append(_cn["fqn"])

                # Expand: callees + callers + ChromaDB code_logic domain search
                _expanded = _gexp.expand_capability_evidence(
                    seed_fqns=_seed_fqns,
                    chroma_client=chroma,
                    # Use plain lowercase terms from the LLM-extracted search list
                    # as domain seed words for ChromaDB code_logic search.
                    domain_terms=[t for t in _kw_search_terms
                                  if t.islower() and not t.startswith('"')][:5],
                )
            finally:
                _gexp.close()

            # Fetch source code for callees — they show WHAT the implementation reads
            for _callee in _expanded["callees"][:6]:
                _callee_s = fetcher.fetch_method(_callee, context_lines=0)
                if _callee_s:
                    _callee_s.context_note = "callee of Phase-A implementation"
                    code_snippets.append(_callee_s)

            # Fetch source code for code_logic hits — other methods in the same domain
            for _cl_hit in _expanded["code_logic_hits"][:4]:
                if any(s.fqn == _cl_hit["fqn"] for s in code_snippets):
                    continue
                _cl_s = fetcher.fetch_method(_cl_hit, context_lines=0)
                if _cl_s:
                    _cl_s.context_note = "code_logic domain expansion"
                    code_snippets.append(_cl_s)

            # Surface callers as primary_targets (they show activation context)
            for _caller in _expanded["callers"][:5]:
                _fp_c = _caller.get("file_path", "")
                if "mirror" in _fp_c:
                    _fp_c = _fp_c[_fp_c.find("mirror"):]
                primary_targets.append({
                    "fqn":       _caller["fqn"],
                    "file_path": _fp_c,
                    "source":    "graph",
                    "edge_type": "CALLS→implementation",
                })

        # ── Phase B: Semantic snippet fetch (context enrichment) ──────────────
        # Enrich top semantic hits with Neo4j line ranges and add up to 5 more
        # snippets for broader architectural context.  These come AFTER grep hits.
        if semantic_hits:
            sem_fqns = [t["fqn"] for t in semantic_hits if t.get("fqn")]
            if sem_fqns:
                from graph.sqlite_retriever import SqliteRetriever as GraphRetriever
                _enricher = GraphRetriever(db_path=settings.sqlite_db_path)
                try:
                    enriched_nodes = _enricher.find_nodes_by_fqns(sem_fqns[:15])
                finally:
                    _enricher.close()
                fqn_to_node = {n["fqn"]: n for n in enriched_nodes}
                for target in semantic_hits:
                    if target.get("fqn") in fqn_to_node:
                        node = fqn_to_node[target["fqn"]]
                        target.setdefault("start_line", node.get("start_line"))
                        target.setdefault("end_line",   node.get("end_line"))
                        if not target.get("file_path") and node.get("file_path"):
                            target["file_path"] = _format_fp(node["file_path"])

            seen_fqns_base: set[str] = set()
            sem_added = 0
            # Phase B-1: fetch method bodies for enriched hits (have line numbers)
            for target in semantic_hits:
                if (
                    target.get("fqn") not in seen_fqns_base
                    and target.get("start_line") is not None
                ):
                    seen_fqns_base.add(target["fqn"])
                    s = fetcher.fetch_method(target)
                    if s:
                        code_snippets.append(s)
                        sem_added += 1
                if sem_added >= 5 or len(code_snippets) >= 12:
                    break

            # Phase B-2: for semantic hits NOT enriched (no line numbers from Neo4j),
            # try grepping for the method name to find its declaration.
            # This catches methods whose FQN with full signature doesn't match
            # Neo4j exactly but whose simple name appears in production code.
            if sem_added < 3 and len(code_snippets) < 12:
                from reasoning.lexical_search import LexicalSearcher
                _lex_sem = LexicalSearcher()
                _seen_snippet_fps: set[str] = set(
                    (s.file_path or "") for s in code_snippets
                )
                for target in semantic_hits:
                    fqn = target.get("fqn", "")
                    if fqn in seen_fqns_base:
                        continue
                    # Extract simple method name from FQN
                    # e.g. "pkg.Class.validateAudience(List<String>,String)" → "validateAudience"
                    _method_name = fqn.split("(")[0].split(".")[-1] if fqn else ""
                    if not _method_name or len(_method_name) < 4:
                        continue
                    seen_fqns_base.add(fqn)
                    _mhits = _lex_sem.search(_method_name, max_hits=5)
                    for mh in _mhits:
                        _mnorm = mh.rel_path.replace("\\", "/")
                        if ("src/main/java" in _mnorm and "Test" not in _mnorm
                                and _mnorm not in _seen_snippet_fps):
                            ms = fetcher.fetch_grep_context(
                                mh.rel_path, mh.line_number, context_lines=25,
                            )
                            if ms:
                                ms.context_note = f"semantic hit: {_method_name}"
                                code_snippets.append(ms)
                                _seen_snippet_fps.add(_mnorm)
                                sem_added += 1
                                break  # one body per method name
                    if sem_added >= 5 or len(code_snippets) >= 12:
                        break

    elif is_semantic and semantic_hits:
        # Non-capability semantic route: enrich semantic hits + always keyword grep
        sem_fqns = [t["fqn"] for t in semantic_hits if t.get("fqn")]
        if sem_fqns:
            from graph.sqlite_retriever import SqliteRetriever as GraphRetriever
            _enricher2 = GraphRetriever(db_path=settings.sqlite_db_path)
            try:
                enriched_nodes2 = _enricher2.find_nodes_by_fqns(sem_fqns[:15])
            finally:
                _enricher2.close()
            fqn_to_node2 = {n["fqn"]: n for n in enriched_nodes2}
            for target in semantic_hits:
                if target.get("fqn") in fqn_to_node2:
                    node = fqn_to_node2[target["fqn"]]
                    target.setdefault("start_line", node.get("start_line"))
                    target.setdefault("end_line",   node.get("end_line"))
                    if not target.get("file_path") and node.get("file_path"):
                        target["file_path"] = _format_fp(node["file_path"])

        seen_fqns_base2: set[str] = set()
        for target in semantic_hits:
            if (
                target.get("fqn") not in seen_fqns_base2
                and target.get("start_line") is not None
            ):
                seen_fqns_base2.add(target["fqn"])
                s = fetcher.fetch_method(target)
                if s:
                    code_snippets.append(s)
            if len(code_snippets) >= 4:
                break
        # ALWAYS supplement with keyword grep — not just a fallback.
        # Keyword grep finds the most directly relevant code (exact term match)
        # which semantic search may rank lower.
        if len(code_snippets) < 6:
            from reasoning.lexical_search import LexicalSearcher
            _lex_fb = LexicalSearcher()
            _seen_fb: set[str] = set()
            _seen_fp_lines: set[str] = set(
                f"{s.file_path}:{s.start_line}" for s in code_snippets
            )
            for term in _kw_search_terms:
                if term in _seen_fb or len(term) < 3:
                    continue
                _seen_fb.add(term)
                _fhits = _lex_fb.search(term, max_hits=10)
                for hit in _fhits:
                    _norm_fb = hit.rel_path.replace("\\", "/")
                    _fp_key = f"{_norm_fb}:{hit.line_number}"
                    if ("src/main/java" in _norm_fb and "Test" not in _norm_fb
                            and _fp_key not in _seen_fp_lines):
                        s = fetcher.fetch_grep_context(hit.rel_path, hit.line_number, context_lines=15)
                        if s:
                            code_snippets.append(s)
                            _seen_fp_lines.add(_fp_key)
                if len(code_snippets) >= 6:
                    break

    if query_intent == "code":
        # First: fetch full method bodies for seed nodes (from Neo4j)
        for node in route_result.seed_nodes[:4]:
            s = fetcher.fetch_method(node)
            if s:
                code_snippets.append(s)

        # If no seed nodes found (partial name / parameter mismatch in Neo4j),
        # grep the method name directly in the mirror to find the file, then fetch
        if not code_snippets and route_result.entity_names:
            from reasoning.lexical_search import LexicalSearcher
            lex = LexicalSearcher()
            for ename in route_result.entity_names[:3]:
                # Only grep method-like names (lowerCamelCase)
                if ename[0].islower() and any(c.isupper() for c in ename[1:]):
                    grep_hits = lex.search(ename, max_hits=5)
                    prod_hits = [h for h in grep_hits if "src/main/java" in h.rel_path
                                 and "Test" not in h.rel_path]
                    for hit in prod_hits[:2]:
                        # Look for the method declaration line specifically
                        s = fetcher.fetch_grep_context(
                            hit.rel_path, hit.line_number, context_lines=30
                        )
                        if s:
                            code_snippets.append(s)
                if code_snippets:
                    break

        # Also fetch context around grep hits (for symbolic queries with code intent)
        for hit in (route_result.grep_hits or [])[:3]:
            s = fetcher.fetch_grep_context(hit.rel_path, hit.line_number, context_lines=8)
            if s:
                code_snippets.append(s)

    elif query_intent == "capability":
        # Capability intent on semantic route: grep already ran in STEP 4c Phase A.
        # Just add any additional graph/seed-node snippets missed by grep.
        _cap_seen: set[str] = set(c.file_path + str(c.start_line) for c in code_snippets)
        for node in route_result.seed_nodes[:3]:
            s = fetcher.fetch_method(node)
            if s and (s.file_path + str(s.start_line)) not in _cap_seen:
                code_snippets.append(s)
                _cap_seen.add(s.file_path + str(s.start_line))

    elif route_result.grep_hits and query_intent in ("safety", "impact"):
        # For safety/impact queries, show the matching lines with context
        for hit in (route_result.grep_hits or [])[:5]:
            _norm_si = hit.rel_path.replace("\\", "/")
            if "src/main/java" in _norm_si:
                s = fetcher.fetch_grep_context(hit.rel_path, hit.line_number, context_lines=3)
                if s:
                    code_snippets.append(s)


    # ── STEP 5: Reduce (single LLM call) ──────────────────────────────────────
    reduce = ReduceStep(llm)
    from reasoning.map_step import MapResult
    reduce_inputs = (
        [MapResult(community_id=s["community_id"], score=100,
                   reason="deterministic hit", summary_text=s["summary_text"])
         for s in summaries_for_reduce]
        if not is_semantic
        # For semantic/question mode, ChromaDB cosine-distance scores top out at ~57 —
        # the LLM score threshold (70) is wrong here.  Pass all map results; the
        # reduce step's _build_summaries_text already filters score >= 50 internally.
        else map_results
    )

    review = reduce.run(
        reduce_inputs,
        query=question,
        primary_targets=primary_targets,
        code_snippets=code_snippets if code_snippets else None,
    )
    t_total = time.perf_counter() - t0

    # ── PRINT RESULTS ──────────────────────────────────────────────────────────

    # Show code snippets for any intent that fetched them (code, capability, general...)
    if code_snippets:
        _snippet_label = {
            "code":       "📜  SOURCE CODE  (read directly from repository mirror)",
            "capability": "📜  CODE EVIDENCE  (used to assess capability)",
            "safety":     "📜  CODE CONTEXT  (around affected symbol)",
            "impact":     "📜  CODE CONTEXT  (around affected area)",
        }
        print(f"\n{'─' * 72}")
        print(f"  {_snippet_label.get(query_intent, '📜  CODE EVIDENCE')}")
        print(f"{'─' * 72}")
        for snip in code_snippets:
            note = f"  ({snip.context_note})" if snip.context_note else ""
            print(f"\n  📄 {snip.file_path}  —  {snip.fqn}{note}")
            print(f"  ┌{'─' * 68}┐")
            for line in snip.code.split("\n"):
                print(f"  │ {line}")
            print(f"  └{'─' * 68}┘")

    # Seed / direct change targets
    if route_result.seed_nodes:
        print(f"{'─' * 72}")
        print("  ✏️   DIRECT MATCHES IN GRAPH  (exact Neo4j entity lookup)")
        print(f"{'─' * 72}")
        by_file: dict[str, list[str]] = defaultdict(list)
        for n in route_result.seed_nodes:
            by_file[_format_fp(n.get("file_path", ""))].append(n["fqn"])
        for fp, fqns in sorted(by_file.items()):
            print(f"\n  📄 {fp}")
            for fqn in sorted(set(fqns)):
                print(f"     ↳ {fqn}")

    # Deterministic affected nodes (callers/users of the changed entity)
    if route_result.affected_nodes:
        print(f"\n{'─' * 72}")
        print("  💥  AFFECTED BY GRAPH TRAVERSAL  (CALLS / INJECTS / DEPENDS_ON / INSTANTIATES)")
        print(f"  ↳  These nodes directly reference the entity via real graph edges")
        print(f"{'─' * 72}")
        by_type: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
        for n in route_result.affected_nodes:
            by_type[n.get("edge_type", "unknown")][_format_fp(n.get("file_path", ""))].append(n["fqn"])
        edge_labels = {"CALLS": "📞 CALLERS", "INJECTS": "💉 INJECTORS", "DEPENDS_ON": "🔗 DEPENDENTS", "INSTANTIATES": "🏗  INSTANTIATORS"}
        for etype, files in by_type.items():
            print(f"\n  {edge_labels.get(etype, etype)}")
            for fp, fqns in sorted(files.items()):
                unique = sorted(set(fqns))
                print(f"    📄 {fp}")
                for fqn in unique[:4]:
                    print(f"       ↳ {fqn}")
                if len(unique) > 4:
                    print(f"       ↳ ... +{len(unique)-4} more")
        total = len(set(n["fqn"] for n in route_result.affected_nodes))
        print(f"\n  → {total} unique methods directly affected via graph edges")

    # Semantic hits (for 'files to change' when no graph hit)
    elif semantic_hits:
        print(f"\n{'─' * 72}")
        print("  ✏️   FILES TO CHANGE  — semantic code search (fallback)")
        print(f"{'─' * 72}")
        seen: set[str] = set()
        for h in semantic_hits:
            fp = h["file_path"]
            if fp not in seen:
                print(f"\n  📄 {fp}")
                seen.add(fp)
            print(f"     ↳ {h['fqn']}")

    # Map scores (semantic route only)
    if map_results:
        print(f"\n{'─' * 72}")
        print("  COMMUNITY SCORES (top 8)")
        print(f"{'─' * 72}")
        for r in map_results[:8]:
            bar = "█" * (r.score // 10) + "░" * (10 - r.score // 10)
            label = "HIGH" if r.score >= 70 else ("MED" if r.score >= 30 else "LOW")
            print(f"  [{bar}] {r.score:3d}/100  {label}  {r.reason[:55]}")

    # Timing summary
    print(f"\n{'─' * 72}")
    if route_result.route == "deterministic":
        print(f"  ⏱  Route: {t_route:.1f}s  |  Retrieval: {t_retrieval:.1f}s  |  Reduce: {t_total - t_retrieval:.1f}s  |  Total: {t_total:.1f}s")
    else:
        print(f"  ⏱  Route+Map: {t_retrieval:.1f}s  |  Reduce: {t_total - t_retrieval:.1f}s  |  Total: {t_total:.1f}s")

    # Architectural review
    print(f"\n{'━' * 72}")
    print("  📋  ARCHITECTURAL REVIEW")
    print(f"{'━' * 72}\n")
    print(review)
    print(f"\n{'━' * 72}\n")
    return review


# ── Shared display helpers ─────────────────────────────────────────────────────

def _print_code_box(snip) -> None:
    """Print a single CodeSnippet in a labelled box."""
    note = f"  ({snip.context_note})" if snip.context_note else ""
    print(f"\n  📄 {snip.file_path}")
    print(f"  FQN: {snip.fqn}{note}   lines {snip.start_line}–{snip.end_line}")
    print(f"  ┌{'─' * 68}┐")
    for line in snip.code.split("\n"):
        print(f"  │ {line}")
    print(f"  └{'─' * 68}┘")


def _connect() -> tuple:
    """Return (chroma_client, llm_client, SqliteRetriever, CodeFetcher)."""
    import chromadb
    from openai import OpenAI
    from graph.sqlite_retriever import SqliteRetriever
    from reasoning.code_fetcher import CodeFetcher
    chroma  = chromadb.HttpClient(host=settings.chroma_host, port=settings.chroma_port)
    llm     = settings.make_llm_client()
    gr      = SqliteRetriever(db_path=settings.sqlite_db_path, chroma_client=chroma)
    fetcher = CodeFetcher()
    return chroma, llm, gr, fetcher


# ── explain ───────────────────────────────────────────────────────────────────

def cmd_explain(name: str, depth: int = 2) -> None:
    """
    Explain what a method or class does.

    Usage:
        python main.py explain validateActorToken
        python main.py explain org.wso2....TokenExchangeGrantHandler
    """
    from reasoning.reduce_step import ReduceStep

    print(f"\n{'━' * 72}")
    print(f"  📖  EXPLAIN  {name}")
    print(f"{'━' * 72}\n")

    t0 = time.perf_counter()
    chroma, llm, gr, fetcher = _connect()

    try:
        # 1. Find seed node(s)
        nodes = gr.find_nodes(name)
        if not nodes:
            print(f"  ✗  No node found for '{name}' in the graph.")
            print(f"     Try: python main.py find {name}")
            return

        node = nodes[0]   # take best match
        fqn  = node["fqn"]
        print(f"  ✓  Resolved: {fqn}")
        print(f"     File: {_format_fp(node.get('file_path',''))}\n")

        # 2. Fetch the method body
        snippets = []
        s = fetcher.fetch_method(node)
        if s:
            snippets.append(s)

        # 3. Callers (who calls this, up to `depth` hops)
        callers = gr.get_callers(fqn, depth=depth)[:12]

        # 4. Callees (what this calls, depth=1)
        callees = gr.get_callees(fqn, depth=1)[:8]

        # 5. Fetch source for top 3 callers so the LLM can cite them
        caller_snippets: list = []
        for c in callers[:3]:
            cs = fetcher.fetch_method(c)
            if cs:
                caller_snippets.append(cs)

    finally:
        gr.close()

    t_fetch = time.perf_counter() - t0

    # 6. Print the source of the target method
    if snippets:
        print(f"{'─' * 72}")
        print(f"  📜  SOURCE CODE")
        print(f"{'─' * 72}")
        for snip in snippets:
            _print_code_box(snip)

    # 7. Print caller list
    if callers:
        print(f"\n{'─' * 72}")
        print(f"  📞  CALLED BY  ({len(callers)} callers, depth {depth})")
        print(f"{'─' * 72}")
        by_file: dict[str, list[str]] = defaultdict(list)
        for c in callers:
            by_file[_format_fp(c.get("file_path",""))].append(
                f"{c['fqn']}  (hop {c.get('hop', '?')})"
            )
        for fp, fqns in sorted(by_file.items()):
            print(f"  📄 {fp}")
            for f in fqns[:4]:
                print(f"     ↳ {f}")

    # 8. Print callee list
    if callees:
        print(f"\n{'─' * 72}")
        print(f"  🔻  CALLS  ({len(callees)} callees)")
        print(f"{'─' * 72}")
        by_file2: dict[str, list[str]] = defaultdict(list)
        for c in callees:
            by_file2[_format_fp(c.get("file_path",""))].append(c["fqn"])
        for fp, fqns in sorted(by_file2.items()):
            print(f"  📄 {fp}")
            for f in fqns[:4]:
                print(f"     ↳ {f}")

    # 9. LLM explanation
    print(f"\n{'─' * 72}")
    print(f"  ⏳  Generating explanation…")
    reduce = ReduceStep(llm)
    explanation = reduce.explain(
        fqn=fqn,
        code_snippets=snippets + caller_snippets,
        callers=callers,
        callees=callees,
    )
    t_total = time.perf_counter() - t0

    print(f"\n{'━' * 72}")
    print(f"  📋  EXPLANATION")
    print(f"{'━' * 72}\n")
    print(explanation)
    print(f"\n{'─' * 72}")
    print(f"  ⏱  fetch: {t_fetch:.1f}s  |  LLM: {t_total - t_fetch:.1f}s  |  total: {t_total:.1f}s")
    print(f"{'━' * 72}\n")


# ── trace ─────────────────────────────────────────────────────────────────────

def cmd_trace(name: str, depth: int = 2) -> None:
    """
    Show the full call chain around a method: callers above, callees below.
    Prints actual code for each node. No LLM — pure deterministic.

    Usage:
        python main.py trace validateActorToken
        python main.py trace validateActorToken --depth 3
    """
    print(f"\n{'━' * 72}")
    print(f"  🕸   CALL CHAIN  {name}")
    print(f"{'━' * 72}\n")

    t0 = time.perf_counter()
    _, _, gr, fetcher = _connect()

    try:
        nodes = gr.find_nodes(name)
        if not nodes:
            print(f"  ✗  '{name}' not found. Try: python main.py find {name}")
            return

        node = nodes[0]
        fqn  = node["fqn"]
        print(f"  ◉  {fqn}")
        print(f"     {_format_fp(node.get('file_path', ''))}\n")

        callers = gr.get_callers(fqn, depth=depth)
        callees = gr.get_callees(fqn, depth=depth)
    finally:
        gr.close()

    # Print call tree (ASCII, grouped by hop)
    if callers:
        print(f"{'─' * 72}")
        print(f"  ▲  CALLERS  (who calls {name})")
        print(f"{'─' * 72}")
        max_hop = max(c.get("hop", 1) for c in callers)
        for hop in range(1, max_hop + 1):
            hop_nodes = [c for c in callers if c.get("hop") == hop]
            indent = "    " * hop
            label = "direct" if hop == 1 else f"{hop} hops away"
            print(f"\n  {indent}── hop {hop} ({label}) ──")
            for c in hop_nodes[:8]:
                fp   = _format_fp(c.get("file_path", ""))
                print(f"  {indent}↑  {c['fqn']}")
                print(f"  {indent}   {fp}")
            if len(hop_nodes) > 8:
                print(f"  {indent}   … +{len(hop_nodes)-8} more")

    print(f"\n  ━━━━ ◉ {name} ━━━━")

    if callees:
        print(f"\n{'─' * 72}")
        print(f"  ▼  CALLEES  (what {name} calls)")
        print(f"{'─' * 72}")
        max_hop = max(c.get("hop", 1) for c in callees)
        for hop in range(1, max_hop + 1):
            hop_nodes = [c for c in callees if c.get("hop") == hop]
            indent = "    " * hop
            print(f"\n  {indent}── hop {hop} ──")
            for c in hop_nodes[:8]:
                print(f"  {indent}↓  {c['fqn']}")
                print(f"  {indent}   {_format_fp(c.get('file_path',''))}")
            if len(hop_nodes) > 8:
                print(f"  {indent}   … +{len(hop_nodes)-8} more")

    # Print the target method source
    print(f"\n{'─' * 72}")
    print(f"  📜  SOURCE OF  {name}")
    print(f"{'─' * 72}")
    s = fetcher.fetch_method(node)
    if s:
        _print_code_box(s)
    else:
        print(f"  (source not available in mirror)")

    t_total = time.perf_counter() - t0
    print(f"\n{'─' * 72}")
    print(f"  ⏱  {t_total:.1f}s  |  {len(callers)} callers  |  {len(callees)} callees")
    print(f"{'━' * 72}\n")


# ── callers ───────────────────────────────────────────────────────────────────

def cmd_callers(name: str, depth: int = 1, show_code: bool = False) -> None:
    """
    Show every method that calls this one, with optional source code.

    Usage:
        python main.py callers validateActorToken
        python main.py callers validateActorToken --depth 2 --code
    """
    print(f"\n{'━' * 72}")
    print(f"  📞  CALLERS OF  {name}  (depth {depth})")
    print(f"{'━' * 72}\n")

    t0 = time.perf_counter()
    _, _, gr, fetcher = _connect()
    try:
        nodes = gr.find_nodes(name)
        if not nodes:
            print(f"  ✗  '{name}' not found.")
            return
        fqn     = nodes[0]["fqn"]
        callers = gr.get_callers(fqn, depth=depth)
    finally:
        gr.close()

    if not callers:
        print(f"  (no callers found in graph for '{fqn}')")
        return

    print(f"  ◉  Target:  {fqn}")
    print(f"  Found {len(callers)} caller(s)\n")

    by_file: dict[str, list] = defaultdict(list)
    for c in callers:
        by_file[_format_fp(c.get("file_path",""))].append(c)

    for fp, caller_nodes in sorted(by_file.items()):
        print(f"{'─' * 72}")
        print(f"  📄 {fp}")
        for c in caller_nodes:
            print(f"     ↳ {c['fqn']}  (hop {c.get('hop','?')})")
            if show_code:
                s = fetcher.fetch_method(c)
                if s:
                    _print_code_box(s)

    t_total = time.perf_counter() - t0
    print(f"\n{'─' * 72}")
    print(f"  ⏱  {t_total:.1f}s  |  tip: add --code to see source at each call site")
    print(f"{'━' * 72}\n")


# ── find ──────────────────────────────────────────────────────────────────────

def cmd_find(term: str, context_lines: int = 6, max_hits: int = 30) -> None:
    """
    Exact text search across the entire mirror. Shows code context. No LLM.

    Usage:
        python main.py find ACTOR_TOKEN_REQUIRED
        python main.py find "throw new OAuthSystemException"
        python main.py find impersonation --context 10
    """
    from reasoning.lexical_search import LexicalSearcher

    print(f"\n{'━' * 72}")
    print(f"  🔎  FIND  '{term}'")
    print(f"{'━' * 72}\n")

    t0 = time.perf_counter()
    lex     = LexicalSearcher()
    fetcher_f = __import__("reasoning.code_fetcher", fromlist=["CodeFetcher"]).CodeFetcher()
    hits    = lex.search(term, max_hits=max_hits)

    if not hits:
        print(f"  (no matches found in mirror)")
        return

    prod  = [h for h in hits if "src/main/java" in h.rel_path and "Test" not in h.rel_path]
    tests = [h for h in hits if "src/test/java" in h.rel_path or "Test" in h.rel_path]
    other = [h for h in hits if h not in prod and h not in tests]

    print(f"  {len(prod)} production  |  {len(tests)} test  |  {len(other)} other\n")

    for category, cat_hits, icon in [
        ("PRODUCTION", prod, "🔴"),
        ("TEST",       tests,"🟡"),
        ("OTHER",      other,"⚪"),
    ]:
        if not cat_hits:
            continue
        print(f"{'─' * 72}")
        print(f"  {icon}  {category} ({len(cat_hits)} hits)")
        print(f"{'─' * 72}")
        for h in cat_hits:
            print(f"\n  📄 {h.rel_path}  line {h.line_number}")
            print(f"  ┄  {h.line_text.strip()}")
            if context_lines > 0:
                snip = fetcher_f.fetch_grep_context(h.rel_path, h.line_number, context_lines)
                if snip:
                    for line in snip.code.split("\n"):
                        print(f"      {line}")

    t_total = time.perf_counter() - t0
    print(f"\n{'─' * 72}")
    print(f"  ⏱  {t_total:.1f}s  |  {len(hits)} total matches")
    print(f"{'━' * 72}\n")


# ── debug ─────────────────────────────────────────────────────────────────────

def cmd_debug(error_input: str, context_lines: int = 10) -> None:
    """
    Root-cause analysis from an exception name or pasted stack trace.
    Fetches source at each frame and asks the LLM to diagnose + fix.

    Usage:
        python main.py debug NullPointerException
        python main.py debug "$(cat stacktrace.txt)"
        python main.py debug -          (reads stack trace from stdin)
    """
    from reasoning.lexical_search import LexicalSearcher
    from reasoning.reduce_step import ReduceStep

    # Read from stdin if "-" passed
    if error_input.strip() == "-":
        print("  Paste your stack trace, then press Ctrl+Z (Windows) or Ctrl+D (Unix):\n")
        error_input = sys.stdin.read()

    # ── Parse the input ───────────────────────────────────────────────────────
    lines = error_input.strip().splitlines()

    # Extract exception class: first line usually "ExceptionClass: message"
    # or "Caused by: ExceptionClass: message"
    exc_class = ""
    for ln in lines[:5]:
        m = re.match(r"(?:Caused by:\s*)?([A-Za-z][\w$.]+(?:Exception|Error|Fault|Throwable))[:\s]", ln)
        if m:
            exc_class = m.group(1)
            break
    if not exc_class:
        # Treat the whole input as the exception class or query term
        exc_class = lines[0].strip() if lines else error_input.strip()

    # Extract "at ClassName.method(File.java:line)" frames
    frame_re = re.compile(r"\s+at\s+([\w$.]+)\.([\w<>$]+)\((\w+\.java):(\d+)\)")
    frames: list[dict] = []
    for ln in lines:
        m = frame_re.match(ln)
        if m:
            frames.append({
                "class_fqn": m.group(1),
                "method":    m.group(2),
                "file":      m.group(3),
                "line":      int(m.group(4)),
            })

    # Keep only frames that are in the mirror (wso2 packages)
    mirror_frames = [
        f for f in frames
        if any(pkg in f["class_fqn"] for pkg in ("wso2", "carbon", "identity"))
    ][:6]

    print(f"\n{'━' * 72}")
    print(f"  🐛  DEBUG  {exc_class}")
    print(f"{'━' * 72}\n")
    print(f"  Exception : {exc_class}")
    print(f"  Frames    : {len(frames)} total  |  {len(mirror_frames)} in mirror\n")

    t0 = time.perf_counter()
    _, llm, gr, fetcher = _connect()
    lex = LexicalSearcher()

    code_snippets = []
    grep_evidence: list[dict] = []

    try:
        # ── 1. Grep for throw sites of this exception ─────────────────────────
        throw_term = f"throw new {exc_class.split('.')[-1]}"
        throw_hits = lex.search(throw_term, max_hits=15)
        prod_throws = [h for h in throw_hits if "src/main/java" in h.rel_path and "Test" not in h.rel_path]
        for h in prod_throws[:4]:
            s = fetcher.fetch_grep_context(h.rel_path, h.line_number, context_lines=8)
            if s:
                code_snippets.append(s)
            grep_evidence.append({
                "fqn":       h.rel_path,
                "file_path": h.rel_path,
                "source":    "grep",
                "edge_type": f"line {h.line_number}",
                "text":      h.line_text[:150],
            })

        # ── 2. Fetch source at each stack frame ───────────────────────────────
        frame_summary_lines: list[str] = []
        for f in mirror_frames:
            frame_summary_lines.append(
                f"  at {f['class_fqn']}.{f['method']}({f['file']}:{f['line']})"
            )
            # Try to enrich from graph (get start/end line for the enclosing method)
            frame_nodes = gr.find_node_by_location(f["file"], f["line"])
            if frame_nodes:
                node = frame_nodes[0]
                s = fetcher.fetch_method(node)
                if s:
                    code_snippets.append(s)
            else:
                # Fall back to a raw context window
                s = fetcher.fetch_grep_context(f["file"], f["line"], context_lines=context_lines)
                if s:
                    code_snippets.append(s)

        # ── 3. If no frames parsed, grep for the exception class name itself ──
        if not mirror_frames:
            hits = lex.search(exc_class.split(".")[-1], max_hits=10)
            for h in [x for x in hits if "src/main/java" in x.rel_path][:3]:
                s = fetcher.fetch_grep_context(h.rel_path, h.line_number, context_lines=8)
                if s:
                    code_snippets.append(s)

    finally:
        gr.close()

    t_fetch = time.perf_counter() - t0

    # ── Print what we gathered ────────────────────────────────────────────────
    if code_snippets:
        print(f"{'─' * 72}")
        print(f"  📜  CODE EVIDENCE  ({len(code_snippets)} snippets)")
        print(f"{'─' * 72}")
        for snip in code_snippets:
            _print_code_box(snip)

    # ── LLM diagnosis ─────────────────────────────────────────────────────────
    print(f"\n{'─' * 72}")
    print(f"  ⏳  Diagnosing…")
    reduce = ReduceStep(llm)
    diagnosis = reduce.debug_error(
        error=error_input[:500],
        stack_frames="\n".join(frame_summary_lines) if frame_summary_lines else error_input[:800],
        code_snippets=code_snippets,
        grep_evidence=grep_evidence,
    )
    t_total = time.perf_counter() - t0

    print(f"\n{'━' * 72}")
    print(f"  🩺  DIAGNOSIS & FIX")
    print(f"{'━' * 72}\n")
    print(diagnosis)
    print(f"\n{'─' * 72}")
    print(f"  ⏱  fetch: {t_fetch:.1f}s  |  LLM: {t_total - t_fetch:.1f}s  |  total: {t_total:.1f}s")
    print(f"{'━' * 72}\n")


# ── interactive REPL ──────────────────────────────────────────────────────────

def cmd_interactive(candidates: int = 20, threshold: int = 70,
                    depth: int = 3, repo: str | None = None):
    """Interactive query REPL — type questions continuously, Ctrl+C or 'exit' to quit."""
    banner = """
╔══════════════════════════════════════════════════════════════════╗
║          CodeNexus — Interactive Query Mode                      ║
║  Type a question and press Enter.  'exit' or Ctrl+C to quit.    ║
╚══════════════════════════════════════════════════════════════════╝
"""
    print(banner)
    while True:
        try:
            question = input("  nexus> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n\n  Goodbye.\n")
            break
        if not question:
            continue
        if question.lower() in ("exit", "quit", "q", ":q"):
            print("\n  Goodbye.\n")
            break
        try:
            query(question, candidates=candidates, threshold=threshold,
                  depth=depth, repo=repo)
        except KeyboardInterrupt:
            print("\n  (interrupted — press Ctrl+C again or type 'exit' to quit)\n")
        except Exception as exc:  # noqa: BLE001
            print(f"\n  [error] {exc}\n")


# ── argparse ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="CodeNexus — GraphRAG knowledge base for WSO2 Java repos",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Commands:
  ingest                  Ingest repositories into the knowledge base
  query   <question>      Ask a free-form question (semantic + graph reasoning)
  interactive             Start an interactive REPL (alias: chat)
  explain <name>          Explain what a method/class does (with source + callers)
  trace   <name>          Show full call chain above and below a method
  callers <name>          List every method that calls this one
  find    <term>          Exact text search across the mirror (no LLM)
  debug   <error>         Root-cause analysis from an exception or stack trace

Examples:
  python main.py interactive
  python main.py query   "is impersonation supported without an actor token"
  python main.py explain validateActorToken
  python main.py trace   TokenExchangeGrantHandler --depth 2
  python main.py callers isImpersonationRequest --code
  python main.py find    "throw new OAuthSystemException"
  python main.py debug   NullPointerException
  python main.py debug   "$(cat trace.txt)"
""",
    )
    sub = parser.add_subparsers(dest="command")

    # ingest
    sub.add_parser("ingest", help="Run the full ingestion pipeline")

    # query
    qp = sub.add_parser("query", help="Free-form question against the knowledge base")
    qp.add_argument("question", type=str)
    qp.add_argument("--candidates", type=int, default=20)
    qp.add_argument("--threshold",  type=int, default=70)
    qp.add_argument("--depth",      type=int, default=3)
    qp.add_argument("--repo",       type=str, default=None,
                    help="Scope query to a specific repository by name (e.g. identity-inbound-auth-oauth)")

    # explain
    ep = sub.add_parser("explain", help="Explain what a method or class does")
    ep.add_argument("name",    type=str, help="Method/class name or FQN")
    ep.add_argument("--depth", type=int, default=2, help="Caller search depth (default 2)")

    # trace
    tp = sub.add_parser("trace", help="Show full call chain around a method")
    tp.add_argument("name",    type=str)
    tp.add_argument("--depth", type=int, default=2)

    # callers
    cp = sub.add_parser("callers", help="List every method that calls this one")
    cp.add_argument("name",    type=str)
    cp.add_argument("--depth", type=int, default=1)
    cp.add_argument("--code",  action="store_true", help="Show source code at each call site")

    # find
    fp = sub.add_parser("find", help="Exact text search across mirror (no LLM)")
    fp.add_argument("term",      type=str)
    fp.add_argument("--context", type=int, default=6,  dest="context_lines",
                    help="Lines of context around each match (default 6)")
    fp.add_argument("--max",     type=int, default=30, dest="max_hits",
                    help="Max total hits to show (default 30)")

    # debug
    dp = sub.add_parser("debug", help="Root-cause analysis from exception or stack trace")
    dp.add_argument("error",     type=str,
                    help="Exception class name, error message, stack trace, or '-' to read from stdin")
    dp.add_argument("--context", type=int, default=10, dest="context_lines",
                    help="Lines of context around each relevant line (default 10)")

    # interactive / chat
    ip = sub.add_parser("interactive", aliases=["chat"],
                        help="Start an interactive REPL to ask questions continuously")
    ip.add_argument("--candidates", type=int, default=20)
    ip.add_argument("--threshold",  type=int, default=70)
    ip.add_argument("--depth",      type=int, default=3)
    ip.add_argument("--repo",       type=str, default=None,
                    help="Scope all queries to a specific repository by name")

    args = parser.parse_args()

    if args.command == "ingest":
        ingest()
    elif args.command == "query":
        query(args.question, candidates=args.candidates, threshold=args.threshold,
              depth=args.depth, repo=args.repo)
    elif args.command == "explain":
        cmd_explain(args.name, depth=args.depth)
    elif args.command == "trace":
        cmd_trace(args.name, depth=args.depth)
    elif args.command == "callers":
        cmd_callers(args.name, depth=args.depth, show_code=args.code)
    elif args.command == "find":
        cmd_find(args.term, context_lines=args.context_lines, max_hits=args.max_hits)
    elif args.command == "debug":
        cmd_debug(args.error, context_lines=args.context_lines)
    elif args.command in ("interactive", "chat"):
        cmd_interactive(candidates=args.candidates, threshold=args.threshold,
                        depth=args.depth, repo=args.repo)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
