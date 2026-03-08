"""
reasoning/code_fetcher.py
Reads actual source code from the local mirror directory.

Used when the user asks for code ("show me", "what does X look like", etc.)
or when the system needs to ground the LLM response in the real implementation.

We already have file paths + line numbers from:
  - Grep hits (exact match location)
  - Neo4j LogicUnit nodes (start_line / end_line of each method)

This module reads those lines from disk and returns clean code blocks.
"""
from __future__ import annotations

import logging
from pathlib import Path
from dataclasses import dataclass

from config.settings import settings

logger = logging.getLogger(__name__)

MIRROR_ROOT = Path(settings.repos_mirror_path).resolve()


@dataclass
class CodeSnippet:
    """A block of source code with context."""
    fqn:         str          # fully qualified name of the method/class
    file_path:   str          # relative path from mirror root
    start_line:  int
    end_line:    int
    language:    str          # "java", "xml", etc.
    code:        str          # the actual source lines
    context_note: str = ""   # e.g. "grep match at line 241"


class CodeFetcher:
    """
    Reads source code snippets from the local mirror.
    No network calls, no LLM — pure filesystem reads.
    """

    def fetch_method(self, node: dict, context_lines: int = 0) -> CodeSnippet | None:
        """
        Fetch a complete method/class body from a Neo4j LogicUnit node dict.
        Node must have: file_path, fqn, start_line, end_line.

        Args:
            node:          Neo4j node dict with file_path and line range
            context_lines: Extra lines of context to include before/after
        """
        fp  = node.get("file_path", "")
        fqn = node.get("fqn", "")
        s   = node.get("start_line")
        e   = node.get("end_line")
        if not fp or s is None or e is None:
            return None
        return self._read_range(fp, fqn, int(s), int(e), context_lines)

    def fetch_grep_context(self, file_path: str, line_number: int,
                           context_lines: int = 5) -> CodeSnippet | None:
        """
        Fetch N lines of context around a grep match.
        Useful for showing HOW a constant/method is used at a specific call site.

        Args:
            file_path:     Relative path (from mirror root) or absolute
            line_number:   The matched line
            context_lines: Lines of context before and after (default 5)
        """
        start = max(1, line_number - context_lines)
        end   = line_number + context_lines
        return self._read_range(
            file_path, f"line {line_number}", start, end,
            context_note=f"grep match at line {line_number}",
        )

    def detect_language(self, file_path: str) -> str:
        ext = Path(file_path).suffix.lower()
        return {"java": "java", ".xml": "xml", ".yaml": "yaml",
                ".yml": "yaml", ".json": "json", ".py": "python"}.get(ext, "text")

    # ── Private ───────────────────────────────────────────────────────────────

    def _read_range(self, file_path: str, fqn: str,
                    start: int, end: int,
                    context_lines: int = 0,
                    context_note: str = "") -> CodeSnippet | None:
        """Read lines [start, end] (1-indexed) from the file."""
        abs_path = self._resolve(file_path)
        if abs_path is None:
            return None

        try:
            all_lines = abs_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception as e:
            logger.warning("Could not read %s: %s", abs_path, e)
            return None

        s = max(0, start - 1 - context_lines)
        e = min(len(all_lines), end + context_lines)
        snippet_lines = all_lines[s:e]

        # Prepend line numbers for readability
        numbered = "\n".join(
            f"{s + i + 1:4d} │ {line}"
            for i, line in enumerate(snippet_lines)
        )

        rel = str(abs_path.relative_to(MIRROR_ROOT)).replace("\\", "/")
        return CodeSnippet(
            fqn=fqn,
            file_path=rel,
            start_line=s + 1,
            end_line=s + len(snippet_lines),
            language=self.detect_language(file_path),
            code=numbered,
            context_note=context_note,
        )

    def _resolve(self, file_path: str) -> Path | None:
        """Resolve a file path (absolute or relative to mirror root) to a Path."""
        fp = Path(file_path)
        if fp.is_absolute() and fp.exists():
            return fp

        # Try as relative to mirror root
        candidate = MIRROR_ROOT / file_path.replace("\\", "/")
        if candidate.exists():
            return candidate

        # Strip leading mirror/ prefix if present
        norm = file_path.replace("\\", "/")
        if norm.startswith("mirror/"):
            norm = norm[len("mirror/"):]
        candidate2 = MIRROR_ROOT / norm
        if candidate2.exists():
            return candidate2

        logger.debug("Could not resolve path: %s", file_path)
        return None
