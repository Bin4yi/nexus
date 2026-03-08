"""
graph/cleanup.py
Stale edge and node cleanup for incremental updates.
Removes CALLS edges and LogicUnit nodes belonging to deleted/renamed methods.
"""
from __future__ import annotations
import logging
from neo4j import Driver

logger = logging.getLogger(__name__)


class GraphCleanup:
    """
    Handles stale data removal after incremental re-indexing.

    Called after a file-scoped re-parse to remove:
    - CALLS edges from changed LogicUnits (fresh edges will be re-created)
    - LogicUnit nodes from deleted files
    - Component nodes from deleted files (if no logic units remain)
    """

    def __init__(self, driver: Driver):
        self.driver = driver

    def delete_edges_for_geids(self, deleted_geids: list[str]) -> int:
        """
        Delete all outgoing CALLS edges from a list of changed/deleted LogicUnits.

        Args:
            deleted_geids: List of GEID strings whose outgoing edges should be removed

        Returns:
            Number of edges deleted
        """
        if not deleted_geids:
            return 0

        with self.driver.session() as session:
            result = session.run(
                """
                MATCH (n:LogicUnit)-[r:CALLS]->()
                WHERE n.geid IN $geids
                DELETE r
                RETURN count(r) AS deleted
                """,
                geids=deleted_geids,
            )
            count = result.single()["deleted"]
            logger.info("Deleted %d stale CALLS edges", count)
            return count

    def delete_nodes_for_files(self, deleted_file_paths: list[str]) -> int:
        """
        Remove all LogicUnit and Component nodes belonging to deleted files.
        Uses DETACH DELETE to remove all relationships as well.

        Args:
            deleted_file_paths: List of file_path strings to purge

        Returns:
            Number of nodes deleted
        """
        if not deleted_file_paths:
            return 0

        with self.driver.session() as session:
            result = session.run(
                """
                MATCH (n)
                WHERE n.file_path IN $paths
                  AND (n:LogicUnit OR n:Component)
                WITH n, count(n) AS cnt
                DETACH DELETE n
                RETURN sum(cnt) AS deleted
                """,
                paths=deleted_file_paths,
            )
            record = result.single()
            count = record["deleted"] if record and record["deleted"] else 0
            logger.info("Deleted %d stale nodes for %d files", count, len(deleted_file_paths))
            return count

    def find_orphaned_logic_units(self) -> list[str]:
        """
        Find LogicUnit nodes that no longer have a parent Component.
        These are safe-to-delete leftovers from renames.

        Returns:
            List of GEIDs for orphaned LogicUnits
        """
        with self.driver.session() as session:
            result = session.run(
                """
                MATCH (lu:LogicUnit)
                WHERE NOT (:Component)-[:HAS_METHOD]->(lu)
                RETURN lu.geid AS geid
                """
            )
            return [r["geid"] for r in result]

    def delete_orphaned_logic_units(self) -> int:
        """Delete all LogicUnit nodes with no parent Component."""
        orphans = self.find_orphaned_logic_units()
        if not orphans:
            return 0

        with self.driver.session() as session:
            result = session.run(
                """
                MATCH (lu:LogicUnit)
                WHERE lu.geid IN $geids
                DETACH DELETE lu
                RETURN count(lu) AS deleted
                """,
                geids=orphans,
            )
            count = result.single()["deleted"]
            logger.info("Deleted %d orphaned LogicUnit nodes", count)
            return count
