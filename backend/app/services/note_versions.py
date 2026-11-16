"""Notes under a revision scope: did this note exist then, and where do its refs land?

A note is never copied per revision. It was written once, at a commit; the
revision view answers two questions about it:

* **Visibility** — was the note's base commit already reachable from the viewed
  commit? A note written later is shown dimmed ("此后新增"), not hidden: hiding
  it would make the knowledge base look like it forgot its own contents.
* **Ref status** — the quoted ranges are re-resolved against the commit's blobs
  using the same five status words as the work-tree resolver
  (``unchanged / moved / historical / modified / deleted``), so the reader has
  one vocabulary to learn.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from app.services import git_service

STATUS_UNCHANGED = "unchanged"
STATUS_MOVED = "moved"
STATUS_HISTORICAL = "historical"
STATUS_MODIFIED = "modified"
STATUS_DELETED = "deleted"


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def _block_hash(lines: list[str], start: int, end: int) -> tuple[str, str] | None:
    if start < 1 or end < start or end > len(lines):
        return None
    text = "\n".join(lines[start - 1 : end])
    return _hash_text(text), text


def _matches(reference: dict[str, Any], lines: list[str], start: int, end: int) -> bool:
    block = _block_hash(lines, start, end)
    if block is None:
        return False
    stored = reference.get("code_hash")
    if stored:
        return block[0] == stored
    snippet = reference.get("snippet")
    return bool(snippet) and block[1] == snippet.replace("\r\n", "\n")


def _find(lines: list[str], reference: dict[str, Any]) -> int | None:
    """Line number where the quoted block now starts, searching the whole file."""
    length = int(reference.get("end_line") or 0) - int(reference.get("start_line") or 0) + 1
    if length < 1:
        return None
    for index in range(0, len(lines) - length + 1):
        if _matches(reference, lines, index + 1, index + length):
            return index + 1
    return None


def resolve_refs(root: Path, refs: list[dict[str, Any]], rev: str) -> list[dict[str, Any]]:
    """Each reference's status *inside this commit*, without touching the work tree."""
    out: list[dict[str, Any]] = []
    cache: dict[str, list[str] | None] = {}
    for reference in refs:
        path = str(reference.get("path") or "")
        status = STATUS_MODIFIED
        start = int(reference.get("start_line") or 1)
        end = int(reference.get("end_line") or start)
        resolved_commit: str | None = None

        if path not in cache:
            raw = git_service.show_bytes(root, rev, path, max_bytes=2 * 1024 * 1024)
            cache[path] = raw.decode("utf-8", errors="replace").splitlines() if raw is not None else None
        lines = cache[path]
        if lines is None:
            status = STATUS_DELETED
        elif _matches(reference, lines, start, end):
            status = STATUS_UNCHANGED
        else:
            found = _find(lines, reference)
            if found is not None:
                status = STATUS_MOVED
                start, end = found, found + (end - start)
            else:
                base = reference.get("commit_sha")
                if base:
                    raw = git_service.show_bytes(root, str(base), path, max_bytes=2 * 1024 * 1024)
                    if raw is not None:
                        base_lines = raw.decode("utf-8", errors="replace").splitlines()
                        if _matches(reference, base_lines, int(reference.get("start_line") or 1), int(reference.get("end_line") or 1)):
                            status = STATUS_HISTORICAL
                            resolved_commit = str(base)

        out.append(
            {
                **reference,
                "status": status,
                "resolved_start_line": start,
                "resolved_end_line": end,
                "resolved_commit": resolved_commit,
                "checked_at": None,
                "scope_rev": rev,
            }
        )
    return out


def annotate_visibility(root: Path, notes: list[dict[str, Any]], rev: str) -> list[dict[str, Any]]:
    """Attach ``visible_at_rev`` / ``note_state`` to each note for this commit.

    A note is "existing" when its base commit is an ancestor of (or equal to) the
    viewed commit. ``unknown`` means the note predates version tracking; it stays
    visible because hiding every old note would empty the knowledge base.
    """
    out: list[dict[str, Any]] = []
    for note in notes:
        base = note.get("base_commit")
        if not base:
            out.append({**note, "visible_at_rev": True, "note_state": "unknown"})
            continue
        if str(base) == rev:
            out.append({**note, "visible_at_rev": True, "note_state": "existing"})
            continue
        exists = git_service.is_ancestor(root, str(base), rev)
        out.append(
            {
                **note,
                "visible_at_rev": bool(exists),
                "note_state": "existing" if exists else "later",
            }
        )
    return out
