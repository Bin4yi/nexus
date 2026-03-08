"""
parsers/sql_schema_parser.py
Lightweight SQL DDL parser — scans ``dbscripts/`` folders in mirrored
repositories and extracts table names from CREATE TABLE statements.

Creates ``(DatabaseTable)`` nodes in Neo4j and links them to
Data Access Object (DAO) classes via ``[:QUERIES_TABLE]`` edges.
"""
from __future__ import annotations
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)

# ── Regex patterns ────────────────────────────────────────────────────────────
# Matches: CREATE TABLE [IF NOT EXISTS] [schema.]table_name
_CREATE_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(?:\w+\.)?"           # optional schema prefix
    r"[`\"']?(\w+)[`\"']?", # table name (with optional quoting)
    re.IGNORECASE,
)

# Patterns that signal a Java class is a DAO / data-access class
_DAO_CLASS_PATTERNS = re.compile(
    r"(DAO|Repository|DataAccessor|JdbcTemplate|PreparedStatement"
    r"|\.executeQuery|\.executeUpdate|\.prepareStatement"
    r"|@Repository|AbstractDAO|JDBCPersistenceManager)",
    re.IGNORECASE,
)

# SQL table reference inside Java strings — catches most JDBC patterns
_SQL_TABLE_REF_RE = re.compile(
    r"""(?:FROM|INTO|UPDATE|JOIN|TABLE)\s+[`"']?(\w+)[`"']?""",
    re.IGNORECASE,
)


@dataclass
class DatabaseTableInfo:
    """Represents a discovered database table."""
    table_name: str
    source_file: str          # path to the .sql script
    repo_name: str


@dataclass
class TableQueryEdge:
    """A DAO class (by geid) → table name edge."""
    component_geid: str
    component_fqn: str
    table_name: str


class SQLSchemaParser:
    """
    Scans repository ``dbscripts/`` folders for CREATE TABLE statements
    and identifies which DAO / repository classes reference those tables.
    """

    # ── Public API ────────────────────────────────────────────────────────────

    def scan_sql_scripts(self, repo_path: Path, repo_name: str) -> list[DatabaseTableInfo]:
        """
        Walk ``dbscripts/`` (and common variants) under *repo_path* and
        extract table names from CREATE TABLE DDL.

        Returns:
            List of ``DatabaseTableInfo`` with table names discovered.
        """
        tables: list[DatabaseTableInfo] = []
        seen_names: set[str] = set()

        for sql_dir_name in ("dbscripts", "db-scripts", "sql", "resources/dbscripts"):
            sql_dir = repo_path / sql_dir_name
            if not sql_dir.exists():
                continue
            for sql_file in sql_dir.rglob("*.sql"):
                tables.extend(self._parse_sql_file(sql_file, repo_name, seen_names))

        # Also check for .sql files at the component level (some repos put them
        # in src/main/resources)
        resources_dirs = repo_path.rglob("src/main/resources")
        for res_dir in resources_dirs:
            for sql_file in res_dir.rglob("*.sql"):
                tables.extend(self._parse_sql_file(sql_file, repo_name, seen_names))

        if tables:
            logger.info(
                "Found %d database tables in %s", len(tables), repo_name,
            )
        return tables

    def detect_table_queries(
        self,
        java_files: list[Path],
        components: list,  # list[Component] — avoids circular import
        known_tables: set[str],
    ) -> list[TableQueryEdge]:
        """
        Scan Java source files for DAO classes that reference known tables.

        Heuristic:
        1. Class body contains DAO indicator (class name, annotation, JDBC calls)
        2. String literals in the class reference a known table name

        Returns:
            List of ``TableQueryEdge`` linking component → table.
        """
        edges: list[TableQueryEdge] = []
        # Build a file_path → Component lookup
        fp_to_comp = {}
        for comp in components:
            if comp.file_path:
                fp_to_comp[str(Path(comp.file_path).resolve())] = comp

        for jf in java_files:
            try:
                source = jf.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            # Check if file looks like a DAO / data-access class
            if not _DAO_CLASS_PATTERNS.search(source):
                continue

            comp = fp_to_comp.get(str(jf.resolve()))
            if not comp:
                continue

            # Find table references in SQL string literals
            referenced_tables = set()
            for m in _SQL_TABLE_REF_RE.finditer(source):
                tname = m.group(1).upper()
                if tname in known_tables:
                    referenced_tables.add(tname)

            # Also do a simple containment check for table names in string literals
            for tname in known_tables:
                # Case-insensitive: many WSO2 schemas use uppercase table names
                if tname.lower() in source.lower():
                    # Verify it's likely in a string context (crude but effective)
                    escaped = re.escape(tname)
                    if re.search(rf'["\'][^"\']*{escaped}[^"\']*["\']', source, re.IGNORECASE):
                        referenced_tables.add(tname)

            for tname in referenced_tables:
                edges.append(TableQueryEdge(
                    component_geid=comp.geid,
                    component_fqn=comp.fqn,
                    table_name=tname,
                ))

        if edges:
            logger.info("Detected %d QUERIES_TABLE edges", len(edges))
        return edges

    # ── Private ───────────────────────────────────────────────────────────────

    def _parse_sql_file(
        self, sql_file: Path, repo_name: str, seen: set[str],
    ) -> list[DatabaseTableInfo]:
        """Parse a single .sql file for CREATE TABLE statements."""
        tables: list[DatabaseTableInfo] = []
        try:
            content = sql_file.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            logger.warning("Cannot read %s: %s", sql_file, e)
            return tables

        for match in _CREATE_TABLE_RE.finditer(content):
            tname = match.group(1).upper()  # normalise to uppercase
            if tname not in seen:
                seen.add(tname)
                tables.append(DatabaseTableInfo(
                    table_name=tname,
                    source_file=str(sql_file),
                    repo_name=repo_name,
                ))

        return tables
