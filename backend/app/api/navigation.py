"""Editor navigation endpoints: definition, references and hover.

These are intentionally separate from the graph endpoints. The graph answers
"what is this node connected to"; these answer "what does the identifier under
the cursor point at" and are queried on every F12/Ctrl+Click, so they read the
persisted symbol index and never re-walk the source tree.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query

from app.api.deps import ProjectDep, TokenDep
from app.core.config import settings
from app.graph import revision_store, revision_symbols, symbols
from app.services import paths

router = APIRouter(tags=["navigation"])

_IDENTIFIER = re.compile(r"[A-Za-z_$][\w$]*")

# Returned locations are capped so a hub function with thousands of call sites
# cannot make the response (and the picker) unusable.
MAX_REFERENCES = 500
MAX_CANDIDATES = 12


def _canonical(root: Path, path: str) -> tuple[str | None, Path]:
    target = paths.resolve_in_project(root, path)
    if not target.is_file():
        return None, target
    return target.relative_to(root).as_posix(), target


def _git_rel(root: Path, path: str) -> str | None:
    """A repository-relative path for a revision, without touching the file system."""
    rel = path.replace("\\", "/").lstrip("/")
    while rel.startswith("./"):
        rel = rel[2:]
    parts = rel.split("/")
    if not rel or any(part in ("", "..") for part in parts):
        return None
    return rel


def _word_in_text(text: str, line: int, column: int) -> str | None:
    lines = text.splitlines()
    if line < 1 or line > len(lines):
        return None
    text_line = lines[line - 1]
    offset = max(0, column - 1)
    for match in _IDENTIFIER.finditer(text_line):
        if match.start() <= offset <= match.end():
            return match.group()
    return None


def _word_at(target: Path, line: int, column: int) -> str | None:
    """The identifier under a 1-based editor cursor.

    Monaco sends the caret position, which can sit immediately after the last
    character. Treating that boundary as "inside the word" is what makes F12 on
    ``helper|()`` work without moving the caret first.
    """
    try:
        raw = target.open("rb").read(settings.max_file_bytes)
    except OSError:
        return None
    return _word_in_text(raw.decode("utf-8", errors="replace"), line, column)


def _payload(row: dict[str, Any], confidence: str) -> dict[str, Any]:
    return {
        "path": row["path"],
        "name": row["name"],
        "kind": row["kind"],
        "node_id": row["node_id"],
        "line": int(row["start_line"]),
        "column": int(row.get("start_col") or 1),
        "end_line": int(row.get("end_line") or row["start_line"]),
        "end_column": int(row.get("end_col") or 1),
        "signature": row.get("signature") or "",
        "doc": row.get("doc") or "",
        "confidence": confidence,
        "language": row.get("language") or "",
    }


def _rank(item: dict[str, Any]) -> tuple[Any, ...]:
    return (
        item["kind"] == "file",
        item["path"].endswith("/__init__.py") or item["path"] == "__init__.py",
        item["path"],
        item["line"],
    )


def _prefer(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse package-import duplicates in favour of the real declaration.

    ``from app import service`` produces both ``app/__init__.py`` and
    ``app/service.py``. Offering both in the picker is noise; a function/class
    target always wins, then a non-package file.
    """
    unique: dict[str, dict[str, Any]] = {}
    for item in targets:
        unique.setdefault(item["node_id"], item)
    items = list(unique.values())
    if len(items) > 1:
        declarations = [item for item in items if item["kind"] != "file"]
        if declarations:
            items = declarations
        else:
            concrete = [
                item
                for item in items
                if not item["path"].endswith("/__init__.py") and item["path"] != "__init__.py"
            ]
            if concrete:
                items = concrete
    return sorted(items, key=_rank)


class _SymbolsAtRevision:
    """Binds ``rev`` once so the shared resolver can call either index alike."""

    def __init__(self, rev: str) -> None:
        self.rev = rev

    def refs_at(self, project_id: int, path: str, line: int, *, word: str | None = None, column: int | None = None):
        return revision_symbols.refs_at(project_id, self.rev, path, line, word=word, column=column)

    def definition_by_node(self, project_id: int, node_id: str):
        return revision_symbols.definition_by_node(project_id, self.rev, node_id)

    def definitions_at(self, project_id: int, path: str, line: int):
        return revision_symbols.definitions_at(project_id, self.rev, path, line)

    def definitions_by_name(self, project_id: int, name: str):
        return revision_symbols.definitions_by_name(project_id, self.rev, name)


def _resolve(
    project_id: int,
    root: Path,
    path: str,
    line: int,
    column: int,
    rev: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None, str | None]:
    """Resolve one cursor position to targets or candidates.

    Returns ``(targets, candidates, reason, word)``. A single target may be
    navigated to immediately; several targets/candidates go to the picker; an
    empty result carries a reason the editor can show in plain language.
    """
    sym: Any = _SymbolsAtRevision(rev) if rev else symbols
    if rev:
        # The path must exist *in that commit*; asking the work tree would answer
        # for a different revision.
        rel = _git_rel(root, path)
        if rel is None:
            return [], [], "file_not_found", None
        source = revision_store.source_content(root, rev, rel)
        target_file: Path | None = None
        if source is None:
            return [], [], "file_not_found", None
    else:
        rel, target_file = _canonical(root, path)
        if rel is None:
            return [], [], "file_not_found", None

    def word_under_cursor() -> str | None:
        if rev:
            return _word_in_text(source or "", line, column)
        return _word_at(target_file, line, column) if target_file is not None else None

    refs = sym.refs_at(project_id, rel, line, column=column)
    word: str | None = None
    if len(refs) != 1:
        word = word_under_cursor()
        if word:
            narrowed = sym.refs_at(project_id, rel, line, word=word, column=column)
            if narrowed:
                refs = narrowed
    if not refs:
        if word is None:
            word = word_under_cursor()
        if word:
            # The caret may sit just outside the recorded span (end of a word);
            # an exact name match on the same line is still unambiguous evidence.
            refs = sym.refs_at(project_id, rel, line, word=word)

    if refs:
        best = refs[0]
        if word is None:
            # A single recorded occurrence did not need the word to disambiguate,
            # but the UI still wants the identifier name for its feedback text.
            word = best["name"]
        same_position = [
            item
            for item in refs
            if item["name"] == best["name"]
            and int(item["start_line"]) == int(best["start_line"])
            and item.get("start_col") == best.get("start_col")
        ]
        targets: list[dict[str, Any]] = []
        external = False
        unresolved = False
        for item in same_position:
            target_id = item.get("target_node_id")
            if not target_id:
                unresolved = True
                continue
            if str(target_id).startswith("<external>"):
                external = True
                continue
            row = sym.definition_by_node(project_id, str(target_id))
            if row is not None:
                targets.append(_payload(row, str(item.get("confidence") or "EXTRACTED")))
        targets = _prefer(targets)
        if targets:
            return targets[:MAX_CANDIDATES], [], None, word
        # A ref that resolved to an external module is a fact; do not replace it
        # with same-named project symbols (``from os import path`` must not offer
        # the project's own ``path`` function).
        if external and not unresolved:
            return [], [], "external", word
        candidates = sym.definitions_by_name(project_id, best["name"])
        if candidates:
            return (
                [],
                [_payload(row, "AMBIGUOUS") for row in candidates[:MAX_CANDIDATES]],
                "ambiguous",
                word,
            )
        return [], [], "external" if external else ("unresolved" if unresolved else "no_definition"), word

    # The cursor may be on a definition itself. F12 there is a no-op jump, which
    # is still the friendly answer: it confirms where the symbol lives.
    definitions = [_payload(row, "EXACT") for row in sym.definitions_at(project_id, rel, line)]
    if definitions:
        return _prefer(definitions)[:MAX_CANDIDATES], [], None, word

    if word is None:
        word = word_under_cursor()
    if word:
        candidates = sym.definitions_by_name(project_id, word)
        if len(candidates) == 1:
            return [_payload(candidates[0], "INFERRED")], [], None, word
        if candidates:
            return (
                [],
                [_payload(row, "INFERRED") for row in candidates[:MAX_CANDIDATES]],
                "ambiguous",
                word,
            )
    return [], [], "no_definition", word


def _ensure(project: dict, rev: str | None = None) -> tuple[Path, str | None]:
    """Resolve the root and, in revision scope, materialise that commit's index."""
    root = paths.resolve_root(project["path"])
    if not rev:
        symbols.ensure_index(project["id"], root)
        return root, None
    payload, _ = revision_store.get_or_build(project["id"], root, rev)
    return root, str(payload.get("rev") or rev)


@router.get("/projects/{project_id}/definition")
def get_definition(
    project: ProjectDep,
    _: TokenDep,
    path: str = Query(min_length=1),
    line: int = Query(ge=1),
    column: int = Query(ge=1),
    rev: str = Query(default=""),
) -> dict[str, Any]:
    """The definition an editor cursor points at, plus same-name candidates."""
    root, sha = _ensure(project, rev or None)
    targets, candidates, reason, word = _resolve(project["id"], root, path, line, column, sha)
    return {
        "targets": targets,
        "candidates": candidates,
        "reason": reason,
        "word": word,
    }


@router.get("/projects/{project_id}/references")
def get_references(
    project: ProjectDep,
    _: TokenDep,
    path: str = Query(min_length=1),
    line: int = Query(ge=1),
    column: int = Query(ge=1),
    rev: str = Query(default=""),
) -> dict[str, Any]:
    """All recorded occurrences of the symbol under the cursor."""
    root, sha = _ensure(project, rev or None)
    targets, candidates, reason, word = _resolve(project["id"], root, path, line, column, sha)
    if len(targets) != 1:
        return {
            "target": None,
            "locations": [],
            "truncated": False,
            "candidates": candidates,
            "reason": reason or ("ambiguous" if candidates else "no_definition"),
            "word": word,
        }
    target = targets[0]
    rows, truncated = (
        revision_symbols.refs_to(project["id"], sha, target["node_id"], limit=MAX_REFERENCES)
        if sha
        else symbols.refs_to(project["id"], target["node_id"], limit=MAX_REFERENCES)
    )
    locations = [
        {
            "path": row["path"],
            "name": row["name"],
            "kind": row["kind"],
            "line": int(row["start_line"]),
            "column": int(row["start_col"] or 1),
            "end_line": int(row.get("end_line") or row["start_line"]),
            "end_column": int(row["end_col"] or 1),
            "from_name": row.get("from_name"),
            "from_kind": row.get("from_kind"),
            "confidence": row.get("confidence"),
        }
        for row in rows
    ]
    return {
        "target": target,
        "locations": locations,
        "truncated": truncated,
        "candidates": [],
        "reason": None,
        "word": word,
    }


@router.get("/projects/{project_id}/hover")
def get_hover(
    project: ProjectDep,
    _: TokenDep,
    path: str = Query(min_length=1),
    line: int = Query(ge=1),
    column: int = Query(ge=1),
    rev: str = Query(default=""),
) -> dict[str, Any]:
    """Markdown for the editor hover card: signature, docstring and location."""
    root, sha = _ensure(project, rev or None)
    targets, candidates, reason, word = _resolve(project["id"], root, path, line, column, sha)
    if not targets:
        if candidates:
            lines = [
                f"**多个同名定义** `{word or candidates[0]['name']}`",
                "",
                "按 **F12** 选择一个：",
                *[
                    f"- `{item['path']}:{item['line']}` · {item['kind']}"
                    for item in candidates[:6]
                ],
            ]
            return {
                "contents": "\n".join(lines),
                "reason": "ambiguous",
                "word": word,
            }
        if reason == "external":
            # VS Code can show an external library's signature because it has a
            # language server. We do not index dependencies, but an explicit
            # "this is external" card is much friendlier than no hover at all.
            return {
                "contents": (
                    f"**外部符号** `{word or 'symbol'}`\n\n"
                    "这个符号来自外部库，项目内没有可跳转的源码。"
                ),
                "reason": "external",
                "word": word,
            }
        return {"contents": None, "reason": reason, "word": word}
    target = targets[0]
    parts = [f"**{target['kind']}** `{target['name']}`"]
    if target.get("signature"):
        language = target.get("language") or ""
        fence = f"```{language}" if language else "```"
        parts.append(f"{fence}\n{target['signature']}\n```")
    if target.get("doc"):
        parts.append(target["doc"])
    parts.append(f"`{target['path']}:{target['line']}`")
    return {
        "contents": "\n\n".join(parts),
        "range": {
            "start_line": target["line"],
            "start_column": target["column"],
            "end_line": target["end_line"],
            "end_column": target["end_column"],
        },
        "kind": target["kind"],
        "path": target["path"],
        "line": target["line"],
        "word": word,
    }


@router.get("/projects/{project_id}/symbols")
def search_symbols(
    project: ProjectDep,
    _: TokenDep,
    q: str = Query(min_length=1),
    limit: int = Query(default=50, ge=1, le=200),
    rev: str = Query(default=""),
) -> dict[str, Any]:
    """Project symbol search, used by the future Ctrl+T palette."""
    root, sha = _ensure(project, rev or None)
    rows = (
        revision_symbols.search_definitions(project["id"], sha, q, limit=limit)
        if sha
        else symbols.search_definitions(project["id"], q, limit=limit)
    )
    del root
    return {"symbols": [_payload(row, "EXTRACTED") for row in rows]}
