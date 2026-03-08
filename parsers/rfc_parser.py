"""
parsers/rfc_parser.py
IETF RFC Specification Grounding — Sprint 4.

Parses RFC markdown/text files into ``(:Specification)`` nodes and scans
Java source code for explicit RFC citations (e.g., ``// RFC 6749``,
``// See RFC 7662``, ``@see RFC 6750``) to draw ``[:IMPLEMENTS_SPEC]`` edges.

This grounds the codebase in its compliance obligations, enabling queries like:
  "Which classes implement RFC 6749 Section 4.1 (Authorization Code Grant)?"
  "Does our token introspection conform to RFC 7662?"
"""
from __future__ import annotations
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Regex patterns ────────────────────────────────────────────────────────────

# Match RFC numbers in Java source comments and Javadoc
# Covers: // RFC 6749, /* RFC 6749 */, @see RFC 6749, "implements RFC 6749"
_RFC_CITATION_RE = re.compile(
    r"(?:RFC\s*[-#:]?\s*(\d{3,5}))",
    re.IGNORECASE,
)

# Extract title from RFC markdown header (e.g., "# RFC 6749: The OAuth 2.0 Authorization Framework")
_RFC_TITLE_RE = re.compile(
    r"^#+\s*RFC\s+(\d+)[:\s]+(.+)$",
    re.MULTILINE | re.IGNORECASE,
)

# Fallback: extract from "Request for Comments: XXXX" header in plain-text RFCs
_RFC_NUMBER_RE = re.compile(
    r"Request\s+for\s+Comments\s*:\s*(\d+)",
    re.IGNORECASE,
)
_RFC_PLAIN_TITLE_RE = re.compile(
    r"^(?:Status|Title|Subject|Name):\s*(.+)$",
    re.MULTILINE | re.IGNORECASE,
)


@dataclass
class SpecificationInfo:
    """An IETF RFC parsed from a markdown/text file."""
    rfc_number: int
    title: str
    source_file: str


@dataclass
class SpecImplementsEdge:
    """A Java Component that cites an RFC in its source/Javadoc."""
    component_geid: str
    component_fqn: str
    rfc_number: int
    citation_context: str  # surrounding comment text for traceability


class RFCParser:
    """
    Parses RFC specification files and links Java classes that cite them.
    """

    # ── Public API ────────────────────────────────────────────────────────────

    def parse_rfc_files(self, rfc_dir: Path) -> list[SpecificationInfo]:
        """
        Scan *rfc_dir* for .md, .txt, and .rst RFC files and extract
        RFC number + title for each one.

        Returns:
            List of SpecificationInfo objects.
        """
        specs: list[SpecificationInfo] = []
        seen: set[int] = set()

        if not rfc_dir.exists():
            logger.debug("RFC directory does not exist: %s", rfc_dir)
            return specs

        for rfc_file in rfc_dir.rglob("*"):
            if rfc_file.suffix.lower() not in (".md", ".txt", ".rst", ""):
                continue
            if not rfc_file.is_file():
                continue
            spec = self._parse_single_rfc(rfc_file)
            if spec and spec.rfc_number not in seen:
                seen.add(spec.rfc_number)
                specs.append(spec)

        logger.info("Parsed %d RFC specification files from %s", len(specs), rfc_dir)
        return specs

    def detect_rfc_citations(
        self,
        java_files: list[Path],
        components: list,  # list[Component]
    ) -> list[SpecImplementsEdge]:
        """
        Scan Java source files for RFC citations and link them to components.

        Scans:
        - Inline comments: // RFC 6749
        - Block comments: /* ... RFC 6749 ... */
        - Javadoc @see tags: @see RFC 6749
        - String literals: "See RFC 6749 Section 4.1"

        Returns:
            List of SpecImplementsEdge objects.
        """
        edges: list[SpecImplementsEdge] = []
        fp_to_comp: dict[str, object] = {}
        for comp in components:
            if comp.file_path:
                fp_to_comp[str(Path(comp.file_path).resolve())] = comp

        for jf in java_files:
            try:
                source = jf.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            if "RFC" not in source.upper():
                continue

            comp = fp_to_comp.get(str(jf.resolve()))
            if not comp:
                continue

            cited_rfcs: set[int] = set()
            context_map: dict[int, str] = {}

            for m in _RFC_CITATION_RE.finditer(source):
                try:
                    rfc_num = int(m.group(1))
                except ValueError:
                    continue
                if rfc_num not in cited_rfcs:
                    cited_rfcs.add(rfc_num)
                    # Capture surrounding context (50 chars before/after)
                    start = max(0, m.start() - 50)
                    end = min(len(source), m.end() + 50)
                    context_map[rfc_num] = source[start:end].replace("\n", " ").strip()

            for rfc_num in cited_rfcs:
                edges.append(SpecImplementsEdge(
                    component_geid=comp.geid,
                    component_fqn=comp.fqn,
                    rfc_number=rfc_num,
                    citation_context=context_map.get(rfc_num, ""),
                ))

        logger.info(
            "Detected %d [:IMPLEMENTS_SPEC] edges across %d Java files",
            len(edges), len(java_files),
        )
        return edges

    # ── Private ───────────────────────────────────────────────────────────────

    def _parse_single_rfc(self, rfc_file: Path) -> Optional[SpecificationInfo]:
        """Parse a single RFC file and extract its number and title."""
        try:
            content = rfc_file.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            logger.debug("Cannot read RFC file %s: %s", rfc_file, e)
            return None

        # Try markdown header: # RFC 6749: The OAuth 2.0 Authorization Framework
        title_m = _RFC_TITLE_RE.search(content)
        if title_m:
            return SpecificationInfo(
                rfc_number=int(title_m.group(1)),
                title=title_m.group(2).strip(),
                source_file=str(rfc_file),
            )

        # Try plain-text RFC format
        num_m = _RFC_NUMBER_RE.search(content)
        if num_m:
            rfc_num = int(num_m.group(1))
            title_m2 = _RFC_PLAIN_TITLE_RE.search(content)
            title = title_m2.group(1).strip() if title_m2 else f"RFC {rfc_num}"
            return SpecificationInfo(
                rfc_number=rfc_num,
                title=title,
                source_file=str(rfc_file),
            )

        # Try to infer from filename: rfc6749.md or 6749.txt
        stem = rfc_file.stem.lower().lstrip("rfc")
        if stem.isdigit():
            return SpecificationInfo(
                rfc_number=int(stem),
                title=f"RFC {stem}",
                source_file=str(rfc_file),
            )

        return None
