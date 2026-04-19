"""
graph/sqlite_retriever.py
Live-API graph retriever backed by SQLite — replaces Neo4j for all query-time lookups.

Drop-in replacement for GraphRetriever: same public method signatures.
Neo4j is never touched by the live API after this migration.

Architecture:
  - CALLS edges loaded into memory at startup → O(1) per-hop BFS for blast radius
  - All simple lookups go to SQLite (FQN, file+line, community_id, etc.)
  - BM25 keyword search via SQLite FTS5 (replaces Neo4j fulltext index)
  - Vector search via ChromaDB (unchanged)
  - Community summaries via ChromaDB (unchanged)

RAM profile (100 repos):
  - CALLS edge dict:  ~50-80 MB  (vs 7-8 GiB for Neo4j)
  - SQLite file:      ~200-500 MB on disk, ~20-50 MB hot pages in RAM
"""
from __future__ import annotations
import logging
import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_VALID_INTENTS = {"safety", "code", "narrative", "impact", "capability", "general"}


class SqliteRetriever:
    """
    Drop-in replacement for GraphRetriever.
    Instantiate with db_path + chroma_client; no Neo4j driver required.
    """

    def __init__(self, db_path: Path | str, chroma_client=None):
        self._db_path  = str(db_path)
        self.chroma    = chroma_client

        # In-memory call graph for fast BFS (loaded once at startup)
        self._callers: dict[str, list[str]] = defaultdict(list)  # callee → [callers]
        self._callees: dict[str, list[str]] = defaultdict(list)  # caller → [callees]
        # Interface → implementing classes (reverse of IMPLEMENTS/EXTENDS edges)
        self._implementors: dict[str, list[str]] = defaultdict(list)
        self._load_call_graph()

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        return conn

    def _load_call_graph(self) -> None:
        """Load CALLS + IMPLEMENTS/EXTENDS edges into Python dicts for O(1) per-hop BFS."""
        try:
            with sqlite3.connect(self._db_path) as conn:
                rows = conn.execute(
                    "SELECT caller_fqn, callee_fqn FROM calls_edges"
                ).fetchall()
            for caller, callee in rows:
                self._callees[caller].append(callee)
                self._callers[callee].append(caller)
            logger.info(
                "SqliteRetriever: %d CALLS edges loaded (%d unique callers)",
                len(rows), len(self._callees),
            )
        except Exception as e:
            logger.warning("Could not load call graph from SQLite (%s): %s", self._db_path, e)

        try:
            with sqlite3.connect(self._db_path) as conn:
                impl_rows = conn.execute(
                    "SELECT src_fqn, dst_fqn FROM structure_edges "
                    "WHERE rel_type IN ('IMPLEMENTS', 'EXTENDS')"
                ).fetchall()
            for impl, iface in impl_rows:
                # iface → impl (so we can find implementations of a given interface)
                self._implementors[iface].append(impl)
            logger.info(
                "SqliteRetriever: %d IMPLEMENTS/EXTENDS edges loaded",
                len(impl_rows),
            )
        except Exception as e:
            logger.warning("Could not load IMPLEMENTS/EXTENDS edges (%s): %s", self._db_path, e)

    def close(self) -> None:
        pass  # SQLite connections are per-call; nothing persistent to close

    # ── Node lookups ──────────────────────────────────────────────────────────

    def find_nodes(self, entity_name: str) -> list[dict]:
        """Find nodes by FQN substring, simple name, or exact match."""
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT fqn, file_path, start_line, end_line,
                       node_type AS label, community_id
                FROM nodes
                WHERE fqn LIKE ?
                   OR fqn LIKE ?
                   OR fqn = ?
                LIMIT 20
            """, (f"%{entity_name}%", f"%.{entity_name}", entity_name)).fetchall()
        return [dict(r) for r in rows]

    def find_nodes_by_fqns(self, fqns: list[str]) -> list[dict]:
        if not fqns:
            return []
        ph = ",".join("?" * len(fqns))
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT fqn, file_path, community_id, start_line, end_line "
                f"FROM nodes WHERE fqn IN ({ph})",
                fqns,
            ).fetchall()
        return [dict(r) for r in rows]

    def find_node_by_location(self, file_path: str, line_number: int) -> list[dict]:
        """Map a grep hit (file:line) to the enclosing LogicUnit or Component."""
        # Normalise path separators for cross-platform matching
        norm = file_path.replace("\\", "/")
        filename = norm.split("/")[-1]

        with self._conn() as conn:
            # Strategy 1: exact line-range match on LogicUnit
            rows = conn.execute("""
                SELECT fqn, file_path, community_id, start_line, end_line
                FROM nodes
                WHERE node_type = 'LogicUnit'
                  AND (file_path LIKE ? OR REPLACE(file_path,'\\','/') LIKE ?)
                  AND start_line IS NOT NULL
                  AND start_line <= ? AND end_line >= ?
                ORDER BY (end_line - start_line)
                LIMIT 5
            """, (f"%{filename}", f"%{norm}%", line_number, line_number)).fetchall()

            if not rows:
                # Strategy 2: any LogicUnit in the same file
                rows = conn.execute("""
                    SELECT fqn, file_path, community_id, start_line, end_line
                    FROM nodes
                    WHERE node_type = 'LogicUnit'
                      AND (file_path LIKE ? OR REPLACE(file_path,'\\','/') LIKE ?)
                    LIMIT 5
                """, (f"%{filename}", f"%{norm}%")).fetchall()

            if not rows:
                # Strategy 3: Component (class) for the file
                rows = conn.execute("""
                    SELECT fqn, file_path, community_id, start_line, end_line
                    FROM nodes
                    WHERE node_type = 'Component'
                      AND (file_path LIKE ? OR REPLACE(file_path,'\\','/') LIKE ?)
                    LIMIT 3
                """, (f"%{filename}", f"%{norm}%")).fetchall()

        return [dict(r) for r in rows]

    def get_class_methods(self, class_fqns: list[str]) -> list[dict]:
        """Return all LogicUnits belonging to the same class as each given FQN."""
        if not class_fqns:
            return []
        seen: set[str] = set(class_fqns)
        results: list[dict] = []

        # Derive class prefixes: "com.example.Foo.method" → "com.example.Foo"
        prefixes: set[str] = set()
        for fqn in class_fqns:
            if "." in fqn:
                prefixes.add(fqn.rsplit(".", 1)[0])
            prefixes.add(fqn)

        with self._conn() as conn:
            for prefix in list(prefixes)[:8]:
                rows = conn.execute("""
                    SELECT fqn, file_path, start_line, end_line, community_id
                    FROM nodes
                    WHERE node_type = 'LogicUnit' AND fqn LIKE ?
                    ORDER BY start_line
                    LIMIT 50
                """, (f"{prefix}.%",)).fetchall()
                for row in rows:
                    if row["fqn"] not in seen:
                        results.append(dict(row))
                        seen.add(row["fqn"])
        return results

    # ── Blast radius (in-memory BFS) ──────────────────────────────────────────

    def compute_blast_radius(
        self, seed_fqns: list[str], depth: int | None = None,
    ) -> dict[str, Any]:
        """
        BFS over the in-memory callers dict — who transitively calls the seeds?
        Same semantics as the APOC path expansion in the old Neo4j retriever.
        """
        from config.settings import settings
        depth = depth or getattr(settings, "blast_radius_depth", 3) or 3

        visited: set[str] = set(seed_fqns)
        frontier: set[str] = set(seed_fqns)

        for _ in range(depth):
            nxt: set[str] = set()
            for fqn in frontier:
                # Follow CALLS edges upward (callers)
                for caller in self._callers.get(fqn, []):
                    if caller not in visited:
                        nxt.add(caller)
                        visited.add(caller)
                # Follow IMPLEMENTS/EXTENDS downward (implementations of this interface/class)
                for impl in self._implementors.get(fqn, []):
                    if impl not in visited:
                        nxt.add(impl)
                        visited.add(impl)
                # Also check short class-name suffix of fqn in case interface is stored unqualified
                short = fqn.rsplit(".", 1)[-1] if "." in fqn else fqn
                if short != fqn:
                    for impl in self._implementors.get(short, []):
                        if impl not in visited:
                            nxt.add(impl)
                            visited.add(impl)
            frontier = nxt
            if not frontier:
                break

        affected_fqns = list(visited - set(seed_fqns))
        affected_nodes: list[dict] = []
        community_ids: set[int] = set()

        if affected_fqns:
            ph = ",".join("?" * len(affected_fqns))
            with self._conn() as conn:
                rows = conn.execute(f"""
                    SELECT fqn, file_path, community_id, start_line, end_line,
                           'CALLS' AS edge_type
                    FROM nodes
                    WHERE fqn IN ({ph})
                      AND (file_path LIKE '%src/main/java%'
                           OR file_path LIKE '%src\\main\\java%')
                      AND community_id IS NOT NULL
                    ORDER BY community_id, fqn
                """, affected_fqns).fetchall()
            for row in rows:
                affected_nodes.append(dict(row))
                if row["community_id"] is not None:
                    community_ids.add(row["community_id"])

        return {"affected_nodes": affected_nodes, "community_ids": community_ids}

    # ── 1-hop callers / callees ───────────────────────────────────────────────

    def get_callers(self, fqn: str, depth: int = 1) -> list[dict]:
        caller_fqns = self._callers.get(fqn, [])[:30]
        if not caller_fqns:
            return []
        ph = ",".join("?" * len(caller_fqns))
        with self._conn() as conn:
            rows = conn.execute(f"""
                SELECT fqn, file_path, start_line, end_line, community_id,
                       'CALLS' AS edge_type, 1 AS hop
                FROM nodes WHERE fqn IN ({ph}) ORDER BY fqn
            """, caller_fqns).fetchall()
        return [dict(r) for r in rows]

    def get_callees(self, fqn: str, depth: int = 1) -> list[dict]:
        callee_fqns = self._callees.get(fqn, [])[:30]
        if not callee_fqns:
            return []
        ph = ",".join("?" * len(callee_fqns))
        with self._conn() as conn:
            rows = conn.execute(f"""
                SELECT fqn, file_path, start_line, end_line, community_id,
                       'CALLED_BY' AS edge_type, 1 AS hop
                FROM nodes WHERE fqn IN ({ph}) ORDER BY fqn
            """, callee_fqns).fetchall()
        return [dict(r) for r in rows]

    # ── Property access ───────────────────────────────────────────────────────

    def find_property_readers(self, key: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT n.fqn, n.file_path, n.start_line, n.end_line, n.community_id
                FROM property_edges pe
                JOIN nodes n ON n.fqn = pe.lu_fqn
                WHERE pe.direction = 'read'
                  AND (pe.prop_key = ? OR pe.prop_key LIKE ?)
                ORDER BY n.fqn LIMIT 20
            """, (key, f"%{key}%")).fetchall()
        return [dict(r) for r in rows]

    def find_property_writers(self, key: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT n.fqn, n.file_path, n.start_line, n.end_line, n.community_id
                FROM property_edges pe
                JOIN nodes n ON n.fqn = pe.lu_fqn
                WHERE pe.direction = 'write'
                  AND (pe.prop_key = ? OR pe.prop_key LIKE ?)
                ORDER BY n.fqn LIMIT 20
            """, (key, f"%{key}%")).fetchall()
        return [dict(r) for r in rows]

    # ── DataSink ──────────────────────────────────────────────────────────────

    def get_datasink_tables(self, fqns: list[str]) -> list[dict]:
        if not fqns:
            return []
        ph = ",".join("?" * len(fqns))
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT component_fqn, table_name, source_file, repo_name "
                f"FROM datasink_tables WHERE component_fqn IN ({ph}) ORDER BY table_name",
                fqns,
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Hybrid search ─────────────────────────────────────────────────────────

    def hybrid_search(
        self, query: str, n_results: int = 15, embedder=None,
    ) -> list[dict]:
        """BM25 (SQLite FTS5) + semantic (ChromaDB) merged with RRF."""
        bm25   = self._bm25_search(query, n_results)
        sem    = self._semantic_search(query, n_results)
        merged = self._rrf_merge(bm25, sem, n_results)
        return merged

    def _bm25_search(self, query: str, n: int) -> list[dict]:
        """SQLite FTS5 BM25 — replaces Neo4j fulltext index."""
        # Strip special FTS5 characters to avoid syntax errors
        safe = re.sub(r'[^\w\s]', ' ', query).strip()
        if not safe:
            return []
        try:
            with self._conn() as conn:
                rows = conn.execute("""
                    SELECT f.fqn,
                           n.file_path, n.start_line, n.end_line, n.community_id,
                           bm25(nodes_fts) AS score
                    FROM nodes_fts f
                    JOIN nodes n ON n.fqn = f.fqn
                    WHERE nodes_fts MATCH ?
                    ORDER BY score          -- bm25() returns negative; lower = better
                    LIMIT ?
                """, (safe, n)).fetchall()
            return [{
                "fqn": r["fqn"], "file_path": r["file_path"],
                "start_line": r["start_line"], "end_line": r["end_line"],
                "community_id": r["community_id"],
                "score": abs(r["score"]), "source": "bm25",
            } for r in rows]
        except Exception as e:
            logger.debug("BM25 search error: %s", e)
            return []

    def _semantic_search(self, query: str, n: int) -> list[dict]:
        """ChromaDB vector search (unchanged from GraphRetriever)."""
        if not self.chroma:
            return []
        try:
            col = self.chroma.get_collection("code_intent")
            results = col.query(query_texts=[query], n_results=n)
            hits = []
            if results.get("ids"):
                for i, _doc_id in enumerate(results["ids"][0]):
                    meta = (results.get("metadatas") or [[]])[0][i] if results.get("metadatas") else {}
                    dist = (results.get("distances") or [[0]])[0][i]
                    hits.append({
                        "fqn": meta.get("fqn", _doc_id),
                        "file_path": meta.get("file_path", ""),
                        "start_line": meta.get("start_line"),
                        "end_line": meta.get("end_line"),
                        "community_id": meta.get("community_id"),
                        "score": max(0.0, 1.0 - dist),
                        "source": "semantic",
                    })
            return hits
        except Exception as e:
            logger.debug("Semantic search error: %s", e)
            return []

    @staticmethod
    def _rrf_merge(
        bm25: list[dict], semantic: list[dict], n: int, k: int = 60,
    ) -> list[dict]:
        scores: dict[str, float] = {}
        meta:   dict[str, dict]  = {}
        for rank, hit in enumerate(bm25):
            fqn = hit["fqn"]
            scores[fqn] = scores.get(fqn, 0.0) + 1.0 / (k + rank + 1)
            meta[fqn]   = hit
        for rank, hit in enumerate(semantic):
            fqn = hit["fqn"]
            scores[fqn] = scores.get(fqn, 0.0) + 1.0 / (k + rank + 1)
            meta.setdefault(fqn, hit)
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:n]
        return [meta[fqn] for fqn, _ in ranked]

    # ── Community summaries (ChromaDB — unchanged) ────────────────────────────

    def get_community_summaries_by_ids(
        self, chroma_client, community_ids: list[int], collection_name: str = "community_summaries",
    ) -> list[dict]:
        if not community_ids or not self.chroma:
            return []
        try:
            col = self.chroma.get_collection(collection_name)
            results = col.get(
                where={"community_id": {"$in": list(community_ids)}},
                include=["documents", "metadatas"],
            )
            summaries = []
            if results and results.get("ids"):
                for i, _id in enumerate(results["ids"]):
                    meta = (results.get("metadatas") or [{}])[i]
                    doc  = (results.get("documents")  or [""])[i]
                    summaries.append({
                        "community_id": meta.get("community_id", 0),
                        "summary_text": doc,
                        "metadata":     meta,
                    })
            return summaries
        except Exception as e:
            logger.warning("Community summaries fetch failed: %s", e)
            return []

    # ── Capability evidence expansion ─────────────────────────────────────────

    def expand_capability_evidence(
        self,
        seed_fqns: list[str],
        chroma_client=None,
        domain_terms: list[str] | None = None,
    ) -> dict:
        """Expand seeds with 1-hop callees, callers, and sibling methods."""
        seen: set[str] = set(seed_fqns)
        callee_fqns: list[str] = []
        caller_fqns: list[str] = []
        sibling_fqns: list[str] = []

        for fqn in seed_fqns[:5]:
            for c in self._callees.get(fqn, [])[:15]:
                if c not in seen:
                    callee_fqns.append(c)
                    seen.add(c)
            for c in self._callers.get(fqn, [])[:10]:
                if c not in seen:
                    caller_fqns.append(c)
                    seen.add(c)

        # Sibling methods (same class prefix)
        for fqn in seed_fqns[:3]:
            prefix = fqn.rsplit(".", 1)[0] if "." in fqn else fqn
            with self._conn() as conn:
                ph = ",".join("?" * len(seed_fqns))
                rows = conn.execute(f"""
                    SELECT fqn FROM nodes
                    WHERE node_type = 'LogicUnit' AND fqn LIKE ?
                      AND fqn NOT IN ({ph})
                    LIMIT 10
                """, [f"{prefix}.%"] + seed_fqns).fetchall()
            for row in rows:
                f = row["fqn"] if isinstance(row, sqlite3.Row) else row[0]
                if f not in seen:
                    sibling_fqns.append(f)
                    seen.add(f)

        all_fqns = callee_fqns[:15] + caller_fqns[:10] + sibling_fqns[:10]
        by_fqn   = {n["fqn"]: n for n in self.find_nodes_by_fqns(all_fqns)}

        # ChromaDB code_logic search — method bodies that reference the domain terms
        code_logic_hits: list[dict] = []
        if chroma_client and domain_terms:
            try:
                col = chroma_client.get_collection("code_logic")
                query_text = " ".join(domain_terms[:4])
                results = col.query(
                    query_texts=[query_text],
                    n_results=min(10, col.count()),
                )
                if results["ids"]:
                    seen_fqns = set(seed_fqns)
                    for i, _doc_id in enumerate(results["ids"][0]):
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
                logger.debug("code_logic expansion search failed: %s", e)

        return {
            "callees":         [by_fqn[f] for f in callee_fqns  if f in by_fqn],
            "callers":         [by_fqn[f] for f in caller_fqns  if f in by_fqn],
            "siblings":        [by_fqn[f] for f in sibling_fqns if f in by_fqn],
            "code_logic_hits": code_logic_hits,
        }

    # ── Throw sites (for debug command) ──────────────────────────────────────

    def find_throw_sites(self, exception_name: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT fqn, file_path, start_line, end_line, community_id
                FROM nodes
                WHERE node_type = 'LogicUnit'
                  AND fqn LIKE ?
                  AND (file_path LIKE '%src/main/java%'
                       OR file_path LIKE '%src\\main\\java%')
                LIMIT 20
            """, (f"%{exception_name}%",)).fetchall()
        return [dict(r) for r in rows]
