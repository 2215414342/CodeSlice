"""Reading a revision: tree, single file, and the source corpus for the graph."""

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
    root = tmp_path / "revproj"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "core.autocrlf", "false")
    (root / "pkg").mkdir()
    (root / "pkg" / "old.py").write_text("def before():\n    return 1\n", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "说明.md").write_text("# 说明\n", encoding="utf-8")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "vendor.js").write_text("x();\n", encoding="utf-8")
    (root / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "first")

    (root / "pkg" / "old.py").write_text("def after():\n    return 2\n", encoding="utf-8")
    (root / "pkg" / "new.py").write_text("def added():\n    return 3\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "second")
    return repo.create_project("revproj", str(root))


def test_resolve_head_and_branch(git_project: dict):
    root = Path(git_project["path"])
    resolved = revisions.resolve(root, "HEAD")
    assert resolved["sha"] == _git(root, "rev-parse", "HEAD").strip()
    assert resolved["kind"] == "commit"
    assert revisions.resolve(root, "no-such-branch") is None


def test_read_file_at_each_revision(git_project: dict):
    root = Path(git_project["path"])
    first = _git(root, "rev-parse", "HEAD~1").strip()
    old_first = revisions.read_file(root, first, "pkg/old.py")
    assert old_first["exists"] is True
    assert "before" in old_first["content"]
    # A file added later did not exist in the older commit.
    assert revisions.read_file(root, first, "pkg/new.py")["exists"] is False

    head = revisions.read_file(root, "HEAD", "pkg/old.py")
    assert "after" in head["content"]
    assert revisions.read_file(root, "HEAD", "docs/说明.md")["exists"] is True


def test_tree_reflects_the_revision(git_project: dict):
    root = Path(git_project["path"])
    top = revisions.list_tree(root, "HEAD", depth=1)
    names = {node["name"] for node in top}
    assert {"pkg", "docs"} <= names
    # Ignored trees and binary files never appear as source.
    assert "node_modules" not in names
    pkgs = revisions.list_tree(root, "HEAD", "pkg", depth=1)
    assert {node["name"] for node in pkgs} == {"new.py", "old.py"}


def test_read_sources_excludes_ignored_and_binary(git_project: dict):
    root = Path(git_project["path"])
    sources, stats = revisions.read_sources(root, "HEAD")
    rels = {rel for rel, _ in sources}
    assert "pkg/new.py" in rels and "pkg/old.py" in rels
    assert "node_modules/vendor.js" not in rels
    assert "logo.png" not in rels
    assert stats["files"] == len(sources)

    older, _ = revisions.read_sources(root, "HEAD~1")
    # Markdown has no AST extractor, so it is not "source" for the graph.
    assert {rel for rel, _ in older} == {"pkg/old.py"}


def test_read_sources_refuses_oversized_corpus(git_project: dict):
    root = Path(git_project["path"])
    with pytest.raises(revisions.RevisionTooLarge):
        revisions.read_sources(root, "HEAD", max_bytes=40)


def test_read_sources_rejects_unknown_revision(git_project: dict):
    root = Path(git_project["path"])
    with pytest.raises(revisions.RevisionError):
        revisions.read_sources(root, "deadbeef" * 5)


def test_api_revision_endpoints(client, git_project):
    root = Path(git_project["path"])
    pid = git_project["id"]
    head = _git(root, "rev-parse", "HEAD").strip()

    branches = client.get(f"/api/projects/{pid}/git/branches").json()
    assert branches["current"] is not None
    assert any(item["current"] for item in branches["branches"])

    rev = client.get(f"/api/projects/{pid}/git/rev", params={"ref": "HEAD"}).json()
    assert rev["sha"] == head

    commits = client.get(f"/api/projects/{pid}/git/commits", params={"limit": 2}).json()
    assert commits["commits"][0]["sha"] == head

    tree = client.get(f"/api/projects/{pid}/git/tree", params={"rev": "HEAD", "depth": 1}).json()
    assert any(node["name"] == "pkg" for node in tree)

    file = client.get(
        f"/api/projects/{pid}/git/file", params={"rev": "HEAD~1", "path": "pkg/new.py"}
    ).json()
    assert file["exists"] is False

    missing = client.get(f"/api/projects/{pid}/git/rev", params={"ref": "nope"})
    assert missing.status_code == 404
