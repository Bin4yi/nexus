"""
linker/maven_resolver.py
Parses pom.xml files to generate DEPENDS_ON cross-repo dependency edges.

Data-flow position:
    Stage 2 (Extract) → ``MavenResolver.parse_pom()`` returns
    ``(module_info, dep_edges)`` → Stage 3 (Link) uses ``dep_edges``
    to create ``[:DEPENDS_ON]`` relationships between ``Module`` nodes.
"""
from __future__ import annotations
import logging
from pathlib import Path
from typing import Optional

from lxml import etree

from parsers.uir import DependencyEdge

logger = logging.getLogger(__name__)

# Maven POM namespace
_NS = {"m": "http://maven.apache.org/POM/4.0.0"}


class MavenResolver:
    """
    Parses Maven pom.xml files and extracts dependency declarations.
    Produces DependencyEdge objects for Neo4j DEPENDS_ON relationships.
    """

    def parse_pom(self, pom_path: Path) -> tuple[dict, list[DependencyEdge]]:
        """
        Parse a pom.xml file.

        Args:
            pom_path: Path to the pom.xml file

        Returns:
            Tuple of:
                - module_info dict: {group_id, artifact_id, version}
                - list of DependencyEdge objects
        """
        tree = etree.parse(str(pom_path))
        root = tree.getroot()

        module_info = {
            "group_id": self._text(root, "m:groupId") or self._text(root, "m:parent/m:groupId") or "",
            "artifact_id": self._text(root, "m:artifactId") or "",
            "version": self._text(root, "m:version") or self._text(root, "m:parent/m:version") or "UNKNOWN",
        }

        dependencies: list[DependencyEdge] = []
        dep_nodes = root.findall(".//m:dependencies/m:dependency", _NS)

        for dep in dep_nodes:
            group_id = self._text(dep, "m:groupId") or ""
            artifact_id = self._text(dep, "m:artifactId") or ""
            version = self._text(dep, "m:version")
            scope = self._text(dep, "m:scope") or "compile"

            if group_id and artifact_id:
                dependencies.append(
                    DependencyEdge(
                        target_group_id=group_id,
                        target_artifact_id=artifact_id,
                        target_version=version,
                        scope=scope,
                    )
                )

        logger.debug(
            "Parsed %s: %d dependencies found",
            pom_path.name,
            len(dependencies),
        )
        return module_info, dependencies

    def find_poms(self, repo_path: Path) -> list[Path]:
        """Find all pom.xml files in a repository."""
        return list(repo_path.rglob("pom.xml"))

    def _text(self, node, xpath: str) -> Optional[str]:
        el = node.find(xpath, _NS)
        return el.text.strip() if el is not None and el.text else None
