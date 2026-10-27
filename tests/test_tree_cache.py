import asyncio
import os
from contextlib import asynccontextmanager

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")

import pytest  # noqa: E402
from prometheus_client import REGISTRY  # noqa: E402

from app import main, tree_cache as tc  # noqa: E402


def refreshes(result: str) -> float:
    return REGISTRY.get_sample_value("pochemuchnik_library_tree_refresh_total", {"result": result}) or 0.0


class FakeDb:
    """Состояние «базы»: отпечаток и дерево, которые можно менять между проверками."""

    def __init__(self):
        self.fingerprint = "v1"
        self.tree = [{"id": 1, "name": "Учёба", "documents": [], "subtopics": []}]
        self.builds = 0
        self.down = False

    def install(self, monkeypatch):
        async def fingerprint(_session):
            if self.down:
                raise ConnectionError("база недоступна")
            return self.fingerprint

        async def build(_session):
            self.builds += 1
            return self.tree

        monkeypatch.setattr(tc, "library_tree_fingerprint", fingerprint)
        monkeypatch.setattr(tc, "build_library_tree", build)

        @asynccontextmanager
        async def session_factory():
            yield object()

        return session_factory


def test_refresh_rebuilds_only_when_data_changed(monkeypatch):
    db = FakeDb()
    db.install(monkeypatch)
    cache = tc.TreeCache()

    assert asyncio.run(cache.refresh(object())) is True
    assert asyncio.run(cache.refresh(object())) is False
    assert db.builds == 1

    db.fingerprint = "v2"
    db.tree = []
    assert asyncio.run(cache.refresh(object())) is True
    assert db.builds == 2 and cache.tree == []


def test_force_rebuilds_unchanged_tree(monkeypatch):
    db = FakeDb()
    db.install(monkeypatch)
    cache = tc.TreeCache()
    asyncio.run(cache.refresh(object()))
    assert asyncio.run(cache.refresh(object(), force=True)) is True
    assert db.builds == 2


def test_cache_recovers_after_failed_start(monkeypatch):
    """IVHR-22: старт при недоступной базе не оставляет кэш пустым навсегда."""
    db = FakeDb()
    db.down = True
    factory = db.install(monkeypatch)
    cache = tc.TreeCache()
    delays = []
    errors_before = refreshes("error")

    async def fake_sleep(delay):
        delays.append(delay)
        if len(delays) == 4:
            db.down = False  # база поднялась
        if len(delays) == 6:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(tc.refresh_loop(cache, factory, first_delay=2, sleep=fake_sleep))

    assert cache.loaded and cache.tree == db.tree
    assert refreshes("error") - errors_before == 3
    # быстрые повторы с ростом паузы, после успеха — обычный интервал
    assert delays[:4] == [2, 2, 4, 8]
    assert delays[4] == tc.TREE_REFRESH_SEC


def test_loop_picks_up_changes_and_keeps_old_tree_on_error(monkeypatch):
    db = FakeDb()
    factory = db.install(monkeypatch)
    cache = tc.TreeCache()
    asyncio.run(cache.refresh(object()))
    old_tree = cache.tree
    steps = []

    async def fake_sleep(_delay):
        steps.append(1)
        if len(steps) == 2:   # после первой проверки: данные изменились
            db.fingerprint, db.tree = "v2", [{"id": 2, "name": "Новая тема", "documents": [], "subtopics": []}]
        if len(steps) == 3:   # после второй: база пропала
            db.down = True
        if len(steps) == 5:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(tc.refresh_loop(cache, factory, sleep=fake_sleep))

    assert old_tree != cache.tree and cache.tree[0]["name"] == "Новая тема"
    assert cache.loaded  # сбой базы не стирает собранное дерево


def test_empty_library_is_a_valid_answer(monkeypatch):
    db = FakeDb()
    db.tree = []
    db.install(monkeypatch)
    cache = tc.TreeCache()
    monkeypatch.setattr(main, "tree_cache", cache)

    with pytest.raises(main.HTTPException) as exc:
        asyncio.run(main.get_tree(_user_id=None))
    assert exc.value.status_code == 503   # пока не собрано

    asyncio.run(cache.refresh(object()))
    assert asyncio.run(main.get_tree(_user_id=None)) == []   # собрано, тем нет
