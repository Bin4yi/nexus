"""
graph/sqlite_loader.py
SQLite-native graph writer — replaces Neo4jLoader, NodeTagger, and SQLiteExporter.

Writes directly to SQLite during ingest. No Neo4j required.
The live API reads exclusively from the resulting SQLite file.

Schema additions vs. the old sqlite_exporter:
  structure_edges  — CALLS + IMPLEMENTS + EXTENDS + INJECTS + RESOLVES_TO
                     used by IGraphCommunityClient for Leiden
  repo_meta        — per-repo last_ingested_sha and last_indexed timestamp
  rfc_specs        — RFC Specification nodes
  rfc_sections     — RFC Section nodes
  implements_spec  — IMPLEMENTS_SPEC edges
"""
from __future__ import annotations
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from config.settings import settings
from parsers.uir import Project, Module, Component, LogicUnit

logger = logging.getLogger(__name__)

# ── Tagging constants (mirrors tagger.py) ────────────────────────────────────

ENTRY_POINT_ANNOTATIONS = {
    "Path", "GET", "POST", "PUT", "DELETE", "PATCH",
    "RequestMapping", "GetMapping", "PostMapping", "PutMapping",
    "DeleteMapping", "PatchMapping", "RestController", "Controller",
    "WebServlet",
}
ENTRY_POINT_FQN_PATTERNS = ("Servlet", "Controller", "Endpoint", "Resource")
ENTRY_POINT_EXTENDS = {
    "HttpServlet", "AbstractHttpServlet", "IdentityServlet", "FrameworkServlet",
}
ENTRY_POINT_METHOD_ANNOTATIONS = {
    "GET", "POST", "PUT", "DELETE", "PATCH",
    "RequestMapping", "GetMapping", "PostMapping", "PutMapping",
    "DeleteMapping", "PatchMapping", "Path",
}

DATA_SINK_ANNOTATIONS = {"Repository"}
DATA_SINK_FQN_PATTERNS = (
    "DAO", "DataAccessor", "Repository",
    "JDBCPersistenceManager", "AbstractDAO", "JdbcTemplate",
)
DATA_SINK_METHOD_INDICATORS = {
    "executeQuery", "executeUpdate", "prepareStatement",
    "createStatement", "prepareCall",
}

_FRAMEWORK_HIGH = {
    "Path", "GET", "POST", "PUT", "DELETE", "PATCH",
    "RequestMapping", "GetMapping", "PostMapping", "PutMapping",
    "DeleteMapping", "PatchMapping", "WebServlet",
}
_FRAMEWORK_MED = {
    "RestController", "Controller", "Service", "Component", "FrameworkServlet",
}
_NAME_BONUS   = ("handle", "on", "process", "execute", "dispatch")
_NAME_PENALTY = ("get", "is", "set", "has", "to", "from")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_annotations(annotations_raw) -> set[str]:
    """Extract annotation simple-names from a JSON string or plain list."""
    if not annotations_raw:
        return set()
    if isinstance(annotations_raw, list):
        items = annotations_raw
    else:
        try:
            items = json.loads(annotations_raw)
        except (json.JSONDecodeError, TypeError):
            return set()
    result: set[str] = set()
    for a in items:
        if isinstance(a, dict):
            result.add(a.get("name", "").lstrip("@").split("(")[0])
        elif isinstance(a, str):
            result.add(a.lstrip("@").split("(")[0])
    return result


def _is_entry_point(comp: Component) -> bool:
    anns = _parse_annotations(getattr(comp, "annotations", None))
    if anns & ENTRY_POINT_ANNOTATIONS:
        return True
    if any(p in comp.fqn for p in ENTRY_POINT_FQN_PATTERNS):
        return True
    ext = getattr(comp, "extends", None) or ""
    if any(p in ext for p in ENTRY_POINT_EXTENDS):
        return True
    return False


def _is_data_sink(comp: Component) -> bool:
    anns = _parse_annotations(getattr(comp, "annotations", None))
    if anns & DATA_SINK_ANNOTATIONS:
        return True
    if any(p in comp.fqn for p in DATA_SINK_FQN_PATTERNS):
        return True
    return False


def _is_data_sink_method(lu: LogicUnit) -> bool:
    body = getattr(lu, "body_text", "") or ""
    return any(ind in body for ind in DATA_SINK_METHOD_INDICATORS)


# ── SQLiteLoader ──────────────────────────────────────────────────────────────

class SQLiteLoader:
    """
    Writes the code graph directly to SQLite during ingest.
    Drop-in replacement for Neo4jLoader + NodeTagger + SQLiteExporter.

    The database is opened in WAL mode for concurrent reads from the live API.
    Call open() before use (or use as a context manager).
    """

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path or settings.sqlite_db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        # In-memory annotation / metadata caches used for deferred scoring
        self._fqn_to_anns: dict[str, set[str]] = {}
        self._fqn_to_meta: dict[str, dict]     = {}

    # ── Connection management ─────────────────────────────────────────────────

    def open(self) -> None:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA cache_size=-65536")   # 64 MB page cache
            self._create_schema()

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *_):
        self.close()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self.open()
        return self._conn

    # ── SHA tracking (replaces Neo4j Project node) ────────────────────────────

    def get_ingested_sha(self, repo_name: str) -> Optional[str]:
        """Return the last successfully ingested commit SHA for a repo."""
        row = self.conn.execute(
            "SELECT last_ingested_sha FROM repo_meta WHERE repo_name = ?",
            (repo_name,),
        ).fetchone()
        return row[0] if row else None

    def set_ingested_sha(self, repo_name: str, sha: str) -> None:
        """Record the commit SHA of a successfully completed ingest for a repo."""
        self.conn.execute(
            """
            INSERT INTO repo_meta (repo_name, last_ingested_sha, last_indexed)
            VALUES (?, ?, ?)
            ON CONFLICT(repo_name) DO UPDATE
              SET last_ingested_sha = excluded.last_ingested_sha,
                  last_indexed      = excluded.last_indexed
            """,
            (repo_name, sha, int(time.time())),
        )
        self.conn.commit()

    # ── Node loading ──────────────────────────────────────────────────────────

    def load_project(self, project: Project) -> None:
        """Write Component + LogicUnit nodes from a parsed Project to SQLite."""
        node_rows: list[tuple] = []
        fts_rows:  list[tuple] = []

        for module in project.modules:
            for comp in module.components:
                is_ep = _is_entry_point(comp)
                is_ds = _is_data_sink(comp)
                anns  = _parse_annotations(getattr(comp, "annotations", None))
                self._fqn_to_anns[comp.fqn] = anns
                self._fqn_to_meta[comp.fqn] = {
                    "visibility": getattr(comp, "visibility", None),
                    "name":       getattr(comp, "name", comp.fqn.split(".")[-1]),
                }
                node_rows.append((
                    comp.fqn, comp.geid, "Component",
                    getattr(comp, "kind", "class"),
                    getattr(comp, "file_path", None),
                    getattr(comp, "start_line", None),
                    getattr(comp, "end_line", None),
                    None,   # community_id — set later by Leiden
                    getattr(comp, "visibility", None),
                    1 if getattr(comp, "is_abstract", False) else 0,
                    1 if getattr(comp, "is_static",   False) else 0,
                    1 if getattr(comp, "is_final",    False) else 0,
                    1 if is_ep else 0,
                    1 if is_ds else 0,
                    None, None,   # entry_point_score, blast_radius_risk
                ))
                simple_name = comp.fqn.split(".")[-1]
                fts_rows.append((comp.fqn, f"{simple_name} {getattr(comp, 'kind', 'class')}"))

                for lu in comp.logic_units:
                    lu_is_ds = is_ds or _is_data_sink_method(lu)
                    lu_anns  = _parse_annotations(getattr(lu, "annotations", None))
                    lu_is_ep = (
                        is_ep and bool(lu_anns & ENTRY_POINT_METHOD_ANNOTATIONS)
                    )
                    self._fqn_to_anns[lu.fqn] = lu_anns
                    self._fqn_to_meta[lu.fqn] = {
                        "visibility": getattr(lu, "visibility", None),
                        "name":       getattr(lu, "name", lu.fqn.split(".")[-1]),
                    }
                    node_rows.append((
                        lu.fqn, lu.geid, "LogicUnit",
                        getattr(lu, "kind", "method"),
                        getattr(lu, "file_path", getattr(comp, "file_path", None)),
                        getattr(lu, "start_line", None),
                        getattr(lu, "end_line",   None),
                        None,
                        getattr(lu, "visibility", None),
                        0, 0, 0,
                        1 if lu_is_ep else 0,
                        1 if lu_is_ds else 0,
                        None, None,
                    ))
                    lu_simple = lu.fqn.split(".")[-1]
                    fts_rows.append((lu.fqn, f"{lu_simple} {getattr(lu, 'kind', 'method')}"))

        self.conn.executemany(
            """
            INSERT OR REPLACE INTO nodes
              (fqn, geid, node_type, kind, file_path, start_line, end_line,
               community_id, visibility, is_abstract, is_static, is_final,
               is_entry_point, is_data_sink, entry_point_score, blast_radius_risk)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            node_rows,
        )
        # FTS: remove stale entries, then insert fresh
        for fqn in (r[0] for r in node_rows):
            self.conn.execute("DELETE FROM nodes_fts WHERE fqn = ?", (fqn,))
        self.conn.executemany(
            "INSERT INTO nodes_fts (fqn, searchtext) VALUES (?,?)",
            fts_rows,
        )
        self.conn.commit()
        logger.info("Loaded project '%s': %d nodes", project.name, len(node_rows))

    # ── Edge loading ──────────────────────────────────────────────────────────

    def load_call_graph(self, all_logic_units: list[LogicUnit]) -> None:
        """Write CALLS edges to calls_edges and structure_edges tables."""
        calls_rows:  list[tuple[str, str]]        = []
        struct_rows: list[tuple[str, str, str]]   = []
        for lu in all_logic_units:
            for callee_fqn in getattr(lu, "calls", []):
                calls_rows.append((lu.fqn, callee_fqn))
                struct_rows.append((lu.fqn, callee_fqn, "CALLS"))
        if calls_rows:
            self.conn.executemany(
                "INSERT OR IGNORE INTO calls_edges (caller_fqn, callee_fqn) VALUES (?,?)",
                calls_rows,
            )
            self.conn.executemany(
                "INSERT OR IGNORE INTO structure_edges (src_fqn, dst_fqn, rel_type) VALUES (?,?,?)",
                struct_rows,
            )
            self.conn.commit()
        logger.info("Loaded %d CALLS edges", len(calls_rows))

    def load_unresolved_calls(self, all_logic_units: list[LogicUnit]) -> None:
        """Cross-repo suffix matching for unresolved call targets."""
        known_fqns = {
            row[0] for row in self.conn.execute("SELECT fqn FROM nodes")
        }
        calls_rows:  list[tuple[str, str]]      = []
        struct_rows: list[tuple[str, str, str]] = []
        for lu in all_logic_units:
            for callee_raw in getattr(lu, "unresolved_calls", []):
                matched = [f for f in known_fqns if f.endswith(callee_raw)]
                for callee_fqn in matched[:1]:
                    calls_rows.append((lu.fqn, callee_fqn))
                    struct_rows.append((lu.fqn, callee_fqn, "CALLS"))
        if calls_rows:
            self.conn.executemany(
                "INSERT OR IGNORE INTO calls_edges (caller_fqn, callee_fqn) VALUES (?,?)",
                calls_rows,
            )
            self.conn.executemany(
                "INSERT OR IGNORE INTO structure_edges (src_fqn, dst_fqn, rel_type) VALUES (?,?,?)",
                struct_rows,
            )
            self.conn.commit()
            logger.info("Loaded %d unresolved cross-repo CALLS edges", len(calls_rows))

    def load_implements_extends(self, all_components: list[Component]) -> None:
        """Write IMPLEMENTS and EXTENDS to structure_edges."""
        rows: list[tuple[str, str, str]] = []
        for comp in all_components:
            for iface in getattr(comp, "implements", []):
                rows.append((comp.fqn, iface, "IMPLEMENTS"))
            ext = getattr(comp, "extends", None)
            if ext:
                rows.append((comp.fqn, ext, "EXTENDS"))
        if rows:
            self.conn.executemany(
                "INSERT OR IGNORE INTO structure_edges (src_fqn, dst_fqn, rel_type) VALUES (?,?,?)",
                rows,
            )
            self.conn.commit()
            logger.info("Loaded %d IMPLEMENTS/EXTENDS structure edges", len(rows))

    def load_injection_edges(self, all_components: list[Component]) -> None:
        """Write INJECTS structure edges (DI wiring)."""
        rows: list[tuple[str, str, str]] = []
        for comp in all_components:
            for injected_fqn in getattr(comp, "injects", []):
                rows.append((comp.fqn, injected_fqn, "INJECTS"))
        if rows:
            self.conn.executemany(
                "INSERT OR IGNORE INTO structure_edges (src_fqn, dst_fqn, rel_type) VALUES (?,?,?)",
                rows,
            )
            self.conn.commit()
            logger.info("Loaded %d INJECTS structure edges", len(rows))

    def load_resolves_to_edges(self, osgi_edges: list) -> None:
        """Write OSGi RESOLVES_TO edges into structure_edges."""
        rows: list[tuple[str, str, str]] = []
        for edge in osgi_edges:
            src = getattr(edge, "reference_fqn",  None) or (edge.get("reference_fqn")  if isinstance(edge, dict) else None)
            dst = getattr(edge, "component_fqn",  None) or (edge.get("component_fqn")  if isinstance(edge, dict) else None)
            if src and dst:
                rows.append((src, dst, "RESOLVES_TO"))
        if rows:
            self.conn.executemany(
                "INSERT OR IGNORE INTO structure_edges (src_fqn, dst_fqn, rel_type) VALUES (?,?,?)",
                rows,
            )
            self.conn.commit()
            logger.info("Loaded %d RESOLVES_TO (OSGi) structure edges", len(rows))

    def load_property_access_edges(self, all_logic_units: list[LogicUnit]) -> None:
        """Write READS_PROPERTY / WRITES_PROPERTY into property_edges."""
        rows: list[tuple[str, str, str]] = []
        for lu in all_logic_units:
            for key in getattr(lu, "reads_properties",  []):
                rows.append((lu.fqn, key, "read"))
            for key in getattr(lu, "writes_properties", []):
                rows.append((lu.fqn, key, "write"))
        if rows:
            self.conn.executemany(
                "INSERT OR IGNORE INTO property_edges (lu_fqn, prop_key, direction) VALUES (?,?,?)",
                rows,
            )
            self.conn.commit()
            logger.info("Loaded %d property edges", len(rows))

    def load_database_tables(self, db_tables: list) -> None:
        """No-op stub — table nodes are created implicitly via load_queries_table_edges."""
        logger.debug(
            "load_database_tables: %d table definitions noted "
            "(rows created in datasink_tables via load_queries_table_edges)",
            len(db_tables),
        )

    def load_queries_table_edges(self, table_edges: list) -> None:
        """Write QUERIES_TABLE edges and mark component nodes as DataSink."""
        rows: list[tuple] = []
        ds_fqns: list[str] = []
        for edge in table_edges:
            comp_fqn   = getattr(edge, "component_fqn",  None) or (edge.get("component_fqn")  if isinstance(edge, dict) else None)
            table_name = getattr(edge, "table_name",      None) or (edge.get("table_name")      if isinstance(edge, dict) else None)
            source_file = getattr(edge, "source_file",   None) or (edge.get("source_file")      if isinstance(edge, dict) else None)
            repo_name   = getattr(edge, "repo_name",     None) or (edge.get("repo_name")        if isinstance(edge, dict) else None)
            if comp_fqn and table_name:
                rows.append((comp_fqn, table_name, source_file, repo_name))
                ds_fqns.append(comp_fqn)
        if rows:
            self.conn.executemany(
                "INSERT OR IGNORE INTO datasink_tables "
                "(component_fqn, table_name, source_file, repo_name) VALUES (?,?,?,?)",
                rows,
            )
        if ds_fqns:
            self.conn.executemany(
                "UPDATE nodes SET is_data_sink = 1 WHERE fqn = ?",
                [(f,) for f in ds_fqns],
            )
        self.conn.commit()
        logger.info("Loaded %d QUERIES_TABLE edges", len(rows))

    def load_configuration_nodes(self, config_entries: list) -> None:
        """No-op — config data lives in ChromaDB chunks, not in the graph."""
        logger.debug("load_configuration_nodes: %d entries (stored in ChromaDB chunks only)", len(config_entries))

    def load_reads_config_edges(self, config_edges: list) -> None:
        """No-op — config read edges not stored in the SQLite graph."""
        logger.debug("load_reads_config_edges: %d edges skipped", len(config_edges))

    def load_specification_nodes(self, rfc_specs: list) -> None:
        """Write RFC Specification nodes to rfc_specs table."""
        rows: list[tuple] = []
        for spec in rfc_specs:
            rfc_id = getattr(spec, "rfc_id", None) or (spec.get("rfc_id", "") if isinstance(spec, dict) else "")
            title  = getattr(spec, "title",  None) or (spec.get("title",  "") if isinstance(spec, dict) else "")
            rows.append((f"spec:{rfc_id}", rfc_id, title))
        if rows:
            self.conn.executemany(
                "INSERT OR REPLACE INTO rfc_specs (fqn, rfc_id, title) VALUES (?,?,?)",
                rows,
            )
            self.conn.commit()
        logger.info("Loaded %d RFC specification nodes", len(rows))

    def load_specification_section_nodes(self, rfc_sections: list) -> None:
        """Write RFC section nodes to rfc_sections table."""
        rows: list[tuple] = []
        for sec in rfc_sections:
            sec_fqn    = getattr(sec, "fqn",        None) or (sec.get("fqn",        "") if isinstance(sec, dict) else "")
            parent_fqn = getattr(sec, "spec_fqn",   None) or (sec.get("spec_fqn",   "") if isinstance(sec, dict) else "")
            section_id = getattr(sec, "section_id", None) or (sec.get("section_id", "") if isinstance(sec, dict) else "")
            text       = getattr(sec, "text",        None) or (sec.get("text",       "") if isinstance(sec, dict) else "")
            rows.append((sec_fqn, parent_fqn, section_id, text))
        if rows:
            self.conn.executemany(
                "INSERT OR REPLACE INTO rfc_sections (fqn, spec_fqn, section_id, text) VALUES (?,?,?,?)",
                rows,
            )
            self.conn.commit()
        logger.info("Loaded %d RFC section nodes", len(rows))

    def load_implements_spec_edges(self, rfc_edges: list) -> None:
        """Write IMPLEMENTS_SPEC edges to implements_spec table."""
        rows: list[tuple] = []
        for edge in rfc_edges:
            comp_fqn = (
                getattr(edge, "component_fqn", None)
                or (edge.get("component_fqn") if isinstance(edge, dict) else None)
                or ""
            )
            spec_fqn = (
                getattr(edge, "spec_section_fqn", None)
                or getattr(edge, "spec_fqn",      None)
                or (edge.get("spec_section_fqn") if isinstance(edge, dict) else None)
                or (edge.get("spec_fqn")          if isinstance(edge, dict) else None)
                or ""
            )
            if comp_fqn and spec_fqn:
                rows.append((comp_fqn, spec_fqn))
        if rows:
            self.conn.executemany(
                "INSERT OR IGNORE INTO implements_spec (component_fqn, spec_fqn) VALUES (?,?)",
                rows,
            )
            self.conn.commit()
        logger.info("Loaded %d IMPLEMENTS_SPEC edges", len(rows))

    def load_dependency_edges(self, source_module_geid: str, deps: list) -> None:
        """Maven dependency edges — skipped (not needed for live API queries)."""
        logger.debug("load_dependency_edges: %d deps from %s skipped", len(deps), source_module_geid)

    def load_annotated_with(self, all_components: list[Component]) -> None:
        """No-op — annotation data captured inline during load_project."""
        logger.debug("load_annotated_with: handled inline in load_project")

    def load_type_edges(self, all_logic_units: list[LogicUnit]) -> None:
        """No-op — RETURNS/RECEIVES edges not used by live API queries."""
        logger.debug("load_type_edges: skipped")

    def load_remote_calls(self, edge_dicts: list[dict]) -> None:
        """Write REMOTE_CALLS to calls_edges (treated as weighted CALLS)."""
        rows: list[tuple[str, str]] = []
        for e in edge_dicts:
            caller = e.get("caller_geid") or e.get("caller_fqn")
            callee = e.get("callee_geid") or e.get("callee_fqn")
            if caller and callee:
                rows.append((caller, callee))
        if rows:
            self.conn.executemany(
                "INSERT OR IGNORE INTO calls_edges (caller_fqn, callee_fqn) VALUES (?,?)",
                rows,
            )
            self.conn.commit()
        logger.info("Loaded %d REMOTE_CALLS edges", len(rows))

    def load_throws_edges(self, all_logic_units: list[LogicUnit]) -> None:
        """No-op — THROWS edges not stored."""
        logger.debug("load_throws_edges: skipped")

    def load_overrides_edges(self, all_logic_units: list[LogicUnit]) -> None:
        """Write OVERRIDES to structure_edges (adds Leiden clustering signal)."""
        rows: list[tuple[str, str, str]] = []
        for lu in all_logic_units:
            overrides = getattr(lu, "overrides", None)
            if overrides:
                rows.append((lu.fqn, overrides, "OVERRIDES"))
        if rows:
            self.conn.executemany(
                "INSERT OR IGNORE INTO structure_edges (src_fqn, dst_fqn, rel_type) VALUES (?,?,?)",
                rows,
            )
            self.conn.commit()
            logger.info("Loaded %d OVERRIDES structure edges", len(rows))

    def load_instantiates_edges(self, all_logic_units: list[LogicUnit]) -> None:
        """No-op — INSTANTIATES not stored."""
        logger.debug("load_instantiates_edges: skipped")

    def load_event_handler_edges(self, all_components: list[Component]) -> None:
        """No-op — event handler edges not stored."""
        logger.debug("load_event_handler_edges: skipped")

    # ── Post-load: tagging & scoring ─────────────────────────────────────────

    def compute_entry_point_scores(self) -> int:
        """
        Compute entry_point_score for all is_entry_point=1 nodes after all
        edges are loaded. Uses SQL aggregates over calls_edges.
        Returns number of nodes scored.
        """
        rows = self.conn.execute(
            "SELECT fqn, visibility FROM nodes WHERE is_entry_point = 1"
        ).fetchall()
        if not rows:
            return 0

        scored: list[tuple[float, str]] = []
        for fqn, vis in rows:
            out_calls = self.conn.execute(
                "SELECT COUNT(*) FROM calls_edges WHERE caller_fqn = ?", (fqn,)
            ).fetchone()[0]
            in_calls = self.conn.execute(
                "SELECT COUNT(*) FROM calls_edges WHERE callee_fqn = ?", (fqn,)
            ).fetchone()[0]

            export_mult = {"public": 2.0, "protected": 1.5}.get((vis or "").lower(), 1.0)
            meta = self._fqn_to_meta.get(fqn, {})
            name = meta.get("name", fqn.split(".")[-1])
            lower = name.lower()
            if any(lower.startswith(p) for p in _NAME_BONUS):
                naming_mult = 1.5
            elif any(lower.startswith(p) for p in _NAME_PENALTY):
                naming_mult = 0.3
            else:
                naming_mult = 1.0

            anns = self._fqn_to_anns.get(fqn, set())
            if anns & _FRAMEWORK_HIGH:
                framework_mult = 3.0
            elif anns & _FRAMEWORK_MED:
                framework_mult = 1.5
            else:
                framework_mult = 1.0

            call_ratio = out_calls / (in_calls + 1)
            effective_ratio = max(call_ratio, 1.0) if framework_mult > 1.0 else call_ratio
            score = round(effective_ratio * export_mult * naming_mult * framework_mult, 4)
            scored.append((score, fqn))

        self.conn.executemany(
            "UPDATE nodes SET entry_point_score = ? WHERE fqn = ?",
            scored,
        )
        self.conn.commit()
        logger.info("Scored %d entry point nodes", len(scored))
        return len(scored)

    def get_entry_point_count(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE is_entry_point = 1"
        ).fetchone()[0]

    def get_data_sink_count(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE is_data_sink = 1"
        ).fetchone()[0]

    def optimize_fts(self) -> None:
        """Optimize FTS5 index after bulk inserts."""
        self.conn.execute("INSERT INTO nodes_fts(nodes_fts) VALUES('optimize')")
        self.conn.commit()
        logger.info("FTS5 index optimized")

    # ── Schema ────────────────────────────────────────────────────────────────

    def _create_schema(self) -> None:
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS nodes (
                fqn               TEXT PRIMARY KEY,
                geid              TEXT,
                node_type         TEXT,
                kind              TEXT,
                file_path         TEXT,
                start_line        INTEGER,
                end_line          INTEGER,
                community_id      INTEGER,
                visibility        TEXT,
                is_abstract       INTEGER DEFAULT 0,
                is_static         INTEGER DEFAULT 0,
                is_final          INTEGER DEFAULT 0,
                is_entry_point    INTEGER DEFAULT 0,
                is_data_sink      INTEGER DEFAULT 0,
                entry_point_score REAL,
                blast_radius_risk TEXT
            );

            CREATE TABLE IF NOT EXISTS calls_edges (
                caller_fqn TEXT NOT NULL,
                callee_fqn TEXT NOT NULL,
                PRIMARY KEY (caller_fqn, callee_fqn)
            );

            CREATE TABLE IF NOT EXISTS property_edges (
                lu_fqn    TEXT NOT NULL,
                prop_key  TEXT NOT NULL,
                direction TEXT NOT NULL CHECK(direction IN ('read','write')),
                PRIMARY KEY (lu_fqn, prop_key, direction)
            );

            CREATE TABLE IF NOT EXISTS datasink_tables (
                component_fqn TEXT NOT NULL,
                table_name    TEXT NOT NULL,
                source_file   TEXT,
                repo_name     TEXT,
                PRIMARY KEY (component_fqn, table_name)
            );

            -- Leiden / igraph projection: all structural edges in one table.
            -- CALLS + IMPLEMENTS + EXTENDS + INJECTS + RESOLVES_TO + OVERRIDES
            CREATE TABLE IF NOT EXISTS structure_edges (
                src_fqn  TEXT NOT NULL,
                dst_fqn  TEXT NOT NULL,
                rel_type TEXT NOT NULL,
                PRIMARY KEY (src_fqn, dst_fqn, rel_type)
            );

            -- Per-repo ingest state (replaces Neo4j Project node SHA tracking)
            CREATE TABLE IF NOT EXISTS repo_meta (
                repo_name         TEXT PRIMARY KEY,
                last_ingested_sha TEXT,
                last_indexed      INTEGER
            );

            -- RFC specifications
            CREATE TABLE IF NOT EXISTS rfc_specs (
                fqn    TEXT PRIMARY KEY,
                rfc_id TEXT,
                title  TEXT
            );

            -- RFC section nodes
            CREATE TABLE IF NOT EXISTS rfc_sections (
                fqn        TEXT PRIMARY KEY,
                spec_fqn   TEXT,
                section_id TEXT,
                text       TEXT
            );

            -- IMPLEMENTS_SPEC edges
            CREATE TABLE IF NOT EXISTS implements_spec (
                component_fqn TEXT NOT NULL,
                spec_fqn      TEXT NOT NULL,
                PRIMARY KEY (component_fqn, spec_fqn)
            );

            -- FTS5 for BM25 keyword search
            CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
                fqn       UNINDEXED,
                searchtext
            );

            CREATE INDEX IF NOT EXISTS idx_nodes_file    ON nodes(file_path);
            CREATE INDEX IF NOT EXISTS idx_nodes_comm    ON nodes(community_id);
            CREATE INDEX IF NOT EXISTS idx_nodes_type    ON nodes(node_type, kind);
            CREATE INDEX IF NOT EXISTS idx_nodes_ep      ON nodes(is_entry_point);
            CREATE INDEX IF NOT EXISTS idx_nodes_ds      ON nodes(is_data_sink);
            CREATE INDEX IF NOT EXISTS idx_calls_callee  ON calls_edges(callee_fqn);
            CREATE INDEX IF NOT EXISTS idx_calls_caller  ON calls_edges(caller_fqn);
            CREATE INDEX IF NOT EXISTS idx_prop_key      ON property_edges(prop_key, direction);
            CREATE INDEX IF NOT EXISTS idx_prop_lu       ON property_edges(lu_fqn);
            CREATE INDEX IF NOT EXISTS idx_struct_src    ON structure_edges(src_fqn);
            CREATE INDEX IF NOT EXISTS idx_struct_dst    ON structure_edges(dst_fqn);
            CREATE INDEX IF NOT EXISTS idx_struct_type   ON structure_edges(rel_type);
        """)
