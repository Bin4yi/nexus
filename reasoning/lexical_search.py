"""
reasoning/lexical_search.py
Lexical (exact text) search over local Git mirrors of source repositories.

Architectural role:
    The first step in the Hybrid Deterministic Router for symbolic queries.
    Provides **zero-hallucination** evidence: every hit is a provably-real
    file + line number in the actual source code.

Supported backends (selectable via ``settings.grep_backend``):
    * ``ripgrep``  — default, fastest (~GB/s throughput)
    * ``git_grep`` — available anywhere ``git`` is installed
    * ``python``   — pure-Python fallback, no external dependency

Pipeline:
    1. ``LexicalSearcher.search()`` finds exact file + line for each symbol
    2. ``GraphRetriever.find_node_by_location()`` maps line → Neo4j LogicUnit
    3. ``GraphRetriever.compute_blast_radius()`` traverses CALLS/INJECTS
    4. ``ReduceStep`` synthesises the grounded review

We intentionally do NOT store constants/string literals in Neo4j
(that would explode node count from ~12k to millions).
"""
from __future__ import annotations

import subprocess
import logging
import re
from pathlib import Path
from dataclasses import dataclass
from typing import Sequence

from config.settings import settings, GrepBackend

logger = logging.getLogger(__name__)

MIRROR_ROOT = Path(settings.repos_mirror_path).resolve()


@dataclass
class GrepHit:
    """One exact match from a lexical search."""
    file_path:   str    # absolute path
    rel_path:    str    # relative to mirror root
    line_number: int
    line_text:   str    # the matching source line
    symbol:      str    # the searched symbol


class LexicalSearcher:
    """
    Runs an exact-string search against local Git mirrors.

    Backend selection is controlled by ``settings.grep_backend``:
        * ``GrepBackend.RIPGREP``  — ``rg --fixed-strings``
        * ``GrepBackend.GIT_GREP`` — ``git grep -n --fixed-strings``
        * ``GrepBackend.PYTHON``   — pure-Python fallback
    """

    def search(
        self,
        symbol: str,
        max_hits: int | None = None,
        repo_paths: Sequence[Path] | None = None,
    ) -> list[GrepHit]:
        """
        Search for **exact** occurrences of *symbol* in the mirror directory.

        Args:
            symbol:     Exact string to search for (e.g. ``'IMPERSONATED_SUBJECT'``).
            max_hits:   Cap on total returned hits (default from settings).
            repo_paths: Optional explicit list of repo root paths to search.
                        If ``None``, searches the entire ``MIRROR_ROOT`` tree.

        Returns:
            List of ``GrepHit`` objects, capped at *max_hits*.
        """
        max_hits = max_hits or settings.grep_max_hits
        search_roots = list(repo_paths) if repo_paths else [MIRROR_ROOT]

        # Validate that search roots exist
        valid_roots = [r for r in search_roots if r.exists()]
        if not valid_roots:
            logger.warning("No valid search roots found: %s", search_roots)
            return []

        # Dispatch to the configured backend
        backend = settings.grep_backend
        hits: list[GrepHit] = []
        for root in valid_roots:
            try:
                if backend == GrepBackend.RIPGREP:
                    hits.extend(self._rg_search(symbol, max_hits, root))
                elif backend == GrepBackend.GIT_GREP:
                    hits.extend(self._git_grep_search(symbol, max_hits, root))
                else:
                    hits.extend(self._python_grep(symbol, max_hits, root))
            except FileNotFoundError:
                # Binary not on PATH — cascade to next backend
                logger.info(
                    "%s not available for %s — trying fallback", backend.value, root,
                )
                try:
                    if backend == GrepBackend.RIPGREP:
                        hits.extend(self._git_grep_search(symbol, max_hits, root))
                    else:
                        hits.extend(self._python_grep(symbol, max_hits, root))
                except FileNotFoundError:
                    hits.extend(self._python_grep(symbol, max_hits, root))

        # Log breakdown for observability
        prod  = [h for h in hits if "src/main/java" in h.rel_path]
        tests = [h for h in hits if "src/test/java" in h.rel_path]
        logger.info(
            "Lexical search '%s' → %d prod + %d test hits (%d total, backend=%s)",
            symbol, len(prod), len(tests), len(hits), backend.value,
        )
        return hits[:max_hits]

    def search_repos(
        self,
        symbol: str,
        repo_paths: Sequence[Path],
        max_hits: int | None = None,
    ) -> list[GrepHit]:
        """
        Convenience wrapper — search only the given *repo_paths*.

        Useful when the caller already knows which subset of the 100+ repos
        to search (e.g. repos in the same Leiden community or Maven
        dependency chain).
        """
        return self.search(symbol, max_hits=max_hits, repo_paths=repo_paths)

    # ── Backend implementations ───────────────────────────────────────────────

    def _rg_search(
        self,
        symbol: str,
        max_hits: int,
        search_root: Path,
        rg_cmd: str = "rg",
    ) -> list[GrepHit]:
        """ripgrep — fastest backend. Raises ``FileNotFoundError`` if absent."""
        cmd = [
            rg_cmd,
            "--fixed-strings",       # exact string, no regex
            "--line-number",
            "--no-heading",
            "--with-filename",
            "--max-count", str(max_hits),
            symbol,
            str(search_root),
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=30,
            encoding="utf-8",
            errors="replace",
        )
        return self._parse_rg_output(result.stdout, symbol)

    def _git_grep_search(
        self,
        symbol: str,
        max_hits: int,
        search_root: Path,
    ) -> list[GrepHit]:
        """
        ``git grep`` backend — available wherever Git is installed.

        Works by recursing into each ``.git``-containing directory under
        *search_root* and running ``git grep`` inside it.  For flat mirrors
        (mirror/<repo-name>/) each repo is searched independently.
        """
        hits: list[GrepHit] = []
        # Detect repo roots under search_root
        repo_roots = self._find_git_repos(search_root)
        if not repo_roots:
            # search_root itself might be a git repo
            if (search_root / ".git").exists():
                repo_roots = [search_root]
            else:
                return []

        for repo_root in repo_roots:
            if len(hits) >= max_hits:
                break
            try:
                cmd = [
                    "git", "-C", str(repo_root),
                    "grep", "-n", "--fixed-strings",
                    "--max-count", str(max_hits - len(hits)),
                    symbol,
                ]
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=30,
                    encoding="utf-8",
                    errors="replace",
                )
                for raw_line in result.stdout.splitlines():
                    m = re.match(r'^(.+?):(\d+):(.*)', raw_line)
                    if not m:
                        continue
                    rel_in_repo = m.group(1)
                    lineno = int(m.group(2))
                    text = m.group(3).strip()
                    abs_path = repo_root / rel_in_repo
                    try:
                        rel_to_mirror = str(abs_path.relative_to(MIRROR_ROOT)).replace("\\", "/")
                    except ValueError:
                        rel_to_mirror = str(abs_path).replace("\\", "/")
                    hits.append(GrepHit(
                        file_path=str(abs_path),
                        rel_path=rel_to_mirror,
                        line_number=lineno,
                        line_text=text,
                        symbol=symbol,
                    ))
            except (subprocess.TimeoutExpired, FileNotFoundError) as e:
                logger.warning("git grep failed in %s: %s", repo_root, e)
        return hits

    def _python_grep(
        self, symbol: str, max_hits: int, search_root: Path,
    ) -> list[GrepHit]:
        """Pure-Python fallback grep — slower but zero external dependencies."""
        hits: list[GrepHit] = []
        for java_file in search_root.rglob("*.java"):
            if len(hits) >= max_hits:
                break
            try:
                lines = java_file.read_text(encoding="utf-8", errors="ignore").splitlines()
                for lineno, line in enumerate(lines, start=1):
                    if symbol in line:
                        try:
                            rel = java_file.relative_to(MIRROR_ROOT)
                        except ValueError:
                            rel = java_file
                        rel_str = str(rel).replace("\\", "/")
                        hits.append(GrepHit(
                            file_path=str(java_file),
                            rel_path=rel_str,
                            line_number=lineno,
                            line_text=line.strip(),
                            symbol=symbol,
                        ))
            except Exception:
                continue
        return hits

    # ── Parsing helpers ───────────────────────────────────────────────────────

    def _parse_rg_output(self, output: str | None, symbol: str) -> list[GrepHit]:
        """Parse ripgrep ``filepath:lineno:matchtext`` lines."""
        if not output:
            return []
        hits: list[GrepHit] = []
        for raw_line in output.splitlines():
            m = re.match(r'^(.+?):(\d+):(.*)', raw_line)
            if not m:
                continue
            fp = m.group(1)
            lineno = int(m.group(2))
            text = m.group(3).strip()
            try:
                rel = str(Path(fp).relative_to(MIRROR_ROOT)).replace("\\", "/")
            except ValueError:
                rel = fp.replace("\\", "/")
            hits.append(GrepHit(
                file_path=fp,
                rel_path=rel,
                line_number=lineno,
                line_text=text,
                symbol=symbol,
            ))
        return hits

    @staticmethod
    def _find_git_repos(root: Path) -> list[Path]:
        """Return immediate child directories of *root* that contain ``.git``."""
        if not root.is_dir():
            return []
        return [d for d in root.iterdir() if d.is_dir() and (d / ".git").exists()]
