"""Symbol navigation queries for one revision.

Mirrors ``app.graph.symbols`` one-for-one, with ``(project_id, rev)`` as the
scope instead of ``project_id``. The tables are written by
``revision_store.get_or_build`` in the same transaction as the revision graph,
so a symbol index can never exist without the graph that produced it.
"""

from __future__ import annotations

from typing import Any

from app.core.db import get_conn
from app.graph.symbols import normalize


def has_index(project_id: int, rev: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM revision_symbol_defs WHERE project_id = ? AND rev = ? LIMIT 1",
            (project_id, rev),
        ).fetchone()
    return row is not None


def definitions_at(project_id: int, rev: str, path: str, line: int) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM revision_symbol_defs
               WHERE project_id = ? AND rev = ? AND path = ? AND start_line = ?
               ORDER BY (end_line - start_line), (end_col - start_col), start_col""",
            (project_id, rev, path, line),
        ).fetchall()
    return [dict(row) for row in rows]


def definition_by_node(project_id: int, rev: str, node_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM revision_symbol_defs WHERE project_id = ? AND rev = ? AND node_id = ?",
            (project_id, rev, node_id),
        ).fetchone()
    return dict(row) if row is not None else None


def refs_at(
    project_id: int,
    rev: str,
    path: str,
    line: int,
    *,
    word: str | None = None,
    column: int | None = None,
) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM revision_symbol_refs
               WHERE project_id = ? AND rev = ? AND path = ? AND start_line = ?
               ORDER BY (COALESCE(end_col, start_col) - COALESCE(start_col, 0)), id""",
            (project_id, rev, path, line),
        ).fetchall()
    items = [dict(row) for row in rows]
    if word:
        items = [item for item in items if item["name"] == word]
    if column is not None:
        contained = [
            item
            for item in items
            if item.get("start_col") is not None
            and int(item["start_col"]) <= column <= int(item.get("end_col") or item["start_col"])
        ]
        items = contained if contained else []
    return items


def refs_to(project_id: int, rev: str, node_id: str, limit: int = 500) -> tuple[list[dict[str, Any]], bool]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT r.*, d.name AS from_name, d.kind AS from_kind
               FROM revision_symbol_refs r
               LEFT JOIN revision_symbol_defs d
                 ON d.project_id = r.project_id AND d.rev = r.rev AND d.node_id = r.from_node_id
               WHERE r.project_id = ? AND r.rev = ? AND r.target_node_id = ?
               ORDER BY r.path, r.start_line, r.start_col
               LIMIT ?""",
            (project_id, rev, node_id, limit + 1),
        ).fetchall()
    items = [dict(row) for row in rows]
    truncated = len(items) > limit
    return items[:limit], truncated


def search_definitions(project_id: int, rev: str, query: str, limit: int = 50) -> list[dict[str, Any]]:
    normalized = normalize(query)
    if not normalized:
        return []
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM revision_symbol_defs
               WHERE project_id = ? AND rev = ? AND kind IN ('function', 'class')
                 AND normalized LIKE ?
               ORDER BY
                 CASE WHEN normalized = ? THEN 0 WHEN normalized LIKE ? THEN 1 ELSE 2 END,
                 length(name), name
               LIMIT ?""",
            (project_id, rev, f"%{normalized}%", normalized, f"{normalized}%", limit),
        ).fetchall()
    return [dict(row) for row in rows]


def definitions_by_name(
    project_id: int,
    rev: str,
    name: str,
    *,
    kinds: tuple[str, ...] = ("function", "class"),
    limit: int = 20,
) -> list[dict[str, Any]]:
    normalized = normalize(name)
    if not normalized:
        return []
    placeholders = ",".join("?" for _ in kinds)
    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT * FROM revision_symbol_defs
                WHERE project_id = ? AND rev = ? AND name = ? AND kind IN ({placeholders})
                ORDER BY path, start_line
                LIMIT ?""",
            (project_id, rev, name, *kinds, limit),
        ).fetchall()
        if not rows:
            rows = conn.execute(
                f"""SELECT * FROM revision_symbol_defs
                    WHERE project_id = ? AND rev = ? AND normalized = ? AND kind IN ({placeholders})
                    ORDER BY path, start_line
                    LIMIT ?""",
                (project_id, rev, normalized, *kinds, limit),
            ).fetchall()
    return [dict(row) for row in rows]
