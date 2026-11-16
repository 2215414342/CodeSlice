"""Notes carry the revision they were written from, and their refs are indexed."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.services import notegen
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
    root = tmp_path / "notes-proj"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "core.autocrlf", "false")
    (root / "pkg").mkdir()
    (root / "pkg" / "sample.py").write_text("def bar(x):\n    return x + 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "first")
    return repo.create_project("notes-proj", str(root))


def _conversation(project: dict) -> dict:
    return repo.create_conversation(
        project_id=project["id"],
        title="t",
        mode="snippet",
        file_path="pkg/sample.py",
        start_line=1,
        end_line=2,
    )


def _refs(project: dict) -> list[dict]:
    conversation = _conversation(project)
    return notegen._refs_from_citations(
        conversation,
        [{"path": "pkg/sample.py", "start_line": 1, "end_line": 2, "symbol": "bar"}],
    )


def test_refs_capture_the_commit(git_project: dict):
    refs = _refs(git_project)
    head = _git(Path(git_project["path"]), "rev-parse", "HEAD").strip()
    assert len(refs) == 1
    assert refs[0]["commit_sha"] == head
    assert refs[0]["code_hash"]
    assert refs[0]["worktree_dirty"] is False


def test_refs_mark_a_dirty_worktree(git_project: dict):
    root = Path(git_project["path"])
    (root / "pkg" / "sample.py").write_text("def bar(x):\n    return 0\n", encoding="utf-8")
    refs = _refs(git_project)
    assert refs[0]["worktree_dirty"] is True


def test_render_code_section_carries_the_short_sha():
    text = notegen.render_code_section(
        [
            {
                "path": "pkg/sample.py",
                "start_line": 1,
                "end_line": 2,
                "commit_sha": "abcdef1234567890abcdef1234567890abcdef12",
                "symbol": "bar",
                "code": "def bar(x):\n    return x + 1",
            }
        ]
    )
    # The exported Markdown has no app around it; the version must travel with it.
    assert "### pkg/sample.py:1-2 @abcdef1（bar）" in text
    assert "```python" in text


def test_store_note_writes_base_and_refs(git_project: dict):
    conversation = _conversation(git_project)
    refs = _refs(git_project)
    note = notegen._store_note(
        conversation, conversation["id"], "标题", "文件名", "# 标题\n\n正文", refs
    )
    head = _git(Path(git_project["path"]), "rev-parse", "HEAD").strip()
    assert note["base_commit"] == head
    assert note["base_dirty"] == 0

    rows = repo.list_note_refs(note["id"])
    assert len(rows) == 1
    assert rows[0]["commit_sha"] == head
    assert rows[0]["status"] == "unchanged"


def test_regenerating_a_note_restamps_the_base(git_project: dict):
    root = Path(git_project["path"])
    conversation = _conversation(git_project)
    refs = _refs(git_project)
    note = notegen._store_note(conversation, conversation["id"], "旧", "旧", "# 旧\n\n正文", refs)

    (root / "pkg" / "sample.py").write_text("def bar(x):\n    return x + 2\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "second")
    refs2 = _refs(git_project)
    note2 = notegen._store_note(conversation, conversation["id"], "新", "新", "# 新\n\n正文", refs2)

    second = _git(root, "rev-parse", "HEAD").strip()
    assert note2["id"] == note["id"]
    assert note2["base_commit"] == second
    assert repo.list_note_refs(note2["id"])[0]["commit_sha"] == second


def test_note_detail_exposes_refs(client, conn_reset):
    project = repo.create_project("api-notes", str(Path(repo.__file__).parent))
    note = repo.create_note(project["id"], "标题", "# 标题\n\n正文")
    repo.replace_note_refs(
        note["id"],
        project["id"],
        [
            {
                "path": "app/main.py",
                "start_line": 1,
                "end_line": 2,
                "commit_sha": "abcdef1234567890abcdef1234567890abcdef12",
                "snippet": "x",
                "code_hash": "h",
            }
        ],
    )
    body = client.get(f"/api/notes/{note['id']}").json()
    assert body["refs"][0]["path"] == "app/main.py"
    assert body["base_commit"] is None
