"""Source-control change lists and file pairs (read-only)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.services import scm
from app.storage import repo


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


@pytest.fixture
def scm_project(tmp_path: Path, conn_reset) -> dict:
    root = tmp_path / "scmproj"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "core.autocrlf", "false")
    (root / "old.py").write_text("def before():\n    return 1\n", encoding="utf-8")
    (root / "keep.txt").write_text("stable\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "root")

    (root / "old.py").write_text("def after():\n    return 2\n\ndef extra():\n    return 9\n", encoding="utf-8")
    (root / "new.py").write_text("def added():\n    return 3\n", encoding="utf-8")
    (root / "keep.txt").unlink()
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "second")
    return repo.create_project("scmproj", str(root))


def test_commit_changes_are_against_the_first_parent(scm_project: dict):
    root = Path(scm_project["path"])
    result = scm.changes(root, "HEAD")
    assert result["kind"] == "commit"
    assert result["base"] == _git(root, "rev-parse", "HEAD~1").strip()
    rows = {item["path"]: item for item in result["files"]}
    assert rows["new.py"]["status"] == "added"
    assert rows["old.py"]["status"] == "modified"
    assert rows["keep.txt"]["status"] == "deleted"
    assert rows["new.py"]["additions"] >= 1
    assert result["additions"] >= 3
    assert result["deletions"] >= 1


def test_root_commit_reports_every_file_as_added(scm_project: dict):
    root = Path(scm_project["path"])
    first = _git(root, "rev-parse", "HEAD~1").strip()
    result = scm.changes(root, first)
    assert result["base"] is None
    assert {item["path"] for item in result["files"]} == {"old.py", "keep.txt"}
    assert all(item["status"] == "added" for item in result["files"])


def test_worktree_changes_cover_modified_untracked_and_deleted(scm_project: dict):
    root = Path(scm_project["path"])
    (root / "old.py").write_text("def after():\n    return 42\n", encoding="utf-8")
    (root / "scratch.py").write_text("x = 1\nx = 2\n", encoding="utf-8")
    (root / "new.py").unlink()

    result = scm.changes(root, None)
    assert result["kind"] == "worktree"
    rows = {item["path"]: item for item in result["files"]}
    assert rows["scratch.py"]["status"] == "added"
    assert rows["scratch.py"]["additions"] == 2
    assert rows["old.py"]["status"] == "modified"
    assert rows["new.py"]["status"] == "deleted"


def test_pair_returns_both_sides_for_a_commit(scm_project: dict):
    root = Path(scm_project["path"])
    pair = scm.file_pair(root, "HEAD", "old.py")
    assert "before" in pair["old_content"]
    assert "after" in pair["new_content"]
    assert pair["old_exists"] is True and pair["new_exists"] is True
    assert pair["language"] == "python"

    added = scm.file_pair(root, "HEAD", "new.py")
    assert added["old_exists"] is False
    assert added["old_content"] == ""


def test_pair_for_worktree(scm_project: dict):
    root = Path(scm_project["path"])
    (root / "scratch.py").write_text("x = 1\n", encoding="utf-8")
    pair = scm.file_pair(root, None, "scratch.py")
    assert pair["old_exists"] is False
    assert pair["new_content"].startswith("x = 1")
    assert pair["new_label"] == "工作区"


def test_unknown_revision_is_a_git_error(scm_project: dict, client):
    pid = scm_project["id"]
    assert client.get(f"/api/projects/{pid}/git/changes", params={"rev": "nope"}).status_code == 404
    assert client.get(f"/api/projects/{pid}/git/pair", params={"rev": "nope", "path": "old.py"}).status_code == 404


def test_changes_endpoint_shape(scm_project: dict, client):
    pid = scm_project["id"]
    data = client.get(f"/api/projects/{pid}/git/changes", params={"rev": "HEAD"}).json()
    assert {"kind", "rev", "base", "files", "additions", "deletions"} <= set(data)
    pair = client.get(f"/api/projects/{pid}/git/pair", params={"rev": "HEAD", "path": "old.py"}).json()
    assert pair["old_label"] and pair["new_label"]
