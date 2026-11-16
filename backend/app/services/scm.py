"""Read-only change lists and file pairs for the source-control view.

This is presentation data, not a Git client: it never stages, commits or
discards. Two scopes are supported and they answer different questions:

* ``rev`` given   -> the commit's own change set against its first parent
                     (``git show <sha>`` semantics), so the history timeline can
                     show "what this commit did".
* ``rev`` omitted -> the working tree against ``HEAD`` (staged + unstaged +
                     untracked), so the panel can show "what is not committed yet".

The pair reader returns both sides' text, never a patch, because the renderer
uses Monaco's diff editor: it needs the old and new buffers, not a hunk list.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.indexing.languages import detect_language
from app.services import git_service, paths

FILE_LIMIT = 2_000
FILE_MAX_BYTES = 2 * 1024 * 1024


def _count(text: str | None) -> int:
    if not text:
        return 0
    return len(text.splitlines())


def _binary(raw: bytes | None) -> bool:
    return bool(raw) and paths.looks_binary(raw)


def _parse_name_status(raw: str) -> list[dict[str, Any]]:
    """Parse ``diff-tree --name-status -z``.

    With ``-z`` Git emits ``STATUS\\0path\\0`` rather than ``STATUS<TAB>path``.
    A rename/copy carries ``R100\\0old\\0new\\0``. Some Git builds still print the
    commit id first; a bare 40-char hex token is therefore skipped.
    """
    tokens = raw.split("\0")
    rows: list[dict[str, Any]] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token:
            continue
        if len(token) == 40 and all(ch in "0123456789abcdef" for ch in token.lower()):
            continue
        code = token[0]
        if len(token) == 1 or (code in ("R", "C") and token[1:].isdigit()):
            # -z form: the path is its own token.
            path = tokens[index] if index < len(tokens) else ""
            index += 1
        else:
            path = token[1:].lstrip(" \t")
        old_path: str | None = None
        if code in ("R", "C"):
            if index < len(tokens):
                old_path = path
                path = tokens[index]
                index += 1
        if not path:
            continue
        status = {
            "A": "added",
            "D": "deleted",
            "R": "renamed",
            "C": "copied",
            "U": "conflicted",
        }.get(code, "modified")
        rows.append({"path": path, "old_path": old_path, "status": status})
    return rows


def _parse_numstat(raw: str) -> dict[str, tuple[int, int, bool]]:
    """Parse ``--numstat -z`` into path -> (additions, deletions, binary)."""
    tokens = raw.split("\0")
    counts: dict[str, tuple[int, int, bool]] = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token:
            continue
        parts = token.split("\t")
        if len(parts) < 3:
            continue
        added, deleted = parts[0], parts[1]
        binary = not added.isdigit() or not deleted.isdigit()
        path = "\t".join(parts[2:])
        if path == "" and index + 1 < len(tokens):
            # rename: counts were emitted before the two path tokens
            old, new = tokens[index], tokens[index + 1]
            index += 2
            value = (
                0 if binary else int(added),
                0 if binary else int(deleted),
                binary,
            )
            counts[new] = value
            counts[old] = value
            continue
        if not path:
            continue
        counts[path] = (
            0 if binary else int(added),
            0 if binary else int(deleted),
            binary,
        )
    return counts


def _first_parent(root: Path, sha: str) -> str | None:
    out = git_service._run(root, ["rev-list", "--parents", "-n1", sha], check=False)
    fields = out.split()
    return fields[1] if len(fields) > 1 else None


def _commit_changes(root: Path, sha: str) -> dict[str, Any]:
    parent = _first_parent(root, sha)
    name_raw = git_service._run(
        root,
        ["diff-tree", "--root", "-r", "--name-status", "-z", "--no-commit-id", sha],
        check=False,
    )
    num_raw = git_service._run(
        root,
        ["diff-tree", "--root", "-r", "--numstat", "-z", "--no-commit-id", sha],
        check=False,
    )
    rows = _parse_name_status(name_raw)
    counts = _parse_numstat(num_raw)
    files: list[dict[str, Any]] = []
    for row in rows:
        path = row["path"]
        added, deleted, binary = counts.get(path, (0, 0, False))
        files.append(
            {
                "path": path,
                "old_path": row["old_path"],
                "status": row["status"],
                "additions": added,
                "deletions": deleted,
                "binary": binary,
            }
        )
    total_add = sum(item["additions"] for item in files)
    total_del = sum(item["deletions"] for item in files)
    return {
        "kind": "commit",
        "rev": sha,
        "sha_short": sha[:7],
        "base": parent,
        "base_short": parent[:7] if parent else "空",
        "files": files[:FILE_LIMIT],
        "truncated": len(files) > FILE_LIMIT,
        "additions": total_add,
        "deletions": total_del,
    }


def _worktree_line_counts(root: Path, rel: str) -> tuple[int, int] | None:
    """Line additions/deletions for one tracked path against HEAD.

    Ask git per path rather than parsing one big patch: a path with an unusual
    name cannot then shift every later record.
    """
    try:
        raw = git_service._run(root, ["diff", "--numstat", "HEAD", "--", rel], check=False)
    except git_service.GitError:
        return None
    for line in raw.splitlines():
        parts = line.split("\t", 2)
        if len(parts) < 3:
            continue
        added, deleted = parts[0], parts[1]
        return (
            int(added) if added.isdigit() else 0,
            int(deleted) if deleted.isdigit() else 0,
        )
    return (0, 0)


def _parse_porcelain(raw: str) -> list[dict[str, Any]]:
    """Parse ``git status --porcelain=v1 -z`` (two status columns, not one)."""
    tokens = raw.split("\0")
    rows: list[dict[str, Any]] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token:
            continue
        if len(token) < 4:
            continue
        code = token[:2]
        path = token[3:]
        old_path: str | None = None
        if "R" in code or "C" in code:
            if index < len(tokens):
                old_path = token[3:]
                path = tokens[index]
                index += 1
        if code == "??":
            status = "added"
        elif "D" in code:
            status = "deleted"
        elif "A" in code:
            status = "added"
        elif "R" in code:
            status = "renamed"
        elif "C" in code:
            status = "copied"
        elif "U" in code:
            status = "conflicted"
        else:
            status = "modified"
        rows.append({"path": path, "old_path": old_path, "status": status})
    return rows


def worktree_changes(root: Path) -> dict[str, Any]:
    """Uncommitted changes against HEAD, including untracked files."""
    if not git_service.is_repo(root):
        return {
            "kind": "worktree",
            "rev": None,
            "sha_short": "HEAD",
            "base": git_service.head_sha(root),
            "base_short": (git_service.head_sha(root) or "")[:7] or "空",
            "files": [],
            "truncated": False,
            "additions": 0,
            "deletions": 0,
            "is_repo": False,
        }
    try:
        raw = git_service._run(root, ["status", "--porcelain=v1", "-z"], check=False)
    except git_service.GitError:
        raw = ""
    rows = _parse_porcelain(raw)
    files: list[dict[str, Any]] = []
    for row in rows:
        path = row["path"]
        counts = _worktree_line_counts(root, path)
        binary = False
        if row["status"] == "added" and counts in (None, (0, 0)):
            # Untracked: git has no blob to diff, so count the file itself.
            try:
                target = paths.resolve_in_project(root, path)
                size = target.stat().st_size
                if size > FILE_MAX_BYTES:
                    counts, binary = (0, 0), False
                else:
                    data = target.read_bytes()
                    binary = paths.looks_binary(data)
                    text = "" if binary else data.decode("utf-8", errors="replace")
                    counts = (_count(text), 0)
            except (OSError, ValueError):
                counts = (0, 0)
        added, deleted = counts or (0, 0)
        files.append(
            {
                "path": path,
                "old_path": row["old_path"],
                "status": row["status"],
                "additions": added,
                "deletions": deleted,
                "binary": binary,
            }
        )
    head = git_service.head_sha(root)
    return {
        "kind": "worktree",
        "rev": None,
        "sha_short": "工作区",
        "base": head,
        "base_short": head[:7] if head else "空",
        "files": files[:FILE_LIMIT],
        "truncated": len(files) > FILE_LIMIT,
        "additions": sum(item["additions"] for item in files),
        "deletions": sum(item["deletions"] for item in files),
        "is_repo": True,
    }


def changes(root: Path, rev: str | None = None) -> dict[str, Any]:
    if not rev:
        return worktree_changes(root)
    sha = git_service.resolve_rev(root, rev)
    if sha is None:
        raise git_service.GitError(f"unknown revision: {rev}")
    return _commit_changes(root, sha)


def _read_worktree(root: Path, rel: str) -> tuple[bytes | None, bool]:
    try:
        target = paths.resolve_in_project(root, rel)
        if not target.is_file():
            return None, False
        if target.stat().st_size > FILE_MAX_BYTES:
            raise git_service.GitOutputLimit(f"{rel} is larger than the pair limit")
        data = target.read_bytes()
        return data, paths.looks_binary(data)
    except (OSError, ValueError):
        return None, False


def _decode(raw: bytes | None) -> str | None:
    if raw is None:
        return None
    return raw.decode("utf-8", errors="replace")


def file_pair(root: Path, rev: str | None, rel_path: str) -> dict[str, Any]:
    """Old and new buffers for one path in the requested scope.

    ``rev`` given: parent side vs commit side. ``rev`` omitted: ``HEAD`` vs the
    working tree. Either side may be absent (added/deleted/untracked); ``exists``
    is reported per side so the diff editor can render an empty pane honestly.
    """
    rel = git_service._safe_rel(rel_path)
    if rel is None:
        raise git_service.GitError("invalid path")

    if rev:
        sha = git_service.resolve_rev(root, rev)
        if sha is None:
            raise git_service.GitError(f"unknown revision: {rev}")
        parent = _first_parent(root, sha)
        old_raw = git_service.show_bytes(root, parent, rel) if parent else None
        new_raw = git_service.show_bytes(root, sha, rel)
        old_label = parent[:7] if parent else "空"
        new_label = sha[:7]
        base_rev = parent
        target_rev = sha
    else:
        head = git_service.head_sha(root)
        old_raw = git_service.show_bytes(root, head, rel) if head else None
        new_raw, _ = _read_worktree(root, rel)
        old_label = head[:7] if head else "空"
        new_label = "工作区"
        base_rev = head
        target_rev = None

    old_binary = _binary(old_raw)
    new_binary = _binary(new_raw)
    binary = bool(old_binary or new_binary)
    old_text = "" if binary or old_raw is None else _decode(old_raw) or ""
    new_text = "" if binary or new_raw is None else _decode(new_raw) or ""
    truncated = bool(
        (old_raw is not None and len(old_raw) > FILE_MAX_BYTES)
        or (new_raw is not None and len(new_raw) > FILE_MAX_BYTES)
    )
    return {
        "path": rel,
        "rev": target_rev,
        "base": base_rev,
        "old_label": old_label,
        "new_label": new_label,
        "old_exists": old_raw is not None,
        "new_exists": new_raw is not None,
        "old_content": old_text,
        "new_content": new_text,
        "binary": binary,
        "truncated": truncated,
        "language": "binary" if binary else detect_language(rel, new_text or old_text),
    }
