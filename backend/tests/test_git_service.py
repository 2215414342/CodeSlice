"""git_service: rev resolution, show/log bounds, refs, dirty detection."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.services import git_service as git


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


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    # Keep the work tree byte-identical to the blobs: otherwise a Windows global
    # autocrlf would make every file look dirty and mask real failures.
    _git(root, "config", "core.autocrlf", "false")


@pytest.fixture
def repo(tmp_path: Path, conn_reset) -> Path:
    root = tmp_path / "repo"
    _init_repo(root)
    (root / "pkg").mkdir()
    (root / "pkg" / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    (root / "empty.txt").write_text("", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "说明.md").write_text("# 说明\n\n中文路径\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "first commit")
    return root


def test_non_repo_is_reported(tmp_path: Path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert git.is_repo(plain) is False
    assert git.head_sha(plain) is None
    state = git.current_state(plain)
    assert state["is_repo"] is False and state["dirty"] is False


def test_state_and_head(repo: Path):
    state = git.current_state(repo)
    assert state["is_repo"] is True
    assert state["head"] and len(state["head"]) == 40
    assert state["head_short"] == state["head"][:7]
    assert state["branch"] is not None and state["detached"] is False
    assert state["dirty"] is False

    (repo / "pkg" / "a.py").write_text("def a():\n    return 2\n", encoding="utf-8")
    dirty = git.current_state(repo)
    assert dirty["dirty"] is True
    assert "pkg/a.py" in dirty["modified"]


def test_resolve_rev_variants(repo: Path):
    head = git.head_sha(repo)
    assert git.resolve_rev(repo, "HEAD") == head
    assert git.resolve_rev(repo, head[:7]) == head
    assert git.resolve_rev(repo, "HEAD~1") is None  # only one commit
    # Option-shaped input can never ride in as a flag.
    assert git.resolve_rev(repo, "--output=NUL") is None
    assert git.resolve_rev(repo, "../HEAD") is None
    assert git.resolve_rev(repo, "does-not-exist") is None


def test_show_returns_content_and_distinguishes_missing(repo: Path):
    head = git.head_sha(repo)
    # Blobs are newline-normalized by git; the exact EOL is not the contract.
    assert git.show(repo, "HEAD", "pkg/a.py").splitlines() == ["def a():", "    return 1"]
    # An empty file exists: "" is an answer, None is "no such path".
    assert git.show(repo, head, "empty.txt") == ""
    assert git.show(repo, head, "nope.py") is None
    # CJK path stays literal instead of becoming an octal-escaped name.
    assert "中文路径" in (git.show(repo, head, "docs/说明.md") or "")
    # Paths that could escape the root never reach git.
    assert git.show(repo, head, "../../etc/passwd") is None


def test_show_previous_commit(repo: Path):
    first = git.head_sha(repo)
    (repo / "pkg" / "a.py").write_text("def a():\n    return 99\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "second")
    second = git.head_sha(repo)
    assert second != first
    assert "return 99" in (git.show(repo, second, "pkg/a.py") or "")
    assert "return 1" in (git.show(repo, first, "pkg/a.py") or "")

    # Every read is bounded: a blob larger than the cap fails loudly instead of
    # being accumulated into memory.
    with pytest.raises(git.GitOutputLimit):
        git.show(repo, second, "pkg/a.py", max_bytes=10)


def test_log_limit_skip_and_path(repo: Path):
    (repo / "pkg" / "a.py").write_text("def a():\n    return 3\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "touch a")
    other = repo / "other.py"
    other.write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "touch other")

    all_recent = git.log(repo, limit=2)
    assert len(all_recent["commits"]) == 2
    assert all_recent["truncated"] is True
    assert all_recent["commits"][0]["subject"] == "touch other"
    assert all_recent["commits"][0]["parents"]

    only_a = git.log(repo, limit=5, rel_path="pkg/a.py")
    assert [c["subject"] for c in only_a["commits"]] == ["touch a", "first commit"]

    skipped = git.log(repo, limit=1, skip=1)
    assert skipped["commits"][0]["subject"] == "touch a"


def test_worktree_dirty_and_ancestor(repo: Path):
    first = git.head_sha(repo)
    assert git.worktree_dirty(repo, "pkg/a.py") is False
    (repo / "pkg" / "a.py").write_text("def a():\n    return 7\n", encoding="utf-8")
    assert git.worktree_dirty(repo, "pkg/a.py") is True
    # Untracked files count too: they are content HEAD does not contain.
    (repo / "new.py").write_text("y = 2\n", encoding="utf-8")
    assert git.worktree_dirty(repo, "new.py") is True

    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "second")
    second = git.head_sha(repo)
    assert git.is_ancestor(repo, first, second) is True
    assert git.is_ancestor(repo, second, first) is False
