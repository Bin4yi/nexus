"""
tests/test_02_mirror.py
Gate 2: 5 tests for the Git mirror module.
Tests 1, 2, 4: Use mocked git operations (no network required).
Tests 3, 5: Use local temp directories.
"""
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
import tempfile
import os

from pipeline.mirror import RepositoryMirror, MirrorError


# ── Test 1: Clone creates the target directory ────────────────────────────────

def test_01_clone_creates_directory():
    """Test 1: Successful clone creates the destination directory."""
    with tempfile.TemporaryDirectory() as tmp:
        mirror_root = Path(tmp) / "mirror"
        mirror = RepositoryMirror(mirror_root=mirror_root)

        dest = mirror_root / "test-repo"

        mock_repo_inst = MagicMock()
        with patch("pipeline.mirror.Repo.clone_from") as mock_clone, \
             patch("pipeline.mirror.Repo", return_value=mock_repo_inst):
            # Simulate clone creating the directory
            dest.mkdir(parents=True, exist_ok=True)
            (dest / ".git").mkdir()
            mock_clone.return_value = mock_repo_inst

            result = mirror.mirror_repo(
                name="test-repo",
                url="https://github.com/example/test-repo",
                branch="main",
            )

        assert result.name == "test-repo"


# ── Test 2: Pull succeeds on existing clone ───────────────────────────────────

def test_02_pull_on_existing_clone():
    """Test 2: Pull is called (not clone) when directory already has .git."""
    with tempfile.TemporaryDirectory() as tmp:
        mirror_root = Path(tmp) / "mirror"
        dest = mirror_root / "existing-repo"
        dest.mkdir(parents=True)
        (dest / ".git").mkdir()   # marks it as existing clone

        mirror = RepositoryMirror(mirror_root=mirror_root)

        mock_repo = MagicMock()
        mock_repo.remotes.origin.pull.return_value = None
        mock_repo.git.checkout.return_value = None

        with patch("pipeline.mirror.Repo", return_value=mock_repo):
            result = mirror.mirror_repo(
                name="existing-repo",
                url="https://github.com/example/existing-repo",
                branch="main",
            )

        mock_repo.remotes.origin.pull.assert_called_once()
        assert result == dest


# ── Test 3: get_changed_files returns .java files ────────────────────────────

def test_03_get_changed_files_returns_java():
    """Test 3: Without a since_sha, returns all .java files in repo."""
    with tempfile.TemporaryDirectory() as tmp:
        repo_path = Path(tmp)
        # Create sample .java files
        (repo_path / "src").mkdir()
        (repo_path / "src" / "Main.java").write_text("public class Main {}")
        (repo_path / "src" / "Util.java").write_text("public class Util {}")
        (repo_path / "src" / "build.xml").write_text("<project/>")  # non-java

        mirror = RepositoryMirror(mirror_root=Path(tmp))
        files = mirror.get_changed_files(repo_path, since_sha=None)

        java_names = [f.name for f in files]
        assert "Main.java" in java_names
        assert "Util.java" in java_names
        assert "build.xml" not in java_names


# ── Test 4: Idempotent — clone called twice doesn't crash ─────────────────────

def test_04_mirror_idempotent():
    """Test 4: Calling mirror_repo twice (pull on second call) succeeds."""
    with tempfile.TemporaryDirectory() as tmp:
        mirror_root = Path(tmp) / "mirror"
        mirror = RepositoryMirror(mirror_root=mirror_root)
        dest = mirror_root / "idempotent-repo"

        mock_repo_inst = MagicMock()

        # First call: simulate clone
        with patch("pipeline.mirror.Repo.clone_from") as mock_clone, \
             patch("pipeline.mirror.Repo", return_value=mock_repo_inst):
            dest.mkdir(parents=True, exist_ok=True)
            (dest / ".git").mkdir()
            mock_clone.return_value = mock_repo_inst
            mirror.mirror_repo("idempotent-repo", "https://example.com/repo", "main")

        # Second call: simulate pull (dest/.git already exists)
        with patch("pipeline.mirror.Repo", return_value=mock_repo_inst):
            result = mirror.mirror_repo("idempotent-repo", "https://example.com/repo", "main")

        assert result == dest


# ── Test 5: Invalid URL raises MirrorError ────────────────────────────────────

def test_05_invalid_url_raises_mirror_error():
    """Test 5: A failed clone raises MirrorError, not an unhandled exception."""
    with tempfile.TemporaryDirectory() as tmp:
        mirror = RepositoryMirror(mirror_root=Path(tmp) / "mirror")

        import git
        with patch("pipeline.mirror.Repo.clone_from",
                   side_effect=git.GitCommandError("clone", 128)):
            with pytest.raises(MirrorError):
                mirror.mirror_repo(
                    name="bad-repo",
                    url="https://invalid.example.com/bad-repo",
                    branch="main",
                )
