"""Anchors keep pointing at the right code after the work tree moves on.

The point of these tests is the third resolution step: a file that was rewritten
or deleted is still recoverable from the commit the citation was made in.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.services import anchors
from app.storage import repo


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


@pytest.fixture
def git_project(tmp_path: Path, conn_reset) -> dict:
    root = tmp_path / "gproj"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "core.autocrlf", "false")
    (root / "pkg").mkdir()
    (root / "pkg" / "sample.py").write_text(
        '"""Sample module."""\n\n\ndef bar(x):\n    return x + 1\n',
        encoding="utf-8",
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "first")
    record = repo.create_project("gproj", str(root))
    return record


def _conversation(project: dict) -> dict:
    return repo.create_conversation(
        project_id=project["id"],
        title="t",
        mode="snippet",
        file_path="pkg/sample.py",
        start_line=4,
        end_line=5,
    )


def _anchor(project: dict, conversation: dict) -> dict:
    anchor = anchors.create_for_citation(
        project_id=project["id"],
        conversation_id=conversation["id"],
        file_path="pkg/sample.py",
        start_line=4,
        end_line=5,
    )
    assert anchor is not None
    return anchor


def test_citation_records_the_revision(git_project: dict):
    conversation = _conversation(git_project)
    anchor = _anchor(git_project, conversation)
    head = _git(Path(git_project["path"]), "rev-parse", "HEAD").strip()
    assert anchor["commit_sha"] == head
    assert anchor["worktree_dirty"] == 0


def test_citation_on_dirty_file_is_flagged(git_project: dict):
    root = Path(git_project["path"])
    (root / "pkg" / "sample.py").write_text("def bar(x):\n    return x + 42\n", encoding="utf-8")
    conversation = _conversation(git_project)
    anchor = _anchor(git_project, conversation)
    assert anchor["worktree_dirty"] == 1


def test_rewritten_file_falls_back_to_the_commit(git_project: dict):
    root = Path(git_project["path"])
    conversation = _conversation(git_project)
    anchor = _anchor(git_project, conversation)
    first = anchor["commit_sha"]

    # Rewrite the file completely and commit: HEAD no longer holds the snippet,
    # but the recorded commit still does.
    (root / "pkg" / "sample.py").write_text("def totally_different():\n    return None\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "rewrite")

    stats = anchors.refresh(git_project["id"])
    assert stats["historical"] == 1
    row = repo.list_anchors(git_project["id"])[0]
    assert row["status"] == "historical"
    assert row["resolved_commit"] == first


def test_deleted_file_is_still_recoverable(git_project: dict):
    root = Path(git_project["path"])
    conversation = _conversation(git_project)
    anchor = _anchor(git_project, conversation)
    first = anchor["commit_sha"]

    (root / "pkg" / "sample.py").unlink()
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "delete")

    stats = anchors.refresh(git_project["id"])
    assert stats["historical"] == 1
    row = repo.list_anchors(git_project["id"])[0]
    assert row["status"] == "historical"
    assert row["resolved_commit"] == first
    assert row["snippet"]


def test_unreachable_commit_degrades_to_modified(git_project: dict):
    root = Path(git_project["path"])
    conversation = _conversation(git_project)
    anchor = _anchor(git_project, conversation)
    # Simulate a sha git cannot read any more (shallow clone / GC). Written
    # directly because the app never rewrites an anchor's revision.
    from app.core import db

    with db.write_conn() as conn:
        conn.execute("UPDATE code_anchors SET commit_sha = ? WHERE id = ?", ("0" * 40, anchor["id"]))
    (root / "pkg" / "sample.py").write_text("def other():\n    pass\n", encoding="utf-8")

    stats = anchors.refresh(git_project["id"])
    assert stats["updated"] == 1
    assert repo.list_anchors(git_project["id"])[0]["status"] == "modified"


def test_matching_worktree_clears_a_stale_resolution(git_project: dict):
    root = Path(git_project["path"])
    conversation = _conversation(git_project)
    anchor = _anchor(git_project, conversation)
    repo.update_anchor(anchor["id"], status="historical", resolved_commit=anchor["commit_sha"])
    stats = anchors.refresh(git_project["id"])
    assert stats["unchanged"] == 1
    row = repo.list_anchors(git_project["id"])[0]
    assert row["status"] == "unchanged"
    assert row["resolved_commit"] is None
