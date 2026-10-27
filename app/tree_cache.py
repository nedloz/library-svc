"""Кэш дерева библиотеки: сам восстанавливается после неудачного старта и сам обновляется.

Раньше дерево собиралось один раз при старте. Если база к этому моменту была недоступна,
кэш оставался пустым и /library/tree отвечал 503, пока кто-то не вызовет POST /library/refresh.
А правки документов и тем (админка, импорт, crawler) в кэш не попадали вообще.

Теперь фоновая задача раз в TREE_REFRESH_SEC сверяет отпечаток данных, из которых строится дерево
(названия, темы, типы, связи), и пересобирает кэш только если он изменился. Пока кэш не собран,
повторы идут чаще и с растущей паузой. POST /library/refresh по-прежнему пересобирает принудительно.
"""

import asyncio
import logging
import os
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.metrics import TREE_REFRESH
from app.services import build_library_tree, library_tree_fingerprint

logger = logging.getLogger("library_service")

TREE_REFRESH_SEC = int(os.getenv("TREE_REFRESH_SEC", "60"))
TREE_RETRY_BASE_SEC = int(os.getenv("TREE_RETRY_BASE_SEC", "2"))
TREE_RETRY_MAX_SEC = int(os.getenv("TREE_RETRY_MAX_SEC", "30"))


class TreeCache:
    def __init__(self) -> None:
        self.tree: list = []
        self.fingerprint: Optional[str] = None
        self.loaded = False

    async def refresh(self, db: AsyncSession, *, force: bool = False) -> bool:
        """Пересобирает дерево, если данные изменились (или force). True — кэш обновлён."""
        fingerprint = await library_tree_fingerprint(db)
        if self.loaded and not force and fingerprint == self.fingerprint:
            TREE_REFRESH.labels(result="unchanged").inc()
            return False
        tree = await build_library_tree(db)
        # Присваивание целиком: читатели видят либо старое дерево, либо новое, но не половину.
        self.tree, self.fingerprint, self.loaded = tree, fingerprint, True
        TREE_REFRESH.labels(result="rebuilt").inc()
        return True

    def clear(self) -> None:
        self.tree, self.fingerprint, self.loaded = [], None, False


async def refresh_loop(cache: TreeCache, session_factory, *, first_delay: float = 0, sleep=asyncio.sleep) -> None:
    """Бесконечный цикл обновления; завершается только отменой задачи.

    first_delay — пауза перед первой проверкой: после удачной стартовой сборки ждём полный
    интервал, после неудачной повторяем быстро.
    """
    failures = 0
    await sleep(first_delay)
    while True:
        try:
            async with session_factory() as session:
                if await cache.refresh(session):
                    logger.info("Дерево библиотеки обновлено")
            failures = 0
            delay = TREE_REFRESH_SEC
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failures += 1
            TREE_REFRESH.labels(result="error").inc()
            # Пока кэша нет, ждём недолго и с нарастанием; уже собранный кэш не трогаем.
            delay = min(TREE_RETRY_BASE_SEC * 2 ** (failures - 1), TREE_RETRY_MAX_SEC) if not cache.loaded else TREE_REFRESH_SEC
            logger.warning("Не удалось обновить дерево библиотеки (попытка %s, повтор через %s с): %s", failures, delay, exc)
        await sleep(delay)
