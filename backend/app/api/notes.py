"""Notes, code anchors and command rules."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field, field_validator

from app.api.deps import ProjectDep, TokenDep
from app.core.errors import NotFound
from app.services import anchors, note_versions, notegen, notes_export, notes_index, paths, revisions
from app.storage import repo

router = APIRouter(tags=["notes"])
log = logging.getLogger(__name__)


class NoteCreate(BaseModel):
    project_id: int
    title: str = Field(min_length=1)
    file_name: str | None = None
    content_md: str = ""
    conversation_id: str | None = None
    folder_id: int | None = None


class NoteUpdate(BaseModel):
    title: str | None = None
    file_name: str | None = None
    content_md: str | None = None
    folder_id: int | None = None
    move_folder: bool = False


class AnnotationCreate(BaseModel):
    file_path: str = Field(min_length=1, max_length=2000)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    selected_code: str = Field(default="", max_length=100000)
    # Optional: an empty title falls back to the first line of the body.
    title: str = Field(default="", max_length=200)
    body: str = Field(min_length=1, max_length=10000)

    @field_validator("body")
    @classmethod
    def nonempty_body(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("annotation must contain text")
        return value.strip()


@router.get("/projects/{project_id}/annotations")
def list_annotations(project: ProjectDep, _: TokenDep, file_path: str) -> list[dict[str, Any]]:
    return repo.list_annotations(int(project["id"]), file_path)


@router.post("/projects/{project_id}/annotations")
def create_annotation(project: ProjectDep, body: AnnotationCreate, _: TokenDep) -> dict[str, Any]:
    if body.end_line < body.start_line:
        from fastapi import HTTPException

        raise HTTPException(status_code=422, detail="end_line must be greater than or equal to start_line")
    return repo.create_annotation(
        int(project["id"]),
        body.file_path,
        body.start_line,
        body.end_line,
        body.selected_code,
        body.body.strip(),
        title=body.title,
    )


@router.delete("/annotations/{annotation_id}")
def delete_annotation(annotation_id: int, _: TokenDep) -> dict[str, bool]:
    repo.delete_annotation(annotation_id)
    return {"ok": True}


class FolderCreate(BaseModel):
    name: str = Field(min_length=1)
    parent_id: int | None = None


class FolderUpdate(BaseModel):
    name: str | None = None
    parent_id: int | None = None
    move: bool = False


def _with_index_state(notes: list[dict[str, Any]], project_id: int | None = None) -> list[dict[str, Any]]:
    """Attach ``indexed`` so the UI can show the "已入知识库" badge.

    The knowledge base lists notes from several projects at once, so the indexed
    chunk set is resolved per project instead of assuming one.
    """
    project_ids = {project_id} if project_id is not None else {int(n["project_id"]) for n in notes}
    indexed: set[int] = set()
    for pid in project_ids:
        if pid is None:
            continue
        try:
            indexed |= notes_index.indexed_note_ids(int(pid))
        except Exception:  # a missing chunk table must not hide the notes
            log.warning("note index state unavailable", exc_info=True)
    return [{**note, "indexed": int(note["id"]) in indexed} for note in notes]


def _index_note(note: dict[str, Any]) -> dict[str, Any]:
    """Keep the retrieval row in step with the saved note; never fail the write."""
    try:
        notes_index.sync_note_background(note)
        indexed = True
    except Exception:
        log.warning("note index sync failed for %s", note.get("id"), exc_info=True)
        indexed = False
    return {**note, "indexed": indexed}


def _scope(project_id: int, rev: str) -> tuple[Any, str]:
    """``(root, full sha)`` for a revision request; an unknown ref is a 404."""
    project = repo.get_project(project_id)
    if project is None:
        raise NotFound("project not found")
    root = paths.resolve_root(project["path"])
    resolved = revisions.resolve(root, rev)
    if resolved is None:
        raise NotFound(f"unknown revision: {rev}")
    return root, str(resolved["sha"])


@router.get("/notes")
def list_notes(
    _: TokenDep,
    project_id: int | None = None,
    folder_id: int | None = None,
    unfiled: bool = False,
    rev: str = "",
) -> list[dict[str, Any]]:
    """List notes in the knowledge base.

    Without ``project_id`` this spans **every** project: a note about a library is
    as useful in the next repo as it was in the one that produced it. Passing
    ``project_id`` keeps the old per-project behaviour for the panel's project
    filter. ``folder_id`` / ``unfiled`` narrow to one place in the folder tree.

    ``rev`` annotates each note with whether it already existed at that commit.
    Nothing is filtered out: a later note is still shown (the panel dims it), so
    browsing history never looks like the knowledge base lost content.
    """
    if project_id is None:
        notes = repo.list_all_notes(folder_id=folder_id, unfiled=unfiled)
    else:
        notes = [
            note for note in repo.list_notes(project_id) if (folder_id is None or note.get("folder_id") == folder_id)
        ]
    notes = _with_index_state(notes)
    if not rev:
        return notes
    # Notes span projects; each needs its own repository to answer ancestry.
    roots: dict[int, Any] = {}
    shas: dict[int, str] = {}
    out: list[dict[str, Any]] = []
    for note in notes:
        pid = int(note["project_id"])
        if pid not in roots:
            project = repo.get_project(pid)
            roots[pid] = paths.resolve_root(project["path"]) if project else None
            if roots[pid] is not None:
                resolved = revisions.resolve(roots[pid], rev)
                if resolved is not None:
                    shas[pid] = str(resolved["sha"])
        root = roots[pid]
        if root is None or pid not in shas:
            out.append({**note, "visible_at_rev": True, "note_state": "unknown"})
            continue
        out.extend(note_versions.annotate_visibility(root, [note], shas[pid]))
    return out


@router.get("/notes/{note_id}")
def get_note(note_id: int, _: TokenDep, rev: str = "") -> dict[str, Any]:
    """One note with its index state — the reader pane reloads through this.

    With ``rev`` the note's references are re-resolved against that commit and
    the note is marked as already existing or written later.
    """
    note = repo.get_note(note_id)
    if note is None:
        raise NotFound("note not found")
    payload = _with_index_state([note], int(note["project_id"]))[0]
    # The reader shows which revision the note was written from and whether each
    # quoted range still points at the same content. Both live beside the note
    # rather than inside the Markdown, so the body stays human-readable.
    refs = repo.list_note_refs(note_id)
    if rev:
        root, sha = _scope(int(note["project_id"]), rev)
        payload = note_versions.annotate_visibility(root, [payload], sha)[0]
        payload["scope_rev"] = sha
        payload["refs"] = note_versions.resolve_refs(root, refs, sha)
        return payload
    payload["refs"] = refs
    return payload


@router.post("/notes/{note_id}/retitle")
def retitle_note(note_id: int, _: TokenDep) -> dict[str, Any]:
    """Rewrite a legacy "关于 X 第 N-M 行" title as a knowledge point.

    A model failure returns the unchanged note with ``retitle_failed`` instead of
    an error: the user still has their note, they just have to title it by hand.
    """
    return notegen.retitle(note_id)


@router.post("/notes")
def create_note(body: NoteCreate, _: TokenDep) -> dict[str, Any]:
    if repo.get_project(body.project_id) is None:
        raise NotFound("project not found")
    note = repo.create_note(
        body.project_id,
        body.title,
        body.content_md,
        body.conversation_id,
        folder_id=body.folder_id,
        file_name=body.file_name,
    )
    if note is None:  # pragma: no cover - defensive
        raise NotFound("note not found")
    return _index_note(note)


@router.put("/notes/{note_id}")
def update_note(note_id: int, body: NoteUpdate, _: TokenDep) -> dict[str, Any]:
    note = repo.update_note(
        note_id,
        body.title,
        body.content_md,
        folder_id=body.folder_id,
        move_folder=body.move_folder,
        file_name=body.file_name,
    )
    if note is None:
        raise NotFound("note not found")
    return _index_note(note)


@router.delete("/notes/{note_id}")
def delete_note(note_id: int, _: TokenDep) -> dict[str, bool]:
    repo.delete_note(note_id)
    try:
        notes_index.remove_note_chunk(note_id)
    except Exception:  # deleting the note is what the user asked for; index is derived
        log.warning("note chunk removal failed for %s", note_id, exc_info=True)
    return {"ok": True}


@router.post("/notes/{note_id}/export")
def export_note(note_id: int, _: TokenDep) -> dict[str, Any]:
    return notes_export.export_note(note_id)


# --------------------------------------------------------------------------- #
# knowledge-base folders
# --------------------------------------------------------------------------- #
@router.get("/note-folders")
def list_note_folders(_: TokenDep) -> list[dict[str, Any]]:
    """The user's own tree for the knowledge base."""
    return repo.list_note_folders()


@router.post("/note-folders")
def create_note_folder(body: FolderCreate, _: TokenDep) -> dict[str, Any]:
    return repo.create_note_folder(body.name, body.parent_id)


@router.put("/note-folders/{folder_id}")
def update_note_folder(folder_id: int, body: FolderUpdate, _: TokenDep) -> dict[str, Any]:
    folder = repo.rename_note_folder(folder_id, body.name, body.parent_id, move=body.move)
    if folder is None:
        raise NotFound("folder not found")
    return folder


@router.delete("/note-folders/{folder_id}")
def delete_note_folder(folder_id: int, _: TokenDep) -> dict[str, bool]:
    """Notes inside fall back to 未分类 instead of being deleted with the folder."""
    repo.delete_note_folder(folder_id)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# anchors
# --------------------------------------------------------------------------- #
@router.get("/projects/{project_id}/anchors")
def list_anchors(project: ProjectDep, _: TokenDep, conversation_id: str | None = None) -> list[dict[str, Any]]:
    return repo.list_anchors(project["id"], conversation_id)


@router.post("/projects/{project_id}/anchors/refresh")
def refresh_anchors(project: ProjectDep, _: TokenDep) -> dict[str, int]:
    return anchors.refresh(project["id"])


# --------------------------------------------------------------------------- #
# command rules
# --------------------------------------------------------------------------- #
class RuleCreate(BaseModel):
    pattern: str = Field(min_length=1)
    scope: str = "once"
    needs_confirm: bool = True


class RuleUpdate(BaseModel):
    pattern: str | None = None
    scope: str | None = None
    needs_confirm: bool | None = None


@router.get("/projects/{project_id}/command-rules")
def list_rules(project: ProjectDep, _: TokenDep) -> list[dict[str, Any]]:
    return repo.list_rules(project["id"])


@router.post("/projects/{project_id}/command-rules")
def create_rule(project: ProjectDep, body: RuleCreate, _: TokenDep) -> dict[str, Any]:
    return repo.create_rule(project["id"], body.pattern, body.scope, body.needs_confirm)


@router.put("/projects/{project_id}/command-rules/{rule_id}")
def update_rule(project: ProjectDep, rule_id: int, body: RuleUpdate, _: TokenDep) -> dict[str, Any]:
    rule = repo.update_rule(rule_id, **body.model_dump(exclude_none=True))
    if rule is None:
        raise NotFound("rule not found")
    return rule


@router.delete("/projects/{project_id}/command-rules/{rule_id}")
def delete_rule(project: ProjectDep, rule_id: int, _: TokenDep) -> dict[str, bool]:
    repo.delete_rule(rule_id)
    return {"ok": True}
