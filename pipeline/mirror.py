"""
pipeline/mirror.py
Git mirror manager — clones and pulls Java repositories to a local volume.

Stage 1 of the ingestion pipeline.  Reads ``sample_repos/repos.yaml``,
performs ``git clone`` (first run) or ``git pull`` (subsequent runs) into
``settings.repos_mirror_path``.  Returns a list of local ``Path`` objects
for Stage 2 (Extract) to process.
"""
from __future__ import annotations
import logging
from pathlib import Path
from typing import Optional

import yaml
import git
from git import Repo, GitCommandError

from config.settings import settings

logger = logging.getLogger(__name__)


class MirrorError(Exception):
    """Raised when a git clone or pull operation fails."""


class RepositoryMirror:
    """
    Manages local mirrors of remote Git repositories.

    Clones on first run, pulls on subsequent runs.
    Always checks out the specified branch.
    """

    def __init__(self, mirror_root: Optional[Path] = None):
        self.mirror_root = mirror_root or settings.repos_mirror_path
        self.mirror_root.mkdir(parents=True, exist_ok=True)

    def mirror_all(self, config_path: Optional[Path] = None) -> list[Path]:
        """
        Mirror all repositories listed in repos.yaml.

        Returns:
            List of local paths to mirrored repositories
        """
        config_path = config_path or settings.repos_config_path
        repos = self._load_config(config_path)
        local_paths = []
        for repo_cfg in repos:
            try:
                local_path = self.mirror_repo(
                    name=repo_cfg["name"],
                    url=repo_cfg["url"],
                    branch=repo_cfg.get("branch", "main"),
                )
                local_paths.append(local_path)
            except MirrorError as e:
                logger.warning("Skipping repo %s — %s", repo_cfg["name"], e)
        return local_paths

    def mirror_repo(self, name: str, url: str, branch: str = "main") -> Path:
        """
        Clone or pull a single repository.

        Args:
            name:   Short name used as local directory name
            url:    Remote Git URL
            branch: Branch to checkout

        Returns:
            Local path to the mirrored repository

        Raises:
            MirrorError: If clone/pull fails
        """
        dest = self.mirror_root / name
        try:
            if dest.exists() and (dest / ".git").exists():
                return self._pull(dest, branch)
            else:
                return self._clone(url, dest, branch)
        except GitCommandError as e:
            raise MirrorError(f"Git operation failed for {name}: {e}") from e

    def get_changed_files(
        self, repo_path: Path, since_sha: Optional[str] = None
    ) -> list[Path]:
        """
        Return list of changed .java files since a given commit SHA.
        Used for incremental re-indexing.

        Args:
            repo_path:  Local path to a mirrored repo
            since_sha:  If None, returns all .java files in the repo

        Returns:
            List of absolute paths to changed/added .java files
        """
        if since_sha is None:
            return list(repo_path.rglob("*.java"))

        repo = Repo(repo_path)
        try:
            diff = repo.commit("HEAD").diff(repo.commit(since_sha))
        except Exception as e:
            logger.warning("Could not compute diff from %s: %s", since_sha, e)
            return list(repo_path.rglob("*.java"))

        changed = []
        for item in diff:
            path = repo_path / item.b_path
            if path.suffix == ".java" and path.exists():
                changed.append(path)
        return changed

    # ── Private ───────────────────────────────────────────────────────────────

    def _clone(self, url: str, dest: Path, branch: str) -> Path:
        dest = dest.resolve()
        logger.info("Cloning %s → %s (branch: %s)", url, dest, branch)
        Repo.clone_from(url, str(dest), branch=branch, depth=1)
        logger.info("Clone complete: %s", dest)
        return dest

    def _pull(self, dest: Path, branch: str) -> Path:
        dest = dest.resolve()
        logger.info("Pulling %s (branch: %s)", dest, branch)
        repo = Repo(str(dest))
        origin = repo.remotes.origin
        repo.git.checkout(branch)
        origin.pull()
        logger.info("Pull complete: %s", dest)
        return dest

    def get_head_sha(self, repo_path: Path) -> str | None:
        """Return the current HEAD commit SHA for a mirrored repo, or None if unresolvable."""
        try:
            return Repo(str(repo_path)).head.commit.hexsha
        except Exception as e:
            logger.debug("Could not read HEAD SHA for %s: %s", repo_path, e)
            return None

    def _load_config(self, config_path: Path) -> list[dict]:
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data.get("repositories", [])
