import json
import os
from datetime import date
from typing import Any, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_gateway_user_id
from app.database import get_db
from app.planner import field_error, is_unique_violation
from app.schemas import (
    GradeScoresBody,
    GradeScoresSchema,
    GradeSubjectTemplateSchema,
    GradeUserTemplateCreate,
    GradeUserTemplatePatch,
    GradeUserTemplateSchema,
)

router = APIRouter(prefix="/api/grades", tags=["grades"])

_USER_TEMPLATE_COLUMNS = "id, name, components, created_at, updated_at"


def current_academic_year(today: Optional[date] = None) -> str:
    """Учебный год начинается 1 сентября: ноябрь 2026 -> «2026/2027», март 2027 -> «2026/2027».

    GRADES_ACADEMIC_YEAR переопределяет вычисление (например, до начала занятий).
    """
    override = os.getenv("GRADES_ACADEMIC_YEAR")
    if override:
        return override
    today = today or date.today()
    start = today.year if today.month >= 9 else today.year - 1
    return f"{start}/{start + 1}"


def _json(value: Any) -> Any:
    """asyncpg отдаёт jsonb из сырых запросов строкой."""
    return json.loads(value) if isinstance(value, str) else value


def _template_row(row) -> dict:
    data = dict(row)
    data["components"] = _json(data["components"])
    return data


def _name_conflict() -> HTTPException:
    return HTTPException(status_code=409, detail="Шаблон с таким названием уже есть")


# =========================================================
# ШАБЛОНЫ ПРЕДМЕТОВ (из ПУД)
# =========================================================

@router.get("/templates", response_model=List[GradeSubjectTemplateSchema])
async def list_templates(
    course: Optional[int] = Query(default=None, ge=1, le=6),
    program_id: Optional[UUID] = None,
    db: AsyncSession = Depends(get_db),
    _user_id: UUID = Depends(get_gateway_user_id),
):
    """
    Шаблоны предметов текущего учебного года. С program_id: для направления и общие
    (program_id IS NULL); без него: только общие.
    """
    conditions = ["is_active", "academic_year = :academic_year"]
    params: dict = {"academic_year": current_academic_year()}
    if course is not None:
        conditions.append("course = :course")
        params["course"] = course
    if program_id is not None:
        conditions.append("(program_id = :program_id OR program_id IS NULL)")
        params["program_id"] = program_id
    else:
        conditions.append("program_id IS NULL")

    rows = await db.execute(
        text(
            "SELECT id, subject, course, program_id, academic_year, source_url, rounding, components "
            f"FROM library.grade_subject_templates WHERE {' AND '.join(conditions)} "
            "ORDER BY course, subject, id"
        ),
        params,
    )
    return [_template_row(row) for row in rows.mappings()]


# =========================================================
# СВОИ ШАБЛОНЫ ПОЛЬЗОВАТЕЛЯ
# =========================================================

@router.get("/my-templates", response_model=List[GradeUserTemplateSchema])
async def list_my_templates(
    db: AsyncSession = Depends(get_db),
    user_id: UUID = Depends(get_gateway_user_id),
):
    rows = await db.execute(
        text(
            f"SELECT {_USER_TEMPLATE_COLUMNS} FROM library.grade_user_templates "
            "WHERE user_id = :user_id ORDER BY created_at, id"
        ),
        {"user_id": user_id},
    )
    return [_template_row(row) for row in rows.mappings()]


@router.post("/my-templates", response_model=GradeUserTemplateSchema, status_code=201)
async def create_my_template(
    body: GradeUserTemplateCreate,
    db: AsyncSession = Depends(get_db),
    user_id: UUID = Depends(get_gateway_user_id),
):
    try:
        row = (
            await db.execute(
                text(
                    "INSERT INTO library.grade_user_templates (user_id, name, components) "
                    f"VALUES (:user_id, :name, CAST(:components AS jsonb)) RETURNING {_USER_TEMPLATE_COLUMNS}"
                ),
                {
                    "user_id": user_id,
                    "name": body.name,
                    "components": json.dumps([c.model_dump(mode="json") for c in body.components]),
                },
            )
        ).mappings().one()
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        if is_unique_violation(exc):
            raise _name_conflict() from exc
        raise
    return _template_row(row)


@router.patch("/my-templates/{template_id}", response_model=GradeUserTemplateSchema)
async def update_my_template(
    template_id: UUID,
    body: GradeUserTemplatePatch,
    db: AsyncSession = Depends(get_db),
    user_id: UUID = Depends(get_gateway_user_id),
):
    params: dict = {"template_id": template_id, "user_id": user_id}
    assignments = ""
    if body.name is not None:
        assignments += "name = :name, "
        params["name"] = body.name
    if body.components is not None:
        assignments += "components = CAST(:components AS jsonb), "
        params["components"] = json.dumps([c.model_dump(mode="json") for c in body.components])
    try:
        row = (
            await db.execute(
                text(
                    f"UPDATE library.grade_user_templates SET {assignments}updated_at = now() "
                    f"WHERE id = :template_id AND user_id = :user_id RETURNING {_USER_TEMPLATE_COLUMNS}"
                ),
                params,
            )
        ).mappings().first()
        if row is None:
            raise HTTPException(status_code=404, detail="Шаблон не найден")
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        if is_unique_violation(exc):
            raise _name_conflict() from exc
        raise
    return _template_row(row)


@router.delete("/my-templates/{template_id}", status_code=204)
async def delete_my_template(
    template_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: UUID = Depends(get_gateway_user_id),
):
    """Вместе с шаблоном удаляются оценки по нему: внешнего ключа у grade_user_scores нет."""
    deleted = await db.execute(
        text("DELETE FROM library.grade_user_templates WHERE id = :template_id AND user_id = :user_id RETURNING id"),
        {"template_id": template_id, "user_id": user_id},
    )
    if deleted.first() is None:
        raise HTTPException(status_code=404, detail="Шаблон не найден")
    await db.execute(
        text("DELETE FROM library.grade_user_scores WHERE template_id = :template_id AND user_id = :user_id"),
        {"template_id": template_id, "user_id": user_id},
    )
    await db.commit()
    return Response(status_code=204)


# =========================================================
# ОЦЕНКИ ПОЛЬЗОВАТЕЛЯ
# =========================================================

async def _available_components(db: AsyncSession, user_id: UUID, template_id: UUID) -> List[dict]:
    """Компоненты шаблона, к которому у пользователя есть доступ: предмет из ПУД или свой."""
    row = (
        await db.execute(
            text(
                "SELECT components FROM library.grade_subject_templates WHERE id = :template_id AND is_active "
                "UNION ALL "
                "SELECT components FROM library.grade_user_templates WHERE id = :template_id AND user_id = :user_id"
            ),
            {"template_id": template_id, "user_id": user_id},
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Шаблон не найден")
    return _json(row.components)


def _check_scores(scores: dict, components: List[dict]) -> None:
    by_key = {c["key"]: c for c in components}
    for key, value in scores.items():
        component = by_key.get(key)
        if component is None:
            raise field_error(f"scores.{key}", "в шаблоне нет такого компонента")
        items = component.get("items")
        if isinstance(value, list):
            if items is None:
                raise field_error(f"scores.{key}", "у компонента одна оценка, а не список")
            if len(value) > items["count"]:
                raise field_error(f"scores.{key}", f"оценок не больше {items['count']}")
        elif items is not None:
            raise field_error(f"scores.{key}", "у компонента несколько оценок, нужен список")


@router.get("/scores/{template_id}", response_model=GradeScoresSchema)
async def get_scores(
    template_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: UUID = Depends(get_gateway_user_id),
):
    """Оценки пользователя по шаблону. Пока ничего не вводилось: пустые scores и target: null."""
    await _available_components(db, user_id, template_id)
    row = (
        await db.execute(
            text("SELECT scores, target FROM library.grade_user_scores WHERE user_id = :user_id AND template_id = :template_id"),
            {"user_id": user_id, "template_id": template_id},
        )
    ).first()
    if row is None:
        return {"scores": {}, "target": None}
    return {"scores": _json(row.scores), "target": row.target}


@router.put("/scores/{template_id}", response_model=GradeScoresSchema)
async def put_scores(
    template_id: UUID,
    body: GradeScoresBody,
    db: AsyncSession = Depends(get_db),
    user_id: UUID = Depends(get_gateway_user_id),
):
    """Заменяет оценки целиком."""
    components = await _available_components(db, user_id, template_id)
    _check_scores(body.scores, components)
    await db.execute(
        text(
            "INSERT INTO library.grade_user_scores (user_id, template_id, scores, target) "
            "VALUES (:user_id, :template_id, CAST(:scores AS jsonb), :target) "
            "ON CONFLICT (user_id, template_id) DO UPDATE "
            "SET scores = EXCLUDED.scores, target = EXCLUDED.target, updated_at = now()"
        ),
        {
            "user_id": user_id,
            "template_id": template_id,
            "scores": json.dumps(body.scores),
            "target": body.target,
        },
    )
    await db.commit()
    return {"scores": body.scores, "target": body.target}
