"""The knowledge graph and symbol index of a commit, isolated from the work tree."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.services import revisions
from app.storage import repo


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


@pytest.fixture
def git_project(tmp_path: Path, conn_reset) -> dict:
    root = tmp_path / "graphrev"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "core.autocrlf", "false")
    (root / "pkg").mkdir()
    (root / "pkg" / "old.py").write_text("def before():\n    return 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "first")
    (root / "pkg" / "old.py").write_text("def after():\n    return 2\n", encoding="utf-8")
    (root / "pkg" / "new.py").write_text("def helper():\n    return before()\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "second")
    return repo.create_project("graphrev", str(root))


def _labels(payload: dict) -> set[str]:
    return {str(node.get("label") or node.get("name") or "") for node in payload.get("nodes") or []}


def test_revision_graph_is_built_from_that_commit(git_project: dict):
    from app.graph import revision_store

    root = Path(git_project["path"])
    head = _git(root, "rev-parse", "HEAD").strip()
    older = _git(root, "rev-parse", "HEAD~1").strip()

    new_graph, rebuilt = revision_store.get_or_build(git_project["id"], root, head)
    assert rebuilt is True
    assert new_graph["rev"] == head
    assert {"after", "helper"} <= _labels(new_graph)
    assert "before" not in _labels(new_graph)

    old_graph, _ = revision_store.get_or_build(git_project["id"], root, older)
    assert "before" in _labels(old_graph)
    assert "helper" not in _labels(old_graph)


def test_revision_graph_is_cached_not_rebuilt(git_project: dict):
    from app.graph import revision_store

    root = Path(git_project["path"])
    head = _git(root, "rev-parse", "HEAD").strip()
    first, rebuilt = revision_store.get_or_build(git_project["id"], root, head)
    second, rebuilt_again = revision_store.get_or_build(git_project["id"], root, head)
    assert rebuilt is True and rebuilt_again is False
    assert [node["id"] for node in first["nodes"]] == [node["id"] for node in second["nodes"]]


def test_revision_symbol_index_answers_f12(git_project: dict, client):
    root = Path(git_project["path"])
    pid = git_project["id"]
    head = _git(root, "rev-parse", "HEAD").strip()

    response = client.get(
        f"/api/projects/{pid}/definition",
        params={"path": "pkg/new.py", "line": 1, "column": 6, "rev": head},
    ).json()
    names = {target["name"] for target in response["targets"]}
    assert "helper" in names

    # The file did not exist one commit earlier: a revision-scoped answer must
    # say so instead of reading the work tree copy.
    older = _git(root, "rev-parse", "HEAD~1").strip()
    missing = client.get(
        f"/api/projects/{pid}/definition",
        params={"path": "pkg/new.py", "line": 1, "column": 6, "rev": older},
    ).json()
    assert missing["reason"] == "file_not_found"


def test_graph_api_scopes_to_revision(git_project: dict, client):
    root = Path(git_project["path"])
    pid = git_project["id"]
    head = _git(root, "rev-parse", "HEAD").strip()

    payload = client.get(f"/api/projects/{pid}/graph", params={"rev": head}).json()
    assert payload["rev"] == head
    assert {"after", "helper"} <= {node["label"] for node in payload["nodes"]}

    worktree = client.get(f"/api/projects/{pid}/graph").json()
    # The work-tree payload keeps its old shape: no revision marker.
    assert "rev" not in worktree

    missing = client.get(f"/api/projects/{pid}/graph", params={"rev": "deadbeef" * 5})
    assert missing.status_code == 404


def test_revision_graph_ignores_worktree_edits(git_project: dict):
    from app.graph import revision_store

    root = Path(git_project["path"])
    head = _git(root, "rev-parse", "HEAD").strip()
    before, _ = revision_store.get_or_build(git_project["id"], root, head)

    # Edit the work tree without committing: the revision view must not move.
    (root / "pkg" / "new.py").write_text("def uncommitted():\n    return 9\n", encoding="utf-8")
    after, rebuilt = revision_store.get_or_build(git_project["id"], root, head)
    assert rebuilt is False
    assert _labels(after) == _labels(before)


def test_unknown_revision_is_an_explicit_error(git_project: dict):
    from app.graph import revision_store

    root = Path(git_project["path"])
    with pytest.raises(revisions.RevisionError):
        revision_store.get_or_build(git_project["id"], root, "no-such-branch")
