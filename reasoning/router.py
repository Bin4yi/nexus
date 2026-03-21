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

import logging
import re
from dataclasses import dataclass, field

from config.settings import settings
from reasoning.graph_retriever import GraphRetriever
from reasoning.lexical_search import LexicalSearcher, GrepHit
from community.summarizer import COLLECTION_NAME as COMMUNITY_COLLECTION
from community.global_rollup import GlobalRollup

logger = logging.getLogger(__name__)


# ── Data classes ──────────────────────────────────────────────────────────────


@dataclass
class QueryClassification:
    """Output of the pure classifier — no DB I/O, fully unit-testable."""
    bucket: str          # "symbolic" | "exact" | "conceptual" | "global"
    confidence: float    # 0.0–1.0 (heuristic)
    symbols: list[str]   # UPPER_SNAKE / quoted literals for grep
    entity_names: list[str]  # CamelCase / FQN candidates


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
        symbols = list(dict.fromkeys(upper_snake + quoted))

        if symbols:
            confidence = min(1.0, 0.6 + 0.1 * len(symbols))
            return QueryClassification("symbolic", confidence, symbols, entity_names)

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
        return sorted(candidates, key=len, reverse=True)


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
        self.retriever  = GraphRetriever(chroma_client=chroma_client)
        self.lexical    = LexicalSearcher()
        self.classifier = QueryClassifier()
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
        """Classify → retrieve → return structured ``RouterResult``."""
        classification = self.classifier.classify(question)
        logger.info(
            "Query classified  bucket=%s  confidence=%.2f  symbols=%s  entities=%s",
            classification.bucket,
            classification.confidence,
            classification.symbols,
            classification.entity_names[:3],
        )

        # ROUTE D — Global architecture: return L3 summary directly
        if classification.bucket == "global":
            return self._global_route(question)

        if classification.bucket == "symbolic":
            return self._symbolic_route(
                question, classification.symbols, classification.entity_names,
            )
        if classification.bucket == "exact":
            # Always use deterministic route for clear CamelCase/FQN patterns
            is_camel_case = any(
                re.match(r'^[A-Z][a-z]+(?:[A-Z][a-z0-9]+)+$', name)
                for name in classification.entity_names
            )
            if not is_camel_case and classification.confidence < settings.router_confidence_threshold:
                logger.info(
                    "Exact confidence %.2f < threshold %.2f — falling back to semantic",
                    classification.confidence,
                    settings.router_confidence_threshold,
                )
                return self._semantic_route(question, classification.entity_names)
            return self._exact_entity_route(question, classification.entity_names)

        return self._semantic_route(question, classification.entity_names)

    def close(self):
        self.retriever.close()

    # ── Private: Route implementations ────────────────────────────────────────

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
        ROUTE C: ChromaDB vector search on ``community_summaries``.
        """
        logger.info("Route: SEMANTIC")
        for _attempt in range(2):
            try:
                col = self.chroma.get_collection(COMMUNITY_COLLECTION)
                col_count = col.count()
                if col_count == 0:
                    logger.warning("Community collection is empty — run global_rollup.py first")
                    return RouterResult("semantic", [], [], [], [], [], entity_names, [])
                results = col.query(
                    query_texts=[question], n_results=min(20, col_count),
                )
                break  # success
            except (ConnectionError, TimeoutError, ConnectionAbortedError) as e:
                if _attempt == 0:
                    logger.warning("ChromaDB connection dropped — reconnecting and retrying")
                    self._reconnect_chroma()
                    continue
                logger.error("ChromaDB semantic search failed: %s", e)
                return RouterResult("semantic", [], [], [], [], [], entity_names, [])
            except Exception as e:
                logger.error("ChromaDB semantic search failed: %s", e)
                return RouterResult("semantic", [], [], [], [], [], entity_names, [])

        summaries = []
        if results["ids"]:
            for i, doc_id in enumerate(results["ids"][0]):
                meta = results["metadatas"][0][i]
                summaries.append({
                    "community_id": meta.get("community_id", 0),
                    "summary_text": results["documents"][0][i],
                    "metadata": meta,
                })

        return RouterResult(
            route="semantic",
            grep_hits=[],
            seed_nodes=[],
            affected_nodes=[],
            community_ids=[s["community_id"] for s in summaries],
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
