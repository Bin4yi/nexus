"""
graph/sqlite_exporter.py
Exports the Neo4j graph into a SQLite database after each ingest run.

The live API reads ONLY from SQLite — Neo4j is offline-only from this point forward.

Schema:
  nodes           — LogicUnit + Component metadata (no body text)
  calls_edges     — CALLS relationships (caller_fqn → callee_fqn)
  property_edges  — READS_PROPERTY / WRITES_PROPERTY
  datasink_tables — QUERIES_TABLE (DataSink → DatabaseTable)
  nodes_fts       — FTS5 virtual table for BM25 keyword search

Usage:
  Called automatically at the end of each ingest run (orchestrator.py).
  Can also be run standalone:
      py -c "from graph.sqlite_exporter import run_export; run_export()"
"""
from __future__ import annotations
import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)


def export_graph_to_sqlite(driver, db_path: Path) -> dict:
    """
    Read Neo4j and write a fresh SQLite snapshot.
    Completely rebuilds the SQLite db — safe to call repeatedly (idempotent).
    Returns stats dict with counts.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    logger.info("SQLite export starting → %s", db_path)
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        _create_schema(conn)
        stats = {}
        stats["nodes"]            = _export_nodes(conn, driver)
        stats["calls_edges"]      = _export_calls_edges(conn, driver)
        stats["property_edges"]   = _export_property_edges(conn, driver)
        stats["datasink_tables"]  = _export_datasink_tables(conn, driver)
        _rebuild_fts(conn)
        conn.commit()

    logger.info("SQLite export complete: %s", stats)
    return stats


# ── Schema ────────────────────────────────────────────────────────────────────

def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS nodes (
            fqn               TEXT PRIMARY KEY,
            geid              TEXT,
            node_type         TEXT,      -- 'LogicUnit' | 'Component'
            kind              TEXT,      -- 'class' | 'interface' | 'method' | ...
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

        -- CALLS relationships for in-memory BFS blast radius
        CREATE TABLE IF NOT EXISTS calls_edges (
            caller_fqn TEXT NOT NULL,
            callee_fqn TEXT NOT NULL,
            PRIMARY KEY (caller_fqn, callee_fqn)
        );

        -- Property key read/write edges
        CREATE TABLE IF NOT EXISTS property_edges (
            lu_fqn    TEXT NOT NULL,
            prop_key  TEXT NOT NULL,
            direction TEXT NOT NULL CHECK(direction IN ('read','write')),
            PRIMARY KEY (lu_fqn, prop_key, direction)
        );

        -- DataSink → table edges
        CREATE TABLE IF NOT EXISTS datasink_tables (
            component_fqn TEXT NOT NULL,
            table_name    TEXT NOT NULL,
            source_file   TEXT,
            repo_name     TEXT,
            PRIMARY KEY (component_fqn, table_name)
        );

        -- FTS5 for BM25 search (replaces Neo4j fulltext index)
        -- Separate table: fqn stored unindexed, searchtext is what FTS indexes
        CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
            fqn       UNINDEXED,
            searchtext            -- simple_name + kind, space-separated
        );

        CREATE INDEX IF NOT EXISTS idx_nodes_file    ON nodes(file_path);
        CREATE INDEX IF NOT EXISTS idx_nodes_comm    ON nodes(community_id);
        CREATE INDEX IF NOT EXISTS idx_nodes_type    ON nodes(node_type, kind);
        CREATE INDEX IF NOT EXISTS idx_calls_callee  ON calls_edges(callee_fqn);
        CREATE INDEX IF NOT EXISTS idx_calls_caller  ON calls_edges(caller_fqn);
        CREATE INDEX IF NOT EXISTS idx_prop_key      ON property_edges(prop_key, direction);
        CREATE INDEX IF NOT EXISTS idx_prop_lu       ON property_edges(lu_fqn);
    """)


# ── Exporters ─────────────────────────────────────────────────────────────────

def _export_nodes(conn: sqlite3.Connection, driver) -> int:
    conn.execute("DELETE FROM nodes")
    conn.execute("DELETE FROM nodes_fts")

    with driver.session() as s:
        records = s.run("""
            MATCH (n)
            WHERE n:LogicUnit OR n:Component
            RETURN
                n.fqn              AS fqn,
                n.geid             AS geid,
                labels(n)[0]       AS node_type,
                n.kind             AS kind,
                n.file_path        AS file_path,
                n.start_line       AS start_line,
                n.end_line         AS end_line,
                n.community_id     AS community_id,
                n.visibility       AS visibility,
                COALESCE(n.is_abstract, false)    AS is_abstract,
                COALESCE(n.is_static, false)      AS is_static,
                COALESCE(n.is_final, false)       AS is_final,
                COALESCE(n.is_entry_point, false) AS is_entry_point,
                COALESCE(n.is_data_sink, false)   AS is_data_sink,
                n.entry_point_score   AS entry_point_score,
                n.blast_radius_risk   AS blast_radius_risk
        """).data()

    node_rows = []
    fts_rows  = []
    for r in records:
        fqn = r.get("fqn")
        if not fqn:
            continue
        node_rows.append((
            fqn, r.get("geid"), r.get("node_type"), r.get("kind"),
            r.get("file_path"), r.get("start_line"), r.get("end_line"),
            r.get("community_id"), r.get("visibility"),
            1 if r.get("is_abstract") else 0,
            1 if r.get("is_static")   else 0,
            1 if r.get("is_final")    else 0,
            1 if r.get("is_entry_point") else 0,
            1 if r.get("is_data_sink")   else 0,
            r.get("entry_point_score"), r.get("blast_radius_risk"),
        ))
        # Build searchtext: simple name + kind for BM25
        simple_name = fqn.split(".")[-1] if "." in fqn else fqn
        kind = r.get("kind") or ""
        fts_rows.append((fqn, f"{simple_name} {kind}"))

    conn.executemany("""
        INSERT OR REPLACE INTO nodes
        (fqn, geid, node_type, kind, file_path, start_line, end_line,
         community_id, visibility, is_abstract, is_static, is_final,
         is_entry_point, is_data_sink, entry_point_score, blast_radius_risk)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, node_rows)

    conn.executemany(
        "INSERT INTO nodes_fts (fqn, searchtext) VALUES (?,?)",
        fts_rows,
    )

    logger.info("Exported %d nodes", len(node_rows))
    return len(node_rows)


def _export_calls_edges(conn: sqlite3.Connection, driver) -> int:
    conn.execute("DELETE FROM calls_edges")

    with driver.session() as s:
        records = s.run("""
            MATCH (caller)-[:CALLS]->(callee)
            WHERE (caller:LogicUnit OR caller:Component)
              AND (callee:LogicUnit  OR callee:Component)
              AND caller.fqn IS NOT NULL
              AND callee.fqn IS NOT NULL
            RETURN caller.fqn AS caller_fqn, callee.fqn AS callee_fqn
        """).data()

    rows = [(r["caller_fqn"], r["callee_fqn"]) for r in records]
    conn.executemany(
        "INSERT OR IGNORE INTO calls_edges (caller_fqn, callee_fqn) VALUES (?,?)",
        rows,
    )
    logger.info("Exported %d CALLS edges", len(rows))
    return len(rows)


def _export_property_edges(conn: sqlite3.Connection, driver) -> int:
    conn.execute("DELETE FROM property_edges")
    total = 0

    with driver.session() as s:
        reads = s.run("""
            MATCH (lu:LogicUnit)-[:READS_PROPERTY]->(k:PropertyKey)
            WHERE lu.fqn IS NOT NULL AND k.key IS NOT NULL
            RETURN lu.fqn AS lu_fqn, k.key AS prop_key
        """).data()
        rows = [(r["lu_fqn"], r["prop_key"], "read") for r in reads]
        conn.executemany("INSERT OR IGNORE INTO property_edges VALUES (?,?,?)", rows)
        total += len(rows)

        writes = s.run("""
            MATCH (lu:LogicUnit)-[:WRITES_PROPERTY]->(k:PropertyKey)
            WHERE lu.fqn IS NOT NULL AND k.key IS NOT NULL
            RETURN lu.fqn AS lu_fqn, k.key AS prop_key
        """).data()
        rows = [(r["lu_fqn"], r["prop_key"], "write") for r in writes]
        conn.executemany("INSERT OR IGNORE INTO property_edges VALUES (?,?,?)", rows)
        total += len(rows)

    logger.info("Exported %d property edges", total)
    return total


def _export_datasink_tables(conn: sqlite3.Connection, driver) -> int:
    conn.execute("DELETE FROM datasink_tables")

    with driver.session() as s:
        records = s.run("""
            MATCH (c:Component)-[:QUERIES_TABLE]->(t:DatabaseTable)
            WHERE c.fqn IS NOT NULL AND t.name IS NOT NULL
            RETURN c.fqn AS component_fqn, t.name AS table_name,
                   t.source_file AS source_file, t.repo_name AS repo_name
        """).data()

    rows = [
        (r["component_fqn"], r["table_name"], r.get("source_file"), r.get("repo_name"))
        for r in records
    ]
    conn.executemany("INSERT OR IGNORE INTO datasink_tables VALUES (?,?,?,?)", rows)
    logger.info("Exported %d DataSink table edges", len(rows))
    return len(rows)


def _rebuild_fts(conn: sqlite3.Connection) -> None:
    """Optimize FTS5 index after bulk insert."""
    conn.execute("INSERT INTO nodes_fts(nodes_fts) VALUES('optimize')")
    logger.info("FTS5 index optimized")


# ── Standalone entry point ────────────────────────────────────────────────────

def run_export() -> None:
    """Run from command line: py -c "from graph.sqlite_exporter import run_export; run_export()" """
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent))

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")
    from neo4j import GraphDatabase
    from config.settings import settings

    driver = GraphDatabase.driver(settings.neo4j_uri, auth=settings.neo4j_auth)
    try:
        stats = export_graph_to_sqlite(driver, settings.sqlite_db_path)
        print("Export complete:", stats)
    finally:
        driver.close()
