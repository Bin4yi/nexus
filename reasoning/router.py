"""
reasoning/router.py
Hybrid Deterministic Router — the query entry point for CodeNexus.

Architectural role:
    Separates every inbound question into one of three determinism tiers
    **before** any LLM is invoked, guaranteeing zero hallucinations for
    symbolic and exact-entity queries.

FOUR ROUTES:
──────────────────────────────────────────────────────────────────────────────
ROUTE A — SYMBOLIC  (e.g. "Can I remove IMPERSONATED_SUBJECT?")
    question → ``QueryClassifier`` detects UPPER_SNAKE_CASE / quoted literal
    → ripgrep / git-grep on mirror → exact file:line hits
    → ``grep_to_graph_bridge`` (line range → Neo4j LogicUnit)
    → ``compute_blast_radius`` traversing [:CALLS], [:DEPENDS_ON],
      [:INJECTS], [:INSTANTIATES]
    → ChromaDB ``get_by_id`` (community summaries) → Reduce

ROUTE B — EXACT-ENTITY  (e.g. "What calls TokenExchangeGrantHandler?")
    question → CamelCase entity extracted
    → Neo4j direct node lookup → ``compute_blast_radius`` → Reduce

ROUTE C — CONCEPTUAL  (e.g. "How does OAuth token refresh work?")
    question → no entity found → ChromaDB vector search → Map → Reduce

ROUTE D — GLOBAL  (e.g. "What is the overarching architecture?")
    question → ``QueryClassifier`` detects global/architecture keywords
    → directly returns Level 3 Global Architecture summary from ChromaDB
    → skips all search — pure retrieval of pre-generated master document
──────────────────────────────────────────────────────────────────────────────
Routes A & B are 100 % deterministic — the LLM receives only mathematically
proven graph data.  Route C uses probabilistic vector search, appropriate
for open-ended architectural questions.  Route D is instantaneous — returns
the pre-computed Microsoft GraphRAG Level 3 global summary.
"""
from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from config.settings import settings, llm_chat
from graph.sqlite_retriever import SqliteRetriever
from reasoning.lexical_search import LexicalSearcher, GrepHit
from community.summarizer import COLLECTION_NAME as COMMUNITY_COLLECTION
from community.global_rollup import GlobalRollup

logger = logging.getLogger(__name__)


# ── Data classes ──────────────────────────────────────────────────────────────


@dataclass
class QueryClassification:
    """Output of the classifier — no DB I/O, fully unit-testable."""
    bucket: str          # "symbolic" | "exact" | "conceptual" | "global"
    confidence: float    # 0.0–1.0
    symbols: list[str]   # UPPER_SNAKE / quoted literals for grep
    entity_names: list[str]  # CamelCase / FQN candidates
    clean_query: str = ""    # typo-corrected query for semantic search (LLM parser only)
    intent: str = ""         # "safety" | "code" | "narrative" | "impact" | "capability" | "general"


@dataclass
class RouterResult:
    """Full retrieval payload passed to the Map-Reduce pipeline."""
    route: str                         # "symbolic" | "exact" | "semantic" | variants
    grep_hits: list[GrepHit]           # raw grep results (Route A only)
    seed_nodes: list[dict]             # Neo4j seed nodes
    affected_nodes: list[dict]         # graph-traversal blast-radius hits
    community_ids: list[int]           # deterministic community IDs
    community_summaries: list[dict]    # fetched by ID (deterministic) or by vector
    entity_names: list[str]            # extracted candidate names
    symbols: list[str]                 # symbols passed to grep (Route A)
    intent: str = "general"            # LLM-classified answer intent


# ── Query classifier (pure logic, no DB) ─────────────────────────────────────


class QueryClassifier:
    """
    Stateless classifier that buckets a natural-language question into one of
    three categories.  **No** database or network calls — safe to call from
    unit tests.

    Classification heuristic (in priority order):
        1. UPPER_SNAKE_CASE constants or quoted literals → **symbolic**
        2. CamelCase / lowerCamelCase / dotted FQN tokens → **exact**
        3. Everything else → **conceptual**
    """

    # Global architecture question keywords
    _GLOBAL_KEYWORDS = [
        "overarching architecture", "overall architecture", "global architecture",
        "big picture", "full architecture", "entire system", "whole system",
        "system overview", "architecture overview", "high-level architecture",
        "complete architecture", "architectural overview", "how does the system work",
        "what does the system do", "describe the architecture",
        "how is the system structured", "codebase overview",
    ]

    def classify(self, question: str) -> QueryClassification:
        """Return a ``QueryClassification`` for *question*."""
        entity_names = self._extract_entity_names(question)

        # ── Global architecture signals (ROUTE D) ─────────────────────────
        q_lower = question.lower()
        for keyword in self._GLOBAL_KEYWORDS:
            if keyword in q_lower:
                return QueryClassification("global", 0.95, [], entity_names)

        # ── Symbolic signals ──────────────────────────────────────────────
        upper_snake = re.findall(r'\b[A-Z][A-Z0-9_]{2,}\b', question)
        quoted = re.findall(r'["\']([A-Za-z_][A-Za-z0-9_]{2,})["\']', question)

        # For safety queries ("can I remove X"), also detect lower_snake_case tokens
        # and promote them to symbolic route by trying the uppercase variant for grep.
        _SAFETY_QUERY_RE = re.compile(
            r'\b(can i|is it safe|safe to|should i|ok to|remove|delete|drop|unused)\b',
            re.IGNORECASE,
        )
        extra_snake = []
        if _SAFETY_QUERY_RE.search(question):
            lower_snake = re.findall(r'\b[a-z][a-z0-9_]{2,}\b', question)
            extra_snake = [t.upper() for t in lower_snake if '_' in t]

        # Promote UPPER_SNAKE_CASE candidates generated from compound phrases
        # (e.g. "may act" → "MAY_ACT") to the symbolic/grep route so claim names
        # are found via lexical search rather than fuzzy graph node lookup.
        compound_snake = [
            name for name in entity_names
            if re.match(r'^[A-Z][A-Z0-9_]{2,}$', name) and '_' in name
        ]

        # Drop candidates that look like typos (3+ consecutive identical letters, e.g. ACCCORDING)
        _typo_re = re.compile(r'(.)\1\1')
        all_symbols = upper_snake + quoted + extra_snake + compound_snake
        symbols = list(dict.fromkeys(s for s in all_symbols if not _typo_re.search(s)))

        if symbols:
            confidence = min(1.0, 0.6 + 0.1 * len(symbols))
            # Strip compound_snake from entity_names to avoid double-routing
            clean_entity_names = [n for n in entity_names if n not in compound_snake]
            return QueryClassification("symbolic", confidence, symbols, clean_entity_names)

        # ── Exact-entity signals ──────────────────────────────────────────
        if entity_names:
            # Boost confidence for deep/specific dotted FQNs.
            # A package like "org.wso2.carbon.identity.api.server.pdp" (6 dots)
            # is far more specific than a bare CamelCase token (0 dots) and
            # should take the deterministic Neo4j exact route.
            max_depth = max(e.count('.') for e in entity_names)
            confidence = min(1.0, 0.5 + 0.1 * len(entity_names) + 0.04 * max_depth)
            return QueryClassification("exact", confidence, [], entity_names)

        # ── Conceptual fallback ───────────────────────────────────────────
        return QueryClassification("conceptual", 0.3, [], [])

    # ── Private helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _extract_entity_names(question: str) -> list[str]:
        """
        Extract CamelCase / lowerCamelCase / dotted FQN candidates.
        Also converts compound noun phrases ("token exchange") to CamelCase
        ("TokenExchange") so feature-name queries hit the exact route.

        Returns a list sorted by length (longest first) so that the most
        specific match anchors graph lookups.
        """
        candidates: set[str] = set()
        # UpperCamelCase class names
        candidates.update(re.findall(r'\b[A-Z][a-z]+(?:[A-Z][a-z0-9]+)+\b', question))
        # lowerCamelCase method names
        candidates.update(re.findall(r'\b[a-z][a-z0-9]+(?:[A-Z][a-z0-9]+)+\b', question))
        # Quoted identifiers
        candidates.update(re.findall(r'["\']([A-Za-z][A-Za-z0-9_$.]+)["\']', question))
        # Dotted FQN fragments
        candidates.update(re.findall(
            r'\b[a-z][a-z0-9]+\.[a-z][a-z0-9]+(?:\.[a-z][a-z0-9]+)*\b', question,
        ))
        # Compound noun phrases: "token exchange" → "TokenExchange"
        # Converts consecutive lowercase words (2–4 words) into CamelCase candidates
        # so "token exchange grant handler" finds TokenExchangeGrantHandler.
        # 2-word phrases ALSO generate UPPER_SNAKE_CASE (e.g. "may act" → "MAY_ACT")
        # so claim/constant names get picked up by grep rather than fuzzy graph lookup.
        _STOP = frozenset([
            "the", "a", "an", "in", "of", "to", "for", "and", "or", "is",
            "are", "what", "how", "can", "this", "that", "with", "from",
            "should", "would", "will", "does", "do", "if", "when", "where",
            "files", "file", "code", "class", "method", "safely", "change",
            "changes", "introduce", "implement", "add", "use", "using",
            "flow", "mandatory", "claim", "request", "response", "grant",
            "impersonation", "oauth", "enable", "support", "check",
            # RFC / spec reference noise — "rfc", "according", version numbers etc.
            "rfc", "according", "specification", "spec", "standard", "based",
            # Generic verbs that produce junk constants (e.g. VALIDATION_HAPPNES)
            "validation", "happens", "happen", "happening", "handles", "handle",
            "performs", "perform", "process", "processes", "execute", "executes",
            "works", "runs", "checks", "triggers", "calls", "returns", "throws",
            "creates", "builds", "gets", "sets", "sends", "receives", "invokes",
        ])
        words = re.findall(r'\b[a-z][a-z0-9]+\b', question.lower())
        content_words = [w for w in words if w not in _STOP]
        for n in (4, 3, 2):
            for i in range(len(content_words) - n + 1):
                phrase = content_words[i:i + n]
                camel = "".join(w.capitalize() for w in phrase)
                if len(camel) > 5:
                    candidates.add(camel)
                # 2-word phrases → UPPER_SNAKE_CASE for constant/claim name detection
                # e.g. "may act" → "MAY_ACT", "subject token" → "SUBJECT_TOKEN"
                if n == 2:
                    snake = "_".join(w.upper() for w in phrase)
                    candidates.add(snake)
        return sorted(candidates, key=len, reverse=True)


# ── LLM-based query parser ────────────────────────────────────────────────────


class LLMQueryParser:
    """
    Replaces the regex-based QueryClassifier with a structured LLM call.

    Advantages over hardcoded heuristics:
    - Handles typos and synonyms ("acccording" → ignored, "actor tkn" → ActorTokenValidator)
    - Generates domain-aware Java constant names the regex would miss
      ("may act" → MAY_ACT, ACTOR_TOKEN_TYPE; "token exchange" → TOKEN_EXCHANGE)
    - Recognises intent from natural phrasing, not keyword lists
    - Clean_query fixes typos before semantic vector search runs

    Falls back to QueryClassifier on any LLM error (timeout, quota, etc).
    Cost: ~0.0001 USD per query (256 output tokens, gpt-4o-mini).
    Latency: ~1s added to total query time.
    """

    _SYSTEM = (
        "You are a search-signal extractor for a Java codebase search engine "
        "(WSO2 Identity Server — OAuth2 / OIDC / JWT identity platform).\n\n"

        "Given a developer's question, return a JSON object with exactly these fields:\n"
        "{\n"
        '  "route": "global" | "symbolic" | "exact" | "conceptual",\n'
        '  "intent": "safety" | "code" | "narrative" | "impact" | "capability" | "general",\n'
        '  "symbols": [...],\n'
        '  "entity_names": [...],\n'
        '  "clean_query": "..."\n'
        "}\n\n"

        "ROUTE meanings:\n"
        "  global     — asks for true system/architecture overview ONLY: 'big picture', 'how does the whole system work', 'describe the architecture'\n"
        "               Do NOT use global for questions about specific features, communities, or topics.\n"
        "               'how does X work' where X is a named feature (SSO, OAuth, token exchange, SAML, OIDC, etc.) is NEVER global — it is conceptual.\n"
        "  symbolic   — involves Java constants (UPPER_SNAKE_CASE), string literals, or asks to find/remove a specific value\n"
        "  exact      — mentions a specific Java class or method name\n"
        "  conceptual — feature understanding, how-does-X-work, implement-X questions, OR questions about which communities/components handle a specific topic\n\n"
        "IMPORTANT: Questions like 'which communities handle token validation' or 'which components are responsible for X' are CONCEPTUAL, not global.\n"
        "IMPORTANT: 'how does SSO work', 'how does Single Sign-On work', 'how does token exchange work', 'how does SAML work' are ALL conceptual with intent=narrative.\n\n"

        "INTENT meanings:\n"
        "  safety     — asks whether something can be removed, deleted, is unused, or is safe to change\n"
        "               Examples: 'can X be removed', 'is it safe to delete', 'is this constant still used', 'can I drop this'\n"
        "  code       — asks to see or show existing source code, or how to implement/add something\n"
        "               Examples: 'show me', 'give me the file', 'how to implement', 'what does X look like'\n"
        "  narrative  — asks for a full end-to-end explanation of how a feature works\n"
        "               Examples: 'walk me through', 'explain the flow', 'how does X work end to end'\n"
        "  impact     — asks about blast radius, what breaks, what callers exist\n"
        "               Examples: 'what calls X', 'what would break', 'impact of changing X'\n"
        "  capability — asks whether the system supports or enforces a specific behaviour\n"
        "               Examples: 'does it support', 'can it handle multiple', 'is X enforced'\n"
        "  general    — everything else: factual questions, architectural questions, lookup questions\n\n"

        "symbols — UPPER_SNAKE_CASE constant names AND lowercase string literals to grep for.\n"
        "  Derive likely names from the question context, even when not explicitly stated.\n"
        "  Examples:\n"
        "    'actor token'          → [ACTOR_TOKEN, actor_token, MAY_ACT, ACTOR_TOKEN_TYPE]\n"
        "    'token exchange flow'  → [TOKEN_EXCHANGE, SUBJECT_TOKEN_TYPE, ACTOR_TOKEN_TYPE]\n"
        "    'impersonating actor'  → [IMPERSONATING_ACTOR, impersonating_actor]\n"
        "    'may act claim'        → [MAY_ACT, may_act]\n"
        "    'subject token'        → [SUBJECT_TOKEN, SUBJECT_TOKEN_TYPE, subject_token]\n\n"

        "entity_names — CamelCase Java class or method names to look up in the graph.\n"
        "  Derive likely class names even when not explicitly stated.\n"
        "  Examples:\n"
        "    'token exchange handler'        → [TokenExchangeGrantHandler]\n"
        "    'actor token validator'         → [ActorTokenValidator]\n"
        "    'authorization code grant'      → [AuthorizationCodeGrantHandler]\n"
        "    'jwt token issuer'              → [JWTTokenIssuer]\n"
        "    'oauth token validator'         → [OAuth2TokenValidator]\n\n"

        "clean_query — the original question with typos corrected and filler removed.\n"
        "  Fix spelling silently. Keep technical terms. Remove 'acccording', number-only tokens, etc.\n\n"

        "RULES:\n"
        "- Never include misspelled words in symbols or entity_names.\n"
        "- Never include common English words (the, how, what, is, etc.) in any list.\n"
        "- Return empty lists [] when nothing applies.\n"
        "- Output ONLY a valid JSON object. No explanation, no markdown, no extra text."
    )

    def __init__(self):
        self._llm = settings.make_llm_client()
        if settings.llm_provider.lower() == "azure":
            self._model = (
                settings.llm_parser_deployment
                or settings.llm_deployment
                or settings.llm_model
            )
        else:
            self._model = settings.llm_parser_model or settings.llm_model

    def parse(self, question: str) -> QueryClassification:
        """
        Call LLM to extract search signals from the question.
        Returns QueryClassification. Falls back to regex on any error.
        """
        try:
            raw = llm_chat(
                self._llm, self._model,
                system=self._SYSTEM,
                user=question,
                max_tokens=1000,
            )
            logger.debug("LLM parser raw response (model=%s, len=%d): %r", self._model, len(raw), raw[:200])

            if not raw:
                raise ValueError(f"Model {self._model!r} returned empty content. "
                                 "Check deployment name and Azure quota.")

            # Model may wrap JSON in markdown fences — strip them
            if raw.startswith("```"):
                raw = re.sub(r"^```[a-z]*\n?", "", raw)
                raw = re.sub(r"\n?```$", "", raw.strip())
            # Find the JSON object (handles extra prose before/after)
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            raw = match.group(0) if match else raw
            data = json.loads(raw)

            route        = data.get("route", "conceptual")
            intent       = data.get("intent", "general")
            symbols      = [s for s in data.get("symbols", []) if isinstance(s, str) and s.strip()]
            entity_names = [e for e in data.get("entity_names", []) if isinstance(e, str) and e.strip()]
            clean_query  = data.get("clean_query", question) or question

            # Sanity-guard: drop anything with 3+ repeated letters (model hallucination / typo)
            _typo_re = re.compile(r'(.)\1\1')
            symbols      = [s for s in symbols      if not _typo_re.search(s)]
            entity_names = [e for e in entity_names if not _typo_re.search(e)]

            # Validate intent against known values
            _VALID_INTENTS = {"safety", "code", "narrative", "impact", "capability", "general"}
            if intent not in _VALID_INTENTS:
                intent = "general"

            confidence = 0.9 if (symbols or entity_names) else 0.55
            logger.info(
                "LLM parser: route=%s intent=%s symbols=%s entities=%s",
                route, intent, symbols[:4], entity_names[:4],
            )
            return QueryClassification(route, confidence, symbols, entity_names, clean_query, intent)

        except Exception as e:
            logger.error("LLM query parser failed (model=%s) — falling back to regex: %s", self._model, e)
            return QueryClassifier().classify(question)


# ── Router (DB-backed orchestration) ─────────────────────────────────────────


class QueryRouter:
    """
    Orchestrates the full ``classify → retrieve → package`` pipeline.

    Uses ``QueryClassifier`` for the pure classification step, then
    dispatches to the appropriate deterministic or semantic retrieval path.
    """

    def __init__(self, chroma_client):
        self.chroma     = chroma_client
        self._chroma_host = getattr(chroma_client, '_host', None) or getattr(
            chroma_client, 'host', None
        )
        self._chroma_port = getattr(chroma_client, '_port', None) or getattr(
            chroma_client, 'port', None
        )
        self.retriever  = SqliteRetriever(
            db_path=settings.sqlite_db_path,
            chroma_client=chroma_client,
        )
        self.lexical    = LexicalSearcher()
        self.llm_parser = LLMQueryParser()   # primary: LLM-based extraction
        self.classifier = QueryClassifier()  # fallback: regex-based extraction
        self.global_rollup = GlobalRollup(chroma_client)

    def _reconnect_chroma(self):
        """Recreate the ChromaDB HttpClient to recover from a stale/dropped connection."""
        import chromadb
        from config.settings import settings as _s
        host = self._chroma_host or _s.chroma_host
        port = self._chroma_port or _s.chroma_port
        self.chroma = chromadb.HttpClient(host=host, port=port)
        self.retriever.chroma = self.chroma
        logger.info("ChromaDB client reconnected to %s:%s", host, port)
        return self.chroma

    def _chroma_exec(self, fn, *args, **kwargs):
        """Run fn(chroma_client, *args, **kwargs) with one reconnect-retry on disconnect."""
        for attempt in range(2):
            try:
                return fn(self.chroma, *args, **kwargs)
            except (ConnectionError, TimeoutError, ConnectionAbortedError) as e:
                if attempt == 0:
                    logger.warning("ChromaDB connection dropped — reconnecting (attempt %d)", attempt + 1)
                    self._reconnect_chroma()
                    continue
                raise
            except Exception:
                raise

    def route(self, question: str) -> RouterResult:
        """LLM-parse → classify → retrieve ALL evidence paths in parallel → merge → return."""
        # Primary: LLM extracts symbols, class names, and fixes typos in one fast call.
        # Fallback inside LLMQueryParser if the LLM call fails.
        classification = self.llm_parser.parse(question)
        logger.info(
            "Query classified  bucket=%s  confidence=%.2f  symbols=%s  entities=%s",
            classification.bucket,
            classification.confidence,
            classification.symbols,
            classification.entity_names[:3],
        )

        # ROUTE D — Global architecture: return L3 summary directly (no merge needed)
        # Guard: if the question asks "how does <specific feature> work", the LLM
        # sometimes returns global despite the prompt. Downgrade to conceptual so
        # ChromaDB searches for the actual feature instead of returning the L3 doc.
        _FEATURE_HOW_RE = re.compile(
            r'\bhow\s+does\s+\w[\w\s]{1,40}\s+work\b'
            r'|\bhow\s+(?:is|are)\s+\w[\w\s]{1,40}\s+(?:handled|implemented|processed)\b',
            re.IGNORECASE,
        )
        if classification.bucket == "global" and _FEATURE_HOW_RE.search(question):
            logger.info(
                "Downgrading global→conceptual: question matches feature-level 'how does X work' pattern"
            )
            classification.bucket = "conceptual"
            if not classification.intent:
                classification.intent = "narrative"

        if classification.bucket == "global":
            return self._global_route(question)

        # Use the LLM-corrected query for semantic search (fixes typos like "acccording")
        effective_query = classification.clean_query or question

        # All other routes: run every applicable evidence path in parallel, then merge.
        return self._multi_route(effective_query, classification)

    def close(self):
        self.retriever.close()

    # ── Private: Route implementations ────────────────────────────────────────

    def _multi_route(self, question: str, classification: QueryClassification) -> RouterResult:
        """
        Run ALL applicable evidence paths in parallel and merge the results.

        Paths run:
          A) Grep/symbolic  — if symbols or entity_names yield any UPPER_SNAKE tokens
          B) Graph/exact    — if entity_names contains CamelCase or dotted FQN candidates
          C) Semantic       — always (hybrid BM25 + vector + community summaries)

        Merging: nodes deduped by FQN, community summaries deduped by community_id.
        The primary route label (for logging) is determined by what found the most evidence.
        """
        symbols     = classification.symbols
        entity_names = classification.entity_names

        tasks: dict[str, callable] = {}

        if symbols:
            tasks["grep"] = lambda: self._symbolic_route(question, symbols, entity_names)
        if entity_names:
            tasks["graph"] = lambda: self._exact_entity_route(question, entity_names)
        tasks["semantic"] = lambda: self._semantic_route(question, entity_names)

        results: dict[str, RouterResult] = {}
        with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
            futures = {pool.submit(fn): name for name, fn in tasks.items()}
            for future in as_completed(futures):
                name = futures[future]
                try:
                    results[name] = future.result()
                except Exception as e:
                    logger.warning("Route '%s' failed: %s", name, e)

        if not results:
            # All paths failed — return empty result
            return RouterResult(
                route="failed", grep_hits=[], seed_nodes=[], affected_nodes=[],
                community_ids=[], community_summaries=[], entity_names=entity_names,
                symbols=symbols,
            )

        # ── Merge: deduplicate by FQN for nodes, by community_id for summaries ──
        all_grep_hits: list[GrepHit] = []
        seen_fqns: set[str] = set()
        seed_nodes: list[dict] = []
        affected_nodes: list[dict] = []
        seen_community_ids: set[int] = set()
        community_summaries: list[dict] = []

        # Priority order: grep seeds first (most precise), then graph, then semantic
        for source in ("grep", "graph", "semantic"):
            r = results.get(source)
            if not r:
                continue
            all_grep_hits.extend(r.grep_hits)
            for n in r.seed_nodes:
                fqn = n.get("fqn", "")
                if fqn and fqn not in seen_fqns:
                    seed_nodes.append(n)
                    seen_fqns.add(fqn)
            for n in r.affected_nodes:
                fqn = n.get("fqn", "")
                if fqn and fqn not in seen_fqns:
                    affected_nodes.append(n)
                    seen_fqns.add(fqn)
            for s in r.community_summaries:
                cid = s.get("community_id")
                if cid not in seen_community_ids:
                    community_summaries.append(s)
                    seen_community_ids.add(cid)

        # Route label: name the dominant path for display
        if "grep" in results and results["grep"].grep_hits:
            route_label = "hybrid_grep+graph+semantic"
        elif "graph" in results and results["graph"].seed_nodes:
            route_label = "hybrid_graph+semantic"
        else:
            route_label = "semantic"

        community_ids = sorted(seen_community_ids)
        logger.info(
            "Multi-route merge: grep=%d hits, seed_nodes=%d, affected=%d, communities=%d  [%s]",
            len(all_grep_hits), len(seed_nodes), len(affected_nodes),
            len(community_summaries), route_label,
        )
        return RouterResult(
            route=route_label,
            grep_hits=all_grep_hits,
            seed_nodes=seed_nodes,
            affected_nodes=affected_nodes,
            community_ids=community_ids,
            community_summaries=community_summaries,
            entity_names=entity_names,
            symbols=symbols,
            intent=classification.intent,
        )

    def _global_route(self, question: str) -> RouterResult:
        """
        ROUTE D: Return the pre-computed Level 3 Global Architecture summary.
        Skips all search — instantaneous retrieval.
        """
        logger.info("Route: GLOBAL — returning Level 3 Global Architecture summary")

        global_summary = self.global_rollup.get_global_summary()
        if global_summary:
            return RouterResult(
                route="global",
                grep_hits=[],
                seed_nodes=[],
                affected_nodes=[],
                community_ids=[],
                community_summaries=[{
                    "community_id": -1,
                    "summary_text": global_summary,
                    "metadata": {"level": "L3", "type": "global_architecture"},
                }],
                entity_names=[],
                symbols=[],
            )

        # Fallback: try L2 subsystem summaries
        domains = self.global_rollup.list_subsystem_domains()
        if domains:
            summaries = []
            for domain in domains:
                text = self.global_rollup.get_subsystem_summary(domain)
                if text:
                    summaries.append({
                        "community_id": -1,
                        "summary_text": f"## {domain}\n\n{text}",
                        "metadata": {"level": "L2", "type": "subsystem", "domain": domain},
                    })
            if summaries:
                return RouterResult(
                    route="global_l2",
                    grep_hits=[],
                    seed_nodes=[],
                    affected_nodes=[],
                    community_ids=[],
                    community_summaries=summaries,
                    entity_names=[],
                    symbols=[],
                )

        # No global summaries available — fall back to semantic
        logger.warning("No global summaries available — falling back to semantic route")
        r = self._semantic_route(question, [])
        r.route = "global_fallback"
        return r

    def _symbolic_route(
        self, question: str, symbols: list[str], entity_names: list[str],
    ) -> RouterResult:
        """
        ROUTE A: Grep → Grep-to-Graph bridge → blast-radius traversal → summaries by ID.
        """
        logger.info("Route: SYMBOLIC — grepping for: %s", symbols)

        # Step 1 — Lexical grep across mirror
        all_grep_hits: list[GrepHit] = []
        for sym in symbols:
            hits = self.lexical.search(sym, max_hits=settings.grep_max_hits)
            all_grep_hits.extend(hits)
        logger.info("Grep returned %d hits", len(all_grep_hits))

        if not all_grep_hits:
            logger.warning("No grep hits for %s — falling back to semantic", symbols)
            r = self._semantic_route(question, entity_names)
            r.route = "symbolic_fallback"
            r.symbols = symbols
            return r

        # Step 2 — Grep-to-graph bridge
        seed_nodes, seen_fqns = self._grep_to_graph_bridge(all_grep_hits)

        if not seed_nodes:
            logger.warning("Bridge found no graph nodes — lines outside method bodies")
            r = self._semantic_route(question, entity_names)
            r.route = "symbolic_no_bridge"
            r.grep_hits = all_grep_hits
            r.symbols = symbols
            return r

        # Step 3 — Deterministic blast-radius
        blast = self.retriever.compute_blast_radius(
            [n["fqn"] for n in seed_nodes],
            depth=settings.blast_radius_depth,
        )

        # Include seed communities
        community_ids = list(blast["community_ids"])
        for n in seed_nodes:
            cid = n.get("community_id")
            if cid is not None and cid not in community_ids:
                community_ids.append(cid)
        community_ids = sorted(set(community_ids))

        # Step 4 — Fetch summaries by ID (deterministic)
        summaries = self._chroma_exec(
            self.retriever.get_community_summaries_by_ids, community_ids, COMMUNITY_COLLECTION,
        )

        return RouterResult(
            route="symbolic",
            grep_hits=all_grep_hits,
            seed_nodes=seed_nodes,
            affected_nodes=blast["affected_nodes"],
            community_ids=community_ids,
            community_summaries=summaries,
            entity_names=entity_names,
            symbols=symbols,
        )

    def _exact_entity_route(
        self, question: str, entity_names: list[str],
    ) -> RouterResult:
        """
        ROUTE B: Neo4j entity lookup → blast-radius → summaries by ID.
        """
        logger.info("Route: EXACT-ENTITY")
        seed_nodes: list[dict] = []
        seen_fqns: set[str] = set()
        for name in entity_names:
            nodes = self.retriever.find_nodes(name)
            for node in nodes:
                fqn = node.get("fqn")
                if fqn and fqn not in seen_fqns:
                    seed_nodes.append(node)
                    seen_fqns.add(fqn)

        seed_fqns = [n["fqn"] for n in seed_nodes]
        blast = self.retriever.compute_blast_radius(
            seed_fqns, depth=settings.blast_radius_depth,
        )

        community_ids = list(blast["community_ids"])
        if not community_ids:
            community_ids = sorted(
                set(n["community_id"] for n in seed_nodes if n.get("community_id")),
            )

        summaries = self._chroma_exec(
            self.retriever.get_community_summaries_by_ids, community_ids, COMMUNITY_COLLECTION,
        )

        return RouterResult(
            route="exact",
            grep_hits=[],
            seed_nodes=seed_nodes,
            affected_nodes=blast["affected_nodes"],
            community_ids=community_ids,
            community_summaries=summaries,
            entity_names=entity_names,
            symbols=[],
        )

    def _semantic_route(
        self, question: str, entity_names: list[str],
    ) -> RouterResult:
        """
        ROUTE C: Hybrid search (BM25 + code_intent vectors) → seed_nodes + community summaries.

        Two-pass retrieval:
          1. hybrid_search() → top code nodes (BM25 + ChromaDB code_intent RRF)
          2. community_summaries vector search → relevant community context
        """
        logger.info("Route: SEMANTIC")

        # ── Pass 1: hybrid code search → seed_nodes ───────────────────────────
        seed_nodes: list[dict] = []
        community_ids: set[int] = set()
        try:
            hybrid_hits = self.retriever.hybrid_search(question, n_results=15)
            seen_fqns: set[str] = set()
            for hit in hybrid_hits:
                fqn = hit.get("fqn", "")
                if fqn and fqn not in seen_fqns:
                    # Enrich with full node data from Neo4j
                    nodes = self.retriever.find_nodes(fqn)
                    for node in nodes:
                        nfqn = node.get("fqn", "")
                        if nfqn and nfqn not in seen_fqns:
                            seed_nodes.append(node)
                            seen_fqns.add(nfqn)
                            cid = node.get("community_id")
                            if cid is not None:
                                community_ids.add(cid)
            logger.info("Semantic hybrid search: %d seed nodes found", len(seed_nodes))
        except Exception as e:
            logger.warning("Hybrid search failed in semantic route: %s", e)

        # ── Pass 2: community_summaries vector search ─────────────────────────
        summaries = []
        for _attempt in range(2):
            try:
                col = self.chroma.get_collection(COMMUNITY_COLLECTION)
                col_count = col.count()
                if col_count == 0:
                    logger.warning("Community collection is empty — run global_rollup.py first")
                    break
                results = col.query(
                    query_texts=[question], n_results=min(20, col_count),
                )
                if results["ids"]:
                    for i, doc_id in enumerate(results["ids"][0]):
                        meta = results["metadatas"][0][i]
                        cid = meta.get("community_id", 0)
                        summaries.append({
                            "community_id": cid,
                            "summary_text": results["documents"][0][i],
                            "metadata": meta,
                        })
                        community_ids.add(cid)
                break
            except (ConnectionError, TimeoutError, ConnectionAbortedError) as e:
                if _attempt == 0:
                    logger.warning("ChromaDB connection dropped — reconnecting and retrying")
                    self._reconnect_chroma()
                    continue
                logger.error("ChromaDB semantic search failed: %s", e)
                break
            except Exception as e:
                logger.error("ChromaDB semantic search failed: %s", e)
                break

        return RouterResult(
            route="semantic",
            grep_hits=[],
            seed_nodes=seed_nodes,
            affected_nodes=[],
            community_ids=sorted(community_ids),
            community_summaries=summaries,
            entity_names=entity_names,
            symbols=[],
        )

    # ── Shared helpers ────────────────────────────────────────────────────────

    def _grep_to_graph_bridge(
        self, grep_hits: list[GrepHit], cap: int = 15,
    ) -> tuple[list[dict], set[str]]:
        """
        Map grep file:line hits → enclosing Neo4j LogicUnit/Component nodes.

        Returns:
            (seed_nodes, seen_fqns)  — deduplicated by FQN.
        """
        seed_nodes: list[dict] = []
        seen_fqns: set[str] = set()
        for hit in grep_hits[:cap]:
            nodes = self.retriever.find_node_by_location(
                hit.rel_path, hit.line_number,
            )
            for n in nodes:
                if n.get("fqn") and n["fqn"] not in seen_fqns:
                    n["grep_hit"] = f"{hit.rel_path}:{hit.line_number}"
                    seed_nodes.append(n)
                    seen_fqns.add(n["fqn"])
        logger.info(
            "Grep-to-graph bridge: %d hits → %d unique seed nodes",
            len(grep_hits), len(seed_nodes),
        )
        return seed_nodes, seen_fqns
