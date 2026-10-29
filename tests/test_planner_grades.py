"""
Календарь (/api/planner) и калькулятор оценок (/api/grades).

Проверки схем идут без базы. Роуты проверяются на настоящем PostgreSQL: задайте
LIBRARY_TEST_DATABASE_URL (postgresql+asyncpg://...) на базу с применённой миграцией
013_planner_grades.sql и таблицами auth.users, core.programs; без неё эти тесты пропускаются.
Для проверки достаточно одноразового контейнера postgres, стек проекта поднимать не нужно.
"""
import asyncio
import os
import uuid
from contextlib import asynccontextmanager

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from pydantic import ValidationError  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app import grades, main, planner  # noqa: E402
from app.auth import get_gateway_user_id  # noqa: E402
from app.database import get_db  # noqa: E402
from app.schemas import (  # noqa: E402
    GradeUserTemplateCreate,
    PlannerNoteCreate,
    PlannerNotePatch,
    PlannerTagCreate,
)

TEST_DB_URL = os.getenv("LIBRARY_TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not TEST_DB_URL, reason="LIBRARY_TEST_DATABASE_URL не задан")

COMPONENTS = [
    {"key": "activity", "label": "Активность", "weight": 0.1},
    {"key": "labs", "label": "Лабораторные работы", "short_label": "ЛР", "weight": 0.25,
     "items": {"count": 8, "aggregate": "mean"}},
    {"key": "homework", "label": "Домашние задания", "weight": 0.3},
    {"key": "tests", "label": "Тесты", "weight": 0.1, "items": {"count": 3, "aggregate": "floor"}},
    {"key": "exam", "label": "Экзамен", "weight": 0.25, "is_exam": True},
]


# =========================================================
# Схемы
# =========================================================

def test_note_end_before_start_rejected():
    with pytest.raises(ValidationError):
        PlannerNoteCreate(title="ЛР5", start="2026-11-06", end="2026-11-04")


def test_note_title_is_trimmed_and_limited():
    assert PlannerNoteCreate(title="  ЛР5  ", start="2026-11-04", end="2026-11-04").title == "ЛР5"
    for bad in ("   ", "x" * 81):
        with pytest.raises(ValidationError):
            PlannerNoteCreate(title=bad, start="2026-11-04", end="2026-11-04")


def test_note_rejects_bad_color_and_duplicate_tags():
    with pytest.raises(ValidationError):
        PlannerNoteCreate(title="a", start="2026-11-04", end="2026-11-04", color="red")
    tag = str(uuid.uuid4())
    with pytest.raises(ValidationError):
        PlannerNoteCreate(title="a", start="2026-11-04", end="2026-11-04", tag_ids=[tag, tag])


def test_note_patch_allows_null_color_only():
    assert PlannerNotePatch(color=None).model_dump(exclude_unset=True) == {"color": None}
    with pytest.raises(ValidationError):
        PlannerNotePatch(title=None)
    with pytest.raises(ValidationError):
        PlannerNotePatch(start=None)


def test_tag_color_and_name():
    PlannerTagCreate(name="Дедлайн", color="#FF0000")
    for kwargs in ({"name": "", "color": "#FF0000"}, {"name": "x" * 31, "color": "#FF0000"},
                   {"name": "ok", "color": "#FFF"}):
        with pytest.raises(ValidationError):
            PlannerTagCreate(**kwargs)


def test_template_components_valid():
    assert len(GradeUserTemplateCreate(name="БД", components=COMPONENTS).components) == 5


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c[0].update(weight=0.2),                              # сумма весов не 1
        lambda c: c[1].update(key="activity"),                          # дубль key
        lambda c: c[0].update(key="акт ивность"),                       # key не латиница
        lambda c: c[0].update(is_exam=True),                            # два экзамена
        lambda c: c[1]["items"].update(aggregate="max"),                # неизвестная агрегация
        lambda c: c[1]["items"].update(names=["a"]),                    # подписей не count
        lambda c: c[2].update(weight=1.5),                              # вес вне 0..1
        lambda c: c[4].update(min_score=11),                            # порог вне шкалы
        lambda c: c[2].update(unknown="x"),                             # опечатка в поле
    ],
)
def test_template_components_invalid(mutate):
    import copy
    components = copy.deepcopy(COMPONENTS)
    mutate(components)
    with pytest.raises(ValidationError):
        GradeUserTemplateCreate(name="БД", components=components)


def test_template_needs_components():
    with pytest.raises(ValidationError):
        GradeUserTemplateCreate(name="БД", components=[])


def test_academic_year(monkeypatch):
    from datetime import date
    monkeypatch.delenv("GRADES_ACADEMIC_YEAR", raising=False)
    assert grades.current_academic_year(date(2026, 11, 4)) == "2026/2027"
    assert grades.current_academic_year(date(2027, 3, 1)) == "2026/2027"
    assert grades.current_academic_year(date(2026, 9, 1)) == "2026/2027"
    assert grades.current_academic_year(date(2026, 8, 31)) == "2025/2026"
    monkeypatch.setenv("GRADES_ACADEMIC_YEAR", "2030/2031")
    assert grades.current_academic_year(date(2026, 11, 4)) == "2030/2031"


# =========================================================
# Роуты на PostgreSQL
# =========================================================

def test_routers_are_registered_in_app():
    paths = set(main.app.openapi()["paths"])
    assert {"/api/planner/notes", "/api/planner/notes/{note_id}", "/api/planner/tags",
            "/api/planner/tags/{tag_id}", "/api/grades/templates", "/api/grades/my-templates",
            "/api/grades/my-templates/{template_id}", "/api/grades/scores/{template_id}"} <= paths


@asynccontextmanager
async def api(user_id: uuid.UUID):
    """HTTP-клиент к роутерам под заданным пользователем, база — тестовая.

    Приложение отдельное, без метрик: полное main.app регистрирует их в общем реестре, где их
    уже зарегистрировал test_metrics.
    """
    app = FastAPI()
    app.include_router(planner.router)
    app.include_router(grades.router)
    engine = create_async_engine(TEST_DB_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def db():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_db] = db
    app.dependency_overrides[get_gateway_user_id] = lambda: user_id
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            client.app = app
            yield client, sessions
    finally:
        app.dependency_overrides.clear()
        await engine.dispose()


def act_as(client, user_id: uuid.UUID) -> None:
    client.app.dependency_overrides[get_gateway_user_id] = lambda: user_id


async def make_user(sessions) -> uuid.UUID:
    user_id = uuid.uuid4()
    async with sessions() as session:
        await session.execute(text("INSERT INTO auth.users (id) VALUES (:id)"), {"id": user_id})
        await session.commit()
    return user_id


def run(coro):
    return asyncio.run(coro)


@needs_db
def test_notes_lifecycle_and_period_filter():
    async def scenario():
        owner = uuid.uuid4()
        async with api(owner) as (client, sessions):
            async with sessions() as s:
                await s.execute(text("INSERT INTO auth.users (id) VALUES (:id)"), {"id": owner})
                await s.commit()

            red = (await client.post("/api/planner/tags", json={"name": "Дедлайн", "color": "#FF0000"})).json()
            blue = (await client.post("/api/planner/tags", json={"name": "Экзамен", "color": "#0000FF"})).json()
            assert (red["position"], blue["position"]) == (0, 1)

            created = await client.post("/api/planner/notes", json={
                "title": "Отчёт по БД (ЛР5)", "start": "2026-11-04", "end": "2026-11-06",
                "tag_ids": [blue["id"], red["id"]],
            })
            assert created.status_code == 201
            note = created.json()
            assert note["tag_ids"] == [blue["id"], red["id"]]  # порядок = приоритет
            assert note["start"] == "2026-11-04" and note["description"] == "" and note["color"] is None
            assert note["created_at"].endswith("Z")

            await client.post("/api/planner/notes", json={"title": "Позже", "start": "2026-12-01", "end": "2026-12-01"})

            def titles(resp):
                return [n["title"] for n in resp.json()]

            # пересечение периода: запись 4–6 ноября попадает, если окно задевает хотя бы день
            assert titles(await client.get("/api/planner/notes", params={"from": "2026-11-06", "to": "2026-11-30"})) == ["Отчёт по БД (ЛР5)"]
            assert titles(await client.get("/api/planner/notes", params={"from": "2026-11-07", "to": "2026-11-30"})) == []
            assert titles(await client.get("/api/planner/notes", params={"to": "2026-11-03"})) == []
            assert titles(await client.get("/api/planner/notes")) == ["Отчёт по БД (ЛР5)", "Позже"]
            assert (await client.get("/api/planner/notes", params={"from": "2026-12-02", "to": "2026-12-01"})).status_code == 422

            # перетаскивание шлёт только даты
            moved = await client.patch(f"/api/planner/notes/{note['id']}", json={"start": "2026-11-10", "end": "2026-11-11"})
            assert moved.status_code == 200
            assert moved.json()["tag_ids"] == [blue["id"], red["id"]] and moved.json()["title"] == "Отчёт по БД (ЛР5)"
            assert moved.json()["updated_at"] >= note["updated_at"]

            # одиночная смена start проверяется вместе со старым end
            bad = await client.patch(f"/api/planner/notes/{note['id']}", json={"start": "2026-11-20"})
            assert bad.status_code == 422 and bad.json()["detail"][0]["loc"] == ["body", "end"]

            # смена порядка тегов и сброс цвета
            reordered = await client.patch(f"/api/planner/notes/{note['id']}", json={"tag_ids": [red["id"]], "color": None})
            assert reordered.json()["tag_ids"] == [red["id"]]

            # удаление тега убирает его из записей
            assert (await client.delete(f"/api/planner/tags/{red['id']}")).status_code == 204
            assert (await client.get("/api/planner/notes")).json()[0]["tag_ids"] == []

            assert (await client.delete(f"/api/planner/notes/{note['id']}")).status_code == 204
            assert (await client.delete(f"/api/planner/notes/{note['id']}")).status_code == 404

    run(scenario())


@needs_db
def test_planner_isolation_and_validation():
    async def scenario():
        async with api(uuid.uuid4()) as (client, sessions):
            alice = await make_user(sessions)
            bob = await make_user(sessions)

            act_as(client, alice)
            tag = (await client.post("/api/planner/tags", json={"name": "Учёба", "color": "#00FF00"})).json()
            note = (await client.post("/api/planner/notes", json={"title": "a", "start": "2026-11-04", "end": "2026-11-04"})).json()

            act_as(client, bob)
            assert (await client.get("/api/planner/notes")).json() == []
            assert (await client.get("/api/planner/tags")).json() == []
            assert (await client.patch(f"/api/planner/notes/{note['id']}", json={"title": "x"})).status_code == 404
            assert (await client.delete(f"/api/planner/notes/{note['id']}")).status_code == 404
            assert (await client.patch(f"/api/planner/tags/{tag['id']}", json={"name": "x"})).status_code == 404
            assert (await client.delete(f"/api/planner/tags/{tag['id']}")).status_code == 404

            # чужой тег в записи: 422 с указанием поля
            resp = await client.post("/api/planner/notes", json={
                "title": "b", "start": "2026-11-04", "end": "2026-11-04", "tag_ids": [tag["id"]]})
            assert resp.status_code == 422 and resp.json()["detail"][0]["loc"] == ["body", "tag_ids"]

            # одно имя у разных пользователей допустимо, у одного — нет, без учёта регистра
            assert (await client.post("/api/planner/tags", json={"name": "учёба", "color": "#00FF00"})).status_code == 201
            dup = await client.post("/api/planner/tags", json={"name": "УЧЁБА", "color": "#00FF00"})
            assert dup.status_code == 409

            other = (await client.post("/api/planner/tags", json={"name": "Другой", "color": "#00FF00"})).json()
            clash = await client.patch(f"/api/planner/tags/{other['id']}", json={"name": "учёба"})
            assert clash.status_code == 409
            ok = await client.patch(f"/api/planner/tags/{other['id']}", json={"color": "#112233", "position": 5})
            assert ok.status_code == 200 and ok.json()["color"] == "#112233" and ok.json()["position"] == 5

            # ошибки полей
            assert (await client.post("/api/planner/notes", json={"title": "", "start": "2026-11-04", "end": "2026-11-04"})).status_code == 422
            assert (await client.post("/api/planner/notes", json={"title": "a", "start": "2026-11-05", "end": "2026-11-04"})).status_code == 422
            assert (await client.patch(f"/api/planner/tags/{other['id']}", json={"name": None})).status_code == 422

    run(scenario())


@needs_db
def test_grades_templates_scores():
    async def scenario():
        async with api(uuid.uuid4()) as (client, sessions):
            alice = await make_user(sessions)
            bob = await make_user(sessions)
            act_as(client, alice)

            # шаблоны предметов: активный текущего года виден, прошлогодний и неактивный нет;
            # предметы с меткой прогона, чтобы тест не зависел от содержимого базы
            tag = uuid.uuid4().hex[:6]
            year = grades.current_academic_year()
            program = uuid.uuid4()

            async def visible(**params):
                resp = await client.get("/api/grades/templates", params=params)
                assert resp.status_code == 200
                return [t for t in resp.json() if t["subject"].endswith(tag)]

            assert await visible(course=2) == []  # пока таблица без наших шаблонов
            assert (await client.get("/api/grades/templates", params={"course": 7})).status_code == 422

            async with sessions() as s:
                await s.execute(text("INSERT INTO core.programs (id) VALUES (:id)"), {"id": program})
                rows = [(f"БД-{tag}", None, year, True), (f"Сети-{tag}", program, year, True),
                        (f"Старое-{tag}", None, "2020/2021", True), (f"Выключено-{tag}", None, year, False)]
                for subject, prog, academic_year, active in rows:
                    await s.execute(text(
                        "INSERT INTO library.grade_subject_templates (subject, course, program_id, academic_year, components, is_active) "
                        "VALUES (:s, 2, :p, :y, CAST(:comp AS jsonb), :a)"),
                        {"s": subject, "p": prog, "y": academic_year, "a": active,
                         "comp": '[{"key": "exam", "label": "Экзамен", "weight": 1, "is_exam": true}]'})
                await s.commit()
            try:
                assert [t["subject"] for t in await visible(course=2)] == [f"БД-{tag}"]
                assert await visible(course=3) == []
                both = await visible(course=2, program_id=str(program))
                assert sorted(t["subject"] for t in both) == [f"БД-{tag}", f"Сети-{tag}"]
                assert both[0]["components"][0]["key"] == "exam"
                subject_id = next(t["id"] for t in both if t["subject"] == f"БД-{tag}")

                # оценки по шаблону предмета
                assert (await client.put(f"/api/grades/scores/{subject_id}", json={"scores": {"exam": 7}})).status_code == 200
                assert (await client.get(f"/api/grades/scores/{subject_id}")).json() == {"scores": {"exam": 7}, "target": None}
            finally:
                async with sessions() as s:
                    await s.execute(text(
                        "DELETE FROM library.grade_user_scores WHERE template_id IN "
                        "(SELECT id FROM library.grade_subject_templates WHERE subject LIKE :p)"), {"p": f"%-{tag}"})
                    await s.execute(text("DELETE FROM library.grade_subject_templates WHERE subject LIKE :p"), {"p": f"%-{tag}"})
                    await s.commit()

            # свои шаблоны
            created = await client.post("/api/grades/my-templates", json={"name": "Своя", "components": COMPONENTS})
            assert created.status_code == 201
            mine = created.json()
            assert mine["components"][1]["items"]["count"] == 8
            assert (await client.post("/api/grades/my-templates", json={"name": "СВОЯ", "components": COMPONENTS})).status_code == 409
            bad = dict(COMPONENTS[0], weight=0.5)
            assert (await client.post("/api/grades/my-templates", json={"name": "Плохая", "components": [bad]})).status_code == 422
            assert [t["id"] for t in (await client.get("/api/grades/my-templates")).json()] == [mine["id"]]

            renamed = await client.patch(f"/api/grades/my-templates/{mine['id']}", json={"name": "Новая"})
            assert renamed.status_code == 200 and renamed.json()["name"] == "Новая"

            # оценки: пока ничего не вводили
            assert (await client.get(f"/api/grades/scores/{mine['id']}")).json() == {"scores": {}, "target": None}
            scores = {"activity": 8, "labs": [9, 7.5, None], "exam": None}
            put = await client.put(f"/api/grades/scores/{mine['id']}", json={"scores": scores, "target": 8})
            assert put.status_code == 200
            assert (await client.get(f"/api/grades/scores/{mine['id']}")).json() == {"scores": scores, "target": 8}
            # PUT заменяет целиком
            await client.put(f"/api/grades/scores/{mine['id']}", json={"scores": {"homework": 10}})
            assert (await client.get(f"/api/grades/scores/{mine['id']}")).json() == {"scores": {"homework": 10}, "target": None}
            for body in ({"scores": {"nope": 5}},             # нет такого компонента
                         {"scores": {"activity": [5]}},       # список у одиночной оценки
                         {"scores": {"labs": 5}},             # число у составной
                         {"scores": {"labs": [1] * 9}},       # больше count
                         {"scores": {"activity": 11}},        # вне шкалы
                         {"scores": {}, "target": 11}):
                assert (await client.put(f"/api/grades/scores/{mine['id']}", json=body)).status_code == 422, body

            # чужой пользователь не видит ни свой шаблон, ни его оценки
            act_as(client, bob)
            assert (await client.get("/api/grades/my-templates")).json() == []
            assert (await client.get(f"/api/grades/scores/{mine['id']}")).status_code == 404
            assert (await client.put(f"/api/grades/scores/{mine['id']}", json={"scores": {}})).status_code == 404
            assert (await client.patch(f"/api/grades/my-templates/{mine['id']}", json={"name": "x"})).status_code == 404
            assert (await client.delete(f"/api/grades/my-templates/{mine['id']}")).status_code == 404
            assert (await client.get(f"/api/grades/scores/{uuid.uuid4()}")).status_code == 404

            # удаление шаблона убирает и оценки по нему
            act_as(client, alice)
            assert (await client.delete(f"/api/grades/my-templates/{mine['id']}")).status_code == 204
            async with sessions() as s:
                left = (await s.execute(text("SELECT count(*) FROM library.grade_user_scores WHERE template_id = :t"),
                                        {"t": uuid.UUID(mine["id"])})).scalar_one()
            assert left == 0

    run(scenario())
