import asyncio
import logging
import mimetypes
import os
import tempfile
from contextlib import asynccontextmanager
from typing import List
from urllib.parse import quote
from uuid import UUID

import anyio.to_thread
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

from app.auth import get_gateway_user_id
from app.database import (
    engine,
    AsyncSessionLocal,
    check_db_connection,
    get_db,
)
from app.schemas import DocumentDetailSchema, TopicSchema
from app.services import (
    PreviewTimeout,
    PreviewTooLarge,
    build_document_detail,
    build_document_file_info,
    build_library_tree,
    detect_preview_mode,
    download_minio_object,
    get_content_type,
    get_minio_client,
    is_office_document,
    convert_office_bytes_to_pdf,
)


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s - %(name)s - "
        "%(levelname)s - %(message)s"
    ),
)

logger = logging.getLogger(
    "library_service"
)


# =========================================================
# LIBRARY CACHE
# =========================================================

library_tree_cache = []


# =========================================================
# OFFICE PREVIEW LIMIT
# =========================================================

# Конвертация Office -> PDF запускается через LibreOffice
# и требует CPU/RAM.
#
# Ограничиваем количество одновременно выполняющихся
# конвертаций.
PREVIEW_MAX_CONCURRENCY = max(
    1,
    int(
        os.getenv(
            "PREVIEW_MAX_CONCURRENCY",
            "2",
        )
    ),
)

_preview_semaphore = asyncio.Semaphore(
    PREVIEW_MAX_CONCURRENCY
)


# =========================================================
# MINIO DOWNLOAD LIMIT
# =========================================================

# Полная загрузка объекта во временный файл необходима,
# чтобы ошибка MinIO произошла ДО отправки HTTP response.
#
# Ограничиваем количество одновременных загрузок,
# чтобы несколько больших файлов не забили /tmp.
MINIO_DOWNLOAD_MAX_CONCURRENCY = max(
    1,
    int(
        os.getenv(
            "MINIO_DOWNLOAD_MAX_CONCURRENCY",
            "4",
        )
    ),
)

_download_semaphore = asyncio.Semaphore(
    MINIO_DOWNLOAD_MAX_CONCURRENCY
)


# =========================================================
# LIFESPAN
# =========================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        "⏳ Просыпаемся, проверяем базу данных..."
    )

    is_connected = await check_db_connection()

    if is_connected:
        try:
            logger.info(
                "⏳ Собираем дерево библиотеки..."
            )

            async with AsyncSessionLocal() as session:
                global library_tree_cache

                library_tree_cache = (
                    await build_library_tree(
                        session
                    )
                )

            logger.info(
                "✅ Дерево библиотеки успешно "
                "собрано и ждет пользователей!"
            )

        except Exception as exc:
            logger.error(
                "❌ Ошибка при сборке дерева: %s",
                exc,
            )

    else:
        logger.warning(
            "⚠️ База данных недоступна, "
            "пока работаем с пустым кэшем."
        )

    yield

    logger.info(
        "⏳ Засыпаем, очищаем ресурсы..."
    )

    library_tree_cache.clear()

    await engine.dispose()

    logger.info(
        "✅ Доброй ночи! Соединения закрыты."
    )


# =========================================================
# FASTAPI
# =========================================================

app = FastAPI(
    lifespan=lifespan,
    title="Library Microservice",
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =========================================================
# LIBRARY TREE
# =========================================================

@app.get(
    "/library/tree",
    response_model=List[TopicSchema],
)
async def get_tree(
    _user_id: UUID = Depends(
        get_gateway_user_id
    ),
):
    """
    Возвращает дерево библиотеки.

    Требуется авторизация через gateway.
    """

    if not library_tree_cache:
        raise HTTPException(
            status_code=503,
            detail=(
                "Библиотека еще загружается "
                "или недоступна"
            ),
        )

    return library_tree_cache


# =========================================================
# LIBRARY REFRESH
# =========================================================

@app.post("/library/refresh")
async def refresh_tree(
    db: AsyncSession = Depends(get_db),
    _user_id: UUID = Depends(
        get_gateway_user_id
    ),
):
    """
    Полная перестройка дерева библиотеки.

    Endpoint защищён той же авторизацией,
    что и остальные library API.
    """

    global library_tree_cache

    library_tree_cache = (
        await build_library_tree(db)
    )

    return {
        "status": "success",
        "message": (
            "Дерево успешно обновлено!"
        ),
    }


# =========================================================
# DOCUMENT DETAILS
# =========================================================

@app.get(
    "/api/documents/{document_id}",
    response_model=DocumentDetailSchema,
)
async def get_document(
    document_id: UUID,
    db: AsyncSession = Depends(get_db),
    _user_id: UUID = Depends(
        get_gateway_user_id
    ),
):
    """
    Возвращает информацию о документе.
    """

    document = await build_document_detail(
        db,
        document_id,
    )

    if not document:
        raise HTTPException(
            status_code=404,
            detail="Документ не найден",
        )

    return document


# =========================================================
# CONTENT DISPOSITION
# =========================================================

def _build_content_disposition(
    disposition: str,
    filename: str,
) -> str:
    """
    Формирует Content-Disposition
    с поддержкой Unicode filename.
    """

    ascii_fallback = (
        filename
        .encode(
            "ascii",
            "ignore",
        )
        .decode("ascii")
        .strip()
        or "document"
    )

    ascii_fallback = (
        ascii_fallback
        .replace("\\", "_")
        .replace('"', "_")
    )

    encoded_filename = quote(
        filename,
        safe="",
    )

    return (
        f'{disposition}; '
        f'filename="{ascii_fallback}"; '
        f"filename*=UTF-8''{encoded_filename}"
    )


# =========================================================
# TEMP FILE CLEANUP
# =========================================================

def _remove_temp_file(
    path: str,
) -> None:
    """
    Удаляет временный файл.

    Ошибка удаления не должна ломать HTTP response,
    поэтому только логируем её.
    """

    try:
        os.remove(path)

    except FileNotFoundError:
        pass

    except OSError as exc:
        logger.warning(
            "Не удалось удалить временный файл %s: %s",
            path,
            exc,
        )


# =========================================================
# VERIFIED MINIO DOWNLOAD
# =========================================================

def _download_minio_object_to_file(
    bucket_name: str,
    object_key: str,
    destination_path: str,
    chunk_size: int = 1024 * 1024,
) -> int:
    """
    Полностью скачивает объект из MinIO
    во временный файл ДО создания HTTP response.

    Дополнительные проверки:

    1. stat_object() получает ожидаемый размер;
    2. stat_object() получает ETag;
    3. get_object() запрашивается с match_etag;
    4. подсчитывается фактически полученное количество байт;
    5. размер итогового файла дополнительно проверяется.

    Если MinIO оборвёт загрузку, исключение возникнет
    до начала HTTP-ответа.

    Возвращает размер успешно сохранённого файла.
    """

    client = get_minio_client()

    if not client:
        raise RuntimeError(
            "MinIO не настроен"
        )

    # ---------------------------------------------------------
    # 1. Получаем метаданные объекта
    # ---------------------------------------------------------

    stat = client.stat_object(
        bucket_name=bucket_name,
        object_name=object_key,
    )

    expected_size = stat.size
    expected_etag = stat.etag

    response = None
    bytes_written = 0

    try:
        # -----------------------------------------------------
        # 2. Получаем именно тот объект,
        #    для которого получили stat
        # -----------------------------------------------------

        response = client.get_object(
            bucket_name=bucket_name,
            object_name=object_key,
            match_etag=expected_etag,
        )

        # -----------------------------------------------------
        # 3. Записываем данные во временный файл
        # -----------------------------------------------------

        with open(
            destination_path,
            "wb",
        ) as output_file:

            while True:
                chunk = response.read(
                    chunk_size
                )

                if not chunk:
                    break

                output_file.write(chunk)

                bytes_written += len(chunk)

        # -----------------------------------------------------
        # 4. Проверяем количество полученных байт
        # -----------------------------------------------------

        if bytes_written != expected_size:
            raise IOError(
                "MinIO object was downloaded incompletely: "
                f"expected {expected_size} bytes, "
                f"received {bytes_written} bytes"
            )

        # -----------------------------------------------------
        # 5. Проверяем размер физического файла
        # -----------------------------------------------------

        actual_size = os.path.getsize(
            destination_path
        )

        if actual_size != expected_size:
            raise IOError(
                "Temporary file size does not match "
                "MinIO object: "
                f"expected {expected_size} bytes, "
                f"got {actual_size} bytes"
            )

        return actual_size

    finally:
        if response is not None:
            response.close()
            response.release_conn()


# =========================================================
# PREPARE TEMPORARY MINIO FILE
# =========================================================

async def _prepare_minio_file(
    bucket_name: str,
    object_key: str,
) -> str:
    """
    Полностью загружает объект из MinIO
    до создания HTTP response.

    При ошибке временный файл удаляется.
    """

    fd, temp_path = tempfile.mkstemp(
        prefix="library-minio-",
        suffix=".download",
    )

    os.close(fd)

    try:
        async with _download_semaphore:
            await anyio.to_thread.run_sync(
                _download_minio_object_to_file,
                bucket_name,
                object_key,
                temp_path,
            )

        return temp_path

    except BaseException:
        _remove_temp_file(
            temp_path
        )
        raise


# =========================================================
# TEMPORARY FILE RESPONSE
# =========================================================

class TemporaryFileResponse(FileResponse):
    """
    FileResponse с гарантированным удалением
    временного файла после завершения или
    прерывания HTTP-ответа.

    BackgroundTask обычного FileResponse может
    не выполниться при некоторых сценариях disconnect,
    поэтому cleanup дополнительно выполняется
    в finally.
    """

    def __init__(
        self,
        path: str,
        *,
        media_type: str,
        headers: dict[str, str],
        cleanup_path: str,
    ):
        super().__init__(
            path=path,
            media_type=media_type,
            headers=headers,
        )

        self._cleanup_path = cleanup_path

    async def __call__(
        self,
        scope,
        receive,
        send,
    ):
        try:
            await super().__call__(
                scope,
                receive,
                send,
            )
        finally:
            _remove_temp_file(
                self._cleanup_path
            )


# =========================================================
# BUILD FILE RESPONSE FROM MINIO
# =========================================================

async def _build_file_response_from_minio(
    bucket_name: str,
    object_key: str,
    filename: str,
    content_type: str,
    disposition: str,
) -> FileResponse:
    """
    Загружает полный объект из MinIO
    и только после этого создаёт HTTP response.
    """

    try:
        temp_path = await _prepare_minio_file(
            bucket_name,
            object_key,
        )

    except Exception as exc:
        logger.exception(
            "Ошибка загрузки объекта %s/%s из MinIO",
            bucket_name,
            object_key,
        )

        raise HTTPException(
            status_code=502,
            detail=(
                "Не удалось получить файл из MinIO"
            ),
        ) from exc

    headers = {
        "Content-Disposition": (
            _build_content_disposition(
                disposition,
                filename,
            )
        )
    }

    try:
        return TemporaryFileResponse(
            path=temp_path,
            media_type=content_type,
            headers=headers,
            cleanup_path=temp_path,
        )

    except Exception:
        _remove_temp_file(
            temp_path
        )
        raise


# =========================================================
# DOCUMENT PREVIEW
# =========================================================

@app.get(
    "/api/documents/{document_id}/preview"
)
async def preview_document(
    document_id: UUID,
    db: AsyncSession = Depends(get_db),
    _user_id: UUID = Depends(
        get_gateway_user_id
    ),
):
    """
    Предпросмотр документа.
    """

    document = await build_document_file_info(
        db,
        document_id,
    )

    if not document:
        raise HTTPException(
            status_code=404,
            detail="Документ не найден",
        )

    bucket_name = document.get(
        "bucket_name"
    )

    object_key = document.get(
        "object_key"
    )

    filename = (
        document.get("filename")
        or f"document-{document_id}"
    )

    content_type = (
        document.get("content_type")
        or get_content_type(filename)
    )

    preview_mode = detect_preview_mode(
        content_type,
        filename,
    )

    if not bucket_name or not object_key:
        raise HTTPException(
            status_code=500,
            detail=(
                "Для документа не настроено "
                "хранилище в MinIO"
            ),
        )

    # =========================================================
    # OFFICE → PDF
    # =========================================================

    if is_office_document(
        content_type,
        filename,
    ):
        # И скачивание, и конвертация — блокирующие операции.
        # Обе выполняются в пуле потоков.
        try:
            async with _preview_semaphore:

                source_bytes = (
                    await anyio.to_thread.run_sync(
                        download_minio_object,
                        bucket_name,
                        object_key,
                    )
                )

                pdf_bytes, pdf_filename = (
                    await anyio.to_thread.run_sync(
                        convert_office_bytes_to_pdf,
                        source_bytes,
                        filename,
                    )
                )

        except PreviewTooLarge as exc:
            logger.warning(
                "Файл слишком велик "
                "для предпросмотра: %s",
                exc,
            )

            raise HTTPException(
                status_code=413,
                detail=str(exc),
            ) from exc

        except PreviewTimeout as exc:
            logger.error(
                "Конвертация в PDF "
                "не уложилась в таймаут: %s",
                exc,
            )

            raise HTTPException(
                status_code=504,
                detail=str(exc),
            ) from exc

        except RuntimeError as exc:
            logger.exception(
                "Не удалось конвертировать "
                "документ в PDF"
            )

            raise HTTPException(
                status_code=500,
                detail=str(exc),
            ) from exc

        except Exception as exc:
            logger.exception(
                "Не удалось получить документ "
                "из MinIO для предпросмотра"
            )

            raise HTTPException(
                status_code=502,
                detail=(
                    "Не удалось получить файл "
                    "из MinIO"
                ),
            ) from exc

        headers = {
            "Content-Disposition": (
                _build_content_disposition(
                    "inline",
                    pdf_filename,
                )
            )
        }

        return StreamingResponse(
            iter([pdf_bytes]),
            media_type="application/pdf",
            headers=headers,
        )

    # =========================================================
    # PDF / IMAGE / TEXT / AUDIO / VIDEO
    # =========================================================

    if preview_mode in {
        "pdf",
        "image",
        "text",
        "audio",
        "video",
    }:
        return await _build_file_response_from_minio(
            bucket_name,
            object_key,
            filename,
            get_content_type(
                filename,
                content_type,
            ),
            "inline",
        )

    raise HTTPException(
        status_code=415,
        detail=(
            "Предпросмотр для этого типа "
            "файла не поддерживается"
        ),
    )


# =========================================================
# DOCUMENT DOWNLOAD
# =========================================================

@app.get(
    "/api/documents/{document_id}/download"
)
async def download_document(
    document_id: UUID,
    db: AsyncSession = Depends(get_db),
    _user_id: UUID = Depends(
        get_gateway_user_id
    ),
):
    """
    Скачивание оригинального файла.

    Важный момент:
    HTTP response создаётся только после
    успешного полного получения объекта из MinIO.
    """

    document = await build_document_file_info(
        db,
        document_id,
    )

    if not document:
        raise HTTPException(
            status_code=404,
            detail="Документ не найден",
        )

    bucket_name = document.get(
        "bucket_name"
    )

    object_key = document.get(
        "object_key"
    )

    filename = (
        document.get("filename")
        or f"document-{document_id}"
    )

    content_type = (
        document.get("content_type")
        or mimetypes.guess_type(filename)[0]
        or "application/octet-stream"
    )

    if not bucket_name or not object_key:
        raise HTTPException(
            status_code=500,
            detail=(
                "Для документа не настроено "
                "хранилище в MinIO"
            ),
        )

    return await _build_file_response_from_minio(
        bucket_name,
        object_key,
        filename,
        content_type,
        "attachment",
    )
