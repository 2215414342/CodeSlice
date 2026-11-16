"""Read-only Git integration.

Two rules shape this module:

* **git is the only source of truth.** No library, no cached repo state, no
  long-lived process. One call, one child process, and the answer git gave.
* **a git failure degrades, it never explodes.** A missing binary, a broken
  repository, a shallow clone that lacks an object: callers get ``None``/an
  empty result plus a reason, not an exception that takes the UI down.

Everything here is read-only. ``status``/``diff``/``show``/``log``/``rev-parse``
never write to the repository; ``GIT_OPTIONAL_LOCKS=0`` keeps even the optional
index refresh out of the way, which matters because these calls run while the
user is reading.

Output is bounded. A 200 MB blob or a pathological ``log`` is refused with an
explicit error instead of being accumulated into memory, because "the string
got too long" is a process-killing failure mode, not a rendering glitch.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

GIT_TIMEOUT_SECONDS = 20
GIT_OUTPUT_MAX_BYTES = 8 * 1024 * 1024

# Commands that accept a caller-supplied rev get this guard: without it a rev of
# ``--output=NUL`` is parsed as an option and the command silently writes a file
# instead of failing. The sentinel ends option parsing for every legitimate rev
# form (``HEAD``, ``sha``, ``sha~2``, ``origin/main``).
_END_OF_OPTIONS = "--end-of-options"

# A rev may be a branch with a slash, a tag, a short sha, ``HEAD~3``. It may not
# start with ``-`` (an option), and it may not contain whitespace or control
# characters. ``rev-parse --verify`` is the real check; this is the cheap gate.
_REV_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/~^@{}-]{0,254}$")

_DIFF_MAX_BYTES = 200_000

# Fixed environment. ``LC_ALL=C`` keeps error text parseable and stable;
# ``GIT_TERMINAL_PROMPT=0`` makes a remote-less command fail instead of asking
# for credentials on a pipe that nobody is reading.
_GIT_ENV = {
    "LC_ALL": "C",
    "LANG": "C",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_PAGER": "cat",
}

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class GitError(RuntimeError):
    """A git invocation failed. ``detail`` is git's own stderr, trimmed."""

    def __init__(self, message: str, *, command: str = "", detail: str = "") -> None:
        super().__init__(message)
        self.command = command
        self.detail = detail


class GitUnavailable(GitError):
    """The ``git`` binary is not installed or not on PATH."""


class GitOutputLimit(GitError):
    """A read produced more bytes than the caller allowed.

    Kept apart from a generic failure because callers must not fold "the answer
    is too big" into "this revision has no such file": one is a missing path, the
    other is a budget we refuse to blow.
    """


def _git_binary() -> str:
    binary = shutil.which("git")
    if not binary:
        raise GitUnavailable("git is not installed or not on PATH")
    return binary


def _env() -> dict[str, str]:
    return {**os.environ, **_GIT_ENV}


def _argv(root: Path, args: list[str]) -> list[str]:
    """The one argv shape every call uses (read-only flags, literal paths)."""
    return [
        _git_binary(),
        "-C",
        str(root),
        "--no-pager",
        "-c",
        "color.ui=false",
        # Emit UTF-8 paths verbatim instead of C-quoting CJK names as octal
        # escapes; every surface that renders a path would show the escaped form.
        "-c",
        "core.quotePath=false",
        *args,
    ]


def _run_raw(
    root: Path,
    args: list[str],
    *,
    timeout: float = GIT_TIMEOUT_SECONDS,
    max_bytes: int = GIT_OUTPUT_MAX_BYTES,
    check: bool = True,
) -> bytes:
    """Run one read-only git command in ``root`` and return raw stdout.

    Bytes, not text: ``show`` reads source files, and decoding before the caller
    decides whether the file is binary would turn a NUL byte into a replacement
    character and hide the fact that it was binary at all.
    """
    argv = _argv(root, args)
    deadline = time.monotonic() + timeout
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_env(),
            creationflags=_CREATE_NO_WINDOW,
        )
    except OSError as exc:  # pragma: no cover - only when the binary vanished
        raise GitUnavailable(f"cannot run git: {exc}") from exc

    # A watchdog, because ``stdout.read`` blocks: a git process that hangs
    # without writing anything would otherwise never reach the deadline check
    # between chunks.
    timed_out = threading.Event()

    def _kill_on_timeout() -> None:
        timed_out.set()
        try:
            proc.kill()
        except OSError:
            pass

    watchdog = threading.Timer(timeout, _kill_on_timeout)
    watchdog.daemon = True
    watchdog.start()

    out = bytearray()
    try:
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read(65536)
            if not chunk:
                break
            out.extend(chunk)
            if len(out) > max_bytes:
                proc.kill()
                raise GitOutputLimit(
                    f"git {args[0] if args else ''} output exceeded {max_bytes} bytes",
                    command=" ".join(args),
                )
            if timed_out.is_set():
                break
        try:
            proc.wait(timeout=max(1.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            timed_out.set()
        if timed_out.is_set():
            raise GitError(f"git {args[0] if args else ''} timed out after {timeout:g}s")
    finally:
        watchdog.cancel()
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)

    stderr = b""
    try:
        if proc.stderr is not None:
            # stderr shares the byte budget: a command whose diagnostics stream
            # past the cap can kill the host just as easily as its answer.
            stderr = proc.stderr.read(GIT_OUTPUT_MAX_BYTES) or b""
    except OSError:
        stderr = b""
    finally:
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass

    if proc.returncode != 0:
        if not check:
            return b""
        detail = stderr.decode("utf-8", errors="replace").strip()
        raise GitError(
            detail or f"git exited with {proc.returncode}",
            command=" ".join(args),
            detail=detail,
        )
    return bytes(out)


def _run(
    root: Path,
    args: list[str],
    *,
    timeout: float = GIT_TIMEOUT_SECONDS,
    max_bytes: int = GIT_OUTPUT_MAX_BYTES,
    check: bool = True,
) -> str:
    """Text variant of :func:`_run_raw`.

    ``check=False`` returns ``""`` on a non-zero exit. ``check=True`` raises
    :class:`GitError` carrying git's stderr: "path does not exist in that
    revision" and "that revision is not a commit" are different answers and must
    not both look like an empty file.
    """
    return _run_raw(root, args, timeout=timeout, max_bytes=max_bytes, check=check).decode(
        "utf-8", errors="replace"
    )


def open_stream(root: Path, args: list[str]) -> subprocess.Popen:
    """Start a git command whose stdout is consumed incrementally.

    Only ``git archive`` uses this: a tar of a whole revision can be far larger
    than what the caller keeps, so it must never be read into one buffer first.
    The caller owns the process, must drain stdout, and must close it.
    """
    try:
        return subprocess.Popen(
            _argv(root, args),
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_env(),
            creationflags=_CREATE_NO_WINDOW,
        )
    except OSError as exc:  # pragma: no cover - only when the binary vanished
        raise GitUnavailable(f"cannot run git: {exc}") from exc


# --------------------------------------------------------------------------- #
# repository facts
# --------------------------------------------------------------------------- #
def is_repo(root: Path) -> bool:
    """True when ``root`` is inside a work tree git can read."""
    if not root.is_dir():
        return False
    try:
        return bool(_run(root, ["rev-parse", "--is-inside-work-tree"], check=False).strip())
    except GitUnavailable:
        return False


def head_sha(root: Path) -> str | None:
    """The commit HEAD points at, or ``None`` for a repo without a first commit."""
    if not is_repo(root):
        return None
    try:
        out = _run(root, ["rev-parse", "--verify", "--end-of-options", "HEAD^{commit}"], check=False)
    except GitError:
        return None
    return out.strip() or None


def _porcelain_paths(root: Path, args: list[str]) -> str:
    return _run(root, ["status", "--porcelain=v1", "-z", *args], check=False)


def current_state(root: Path, *, max_paths: int = 5_000) -> dict[str, Any]:
    """Branch, HEAD and a three-bucket status, bounded by ``max_paths``."""
    if not is_repo(root):
        return {
            "is_repo": False,
            "branch": None,
            "head": None,
            "head_short": None,
            "detached": False,
            "dirty": False,
            "staged": [],
            "modified": [],
            "untracked": [],
            "truncated": False,
        }
    head = head_sha(root)
    branch = None
    detached = False
    if head:
        name = _run(root, ["rev-parse", "--abbrev-ref", "HEAD"], check=False).strip()
        if name == "HEAD":
            detached = True
        else:
            branch = name or None
    raw = _porcelain_paths(root, [])
    staged, modified, untracked = [], [], []
    truncated = False
    # ``-z`` frames each entry as ``XY<space>path\0``. A rename/copy (``R``/``C``)
    # is followed by a second NUL-framed original path that belongs to the same
    # entry — consuming it keeps the next loop from reading "ol..." as a status
    # code for a file that does not exist.
    entries = raw.split("\0")
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if len(entry) < 4:
            continue
        code, path = entry[:2], entry[3:]
        if code[0] in ("R", "C") and index < len(entries):
            index += 1
        if len(staged) + len(modified) + len(untracked) >= max_paths:
            truncated = True
            break
        if code == "??":
            untracked.append(path)
            continue
        if code[0] not in (" ", "?"):
            staged.append(path)
        if code[1] not in (" ", "?"):
            modified.append(path)
    return {
        "is_repo": True,
        "branch": branch,
        "head": head,
        "head_short": head[:7] if head else None,
        "detached": detached,
        "dirty": bool(staged or modified or untracked),
        "staged": staged,
        "modified": modified,
        "untracked": untracked,
        "truncated": truncated,
    }


def worktree_dirty(root: Path, rel_path: str) -> bool:
    """Whether ``rel_path`` differs from HEAD (including untracked/deleted).

    An anchor created while this is true cannot honestly claim "I saw HEAD's
    version": the user saw the work tree, which no commit contains yet. That is
    recorded on the anchor so the UI can say so instead of guessing.
    """
    rel = _safe_rel(rel_path)
    if not is_repo(root) or not head_sha(root) or rel is None:
        return False
    try:
        raw = _run(root, ["status", "--porcelain=v1", "-z", "--", rel], check=False)
    except GitError:
        return False
    return bool(raw.strip("\0").strip())


def diff(root: Path, rel_path: str | None = None, max_bytes: int = _DIFF_MAX_BYTES) -> str:
    """The working-tree patch against HEAD, capped. ``""`` when not a repo."""
    if not is_repo(root):
        return ""
    rel = _safe_rel(rel_path) if rel_path else None
    if rel_path and rel is None:
        return ""
    args = ["diff", "--no-ext-diff", "--no-color", "--stat", "--patch", "HEAD"]
    if rel:
        args += ["--", rel]
    try:
        out = _run(root, args, check=False, max_bytes=max_bytes)
    except GitError:
        return ""
    if out:
        return out
    # No commits yet: HEAD does not resolve, so compare against the index.
    args = ["diff", "--no-ext-diff", "--no-color", "--stat", "--patch"]
    if rel:
        args += ["--", rel]
    try:
        return _run(root, args, check=False, max_bytes=max_bytes)
    except GitError:
        return ""


# --------------------------------------------------------------------------- #
# revisions
# --------------------------------------------------------------------------- #
def _safe_rel(rel_path: str | None) -> str | None:
    """A repo-relative POSIX path, or ``None`` when it could escape the root."""
    if not rel_path:
        return None
    rel = rel_path.replace("\\", "/").strip()
    while rel.startswith("./"):
        rel = rel[2:]
    if not rel or rel.startswith("/") or ":" in rel.split("/")[0]:
        return None
    parts = rel.split("/")
    if any(part in ("..", "") for part in parts):
        return None
    return rel


def resolve_rev(root: Path, rev: str) -> str | None:
    """Resolve any ref/rev to its full commit sha.

    Returns ``None`` for anything that is not a commit git can verify: a branch
    that does not exist, a short sha that is ambiguous, an annotated tag whose
    peeled commit is missing, or input shaped like an option.
    """
    if not rev or not _REV_RE.match(rev):
        return None
    try:
        out = _run(
            root,
            ["rev-parse", "--verify", "--quiet", _END_OF_OPTIONS, f"{rev}^{{commit}}"],
            check=False,
        )
    except GitError:
        return None
    sha = out.strip()
    return sha if len(sha) == 40 and all(c in "0123456789abcdef" for c in sha) else None


def resolve_ref(root: Path, ref: str) -> dict[str, Any] | None:
    """``branch/tag/commit`` → ``{sha, sha_short, kind, name}`` for the switcher."""
    sha = resolve_rev(root, ref)
    if sha is None:
        return None
    kind = "commit"
    name = ref
    try:
        if _run(root, ["show-ref", "--verify", "--quiet", f"refs/heads/{ref}"], check=False):
            kind = "branch"
        elif _run(root, ["show-ref", "--verify", "--quiet", f"refs/tags/{ref}"], check=False):
            kind = "tag"
        elif ref.startswith("refs/heads/"):
            kind, name = "branch", ref.removeprefix("refs/heads/")
        elif ref.startswith("refs/remotes/"):
            kind, name = "branch", ref.removeprefix("refs/remotes/")
        elif ref == "HEAD":
            kind, name = "commit", "HEAD"
    except GitError:
        pass
    return {"sha": sha, "sha_short": sha[:7], "kind": kind, "name": name}


def refs(root: Path) -> dict[str, Any]:
    """Local branches, remote branches, tags, plus the current branch."""
    empty: dict[str, Any] = {"current": None, "detached": False, "branches": [], "tags": []}
    if not is_repo(root):
        return empty
    head = head_sha(root)
    if not head:
        return empty
    try:
        raw = _run(
            root,
            [
                "for-each-ref",
                "--format=%(refname)%09%(objectname)%09%(HEAD)",
                "refs/heads",
                "refs/remotes",
                "refs/tags",
            ],
            check=False,
        )
    except GitError:
        return empty
    branches: list[dict[str, Any]] = []
    tags: list[dict[str, Any]] = []
    current: str | None = None
    for line in raw.splitlines():
        fields = line.split("\t")
        if len(fields) < 2:
            continue
        name, sha = fields[0], fields[1]
        marker = fields[2].strip() if len(fields) > 2 else ""
        item = {
            "name": name,
            "sha": sha,
            "sha_short": sha[:7],
            "current": marker == "*",
        }
        if name.startswith("refs/heads/"):
            item["name"] = name.removeprefix("refs/heads/")
            branches.append(item)
            if item["current"]:
                current = item["name"]
        elif name.startswith("refs/remotes/"):
            short = name.removeprefix("refs/remotes/")
            if short.endswith("/HEAD"):
                continue
            item["name"] = short
            branches.append(item)
        elif name.startswith("refs/tags/"):
            item["name"] = name.removeprefix("refs/tags/")
            tags.append(item)
    branches.sort(key=lambda b: (not b["current"], b["name"].count("/"), b["name"]))
    return {"current": current, "detached": current is None, "branches": branches, "tags": tags}


def log(
    root: Path,
    *,
    limit: int = 30,
    skip: int = 0,
    rel_path: str | None = None,
    rev: str = "HEAD",
    max_commits: int = 100,
    max_skip: int = 5_000,
    topo: bool = False,
) -> dict[str, Any]:
    """Commits touched by ``rel_path`` (or the whole history).

    ``--follow`` is deliberately absent: rename tracking is a separate feature
    and a half-tracked history is worse than an honest one. The caller gets
    ``truncated`` when a request asked for more rows than the cap.
    """
    limit = max(1, min(int(limit), max_commits))
    skip = max(0, min(int(skip), max_skip))
    if not is_repo(root):
        return {"commits": [], "truncated": False}
    sha = resolve_rev(root, rev)
    if sha is None:
        return {"commits": [], "truncated": False}
    rel = _safe_rel(rel_path) if rel_path else None
    if rel_path and rel is None:
        return {"commits": [], "truncated": False}
    args = [
        "log",
        f"-n{limit + 1}",
        f"--skip={skip}",
        "--no-color",
        "--no-decorate",
        "--format=%H%x1f%s%x1f%an%x1f%aI%x1f%P",
        _END_OF_OPTIONS,
        sha,
    ]
    if topo:
        args.insert(1, "--topo-order")
    if rel:
        args += ["--", rel]
    try:
        raw = _run(root, args, check=False)
    except GitError:
        return {"commits": [], "truncated": False}
    commits: list[dict[str, Any]] = []
    for line in raw.splitlines():
        fields = line.split("\x1f")
        if len(fields) < 4:
            continue
        commits.append(
            {
                "sha": fields[0],
                "sha_short": fields[0][:7],
                "subject": fields[1],
                "author": fields[2],
                "author_time": fields[3],
                "parents": fields[4].split() if len(fields) > 4 and fields[4] else [],
            }
        )
    return {"commits": commits[:limit], "truncated": len(commits) > limit}


def show(root: Path, rev: str, rel_path: str, *, max_bytes: int = GIT_OUTPUT_MAX_BYTES) -> str | None:
    """A file's content at ``rev`` (``git show <sha>:<path>``).

    ``None`` means "this revision has no such file" — a file added later has no
    parent side, a deleted file has no HEAD side, and both are legitimate
    answers rather than errors. The path is resolved to a commit first so a
    caller-supplied rev can never ride in as an option.
    """
    sha = resolve_rev(root, rev)
    rel = _safe_rel(rel_path)
    if sha is None or rel is None:
        return None
    try:
        # ``check=True`` on purpose: a missing path exits non-zero, an empty file
        # exits 0 with empty stdout. Both are answers, and they differ.
        return _run(
            root,
            ["show", "--no-ext-diff", "--no-textconv", "--no-color", _END_OF_OPTIONS, f"{sha}:{rel}"],
            max_bytes=max_bytes,
        )
    except GitOutputLimit:
        # A budget refusal is not "no such file"; it must reach the caller.
        raise
    except GitError:
        return None


def show_bytes(root: Path, rev: str, rel_path: str, *, max_bytes: int = GIT_OUTPUT_MAX_BYTES) -> bytes | None:
    """Binary-safe :func:`show`: ``None`` when the path is absent at ``rev``."""
    sha = resolve_rev(root, rev)
    rel = _safe_rel(rel_path)
    if sha is None or rel is None:
        return None
    try:
        return _run_raw(
            root,
            ["show", "--no-ext-diff", "--no-textconv", "--no-color", _END_OF_OPTIONS, f"{sha}:{rel}"],
            max_bytes=max_bytes,
        )
    except GitOutputLimit:
        raise
    except GitError:
        return None


def ls_tree_names(root: Path, rev: str) -> list[str]:
    """Every file path in a commit, NUL-framed by git so CJK names stay literal."""
    sha = resolve_rev(root, rev)
    if sha is None:
        raise GitError("revision not found")
    raw = _run(root, ["ls-tree", "-r", "-z", "--name-only", _END_OF_OPTIONS, sha])
    return [item for item in raw.split("\0") if item]


def size_at(root: Path, rev: str, rel_path: str) -> int | None:
    """Byte size of a blob at ``rev`` without reading it."""
    sha = resolve_rev(root, rev)
    rel = _safe_rel(rel_path)
    if sha is None or rel is None:
        return None
    try:
        out = _run(root, ["cat-file", "-s", _END_OF_OPTIONS, f"{sha}:{rel}"], check=False)
    except GitError:
        return None
    out = out.strip()
    return int(out) if out.isdigit() else None


def is_ancestor(root: Path, older: str, newer: str) -> bool:
    """True when ``older`` is reachable from ``newer`` (git merge-base --is-ancestor)."""
    old_sha = resolve_rev(root, older)
    new_sha = resolve_rev(root, newer)
    if old_sha is None or new_sha is None:
        return False
    try:
        code = subprocess.run(
            [
                _git_binary(),
                "-C",
                str(root),
                "merge-base",
                "--is-ancestor",
                old_sha,
                new_sha,
            ],
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=GIT_TIMEOUT_SECONDS,
            env=_env(),
            creationflags=_CREATE_NO_WINDOW,
        ).returncode
    except (OSError, subprocess.TimeoutExpired):
        return False
    return code == 0


def file_at_head(root: Path, rel_path: str) -> str | None:
    """Compatibility shim for callers that still ask for "the HEAD version"."""
    return show(root, "HEAD", rel_path)
