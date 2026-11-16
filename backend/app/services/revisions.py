"""Read one revision of a repository as *data*, without touching the work tree.

Why not ``git worktree add``: it writes ``.git/worktrees`` metadata, materialises
the whole tree on disk, and leaves cleanup to whoever crashes first. Why not one
``git show`` per file: a thousand files means a thousand processes. ``git archive``
is a single process streaming a tar, which is exactly the shape the graph builder
and the symbol index want — they already accept ``(relative_path, bytes)`` pairs.

Everything is bounded. A revision whose sources exceed the caps is refused with
an explicit error; returning a half-read corpus would build a graph that silently
describes a different repository than the one the user asked for.
"""

from __future__ import annotations

import tarfile
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.graph.ast.languages import config_for
from app.indexing.languages import detect_language
from app.services import git_service, paths

ARCHIVE_TIMEOUT_SECONDS = 120
MAX_REVISION_FILES = 3_000
MAX_REVISION_BYTES = 80 * 1024 * 1024
MAX_GRAPH_FILE_BYTES = 1_500_000  # the graph builder's own per-file ceiling

# File lists are immutable per commit, so caching them is free correctness. Keyed
# by (root, sha) and bounded: a user browsing history must not grow this forever.
_TREE_CACHE: OrderedDict[tuple[str, str], list[str]] = OrderedDict()
_TREE_CACHE_LOCK = threading.Lock()
_TREE_CACHE_SIZE = 8


class RevisionError(RuntimeError):
    """A revision could not be read (bad ref, missing object, unreadable tar)."""


class RevisionTooLarge(RevisionError):
    """The revision exceeds the source caps; refuse rather than build a partial graph."""


def resolve(root: Path, ref: str) -> dict[str, Any] | None:
    """``branch/tag/sha`` → ``{sha, sha_short, kind, name}`` or ``None``."""
    return git_service.resolve_ref(root, ref)


def _safe_member_path(name: str) -> str | None:
    rel = name.replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    if not rel or rel.startswith("/"):
        return None
    parts = rel.split("/")
    if any(part in ("", "..") for part in parts):
        return None
    return rel


def _tracked_paths(root: Path, sha: str) -> list[str]:
    """Every file path in the commit, minus ignored trees and binaries."""
    key = (str(root), sha)
    with _TREE_CACHE_LOCK:
        cached = _TREE_CACHE.get(key)
        if cached is not None:
            _TREE_CACHE.move_to_end(key)
            return cached
    try:
        names = git_service.ls_tree_names(root, sha)
    except git_service.GitError as exc:
        raise RevisionError(exc.detail or str(exc)) from exc
    out = []
    for item in names:
        rel = _safe_member_path(item)
        if rel is None:
            continue
        parts = rel.split("/")
        if any(paths.is_ignored_dir(part) for part in parts[:-1]):
            continue
        out.append(rel)
    with _TREE_CACHE_LOCK:
        _TREE_CACHE[key] = out
        _TREE_CACHE.move_to_end(key)
        while len(_TREE_CACHE) > _TREE_CACHE_SIZE:
            _TREE_CACHE.popitem(last=False)
    return out


def read_sources(
    root: Path,
    sha: str,
    *,
    max_files: int = MAX_REVISION_FILES,
    max_bytes: int = MAX_REVISION_BYTES,
) -> tuple[list[tuple[str, bytes]], dict[str, Any]]:
    """Source files of one commit, as ``(relative_path, bytes)`` pairs.

    Reads through ``git archive`` so the bytes come from the object database, not
    from whatever the work tree happens to contain right now.
    """
    if git_service.resolve_rev(root, sha) is None:
        raise RevisionError("revision not found")
    stats: dict[str, Any] = {"files": 0, "bytes": 0, "skipped_large": 0, "truncated": False}
    out: list[tuple[str, bytes]] = []
    proc = git_service.open_stream(root, ["archive", "--format=tar", sha])
    killed = threading.Event()

    def _kill() -> None:
        killed.set()
        try:
            proc.kill()
        except OSError:
            pass

    watchdog = threading.Timer(ARCHIVE_TIMEOUT_SECONDS, _kill)
    watchdog.daemon = True
    watchdog.start()
    total = 0
    stderr_bytes = b""
    try:
        assert proc.stdout is not None
        with tarfile.open(fileobj=proc.stdout, mode="r|") as archive:
            for member in archive:
                if killed.is_set():
                    raise RevisionError("reading the revision timed out")
                if not member.isfile():
                    continue  # directories, symlinks, submodule gitlinks
                rel = _safe_member_path(member.name)
                if rel is None:
                    continue
                parts = rel.split("/")
                if any(paths.is_ignored_dir(part) for part in parts[:-1]):
                    continue
                if Path(rel).suffix.lower() in paths.BINARY_SUFFIXES | paths.IGNORED_SUFFIXES:
                    continue
                if config_for(rel) is None:
                    continue
                if member.size > MAX_GRAPH_FILE_BYTES:
                    stats["skipped_large"] += 1
                    continue
                handle = archive.extractfile(member)
                if handle is None:
                    continue
                data = handle.read()
                if paths.looks_binary(data):
                    continue
                total += len(data)
                if total > max_bytes:
                    raise RevisionTooLarge(f"revision sources exceed {max_bytes} bytes")
                out.append((rel, data))
                stats["files"] = len(out)
                stats["bytes"] = total
                if len(out) > max_files:
                    raise RevisionTooLarge(f"revision has more than {max_files} source files")
        try:
            proc.wait(timeout=30)
        except Exception:
            proc.kill()
        stderr_bytes = b""
        try:
            if proc.stderr is not None:
                stderr_bytes = proc.stderr.read(2000) or b""
        except Exception:
            stderr_bytes = b""
    finally:
        watchdog.cancel()
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
    if killed.is_set():
        raise RevisionError("reading the revision timed out")
    if proc.returncode not in (0, None) and not out:
        detail = stderr_bytes.decode("utf-8", errors="replace").strip()
        raise RevisionError(detail or "git archive failed")
    out.sort(key=lambda item: item[0])
    return out, stats


def list_tree(root: Path, rev: str, path: str = "", depth: int = 2) -> list[dict[str, Any]]:
    """The commit's file tree, shaped exactly like ``GET /projects/{id}/tree``."""
    sha = git_service.resolve_rev(root, rev)
    if sha is None:
        raise RevisionError("revision not found")
    paths_in_rev = _tracked_paths(root, sha)
    base = path.replace("\\", "/").strip("/")
    depth = max(0, min(int(depth), 4))
    if depth == 0:
        return []
    return _build_tree(paths_in_rev, base, depth)


def _build_tree(items: list[str], base: str, depth: int, level: int = 0) -> list[dict[str, Any]]:
    dirs: dict[str, list[str]] = {}
    files: list[str] = []
    prefix = f"{base}/" if base else ""
    for item in items:
        if prefix and not item.startswith(prefix):
            continue
        rest = item[len(prefix) :]
        if not rest:
            continue
        head, sep, _tail = rest.partition("/")
        if sep:
            dirs.setdefault(head, []).append(item)
        else:
            files.append(head)
    out: list[dict[str, Any]] = []
    for name in sorted(dirs, key=str.lower):
        children = None
        if level + 1 < depth:
            children = _build_tree(dirs[name], f"{base}/{name}" if base else name, depth, level + 1)
        out.append(
            {
                "name": name,
                "path": f"{base}/{name}" if base else name,
                "is_dir": True,
                "children": children,
            }
        )
    for name in sorted(files, key=str.lower):
        out.append(
            {
                "name": name,
                "path": f"{base}/{name}" if base else name,
                "is_dir": False,
                "children": None,
            }
        )
    return out


def read_file(root: Path, rev: str, rel_path: str) -> dict[str, Any]:
    """One file as it existed at ``rev``; ``exists=False`` when the path is absent.

    ``exists`` is deliberately separate from an HTTP error: "this revision does
    not have this file" is a normal answer while browsing history, and the caller
    still wants to show the path and the revision it asked about.
    """
    sha = git_service.resolve_rev(root, rev)
    if sha is None:
        raise RevisionError("revision not found")
    rel = rel_path.replace("\\", "/").lstrip("/")
    if ".." in rel.split("/"):
        raise RevisionError("path escapes the repository")
    cap = settings.max_file_bytes
    raw = git_service.show_bytes(root, sha, rel, max_bytes=cap + 1)
    if raw is None:
        return {
            "path": rel,
            "rev": sha,
            "sha": sha,
            "exists": False,
            "content": "",
            "size": 0,
            "mtime": None,
            "truncated": False,
            "binary": False,
            "language": "binary",
        }
    truncated = len(raw) > cap
    raw = raw[:cap]
    binary = Path(rel).suffix.lower() in paths.BINARY_SUFFIXES or paths.looks_binary(raw)
    text = "" if binary else raw.decode("utf-8", errors="replace")
    return {
        "path": rel,
        "rev": sha,
        "sha": sha,
        "exists": True,
        "content": text,
        "size": git_service.size_at(root, sha, rel) or len(raw),
        "mtime": None,
        "truncated": truncated,
        "binary": binary,
        "language": "binary" if binary else detect_language(rel, text),
    }
