"""reasoning/graph_retriever.py
Deterministic graph-based retrieval from Neo4j.

Architectural role:
    The engine of the Hybrid Deterministic Router.  Converts a set of seed
    FQNs (from lexical grep or direct Neo4j lookup) into a **mathematically
    proven** blast radius by traversing [:CALLS], [:INJECTS], [:DEPENDS_ON],
    and [:INSTANTIATES] edges.  ChromaDB is used **only** to fetch community
    summaries by their integer ID — never by vector similarity.

Key public methods:
    * ``find_nodes``              — entity lookup (graph + ChromaDB literal fallback)
    * ``find_node_by_location``   — grep-to-graph bridge (file:line → LogicUnit)
    * ``compute_blast_radius``    — deterministic N-hop traversal → community IDs
    * ``get_community_summaries_by_ids`` — fetch summaries by integer ID

Resilience:
    All Neo4j session calls are wrapped with ``tenacity`` exponential-backoff
    retry (configurable via ``settings.retry_max_attempts`` /
    ``settings.retry_backoff_seconds``) so that transient connection drops
    during long 100+-repo ingestion runs do not crash the pipeline.
"""
from __future__ import annotations
import logging
from typing import Any

from neo4j import GraphDatabase
from neo4j.exceptions import ServiceUnavailable, SessionExpired
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

from config.settings import settings

logger = logging.getLogger(__name__)

# ── Retry decorator for Neo4j transient failures ─────────────────────────────
_neo4j_retry = retry(
    retry=retry_if_exception_type((ServiceUnavailable, SessionExpired, ConnectionError)),
    stop=stop_after_attempt(settings.retry_max_attempts),
    wait=wait_exponential(
        multiplier=settings.retry_backoff_seconds, min=1, max=30,
    ),
    reraise=True,
)


class GraphRetriever:
    """
    Deterministic retrieval pipeline.  No LLM or vector search involved.

    Every graph query is decorated with ``_neo4j_retry`` so that Neo4j
    connection hiccups are retried transparently.
    """

    def __init__(self, chroma_client=None):
        self._driver = GraphDatabase.driver(settings.neo4j_uri, auth=settings.neo4j_auth)
        self._chroma = chroma_client

    def close(self):
        self._driver.close()

    # ── Primary deterministic entry point ─────────────────────────────────────

    def compute_blast_radius(
        self,
        seed_fqns: list[str],
        depth: int | None = None,
    ) -> dict[str, Any]:
        """
        **Core deterministic method.**

        Given a list of seed FQNs (from grep or direct lookup), traverse
        ``[:CALLS]``, ``[:INJECTS]``, ``[:DEPENDS_ON]``, ``[:INSTANTIATES]``
        edges up to *depth* hops and return the full blast radius.

        Args:
            seed_fqns: FQNs of the changed/removed/queried entities.
            depth:     Max traversal hops (defaults to ``settings.blast_radius_depth``).

        Returns:
            Dict with keys:
              * ``seed_fqns``      — echo of the input
              * ``affected_nodes`` — list[dict] with fqn, file_path, community_id, edge_type
              * ``community_ids``  — sorted unique list[int]
              * ``edge_counts``    — dict mapping edge_type → int
        """
        depth = depth or settings.blast_radius_depth
        if not seed_fqns:
            return {
                "seed_fqns": seed_fqns,
                "affected_nodes": [],
                "community_ids": [],
                "edge_counts": {},
            }

        community_ids, affected_nodes = self.get_blast_radius_communities(
            seed_fqns, depth=depth,
        )

        # Compute a per-edge-type breakdown
        edge_counts: dict[str, int] = {}
        for node in affected_nodes:
            etype = node.get("edge_type", "UNKNOWN")
            edge_counts[etype] = edge_counts.get(etype, 0) + 1

        logger.info(
            "compute_blast_radius: %d seeds → %d affected, %d communities, edges=%s",
            len(seed_fqns), len(affected_nodes), len(community_ids), edge_counts,
        )
        return {
            "seed_fqns": seed_fqns,
            "affected_nodes": affected_nodes,
            "community_ids": community_ids,
            "edge_counts": edge_counts,
        }

    # ── Hybrid Search (BM25 + Semantic + RRF) ────────────────────────────────

    def hybrid_search(
        self,
        query: str,
        embedder=None,
        n_results: int | None = None,
        rrf_k: int | None = None,
    ) -> list[dict]:
        """
        Reciprocal Rank Fusion over Neo4j BM25 fulltext + ChromaDB semantic search.

        Algorithm (from GitNexus):
          1. BM25 fulltext search via Neo4j ``code_search_names`` index.
          2. Semantic vector search via ChromaDB ``code_intent`` collection.
          3. RRF score = 1/(K + rank) for each source, K=60 (literature default).
          4. Sum RRF scores for GEIDs appearing in both result sets.
          5. Return merged list sorted by descending RRF score.

        Args:
            query:     Natural language or identifier query string.
            embedder:  ChromaEmbedder instance (falls back to self._chroma if None).
            n_results: Number of candidates per source (defaults to settings value).
            rrf_k:     RRF constant K (defaults to settings.rrf_k).

        Returns:
            List of dicts with keys: geid, fqn, rrf_score, sources, text, distance
        """
        n   = n_results or settings.hybrid_search_n_results
        k   = rrf_k     or settings.rrf_k

        bm25_results     = self._bm25_search(query, n)
        semantic_results = self._semantic_search(query, embedder, n)

        # Build RRF score maps keyed by geid
        rrf_bm25: dict[str, float] = {
            r["geid"]: 1.0 / (k + rank + 1)
            for rank, r in enumerate(bm25_results)
            if r.get("geid")
        }
        # Semantic results are sorted ascending by distance (lower = more similar)
        rrf_semantic: dict[str, float] = {
            r["geid"]: 1.0 / (k + rank + 1)
            for rank, r in enumerate(
                sorted(semantic_results, key=lambda x: x.get("distance", 999.0))
            )
            if r.get("geid")
        }

        # Collect metadata per geid
        meta: dict[str, dict] = {}
        for r in bm25_results:
            geid = r.get("geid")
            if geid:
                meta[geid] = {"geid": geid, "fqn": r.get("fqn", ""),
                               "text": r.get("text", ""), "distance": None}
        for r in semantic_results:
            geid = r.get("geid")
            if geid and geid not in meta:
                meta[geid] = {"geid": geid, "fqn": r.get("fqn", ""),
                               "text": r.get("text", ""), "distance": r.get("distance")}
            elif geid:
                meta[geid]["distance"] = r.get("distance")
                if not meta[geid].get("text"):
                    meta[geid]["text"] = r.get("text", "")

        # Merge and rank
        all_geids = set(rrf_bm25) | set(rrf_semantic)
        results = []
        for geid in all_geids:
            score  = rrf_bm25.get(geid, 0.0) + rrf_semantic.get(geid, 0.0)
            sources = []
            if geid in rrf_bm25:
                sources.append("bm25")
            if geid in rrf_semantic:
                sources.append("semantic")
            row = {**meta.get(geid, {"geid": geid, "fqn": ""}),
                   "rrf_score": round(score, 6),
                   "sources":   sources}
            results.append(row)

        results.sort(key=lambda x: x["rrf_score"], reverse=True)
        results = results[:n]

        logger.info(
            "hybrid_search('%s'): bm25=%d semantic=%d merged=%d",
            query[:60], len(bm25_results), len(semantic_results), len(results),
        )
        return results

    @_neo4j_retry
    def _bm25_search(self, query: str, n_results: int) -> list[dict]:
        """
        BM25 fulltext search using Neo4j ``code_search_names`` index.
        Falls back to ``code_search`` if the names index isn't populated yet.
        Returns list of {geid, fqn, text, bm25_score} sorted by score desc.
        """
        # Escape special Lucene characters that Neo4j fulltext uses
        escaped = query.replace('"', '\\"').replace("'", "\\'")
        with self._driver.session() as session:
            for index_name in ("code_search_names", "code_search"):
                try:
                    rows = list(session.run(
                        f"""
                        CALL db.index.fulltext.queryNodes('{index_name}', $query)
                        YIELD node, score
                        WHERE score > 0
                        RETURN node.geid     AS geid,
                               node.fqn      AS fqn,
                               node.docstring AS text,
                               score
                        ORDER BY score DESC
                        LIMIT $n
                        """,
                        query=escaped,
                        n=n_results,
                    ))
                    if rows is not None:
                        return [dict(r) for r in rows]
                except Exception as e:
                    logger.debug("BM25 index '%s' query failed: %s", index_name, e)
        logger.debug("BM25 fulltext search returned no results for: %s", query)
        return []

    def _semantic_search(self, query: str, embedder, n_results: int) -> list[dict]:
        """
        Semantic vector search via ChromaDB code_intent collection.
        Returns list of {geid, fqn, text, distance} sorted ascending by distance.
        """
        chroma_embedder = embedder or self._chroma
        if not chroma_embedder:
            return []
        try:
            # Accept either a ChromaEmbedder instance or a raw chromadb client
            if hasattr(chroma_embedder, "semantic_search"):
                return chroma_embedder.semantic_search(
                    query=query,
                    collection="code_intent",
                    n_results=n_results,
                )
            # Raw chroma client fallback
            from vectorstore.embedder import ChromaEmbedder
            embedder_obj = ChromaEmbedder(chroma_embedder)
            return embedder_obj.semantic_search(
                query=query,
                collection="code_intent",
                n_results=n_results,
            )
        except Exception as e:
            logger.warning("Semantic search failed in hybrid_search: %s", e)
            return []

    def find_nodes(self, entity_name: str) -> list[dict]:
        """
        Two-strategy entity lookup:
        1. Neo4j: search LogicUnit/Component by FQN or name (exact node match)
        2. Fallback: if entity is a string constant not stored as a node (e.g.
           IMPERSONATED_SUBJECT), search ChromaDB code_intent for methods that
           literally reference the constant in their source text. Those methods
           become the traversal seeds.
        Returns list of {fqn, file_path, label, community_id}.
        """
        rows = self._find_in_graph(entity_name)
        if rows:
            return rows

        # String constant / field not stored as node — find methods that use it
        if self._chroma:
            rows = self._find_in_code_intent(entity_name)
            if rows:
                logger.info(
                    "'%s' not in graph — found %d methods referencing it literally in code_intent",
                    entity_name, len(rows),
                )
        return rows

    @_neo4j_retry
    def _find_in_graph(self, entity_name: str) -> list[dict]:
        """Direct Neo4j node lookup by FQN, name, or docstring reference."""
        with self._driver.session() as session:
            result = session.run(
                """
                MATCH (n)
                WHERE (n:LogicUnit OR n:Component)
                  AND (
                    n.fqn       CONTAINS $name
                    OR n.name    = $name
                    OR n.fqn    ENDS WITH ('.' + $name)
                    OR (n.docstring IS NOT NULL AND n.docstring CONTAINS $name)
                  )
                RETURN
                    n.fqn          AS fqn,
                    n.file_path    AS file_path,
                    n.start_line   AS start_line,
                    n.end_line     AS end_line,
                    labels(n)[0]   AS label,
                    n.community_id AS community_id
                LIMIT 20
                """,
                name=entity_name,
            )
            rows = [dict(r) for r in result]
        logger.info("Neo4j lookup '%s' → %d nodes", entity_name, len(rows))
        return rows

    def _find_in_code_intent(self, entity_name: str) -> list[dict]:
        """
        Search ChromaDB code_intent for chunks that LITERALLY contain the entity name.
        Used for Java string constants and field names not stored as graph nodes.
        Only returns chunks where the literal string appears in the document text.
        """
        try:
            col = self._chroma.get_collection("code_intent")
            results = col.query(query_texts=[entity_name], n_results=min(20, col.count()))
        except Exception as e:
            logger.warning("code_intent literal search failed: %s", e)
            return []

        hits = []
        if results["ids"]:
            for i, doc_id in enumerate(results["ids"][0]):
                meta     = results["metadatas"][0][i]
                doc_text = results["documents"][0][i]
                # Only accept chunks that literally contain the constant name
                if entity_name not in doc_text:
                    continue
                fp = meta.get("file_path", "")
                if "mirror" in fp:
                    fp = fp[fp.find("mirror"):]
                hits.append({
                    "fqn":          meta.get("fqn", ""),
                    "file_path":    fp,
                    "label":        "LogicUnit",
                    "community_id": meta.get("community_id"),
                    "source":       "code_intent_literal",
                })
        logger.info("code_intent literal search for '%s' → %d method hits", entity_name, len(hits))
        return hits

    @_neo4j_retry
    def get_blast_radius_communities(
        self, seed_fqns: list[str], depth: int = 3
    ) -> tuple[list[int], list[dict]]:
        """
        Traverse CALLS, INJECTS, DEPENDS_ON, INSTANTIATES edges from seed nodes
        to find all impacted community IDs deterministically.

        Args:
            seed_fqns: FQNs of the changed/removed entities
            depth:     Max traversal hops (default 3)

        Returns:
            (community_ids, affected_nodes)
            community_ids: sorted unique list of impacted community IDs
            affected_nodes: list of {fqn, file_path, community_id, edge_type}
        """
        if not seed_fqns:
            return [], []

        with self._driver.session() as session:
            result = session.run(
                """
                // Find seed nodes
                MATCH (seed)
                WHERE (seed:LogicUnit OR seed:Component)
                  AND seed.fqn IN $fqns

                // Traverse inbound edges — who calls/uses/injects the seed?
                CALL apoc.path.expandConfig(seed, {
                    relationshipFilter: '<CALLS|<INJECTS|<DEPENDS_ON|<INSTANTIATES|<OVERRIDES',
                    minLevel: 1,
                    maxLevel: $depth,
                    uniqueness: 'NODE_GLOBAL'
                }) YIELD path
                WITH nodes(path)[-1] AS affected, relationships(path)[0] AS rel
                WHERE (affected:LogicUnit OR affected:Component)
                  AND affected.community_id IS NOT NULL
                RETURN DISTINCT
                    affected.fqn          AS fqn,
                    affected.file_path    AS file_path,
                    affected.start_line   AS start_line,
                    affected.end_line     AS end_line,
                    affected.community_id AS community_id,
                    type(rel)             AS edge_type
                ORDER BY affected.community_id, affected.fqn
                """,
                fqns=seed_fqns,
                depth=depth,
            )
            rows = [dict(r) for r in result]

        # If APOC not available, fall back to simple 1-hop Cypher
        if not rows:
            rows = self._simple_traversal(seed_fqns)

        community_ids = sorted(set(r["community_id"] for r in rows if r["community_id"] is not None))
        logger.info(
            "Graph traversal from %d seeds → %d affected nodes across %d communities",
            len(seed_fqns), len(rows), len(community_ids),
        )
        return community_ids, rows

    def get_community_summaries_by_ids(
        self, chroma_client, community_ids: list[int], collection_name: str = "community_summaries"
    ) -> list[dict]:
        """
        Fetch community summaries from ChromaDB BY ID — not by vector search.
        IDs in ChromaDB are stored as 'community_N' strings (e.g. 'community_19').
        """
        if not community_ids:
            return []
        try:
            collection = chroma_client.get_collection(collection_name)
            ids_as_str = [f"community_{cid}" for cid in community_ids]
            results = collection.get(ids=ids_as_str, include=["documents", "metadatas"])
            summaries = []
            for i, doc_id in enumerate(results["ids"]):
                # Parse community_id from the 'community_N' string format
                try:
                    cid = int(doc_id.replace("community_", ""))
                except (ValueError, AttributeError):
                    cid = i
                summaries.append({
                    "community_id": cid,
                    "summary_text": results["documents"][i],
                    "metadata":     results["metadatas"][i],
                })
            logger.info("Fetched %d community summaries by ID", len(summaries))
            return summaries
        except Exception as e:
            logger.error("Failed to fetch community summaries by ID: %s", e)
            return []

    @_neo4j_retry
    def find_node_by_location(self, file_path: str, line_number: int) -> list[dict]:
        """
        GREP-TO-GRAPH BRIDGE.
        Maps a grep(file_path, line_number) hit to Neo4j LogicUnit nodes.

        Strategy:
          1. Line-range lookup: find LogicUnit whose start_line ≤ line ≤ end_line
          2. File fallback: if line is a class-level field/constant (no enclosing method),
             return all LogicUnits in that file — the constant is "owned" by the class
          3. Component fallback: return the Component (class) node for that file

        Args:
            file_path:   Relative or absolute path (normalised internally)
            line_number: Exact line number from the grep hit
        """
        norm_path = file_path.replace("\\", "/")
        if "mirror/" in norm_path:
            norm_path = norm_path[norm_path.index("mirror/"):]
        filename = norm_path.split("/")[-1]   # e.g. "OAuthConstants.java"

        with self._driver.session() as session:

            # Strategy 1: Precise line-range match (method body)
            rows = list(session.run(
                """
                MATCH (n:LogicUnit)
                WHERE n.file_path CONTAINS $filename
                  AND n.start_line IS NOT NULL
                  AND n.start_line <= $line
                  AND n.end_line   >= $line
                RETURN n.fqn AS fqn, n.file_path AS file_path,
                       n.community_id AS community_id,
                       n.start_line AS start_line, n.end_line AS end_line
                ORDER BY (n.end_line - n.start_line)
                LIMIT 5
                """,
                filename=filename, line=line_number,
            ))
            nodes = [dict(r) for r in rows]

            if not nodes:
                # Strategy 2: All LogicUnits in this file
                # (constant is class-level; its methods are the blast-radius seeds)
                rows2 = list(session.run(
                    """
                    MATCH (n:LogicUnit)
                    WHERE n.file_path CONTAINS $filename
                      AND (n.file_path CONTAINS 'src/main/java' OR n.file_path CONTAINS 'src\\main\\java')
                    RETURN n.fqn AS fqn, n.file_path AS file_path,
                           n.community_id AS community_id,
                           n.start_line AS start_line, n.end_line AS end_line
                    LIMIT 10
                    """,
                    filename=filename,
                ))
                nodes = [dict(r) for r in rows2]

            if not nodes:
                # Strategy 3: Component (class) node for this file
                rows3 = list(session.run(
                    """
                    MATCH (n:Component)
                    WHERE n.file_path CONTAINS $filename
                      AND (n.file_path CONTAINS 'src/main/java' OR n.file_path CONTAINS 'src\\main\\java')
                    RETURN n.fqn AS fqn, n.file_path AS file_path,
                           n.community_id AS community_id,
                           null AS start_line, null AS end_line
                    LIMIT 5
                    """,
                    filename=filename,
                ))
                nodes = [dict(r) for r in rows3]

        logger.info(
            "Grep-to-graph bridge: %s:%d → %d nodes",
            filename, line_number, len(nodes),
        )
        return nodes

    @_neo4j_retry
    def get_callers(self, fqn: str, depth: int = 1) -> list[dict]:
        """
        Who calls this method/class? Returns inbound CALLS edges up to `depth` hops.
        For Component FQNs, also finds callers of any of its methods.
        Each row: {fqn, file_path, start_line, end_line, community_id, edge_type, hop}
        """
        with self._driver.session() as session:
            # Direct callers of this exact FQN
            result = session.run(
                """
                MATCH (caller)-[:CALLS]->(target)
                WHERE (target:LogicUnit OR target:Component)
                  AND target.fqn = $fqn
                  AND (caller:LogicUnit OR caller:Component)
                RETURN DISTINCT
                    caller.fqn          AS fqn,
                    caller.file_path    AS file_path,
                    caller.start_line   AS start_line,
                    caller.end_line     AS end_line,
                    caller.community_id AS community_id,
                    'CALLS'             AS edge_type,
                    1                   AS hop
                ORDER BY caller.fqn
                LIMIT 30
                """,
                fqn=fqn,
            )
            rows = [dict(r) for r in result]

            # If this is a Component (class), also find callers of its methods
            if not rows:
                result2 = session.run(
                    """
                    MATCH (comp:Component {fqn: $fqn})-[:HAS_METHOD]->(lu:LogicUnit)
                    MATCH (caller)-[:CALLS]->(lu)
                    WHERE caller:LogicUnit OR caller:Component
                    RETURN DISTINCT
                        caller.fqn          AS fqn,
                        caller.file_path    AS file_path,
                        caller.start_line   AS start_line,
                        caller.end_line     AS end_line,
                        caller.community_id AS community_id,
                        'CALLS'             AS edge_type,
                        1                   AS hop
                    ORDER BY caller.fqn
                    LIMIT 30
                    """,
                    fqn=fqn,
                )
                rows = [dict(r) for r in result2]

        logger.info("get_callers('%s', depth=%d) → %d nodes", fqn, depth, len(rows))
        return rows

    @_neo4j_retry
    def get_callees(self, fqn: str, depth: int = 1) -> list[dict]:
        """
        What does this method/class call? Returns outbound CALLS edges up to `depth` hops.
        For Component FQNs, returns callees of all its methods.
        Each row: {fqn, file_path, start_line, end_line, community_id, hop}
        """
        with self._driver.session() as session:
            # Direct callees from this exact FQN
            result = session.run(
                """
                MATCH (target)-[:CALLS]->(callee)
                WHERE (target:LogicUnit OR target:Component)
                  AND target.fqn = $fqn
                  AND (callee:LogicUnit OR callee:Component)
                RETURN DISTINCT
                    callee.fqn          AS fqn,
                    callee.file_path    AS file_path,
                    callee.start_line   AS start_line,
                    callee.end_line     AS end_line,
                    callee.community_id AS community_id,
                    'CALLED_BY'         AS edge_type,
                    1                   AS hop
                ORDER BY callee.fqn
                LIMIT 30
                """,
                fqn=fqn,
            )
            rows = [dict(r) for r in result]

            # If this is a Component (class), aggregate callees across all its methods
            if not rows:
                result2 = session.run(
                    """
                    MATCH (comp:Component {fqn: $fqn})-[:HAS_METHOD]->(lu:LogicUnit)
                    MATCH (lu)-[:CALLS]->(callee)
                    WHERE callee:LogicUnit OR callee:Component
                    RETURN DISTINCT
                        callee.fqn          AS fqn,
                        callee.file_path    AS file_path,
                        callee.start_line   AS start_line,
                        callee.end_line     AS end_line,
                        callee.community_id AS community_id,
                        'CALLED_BY'         AS edge_type,
                        1                   AS hop
                    ORDER BY callee.fqn
                    LIMIT 30
                    """,
                    fqn=fqn,
                )
                rows = [dict(r) for r in result2]

        logger.info("get_callees('%s', depth=%d) → %d nodes", fqn, depth, len(rows))
        return rows

    @_neo4j_retry
    def find_throw_sites(self, exception_name: str) -> list[dict]:
        """
        Find all LogicUnit nodes that throw a given exception by name.
        Used by the `debug` command to anchor investigation.
        Returns {fqn, file_path, start_line, end_line, community_id}
        """
        with self._driver.session() as session:
            result = session.run(
                """
                MATCH (n:LogicUnit)
                WHERE (n.docstring IS NOT NULL AND n.docstring CONTAINS $exc)
                   OR (n.fqn CONTAINS $exc)
                   AND (n.file_path CONTAINS 'src/main/java' OR n.file_path CONTAINS 'src\\main\\java')
                RETURN
                    n.fqn          AS fqn,
                    n.file_path    AS file_path,
                    n.start_line   AS start_line,
                    n.end_line     AS end_line,
                    n.community_id AS community_id
                LIMIT 20
                """,
                exc=exception_name,
            )
            rows = [dict(r) for r in result]
        logger.info("find_throw_sites('%s') → %d nodes", exception_name, len(rows))
        return rows

    @_neo4j_retry
    def expand_capability_evidence(
        self,
        seed_fqns: list[str],
        chroma_client=None,
        domain_terms: list[str] | None = None,
    ) -> dict:
        """
        Given seeds found by Phase A grep (the direct implementation nodes), expand
        the evidence outward using the graph so the LLM can reason about the FULL
        implementation context without any hardcoded domain rules.

        Strategy:
          1. Fetch the callee graph (1-hop out): what do these methods call?
             If they implement a structural property (nesting, propagation) they
             MUST call something that reads the existing value from the incoming
             context.  If the callees only write constants / singletons, nesting
             is absent — the LLM can see this directly.
          2. Fetch the caller graph (1-hop in): who activates these methods?
             Shows the activation path and what context object is passed in.
          3. ChromaDB code_logic search: for each domain term, find methods whose
             actual method BODY (code_logic chunks) mentions the term — these are
             the methods that work with the concept at implementation level.

        Returns a dict with:
          callees   — list[dict] with fqn, file_path, start_line, end_line
          callers   — list[dict] with fqn, file_path, start_line, end_line
          code_logic_hits — list[dict] with fqn, file_path from ChromaDB code_logic
        """
        callees: list[dict] = []
        callers: list[dict] = []
        code_logic_hits: list[dict] = []

        if not seed_fqns:
            return {"callees": callees, "callers": callers, "code_logic_hits": code_logic_hits}

        with self._driver.session() as session:
            # 1-hop callees: methods/classes these seeds call
            result = session.run(
                """
                MATCH (seed)-[:CALLS]->(callee)
                WHERE seed.fqn IN $fqns
                  AND (callee:LogicUnit OR callee:Component)
                  AND (callee.file_path CONTAINS 'src/main/java' OR callee.file_path CONTAINS 'src\\main\\java')
                RETURN DISTINCT
                    callee.fqn        AS fqn,
                    callee.file_path  AS file_path,
                    callee.start_line AS start_line,
                    callee.end_line   AS end_line,
                    callee.community_id AS community_id
                LIMIT 15
                """,
                fqns=seed_fqns,
            )
            callees = [dict(r) for r in result]

            # 1-hop callers: what activates these seeds
            result = session.run(
                """
                MATCH (caller)-[:CALLS]->(seed)
                WHERE seed.fqn IN $fqns
                  AND (caller:LogicUnit OR caller:Component)
                  AND (caller.file_path CONTAINS 'src/main/java' OR caller.file_path CONTAINS 'src\\main\\java')
                RETURN DISTINCT
                    caller.fqn        AS fqn,
                    caller.file_path  AS file_path,
                    caller.start_line AS start_line,
                    caller.end_line   AS end_line,
                    caller.community_id AS community_id
                LIMIT 10
                """,
                fqns=seed_fqns,
            )
            callers = [dict(r) for r in result]

            # If no direct CALLS edges exist (e.g. leaf methods calling JDK only),
            # try DECLARES relationship: find the Component that declares these methods
            # and then get ALL other methods declared in those same Component classes.
            # This surfaces sibling methods — e.g. for a ClaimProvider, also fetching
            # other getAdditionalClaims overloads shows the full class behaviour.
            if not callees:
                result = session.run(
                    """
                    MATCH (comp)-[:DECLARES]->(seed)
                    WHERE seed.fqn IN $fqns
                      AND (comp:Component)
                    MATCH (comp)-[:DECLARES]->(sibling)
                    WHERE NOT sibling.fqn IN $fqns
                      AND (sibling:LogicUnit)
                    RETURN DISTINCT
                        sibling.fqn        AS fqn,
                        sibling.file_path  AS file_path,
                        sibling.start_line AS start_line,
                        sibling.end_line   AS end_line,
                        sibling.community_id AS community_id
                    LIMIT 10
                    """,
                    fqns=seed_fqns,
                )
                siblings = [dict(r) for r in result]
                # Also get callers of the Component itself (what invokes this class)
                result = session.run(
                    """
                    MATCH (comp)-[:DECLARES]->(seed)
                    WHERE seed.fqn IN $fqns AND (comp:Component)
                    MATCH (invoker)-[:CALLS|INSTANTIATES]->(comp)
                    WHERE (invoker:LogicUnit OR invoker:Component)
                      AND (invoker.file_path CONTAINS 'src/main/java' OR invoker.file_path CONTAINS 'src\\main\\java')
                    RETURN DISTINCT
                        invoker.fqn        AS fqn,
                        invoker.file_path  AS file_path,
                        invoker.start_line AS start_line,
                        invoker.end_line   AS end_line,
                        invoker.community_id AS community_id
                    LIMIT 10
                    """,
                    fqns=seed_fqns,
                )
                callers = [dict(r) for r in result]
                # Use siblings as callees (same class, full implementation context)
                callees = siblings

        logger.info(
            "expand_capability_evidence: %d seeds → %d callees, %d callers",
            len(seed_fqns), len(callees), len(callers),
        )

        # ChromaDB code_logic search: find method BODIES that reference the domain terms
        # code_logic chunks contain the actual method body text (not just docstrings),
        # so this surfaces any other method in the repo that operates on this concept.
        chroma = chroma_client or self._chroma
        if chroma and domain_terms:
            try:
                col = chroma.get_collection("code_logic")
                query_text = " ".join(domain_terms[:4])
                results = col.query(
                    query_texts=[query_text],
                    n_results=min(10, col.count()),
                )
                if results["ids"]:
                    seen_fqns = set(seed_fqns)
                    for i, doc_id in enumerate(results["ids"][0]):
                        meta = results["metadatas"][0][i]
                        fqn = meta.get("fqn", "")
                        if fqn and fqn not in seen_fqns:
                            seen_fqns.add(fqn)
                            fp = meta.get("file_path", "")
                            if "mirror" in fp:
                                fp = fp[fp.find("mirror"):]
                            code_logic_hits.append({
                                "fqn":        fqn,
                                "file_path":  fp,
                                "start_line": meta.get("start_line", 0),
                                "end_line":   meta.get("end_line", 0),
                                "source":     "code_logic",
                            })
            except Exception as e:
                logger.warning("code_logic expansion search failed: %s", e)

        logger.info("code_logic domain search → %d additional hits", len(code_logic_hits))
        return {"callees": callees, "callers": callers, "code_logic_hits": code_logic_hits}

    @_neo4j_retry
    def find_nodes_by_fqns(self, fqns: list[str]) -> list[dict]:
        """
        Bulk look-up LogicUnit/Component nodes by FQN list.
        Returns nodes with start_line and end_line so CodeFetcher can read them.
        Used to enrich semantic hits (which only carry fqn/file_path) before code fetch.
        """
        if not fqns:
            return []
        with self._driver.session() as session:
            result = session.run(
                """
                MATCH (n)
                WHERE (n:LogicUnit OR n:Component)
                  AND n.fqn IN $fqns
                RETURN
                    n.fqn          AS fqn,
                    n.file_path    AS file_path,
                    n.community_id AS community_id,
                    n.start_line   AS start_line,
                    n.end_line     AS end_line
                """,
                fqns=fqns,
            )
            rows = [dict(r) for r in result]
        logger.info("find_nodes_by_fqns: %d fqns → %d nodes", len(fqns), len(rows))
        return rows

    # ── Private ───────────────────────────────────────────────────────────────

    @_neo4j_retry
    def _simple_traversal(self, seed_fqns: list[str]) -> list[dict]:
        """
        Fallback 1-hop traversal when APOC is not available.
        Traverses CALLS, INJECTS, DEPENDS_ON, INSTANTIATES.
        """
        with self._driver.session() as session:
            result = session.run(
                """
                MATCH (seed)
                WHERE (seed:LogicUnit OR seed:Component)
                  AND seed.fqn IN $fqns
                MATCH (affected)-[r:CALLS|INJECTS|DEPENDS_ON|INSTANTIATES|OVERRIDES]->(seed)
                WHERE (affected:LogicUnit OR affected:Component)
                  AND (affected.file_path CONTAINS 'src/main/java' OR affected.file_path CONTAINS 'src\\main\\java')
                  AND affected.community_id IS NOT NULL
                RETURN DISTINCT
                    affected.fqn          AS fqn,
                    affected.file_path    AS file_path,
                    affected.community_id AS community_id,
                    type(r)               AS edge_type
                ORDER BY affected.community_id, affected.fqn
                """,
                fqns=seed_fqns,
            )
            return [dict(r) for r in result]
