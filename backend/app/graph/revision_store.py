"""The knowledge graph of one git revision, built from git blobs.

Why a second table family instead of reusing ``graphs``/``symbol_defs``: a
revision graph is *immutable*. It is not invalidated by an index run, it cannot
go stale, and it must never be confused with the work-tree graph. Keeping it in
its own scope (``code@<sha>``) and its own symbol tables makes both guarantees
structural instead of a convention someone has to remember.

Eviction is therefore the only lifecycle: each project keeps the most recently
built ``REVISION_GRAPH_LIMIT`` revisions. Deleting one removes a cache entry, not
a fact — the commit is still in git.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from app.core.db import get_conn, utcnow, write_conn
from app.graph import SCHEMA_VERSION, builder
from app.graph.cluster import apply_communities, detect_communities
from app.services import git_service, revisions

log = logging.getLogger(__name__)

SCOPE_PREFIX = "code@"
REVISION_GRAPH_LIMIT = 8


def scope_for(sha: str) -> str:
    return f"{SCOPE_PREFIX}{sha}"


def is_revision_scope(scope: str) -> bool:
    return scope.startswith(SCOPE_PREFIX)


def load(project_id: int, sha: str) -> dict[str, Any] | None:
    """The persisted payload for this commit, or ``None`` (missing / old schema)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT payload, schema_version FROM graphs WHERE project_id = ? AND scope = ?",
            (project_id, scope_for(sha)),
        ).fetchone()
    if row is None or int(row["schema_version"]) != SCHEMA_VERSION:
        return None
    try:
        payload = json.loads(row["payload"])
    except (TypeError, ValueError):
        log.warning("discarding unreadable revision graph %s for project %s", sha, project_id)
        return None
    return payload if isinstance(payload, dict) else None


def get_or_build(project_id: int, root: Path, ref: str, *, force: bool = False) -> tuple[dict[str, Any], bool]:
    """Return ``(payload, rebuilt)`` for a commit, building it once.

    ``RevisionError``/``RevisionTooLarge`` propagate to the API, which turns them
    into 404/400: a revision that cannot be read must not be answered with an
    empty graph.
    """
    resolved = revisions.resolve(root, ref)
    if resolved is None:
        raise revisions.RevisionError("revision not found")
    sha = str(resolved["sha"])
    if not force:
        cached = load(project_id, sha)
        if cached is not None:
            return cached, False
    sources, source_stats = revisions.read_sources(root, sha)
    graph = builder.build_graph(sources=sources, root=root)
    index = builder.build_symbol_index(sources=sources, root=root)
    assignment, summaries = detect_communities(graph["nodes"], graph["edges"])
    nodes = apply_communities(graph["nodes"], assignment, summaries)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "rev": sha,
        "sha_short": sha[:7],
        "nodes": nodes,
        "edges": graph["edges"],
        "communities": summaries,
        "stats": {**graph["stats"], "source": source_stats},
    }
    _persist(project_id, sha, payload, index.get("defs") or [], index.get("refs") or [])
    log.info(
        "built revision graph %s for project %s: %d nodes, %d edges from %d files",
        sha[:7],
        project_id,
        len(nodes),
        len(graph["edges"]),
        source_stats.get("files", 0),
    )
    return payload, True


def _persist(
    project_id: int,
    sha: str,
    payload: dict[str, Any],
    defs: list[dict[str, Any]],
    refs: list[dict[str, Any]],
) -> None:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    with write_conn() as conn:
        conn.execute(
            """INSERT INTO graphs
                   (project_id, scope, payload, built_hash, schema_version,
                    node_count, edge_count, built_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(project_id, scope) DO UPDATE SET
                   payload = excluded.payload,
                   built_hash = excluded.built_hash,
                   schema_version = excluded.schema_version,
                   node_count = excluded.node_count,
                   edge_count = excluded.edge_count,
                   built_at = excluded.built_at""",
            (
                project_id,
                scope_for(sha),
                body,
                sha,
                SCHEMA_VERSION,
                len(payload.get("nodes") or []),
                len(payload.get("edges") or []),
                utcnow(),
            ),
        )
        conn.execute("DELETE FROM revision_symbol_defs WHERE project_id = ? AND rev = ?", (project_id, sha))
        conn.execute("DELETE FROM revision_symbol_refs WHERE project_id = ? AND rev = ?", (project_id, sha))
        if defs:
            conn.executemany(
                """INSERT INTO revision_symbol_defs
                       (project_id, rev, node_id, name, normalized, kind, path,
                        start_line, start_col, end_line, end_col,
                        parent_node_id, signature, doc, language)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        project_id,
                        sha,
                        item["node_id"],
                        item["name"],
                        item.get("normalized") or item["name"].lower(),
                        item["kind"],
                        item["path"],
                        int(item.get("line") or 1),
                        int(item.get("column") or 1),
                        int(item.get("end_line") or item.get("line") or 1),
                        int(item.get("end_column") or 1),
                        item.get("parent_node_id"),
                        item.get("signature") or "",
                        item.get("doc") or "",
                        item.get("language") or "",
                    )
                    for item in defs
                ],
            )
        if refs:
            conn.executemany(
                """INSERT INTO revision_symbol_refs
                       (project_id, rev, path, start_line, start_col, end_line, end_col,
                        name, normalized, kind, from_node_id, target_node_id, confidence)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        project_id,
                        sha,
                        item["path"],
                        int(item.get("line") or 1),
                        item.get("column"),
                        item.get("end_line"),
                        item.get("end_column"),
                        item["name"],
                        item.get("normalized") or item["name"].lower(),
                        item["kind"],
                        item.get("from_node_id"),
                        item.get("target_node_id"),
                        item.get("confidence") or "EXTRACTED",
                    )
                    for item in refs
                ],
            )
        _evict(conn, project_id)


def _evict(conn: Any, project_id: int) -> None:
    """Keep only the newest revisions; drop the payload and its symbol rows together."""
    rows = conn.execute(
        "SELECT scope FROM graphs WHERE project_id = ? AND scope LIKE ? ORDER BY built_at DESC",
        (project_id, f"{SCOPE_PREFIX}%"),
    ).fetchall()
    for row in rows[REVISION_GRAPH_LIMIT:]:
        scope = str(row["scope"])
        rev = scope[len(SCOPE_PREFIX) :]
        conn.execute("DELETE FROM graphs WHERE project_id = ? AND scope = ?", (project_id, scope))
        conn.execute("DELETE FROM revision_symbol_defs WHERE project_id = ? AND rev = ?", (project_id, rev))
        conn.execute("DELETE FROM revision_symbol_refs WHERE project_id = ? AND rev = ?", (project_id, rev))


def source_content(root: Path, sha: str, rel_path: str) -> str | None:
    """One file from the commit, for cursor-word resolution in the editor."""
    try:
        data = git_service.show_bytes(root, sha, rel_path, max_bytes=2 * 1024 * 1024)
    except git_service.GitOutputLimit:
        return None
    if data is None:
        return None
    return data.decode("utf-8", errors="replace")
