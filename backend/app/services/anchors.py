"""Code anchors: bind a conversation to the exact code it was discussing, and
keep that binding meaningful after the file changes.

Status semantics (from docs/ARCHITECTURE.md, extended by docs/GIT_AWARE_KB.md):

  unchanged   the recorded range still exists and its content hash matches
  moved       the same content block was found elsewhere in the current file
  historical  the file changed, but the recorded commit still has the exact
              content — the anchor is recoverable, just not in the work tree
  modified    content matches nowhere: not the file, not the recorded commit
  deleted     the file is gone from the work tree *and* the recorded commit has
              no such path (the snippet snapshot is the last copy)

The resolution order is deliberate: the work tree is what the user is reading
now, so it wins when it matches; git comes second because it is the only place
that still holds what was deleted or moved; the snippet snapshot is last
because a snapshot has no line numbers left to navigate to.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from app.core.errors import InvalidInput, NotFound
from app.services import git_service, paths
from app.storage import repo


def _hash(text: str) -> str:
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def extract_snippet(root: Path, rel: str, start_line: int, end_line: int) -> str | None:
    try:
        path = paths.resolve_in_project(root, rel)
    except InvalidInput:
        return None
    if not path.is_file():
        return None
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    start = max(1, int(start_line))
    end = min(len(lines), int(end_line))
    if start > len(lines):
        return None
    return "\n".join(lines[start - 1 : end])


def create_for_citation(
    project_id: int,
    conversation_id: str,
    file_path: str,
    start_line: int,
    end_line: int,
    symbol: str | None = None,
) -> dict[str, Any] | None:
    project = repo.get_project(project_id)
    if project is None:
        return None
    try:
        root = paths.resolve_root(project["path"])
    except Exception:
        return None
    snippet = extract_snippet(root, file_path, start_line, end_line)
    # Pin both the revision and whether the work tree had drifted from it. A
    # citation made on a dirty file cannot claim "this is HEAD's version": it
    # is the work tree's version, which no commit contains yet.
    commit = git_service.head_sha(root)
    dirty = git_service.worktree_dirty(root, file_path) if commit else False
    return repo.create_anchor(
        project_id=project_id,
        conversation_id=conversation_id,
        file_path=file_path,
        start_line=start_line,
        end_line=end_line,
        symbol=symbol,
        snippet=snippet,
        code_hash=_hash(snippet) if snippet else None,
        commit_sha=commit,
        worktree_dirty=dirty,
    )


def _window(lines: list[str], start: int, end: int) -> str:
    start = max(1, int(start))
    if start > len(lines):
        return ""
    return "\n".join(lines[start - 1 : min(int(end), len(lines))])


def _find_block(lines: list[str], snippet: str | None) -> int | None:
    """1-based line where ``snippet`` appears verbatim, or ``None``."""
    target = [line.strip() for line in (snippet or "").splitlines() if line.strip()]
    if not target:
        return None
    stripped = [line.strip() for line in lines]
    head = target[0]
    for idx, line in enumerate(stripped):
        if line != head:
            continue
        if stripped[idx : idx + len(target)] == target:
            return idx + 1
    return None


def refresh(project_id: int) -> dict[str, int]:
    project = repo.get_project(project_id)
    if project is None:
        raise NotFound("project not found")
    root = paths.resolve_root(project["path"])
    stats = {"updated": 0, "deleted": 0, "moved": 0, "unchanged": 0, "historical": 0}
    has_git = git_service.is_repo(root)

    for anchor in repo.list_anchors(project_id):
        rel = anchor["file_path"]
        start, end = int(anchor["start_line"]), int(anchor["end_line"])
        expected = anchor["code_hash"] or ""

        try:
            path = paths.resolve_in_project(root, rel)
        except InvalidInput:
            path = None
        text = ""
        if path is not None and path.is_file():
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
        lines = text.splitlines() if text else []

        # 1. the work tree, at the recorded range
        current_hash = _hash(_window(lines, start, end)) if lines else ""
        if expected and current_hash == expected:
            # Count every verified anchor: a 0 in "N 个锚点已核对" looks broken.
            stats["unchanged"] += 1
            if anchor["status"] != "unchanged" or anchor.get("resolved_commit"):
                repo.update_anchor(
                    anchor["id"], status="unchanged", resolved_commit=None, resolved_status=None, checked_at=_now()
                )
            continue

        # 2. the same block elsewhere in the current file
        found_at = _find_block(lines, anchor["snippet"]) if lines else None
        if found_at is not None and expected:
            repo.update_anchor(
                anchor["id"],
                status="moved",
                start_line=found_at,
                end_line=found_at + max(0, len((anchor["snippet"] or "").strip().splitlines()) - 1),
                resolved_commit=None,
                resolved_status=None,
                checked_at=_now(),
            )
            stats["moved"] += 1
            continue

        # 3. the recorded commit still has this exact content
        blob = None
        if has_git and anchor.get("commit_sha"):
            try:
                blob = git_service.show(root, anchor["commit_sha"], rel)
            except git_service.GitOutputLimit:
                # An enormous blob is not a resolution; fall through to the
                # snapshot instead of failing the whole refresh.
                blob = None
        if blob is not None:
            blob_lines = blob.splitlines()
            if expected and _hash(_window(blob_lines, start, end)) == expected:
                repo.update_anchor(
                    anchor["id"],
                    status="historical",
                    resolved_commit=anchor["commit_sha"],
                    resolved_status="content-match",
                    checked_at=_now(),
                )
                stats["historical"] += 1
                continue

        # 4. nothing matches: changed, or gone
        if lines:
            repo.update_anchor(anchor["id"], status="modified", checked_at=_now())
            stats["updated"] += 1
        else:
            repo.update_anchor(anchor["id"], status="deleted", checked_at=_now())
            stats["deleted"] += 1
    return stats


def _now() -> str:
    from app.core.db import utcnow

    return utcnow()
