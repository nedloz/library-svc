"""Метрики Prometheus: HTTP-метрики приложения и бизнес-метрики сервиса.

/metrics отдаёт только prometheus (политика прокси, infra/mtls/*.conf); наружу через шлюз его
нет. HTTP-метрики группируются по шаблону маршрута, а не по реальному пути, поэтому число
значений метки handler ограничено числом маршрутов.
"""

from fastapi import FastAPI
from prometheus_client import Counter, Gauge, Histogram
from prometheus_fastapi_instrumentator import Instrumentator


def setup_metrics(app: FastAPI) -> None:
    """Подключает сбор HTTP-метрик и маршрут GET /metrics."""
    instrumentator = Instrumentator(
        should_group_status_codes=True,
        should_ignore_untemplated=True,
        should_instrument_requests_inprogress=True,
        excluded_handlers=["/metrics"],
    )
    instrumentator.instrument(app).expose(app, include_in_schema=False, should_gzip=False)


PREVIEW_REQUESTS = Counter(
    "pochemuchnik_preview_requests_total",
    "Запросы предпросмотра по режиму",
    ["mode"],  # office_pdf | pdf | image | text | audio | video | download
)
PREVIEW_CACHE_REQUESTS = Counter(
    "pochemuchnik_preview_cache_requests_total",
    "Обращения к кэшу конвертации Office в PDF",
    ["outcome"],  # hit | miss | disabled
)
PREVIEW_CACHE_EVICTIONS = Counter(
    "pochemuchnik_preview_cache_evictions_total",
    "PDF, удалённые из кэша по лимиту размера",
)
PREVIEW_CONVERT_DURATION = Histogram(
    "pochemuchnik_preview_convert_duration_seconds",
    "Время работы LibreOffice над одним документом",
    buckets=(0.5, 1, 2, 4, 8, 16, 32, 64, 128),
)
DOWNLOADS = Counter(
    "pochemuchnik_downloads_total",
    "Скачивания оригиналов документов",
)
DOCUMENTS = Gauge(
    "pochemuchnik_documents",
    "Документы библиотеки по статусу публикации",
    ["status"],  # draft | published | archived
)
