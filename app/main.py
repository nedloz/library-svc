import asyncio
import logging
import mimetypes
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator, List
from uuid import UUID
from urllib.parse import quote

import anyio.to_thread
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import engine, AsyncSessionLocal, check_db_connection, get_db
from app.schemas import DocumentDetailSchema, TopicSchema
from app.services import (
    PreviewTimeout,
    PreviewTooLarge,
    build_document_detail,
    build_library_tree,
    detect_preview_mode,
    download_minio_object,
    get_content_type,
    get_minio_client,
    get_preview_filename,
    is_office_document,
    convert_office_bytes_to_pdf,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("library_service")

library_tree_cache = []

# Конвертация Office → PDF запускается внешним LibreOffice и требует заметной памяти и CPU.
# Без ограничения параллелизма несколько одновременных предпросмотров просто съедят машину,
# поэтому одновременно выполняется не больше PREVIEW_MAX_CONCURRENCY конвертаций, а остальные
# ждут своей очереди.
PREVIEW_MAX_CONCURRENCY = int(os.getenv("PREVIEW_MAX_CONCURRENCY", "2"))
_preview_semaphore = asyncio.Semaphore(PREVIEW_MAX_CONCURRENCY)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("⏳ Просыпаемся, проверяем базу данных...")
    is_connected = await check_db_connection()

    if is_connected:
        try:
            logger.info("⏳ Собираем дерево библиотеки...")
            async with AsyncSessionLocal() as session:
                global library_tree_cache
                library_tree_cache = await build_library_tree(session)
            logger.info("✅ Дерево библиотеки успешно собрано и ждет пользователей!")
        except Exception as e:
            logger.error(f"❌ Ошибка при сборке дерева: {e}")
    else:
        logger.warning("⚠️ База данных недоступна, пока работаем с пустым кэшем.")

    yield

    logger.info("⏳ Засыпаем, очищаем ресурсы...")
    library_tree_cache.clear()
    await engine.dispose()
    logger.info("✅ Доброй ночи! Соединения закрыты.")


app = FastAPI(lifespan=lifespan, title="Library Microservice")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/library/tree", response_model=List[TopicSchema])
async def get_tree():
    if not library_tree_cache:
        raise HTTPException(status_code=503, detail="Библиотека еще загружается или недоступна")
    return library_tree_cache


@app.post("/library/refresh")
async def refresh_tree(db: AsyncSession = Depends(get_db)):
    global library_tree_cache
    library_tree_cache = await build_library_tree(db)
    return {"status": "success", "message": "Дерево успешно обновлено!"}


@app.get("/documents/{document_id}", response_model=DocumentDetailSchema)
@app.get("/api/documents/{document_id}", response_model=DocumentDetailSchema)
async def get_document(document_id: UUID, db: AsyncSession = Depends(get_db)):
    document = await build_document_detail(db, document_id)
    if not document:
        raise HTTPException(status_code=404, detail="Документ не найден")
    return document

def _build_content_disposition(disposition: str, filename: str) -> str:
    ascii_fallback = filename.encode("ascii", "ignore").decode("ascii").strip() or "document"
    ascii_fallback = ascii_fallback.replace("\\", "_").replace('"', "_")
    encoded_filename = quote(filename, safe="")
    return f'{disposition}; filename="{ascii_fallback}"; filename*=UTF-8\'\'{encoded_filename}'

async def _stream_minio_object(bucket_name: str, object_key: str, chunk_size: int = 1024 * 1024) -> AsyncIterator[bytes]:
    client = get_minio_client()
    if not client:
        raise HTTPException(status_code=500, detail="MinIO не настроен")

    response = None
    try:
        response = client.get_object(bucket_name, object_key)
        while True:
            chunk = response.read(chunk_size)
            if not chunk:
                break
            yield chunk
    except Exception as exc:
        logger.exception("Ошибка при чтении объекта из MinIO: %s", exc)
        return
    finally:
        if response is not None:
            response.close()
            response.release_conn()


async def _build_streaming_response(document_id: UUID, db: AsyncSession, disposition: str) -> StreamingResponse:
    document = await build_document_detail(db, document_id)
    if not document:
        raise HTTPException(status_code=404, detail="Документ не найден")

    bucket_name = document.get("bucket_name")
    object_key = document.get("object_key")
    filename = document.get("filename") or f"document-{document_id}"
    content_type = document.get("content_type") or mimetypes.guess_type(filename)[0] or "application/octet-stream"

    if not bucket_name or not object_key:
        raise HTTPException(status_code=500, detail="Для документа не настроено хранилище в MinIO")

    headers = {"Content-Disposition": _build_content_disposition(disposition, filename)}
    return StreamingResponse(
        _stream_minio_object(bucket_name, object_key),
        media_type=content_type,
        headers=headers,
    )


@app.get("/documents/{document_id}/preview")
@app.get("/api/documents/{document_id}/preview")
async def preview_document(document_id: UUID, db: AsyncSession = Depends(get_db)):
    document = await build_document_detail(db, document_id)
    if not document:
        raise HTTPException(status_code=404, detail="Документ не найден")

    bucket_name = document.get("bucket_name")
    object_key = document.get("object_key")
    filename = document.get("filename") or f"document-{document_id}"
    content_type = document.get("content_type") or get_content_type(filename)
    preview_mode = detect_preview_mode(content_type, filename)

    if not bucket_name or not object_key:
        raise HTTPException(status_code=500, detail="Для документа не настроено хранилище в MinIO")

    if is_office_document(content_type, filename):
        # И скачивание, и конвертация — блокирующие операции. Раньше они вызывались прямо
        # здесь, в async-обработчике, и на время конвертации event loop вставал: сервис
        # переставал отвечать вообще всем. Теперь обе уходят в пул потоков.
        try:
            async with _preview_semaphore:
                source_bytes = await anyio.to_thread.run_sync(
                    download_minio_object, bucket_name, object_key
                )
                pdf_bytes, pdf_filename = await anyio.to_thread.run_sync(
                    convert_office_bytes_to_pdf, source_bytes, filename
                )
        except PreviewTooLarge as exc:
            logger.warning("Файл слишком велик для предпросмотра: %s", exc)
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        except PreviewTimeout as exc:
            logger.error("Конвертация в PDF не уложилась в таймаут: %s", exc)
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        except RuntimeError as exc:
            logger.exception("Не удалось конвертировать документ в PDF")
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("Не удалось получить документ из MinIO для предпросмотра")
            raise HTTPException(status_code=404, detail=f"Не удалось получить файл из MinIO: {exc}") from exc

        headers = {"Content-Disposition": _build_content_disposition("inline", pdf_filename)}

        return StreamingResponse(iter([pdf_bytes]), media_type="application/pdf", headers=headers)

    if preview_mode in {"pdf", "image", "text", "audio", "video"}:
        headers = {"Content-Disposition": _build_content_disposition("inline", filename)}
        return StreamingResponse(
            _stream_minio_object(bucket_name, object_key),
            media_type=get_content_type(filename, content_type),
            headers=headers,
        )

    raise HTTPException(status_code=415, detail="Предпросмотр для этого типа файла не поддерживается")


@app.get("/documents/{document_id}/download")
@app.get("/api/documents/{document_id}/download")
async def download_document(document_id: UUID, db: AsyncSession = Depends(get_db)):
    return await _build_streaming_response(document_id, db, "attachment")
