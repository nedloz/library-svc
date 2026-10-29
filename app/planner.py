from datetime import date
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_gateway_user_id
from app.database import get_db
from app.schemas import (
    PlannerNoteCreate,
    PlannerNotePatch,
    PlannerNoteSchema,
    PlannerTagCreate,
    PlannerTagPatch,
    PlannerTagSchema,
)

router = APIRouter(prefix="/api/planner", tags=["planner"])

_UNIQUE_VIOLATION = "23505"

_NOTE_SELECT = """
    SELECT n.id, n.title, n.start_date AS start, n.end_date AS "end", n.description, n.color,
           n.created_at, n.updated_at,
           coalesce(array_agg(nt.tag_id ORDER BY nt.position)
                    FILTER (WHERE nt.tag_id IS NOT NULL), '{}') AS tag_ids
    FROM library.planner_notes n
    LEFT JOIN library.planner_note_tags nt ON nt.note_id = n.id
"""


def is_unique_violation(exc: IntegrityError) -> bool:
    code = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
    return code == _UNIQUE_VIOLATION


def field_error(field: str, message: str) -> HTTPException:
    """422 в том же виде, что у ошибок валидации FastAPI: клиент находит поле по loc."""
    return HTTPException(
        status_code=422,
        detail=[{"loc": ["body", field], "msg": message, "type": "value_error"}],
    )


async def _fetch_notes(db: AsyncSession, user_id: UUID, where: str = "", **params) -> List[dict]:
    query = text(
        f"{_NOTE_SELECT} WHERE n.user_id = :user_id {where} "
        "GROUP BY n.id ORDER BY n.start_date, n.created_at, n.id"
    )
    rows = (await db.execute(query, {"user_id": user_id, **params})).mappings().all()
    return [dict(row) for row in rows]


async def _get_note(db: AsyncSession, user_id: UUID, note_id: UUID) -> dict:
    notes = await _fetch_notes(db, user_id, "AND n.id = :note_id", note_id=note_id)
    if not notes:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    return notes[0]


async def _check_tags_owned(db: AsyncSession, user_id: UUID, tag_ids: List[UUID]) -> None:
    """Тег и запись должны принадлежать одному пользователю: внешний ключ этого не гарантирует."""
    if not tag_ids:
        return
    rows = await db.execute(
        text("SELECT id FROM library.planner_tags WHERE user_id = :user_id AND id = ANY(CAST(:ids AS uuid[]))"),
        {"user_id": user_id, "ids": tag_ids},
    )
    unknown = set(tag_ids) - {row.id for row in rows}
    if unknown:
        raise field_error("tag_ids", "тег не найден: " + ", ".join(str(t) for t in sorted(unknown, key=str)))


async def _set_note_tags(db: AsyncSession, note_id: UUID, tag_ids: List[UUID]) -> None:
    await db.execute(text("DELETE FROM library.planner_note_tags WHERE note_id = :note_id"), {"note_id": note_id})
    if tag_ids:
        await db.execute(
            text("INSERT INTO library.planner_note_tags (note_id, tag_id, position) VALUES (:note_id, :tag_id, :position)"),
            [{"note_id": note_id, "tag_id": tag_id, "position": i} for i, tag_id in enumerate(tag_ids)],
        )


# =========================================================
# ЗАПИСИ
# =========================================================

@router.get("/notes", response_model=List[PlannerNoteSchema])
async def list_notes(
    from_: Optional[date] = Query(default=None, alias="from"),
    to: Optional[date] = None,
    db: AsyncSession = Depends(get_db),
    user_id: UUID = Depends(get_gateway_user_id),
):
    """Записи, пересекающие период: start_date <= to AND end_date >= from. Границы необязательны."""
    if from_ and to and to < from_:
        raise HTTPException(
            status_code=422,
            detail=[{"loc": ["query", "to"], "msg": "to раньше from", "type": "value_error"}],
        )
    conditions = ""
    params = {}
    if from_:
        conditions += " AND n.end_date >= :from_date"
        params["from_date"] = from_
    if to:
        conditions += " AND n.start_date <= :to_date"
        params["to_date"] = to
    return await _fetch_notes(db, user_id, conditions, **params)


@router.post("/notes", response_model=PlannerNoteSchema, status_code=201)
async def create_note(
    body: PlannerNoteCreate,
    db: AsyncSession = Depends(get_db),
    user_id: UUID = Depends(get_gateway_user_id),
):
    await _check_tags_owned(db, user_id, body.tag_ids)
    note_id = (
        await db.execute(
            text(
                "INSERT INTO library.planner_notes (user_id, title, start_date, end_date, description, color) "
                "VALUES (:user_id, :title, :start, :end, :description, :color) RETURNING id"
            ),
            {
                "user_id": user_id,
                "title": body.title,
                "start": body.start,
                "end": body.end,
                "description": body.description,
                "color": body.color,
            },
        )
    ).scalar_one()
    await _set_note_tags(db, note_id, body.tag_ids)
    await db.commit()
    return await _get_note(db, user_id, note_id)


@router.patch("/notes/{note_id}", response_model=PlannerNoteSchema)
async def update_note(
    note_id: UUID,
    body: PlannerNotePatch,
    db: AsyncSession = Depends(get_db),
    user_id: UUID = Depends(get_gateway_user_id),
):
    """Частичное изменение: перетаскивание шлёт только start и end."""
    current = await _get_note(db, user_id, note_id)
    changes = body.model_dump(exclude_unset=True)

    start = changes.get("start", current["start"])
    end = changes.get("end", current["end"])
    if end < start:
        raise field_error("end", "конец записи раньше начала")

    tag_ids = changes.pop("tag_ids", None)
    if tag_ids is not None:
        await _check_tags_owned(db, user_id, tag_ids)

    columns = {"start": "start_date", "end": "end_date"}
    assignments = [f"{columns.get(name, name)} = :{name}" for name in changes]
    if assignments:
        await db.execute(
            text(
                f"UPDATE library.planner_notes SET {', '.join(assignments)}, updated_at = now() "
                "WHERE id = :note_id AND user_id = :user_id"
            ),
            {**changes, "note_id": note_id, "user_id": user_id},
        )
    if tag_ids is not None:
        await _set_note_tags(db, note_id, tag_ids)
        if not assignments:
            await db.execute(
                text("UPDATE library.planner_notes SET updated_at = now() WHERE id = :note_id"),
                {"note_id": note_id},
            )
    await db.commit()
    return await _get_note(db, user_id, note_id)


@router.delete("/notes/{note_id}", status_code=204)
async def delete_note(
    note_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: UUID = Depends(get_gateway_user_id),
):
    deleted = await db.execute(
        text("DELETE FROM library.planner_notes WHERE id = :note_id AND user_id = :user_id RETURNING id"),
        {"note_id": note_id, "user_id": user_id},
    )
    if deleted.first() is None:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    await db.commit()
    return Response(status_code=204)


# =========================================================
# ТЕГИ
# =========================================================

_TAG_COLUMNS = "id, name, color, position"


def _tag_conflict() -> HTTPException:
    return HTTPException(status_code=409, detail="Тег с таким названием уже есть")


@router.get("/tags", response_model=List[PlannerTagSchema])
async def list_tags(
    db: AsyncSession = Depends(get_db),
    user_id: UUID = Depends(get_gateway_user_id),
):
    rows = await db.execute(
        text(f"SELECT {_TAG_COLUMNS} FROM library.planner_tags WHERE user_id = :user_id ORDER BY position, created_at, id"),
        {"user_id": user_id},
    )
    return [dict(row) for row in rows.mappings()]


@router.post("/tags", response_model=PlannerTagSchema, status_code=201)
async def create_tag(
    body: PlannerTagCreate,
    db: AsyncSession = Depends(get_db),
    user_id: UUID = Depends(get_gateway_user_id),
):
    try:
        row = (
            await db.execute(
                text(
                    "INSERT INTO library.planner_tags (user_id, name, color, position) "
                    "VALUES (:user_id, :name, :color, "
                    "        (SELECT coalesce(max(position) + 1, 0) FROM library.planner_tags WHERE user_id = :user_id)) "
                    f"RETURNING {_TAG_COLUMNS}"
                ),
                {"user_id": user_id, "name": body.name, "color": body.color},
            )
        ).mappings().one()
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        if is_unique_violation(exc):
            raise _tag_conflict() from exc
        raise
    return dict(row)


@router.patch("/tags/{tag_id}", response_model=PlannerTagSchema)
async def update_tag(
    tag_id: UUID,
    body: PlannerTagPatch,
    db: AsyncSession = Depends(get_db),
    user_id: UUID = Depends(get_gateway_user_id),
):
    changes = body.model_dump(exclude_unset=True)
    assignments = "".join(f"{name} = :{name}, " for name in changes)
    try:
        row = (
            await db.execute(
                text(
                    f"UPDATE library.planner_tags SET {assignments}updated_at = now() "
                    f"WHERE id = :tag_id AND user_id = :user_id RETURNING {_TAG_COLUMNS}"
                ),
                {**changes, "tag_id": tag_id, "user_id": user_id},
            )
        ).mappings().first()
        if row is None:
            raise HTTPException(status_code=404, detail="Тег не найден")
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        if is_unique_violation(exc):
            raise _tag_conflict() from exc
        raise
    return dict(row)


@router.delete("/tags/{tag_id}", status_code=204)
async def delete_tag(
    tag_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: UUID = Depends(get_gateway_user_id),
):
    """Тег пропадает из всех записей (ON DELETE CASCADE у planner_note_tags)."""
    deleted = await db.execute(
        text("DELETE FROM library.planner_tags WHERE id = :tag_id AND user_id = :user_id RETURNING id"),
        {"tag_id": tag_id, "user_id": user_id},
    )
    if deleted.first() is None:
        raise HTTPException(status_code=404, detail="Тег не найден")
    await db.commit()
    return Response(status_code=204)
