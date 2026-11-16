"""Notes in a revision scope: visibility ("did it exist then?") and ref status."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from app.storage import repo


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _hash(text: str) -> str:
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


@pytest.fixture
def git_project(tmp_path: Path, conn_reset) -> dict:
    root = tmp_path / "notesrev"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "core.autocrlf", "false")
    (root / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "first")
    (root / "a.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "second")
    return repo.create_project("notesrev", str(root))


def _add_note(project: dict, base: str | None, snippet: str, path: str = "a.py", lines: tuple[int, int] = (1, 2)) -> dict:
    note = repo.create_note(project["id"], "标题", "# 标题\n\n正文", base_commit=base)
    repo.replace_note_refs(
        note["id"],
        project["id"],
        [
            {
                "path": path,
                "start_line": lines[0],
                "end_line": lines[1],
                "symbol": None,
                "snippet": snippet,
                "code_hash": _hash(snippet),
                "commit_sha": base,
                "status": "unchanged",
            }
        ],
    )
    return note


def test_note_written_later_is_marked_not_hidden(git_project: dict, client):
    root = Path(git_project["path"])
    older = _git(root, "rev-parse", "HEAD~1").strip()
    head = _git(root, "rev-parse", "HEAD").strip()
    pid = git_project["id"]

    old_note = _add_note(git_project, older, "def f():\n    return 1")
    new_note = _add_note(git_project, head, "def f():\n    return 2")

    at_old = client.get("/api/notes", params={"project_id": pid, "rev": older}).json()
    states = {note["id"]: note["note_state"] for note in at_old}
    assert states[old_note["id"]] == "existing"
    assert states[new_note["id"]] == "later"
    assert all(note["visible_at_rev"] is True or note["note_state"] == "later" for note in at_old)

    at_head = client.get("/api/notes", params={"project_id": pid, "rev": head}).json()
    assert {note["id"]: note["note_state"] for note in at_head}[old_note["id"]] == "existing"


def test_refs_resolve_inside_the_commit(git_project: dict, client):
    root = Path(git_project["path"])
    older = _git(root, "rev-parse", "HEAD~1").strip()
    head = _git(root, "rev-parse", "HEAD").strip()
    note = _add_note(git_project, older, "def f():\n    return 1")

    same = client.get(f"/api/notes/{note['id']}", params={"rev": older}).json()
    assert same["refs"][0]["status"] == "unchanged"

    changed = client.get(f"/api/notes/{note['id']}", params={"rev": head}).json()
    assert changed["refs"][0]["status"] == "historical"
    assert changed["refs"][0]["resolved_commit"] == older


def test_refs_prefer_the_current_file_when_the_block_still_exists(git_project: dict, client):
    root = Path(git_project["path"])
    older = _git(root, "rev-parse", "HEAD~1").strip()
    # The note records an older commit, but the exact block it quotes is still
    # present in the work tree. Opening it should stay on the live file.
    note = _add_note(git_project, older, "def f():\n    return 2")
    result = client.get(f"/api/notes/{note['id']}").json()
    ref = result["refs"][0]
    assert ref["status"] == "unchanged"
    assert ref["resolved_commit"] is None
    assert (ref["resolved_start_line"], ref["resolved_end_line"]) == (1, 2)


def test_refs_follow_the_block_when_lines_move(git_project: dict, client):
    root = Path(git_project["path"])
    (root / "a.py").write_text("# header\n\n# note\ndef f():\n    return 2\n", encoding="utf-8")
    note = _add_note(git_project, None, "def f():\n    return 2")
    result = client.get(f"/api/notes/{note['id']}").json()
    ref = result["refs"][0]
    assert ref["status"] == "moved"
    assert ref["resolved_commit"] is None
    assert (ref["resolved_start_line"], ref["resolved_end_line"]) == (4, 5)


def test_refs_fall_back_to_history_when_the_file_is_gone(git_project: dict, client):
    root = Path(git_project["path"])
    head = _git(root, "rev-parse", "HEAD").strip()
    note = _add_note(git_project, head, "def f():\n    return 2")
    (root / "a.py").unlink()
    result = client.get(f"/api/notes/{note['id']}").json()
    ref = result["refs"][0]
    assert ref["status"] == "historical"
    assert ref["resolved_commit"] == head
    assert (ref["resolved_start_line"], ref["resolved_end_line"]) == (1, 2)


def test_refs_are_deleted_only_when_neither_worktree_nor_commit_matches(git_project: dict, client):
    root = Path(git_project["path"])
    head = _git(root, "rev-parse", "HEAD").strip()
    note = _add_note(git_project, head, "def f():\n    return 9")
    (root / "a.py").unlink()
    result = client.get(f"/api/notes/{note['id']}").json()
    assert result["refs"][0]["status"] == "deleted"


def test_ref_to_a_file_missing_at_that_revision_is_deleted(git_project: dict, client):
    root = Path(git_project["path"])
    older = _git(root, "rev-parse", "HEAD~1").strip()
    note = _add_note(git_project, older, "def f():\n    return 1", path="gone.py")
    result = client.get(f"/api/notes/{note['id']}", params={"rev": older}).json()
    assert result["refs"][0]["status"] == "deleted"


def test_unknown_revision_on_note_is_404(git_project: dict, client):
    note = _add_note(git_project, None, "def f():\n    return 1")
    assert client.get(f"/api/notes/{note['id']}", params={"rev": "nope"}).status_code == 404


def test_notes_graph_filters_notes_from_the_future(git_project: dict, client):
    root = Path(git_project["path"])
    older = _git(root, "rev-parse", "HEAD~1").strip()
    head = _git(root, "rev-parse", "HEAD").strip()
    old_note = _add_note(git_project, older, "def f():\n    return 1")
    new_note = _add_note(git_project, head, "def f():\n    return 2")

    future = {node.get("note_id") for node in client.get("/api/graphs/notes", params={"rev": older}).json()["nodes"]}
    assert old_note["id"] in future
    assert new_note["id"] not in future
