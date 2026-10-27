import hashlib
import mimetypes
import os
import shutil
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional
from uuid import UUID

from minio import Minio
from minio.error import S3Error
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app import s3_tls
from app.metrics import DOCUMENTS, PREVIEW_CACHE_EVICTIONS, PREVIEW_CONVERT_DURATION
from app.models import Topic, Document, DocumentRelation

_OBJECT_KEY_FIELDS = (
    "object_key",
    "object_name",
    "minio_key",
    "file_key",
    "storage_key",
    "s3_key",
    "blob_key",
    "path",
    "file_path",
    "storage_path",
)
_BUCKET_FIELDS = ("bucket_name", "bucket", "minio_bucket", "storage_bucket", "s3_bucket")
_FILENAME_FIELDS = ("filename", "file_name", "original_filename", "name")
_DESCRIPTION_FIELDS = ("description", "summary", "annotation")
_SIZE_FIELDS = ("size_bytes", "file_size", "size", "content_length")

# Лимиты предпросмотра Office-документов. Конвертация выполняется внешним LibreOffice и
# может занимать секунды или не завершиться вовсе, поэтому нужен и таймаут, и потолок по
# размеру исходника: файл целиком читается в память перед конвертацией.
PREVIEW_CONVERT_TIMEOUT_SEC = int(os.getenv("PREVIEW_CONVERT_TIMEOUT_SEC", "120"))
PREVIEW_MAX_SOURCE_BYTES = int(os.getenv("PREVIEW_MAX_SOURCE_BYTES", str(50 * 1024 * 1024)))

# Кэш результатов конвертации. Без него каждый предпросмотр одного и того же .docx заново
# запускал LibreOffice (секунды CPU и память под семафором). Файлы лежат на диске контейнера:
# после его пересоздания кэш пустеет и наполняется заново, а общего хранилища для этого не
# заводим. PREVIEW_CACHE_MAX_BYTES=0 выключает кэш.
PREVIEW_CACHE_DIR = Path(os.getenv("PREVIEW_CACHE_DIR", "/tmp/library-preview-cache"))
PREVIEW_CACHE_MAX_BYTES = int(os.getenv("PREVIEW_CACHE_MAX_BYTES", str(512 * 1024 * 1024)))

OFFICE_EXTENSIONS = {".doc", ".docx", ".rtf", ".odt"}
TEXT_EXTENSIONS = {".txt", ".md", ".html", ".csv", ".json", ".xml", ".log"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}


def get_minio_client() -> Optional[Minio]:
    # Клиент кешируется до смены сертификата: суточный сертификат обновляется на месте.
    return _minio_client(s3_tls.cert_stamp())


@lru_cache(maxsize=2)
def _minio_client(_cert_stamp: int) -> Optional[Minio]:
    endpoint = os.getenv("MINIO_ENDPOINT") or os.getenv("S3_ENDPOINT")
    access_key = os.getenv("MINIO_ACCESS_KEY") or os.getenv("MINIO_ROOT_USER") or os.getenv("AWS_ACCESS_KEY_ID")
    secret_key = os.getenv("MINIO_SECRET_KEY") or os.getenv("MINIO_ROOT_PASSWORD") or os.getenv("AWS_SECRET_ACCESS_KEY")

    if not endpoint or not access_key or not secret_key:
        return None

    secure = str(os.getenv("MINIO_SECURE", "false")).lower() in {"1", "true", "yes", "on"}
    return Minio(
        endpoint, access_key=access_key, secret_key=secret_key, secure=secure, http_client=s3_tls.urllib3_pool()
    )


async def build_library_tree(db: AsyncSession) -> List[dict]:
    topics_res = await db.execute(select(Topic))
    docs_res = await db.execute(select(Document))
    rels_res = await db.execute(select(DocumentRelation))

    topics = topics_res.scalars().all()
    documents = docs_res.scalars().all()
    relations = rels_res.scalars().all()

    topic_map = {t.id: {"id": t.id, "name": t.name, "documents": [], "subtopics": []} for t in topics}

    doc_map = {}
    for d in documents:
        doc_map[d.id] = {
            "id": d.id,
            "title": d.title,
            "content_type": d.content_type,
            "view_url": f"/file.html?id={d.id}",
            "download_url": f"/api/documents/{d.id}/download",
            "related_documents": [],
        }

    for r in relations:
        if r.from_document_id in doc_map and r.to_document_id in doc_map:
            related_doc = doc_map[r.to_document_id]
            doc_map[r.from_document_id]["related_documents"].append(
                {
                    "id": related_doc["id"],
                    "title": related_doc["title"],
                    "relation_type": r.relation_type,
                    "label": r.label,
                    "view_url": related_doc["view_url"],
                    "download_url": related_doc["download_url"],
                }
            )

    for d in documents:
        if d.topic_id and d.topic_id in topic_map:
            topic_map[d.topic_id]["documents"].append(doc_map[d.id])

    root_topics = []
    for t in topics:
        if t.parent_id and t.parent_id in topic_map:
            topic_map[t.parent_id]["subtopics"].append(topic_map[t.id])
        else:
            root_topics.append(topic_map[t.id])

    return root_topics


async def library_tree_fingerprint(db: AsyncSession) -> str:
    """Отпечаток данных, из которых строится дерево: меняется, только если изменилось само дерево.

    Считается по тем же полям, что попадают в ответ, а не по updated_at: правки через админку
    и импорт не везде обновляют эту колонку, а у связей её нет совсем.
    """
    row = (
        await db.execute(
            text(
                """
                SELECT md5(
                    coalesce((SELECT string_agg(concat_ws('|', id, parent_id, name), ',' ORDER BY id) FROM topics), '') || '#' ||
                    coalesce((SELECT string_agg(concat_ws('|', id, topic_id, title, content_type), ',' ORDER BY id) FROM documents), '') || '#' ||
                    coalesce((SELECT string_agg(concat_ws('|', id, from_document_id, to_document_id, relation_type, label), ',' ORDER BY id)
                              FROM document_relations), '')
                )
                """
            )
        )
    ).scalar()
    return row


async def fetch_document_row(db: AsyncSession, document_id: UUID) -> Optional[Mapping[str, Any]]:
    query = text(
        """
        SELECT d.*, t.name AS topic_name,
               f.bucket, f.storage_key, f.file_name, f.size_bytes,
               f.mime_type AS file_mime_type
        FROM documents d
        LEFT JOIN topics t ON t.id = d.topic_id
        -- Ключ объекта в MinIO и имя файла лежат в document_files. Без этого соединения ключ
        -- достраивался по content_type и не совпадал с реальным (например, source.txt у
        -- html-документов, префикс documents/), и скачивание давало 502.
        LEFT JOIN LATERAL (
            SELECT bucket, storage_key, file_name, size_bytes, mime_type
            FROM document_files
            WHERE document_id = d.id
            ORDER BY created_at DESC
            LIMIT 1
        ) f ON TRUE
        WHERE d.id = :document_id
        """
    )
    result = await db.execute(query, {"document_id": document_id})
    row = result.mappings().first()
    return row


async def fetch_related_documents(db: AsyncSession, document_id: UUID) -> List[Dict[str, Any]]:
    query = text(
        """
        SELECT d.id, d.title, dr.relation_type, dr.label
        FROM document_relations dr
        JOIN documents d ON d.id = dr.to_document_id
        WHERE dr.from_document_id = :document_id
        ORDER BY d.title
        """
    )
    result = await db.execute(query, {"document_id": document_id})
    rows = result.mappings().all()
    return [
        {
            "id": row["id"],
            "title": row["title"],
            "relation_type": row.get("relation_type"),
            "label": row.get("label"),
            "view_url": f"/file.html?id={row['id']}",
            "download_url": f"/api/documents/{row['id']}/download",
        }
        for row in rows
    ]



def _first_present(row: Mapping[str, Any], field_names: tuple[str, ...]) -> Optional[Any]:
    for field in field_names:
        value = row.get(field)
        if value not in (None, ""):
            return value
    return None



def _guess_extension(content_type: Optional[str]) -> str:
    if not content_type:
        return ""
    return mimetypes.guess_extension(content_type) or ""

def _resolve_source_extension(content_type: Optional[str], filename: Optional[str]) -> str:
    normalized = (content_type or "").split(";", 1)[0].strip().lower()

    mime_to_ext = {
        "application/pdf": ".pdf",
        "application/msword": ".doc",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "application/rtf": ".rtf",
        "text/plain": ".txt",
        "text/markdown": ".md",
        "application/vnd.oasis.opendocument.text": ".odt",
    }

    if normalized in mime_to_ext:
        return mime_to_ext[normalized]

    if filename:
        ext = os.path.splitext(filename)[1]
        if ext:
            return ext.lower()

    return ".docx"

def resolve_document_storage(row: Mapping[str, Any]) -> Dict[str, Optional[str]]:
    content_type = row.get("content_type")
    bucket_name = _first_present(row, _BUCKET_FIELDS) or os.getenv("MINIO_BUCKET") or os.getenv("S3_BUCKET") or "documents"

    filename = _first_present(row, _FILENAME_FIELDS)
    if not filename:
        title = (row.get("title") or "source").strip() or "source"
        ext = _resolve_source_extension(content_type, None)
        if ext and not title.lower().endswith(ext):
            filename = f"{title}{ext}"
        else:
            filename = title

    object_key = _first_present(row, _OBJECT_KEY_FIELDS)
    if not object_key:
        doc_id = str(row.get("id"))
        ext = _resolve_source_extension(content_type, filename)
        object_key = f"{doc_id}/v1/original/source{ext}"

    return {
        "bucket_name": bucket_name,
        "object_key": object_key,
        "filename": filename,
    }


def _normalized_name(filename: Optional[str]) -> str:
    return (filename or "").strip().lower()



def is_office_document(content_type: Optional[str], filename: Optional[str]) -> bool:
    normalized = (content_type or "").split(";", 1)[0].strip().lower()
    name = _normalized_name(filename)
    return (
        normalized in {
            "application/msword",
            "application/rtf",
            "application/vnd.oasis.opendocument.text",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }
        or any(name.endswith(ext) for ext in OFFICE_EXTENSIONS)
    )



def detect_preview_mode(content_type: Optional[str], filename: Optional[str]) -> str:
    normalized = (content_type or "").split(";", 1)[0].strip().lower()
    name = _normalized_name(filename)

    if is_office_document(content_type, filename):
        return "pdf"
    if normalized.startswith("image/") or any(name.endswith(ext) for ext in IMAGE_EXTENSIONS):
        return "image"
    if normalized == "application/pdf" or name.endswith(".pdf"):
        return "pdf"
    if normalized.startswith("text/") or any(name.endswith(ext) for ext in TEXT_EXTENSIONS):
        return "text"
    if normalized.startswith("audio/"):
        return "audio"
    if normalized.startswith("video/"):
        return "video"
    return "download"

def get_content_type(filename: Optional[str], fallback: Optional[str] = None) -> str:
    return fallback or mimetypes.guess_type(filename or "")[0] or "application/octet-stream"



def get_preview_filename(filename: Optional[str]) -> str:
    if not filename:
        return "preview.pdf"
    base = Path(filename).stem or "preview"
    return f"{base}.pdf"



def download_minio_object(bucket_name: str, object_key: str) -> bytes:
    client = get_minio_client()
    if not client:
        raise RuntimeError("MinIO не настроен")

    response = None
    try:
        response = client.get_object(bucket_name, object_key)
        return response.read()
    finally:
        if response is not None:
            response.close()
            response.release_conn()



def preview_cache_enabled() -> bool:
    return PREVIEW_CACHE_MAX_BYTES > 0


def preview_cache_key(bucket_name: str, object_key: str) -> str:
    """Ключ кэша привязан к содержимому объекта: ETag меняется при перезаписи файла.

    Поэтому заменённый в MinIO документ не отдаёт устаревший PDF, а явной инвалидации не
    нужно. Блокирующая функция (запрос к MinIO), вызывать через пул потоков.
    """
    client = get_minio_client()
    if not client:
        raise RuntimeError("MinIO не настроен")
    stat = client.stat_object(bucket_name, object_key)
    identity = f"{bucket_name}\0{object_key}\0{stat.etag}\0{stat.size}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def preview_cache_get(cache_key: str) -> Optional[Path]:
    path = PREVIEW_CACHE_DIR / f"{cache_key}.pdf"
    try:
        os.utime(path)  # время последнего использования: по нему вытесняются старые
    except OSError:
        return None
    return path


def preview_cache_put(cache_key: str, pdf_bytes: bytes) -> Optional[Path]:
    """Кладёт PDF в кэш; сбой записи не должен ломать предпросмотр, поэтому вернёт None."""
    if len(pdf_bytes) > PREVIEW_CACHE_MAX_BYTES:
        return None
    try:
        PREVIEW_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = PREVIEW_CACHE_DIR / f"{cache_key}.pdf"
        # Запись во временный файл и rename: читатель никогда не увидит недописанный PDF.
        fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", suffix=".pdf", dir=PREVIEW_CACHE_DIR)
        try:
            with os.fdopen(fd, "wb") as tmp:
                tmp.write(pdf_bytes)
            os.replace(tmp_name, path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        _preview_cache_evict(keep=path)
        return path
    except OSError:
        return None


def _preview_cache_evict(keep: Path) -> None:
    """Удаляет давно не используемые PDF, пока кэш не уложится в лимит."""
    entries = []
    for entry in PREVIEW_CACHE_DIR.glob("*.pdf"):
        try:
            stat = entry.stat()
        except OSError:
            continue
        entries.append((stat.st_mtime, stat.st_size, entry))
    total = sum(size for _, size, _ in entries)
    for _, size, entry in sorted(entries, key=lambda item: item[0]):
        if total <= PREVIEW_CACHE_MAX_BYTES:
            break
        if entry == keep:
            continue
        try:
            entry.unlink()
            PREVIEW_CACHE_EVICTIONS.inc()
            total -= size
        except OSError:
            pass


DOCUMENT_STATUSES = ("draft", "published", "archived")


async def refresh_document_counts(db: AsyncSession) -> None:
    """Обновляет gauge документов по статусу публикации; вызывается периодически."""
    rows = (await db.execute(text("SELECT status, count(*) FROM library.documents GROUP BY status"))).all()
    counts = {status: count for status, count in rows}
    for status in DOCUMENT_STATUSES:
        DOCUMENTS.labels(status=status).set(counts.get(status, 0))


class PreviewTimeout(RuntimeError):
    """Конвертация не уложилась в отведённое время."""


class PreviewTooLarge(RuntimeError):
    """Исходный файл слишком велик для конвертации в памяти."""


def convert_office_bytes_to_pdf(file_bytes: bytes, source_filename: Optional[str]) -> tuple[bytes, str]:
    """Синхронная конвертация через LibreOffice.

    ВАЖНО: функция блокирующая и вызывать её напрямую из async-обработчика нельзя — именно
    так и было раньше, из-за чего один предпросмотр .docx замораживал весь сервис. Вызов
    должен идти через пул потоков (см. app/main.py::preview_document).
    """
    if len(file_bytes) > PREVIEW_MAX_SOURCE_BYTES:
        raise PreviewTooLarge(
            f"Размер файла {len(file_bytes)} байт превышает лимит {PREVIEW_MAX_SOURCE_BYTES}"
        )

    soffice_bin = shutil.which("libreoffice") or shutil.which("soffice")
    if not soffice_bin:
        raise RuntimeError("LibreOffice не установлен в контейнере")

    source_name = source_filename or "document.docx"
    suffix = Path(source_name).suffix or ".docx"

    with tempfile.TemporaryDirectory(prefix="preview-") as tmpdir:
        input_path = Path(tmpdir) / f"source{suffix}"
        input_path.write_bytes(file_bytes)
        output_dir = Path(tmpdir) / "out"
        output_dir.mkdir(parents=True, exist_ok=True)

        command = [
            soffice_bin,
            "--headless",
            "--nologo",
            "--nolockcheck",
            "--nodefault",
            "--norestore",
            "--convert-to",
            "pdf",
            "--outdir",
            str(output_dir),
            str(input_path),
        ]
        try:
            # Таймаут обязателен: без него зависший LibreOffice держал бы поток бесконечно.
            with PREVIEW_CONVERT_DURATION.time():
                process = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=PREVIEW_CONVERT_TIMEOUT_SEC,
                )
        except subprocess.TimeoutExpired as exc:
            raise PreviewTimeout(
                f"LibreOffice не уложился в {PREVIEW_CONVERT_TIMEOUT_SEC} с"
            ) from exc

        if process.returncode != 0:
            stderr = (process.stderr or "").strip()
            stdout = (process.stdout or "").strip()
            raise RuntimeError(stderr or stdout or "Не удалось конвертировать документ в PDF")

        pdf_candidates = list(output_dir.glob("*.pdf"))
        if not pdf_candidates:
            raise RuntimeError("LibreOffice не создал PDF-файл")

        pdf_path = pdf_candidates[0]
        return pdf_path.read_bytes(), get_preview_filename(source_name)


async def build_document_detail(db: AsyncSession, document_id: UUID) -> Optional[Dict[str, Any]]:
    row = await fetch_document_row(db, document_id)
    if not row:
        return None

    storage = resolve_document_storage(row)
    client = get_minio_client()
    size_bytes = _first_present(row, _SIZE_FIELDS)
    if size_bytes is not None:
        try:
            size_bytes = int(size_bytes)
        except (TypeError, ValueError):
            size_bytes = None

    if client and storage["bucket_name"] and storage["object_key"] and size_bytes is None:
        try:
            stat = client.stat_object(storage["bucket_name"], storage["object_key"])
            size_bytes = stat.size
        except S3Error:
            size_bytes = None

    related_documents = await fetch_related_documents(db, document_id)
    preview_mode = detect_preview_mode(row.get("content_type"), storage["filename"])

    topic = None
    if row.get("topic_id") and row.get("topic_name"):
        topic = {"id": row["topic_id"], "name": row["topic_name"]}

    return {
        "id": row["id"],
        "title": row["title"],
        "content_type": row.get("content_type"),
        "description": _first_present(row, _DESCRIPTION_FIELDS),
        "filename": storage["filename"],
        "bucket_name": storage["bucket_name"],
        "object_key": storage["object_key"],
        "size_bytes": size_bytes,
        "preview_mode": preview_mode,
        "view_url": f"/file.html?id={row['id']}",
        "preview_url": f"/api/documents/{row['id']}/preview",
        "download_url": f"/api/documents/{row['id']}/download",
        "topic": topic,
        "related_documents": related_documents,
    }


async def build_document_file_info(
    db: AsyncSession,
    document_id: UUID,
) -> Optional[Dict[str, Optional[str]]]:
    """Возвращает данные, необходимые для выдачи файла, одним запросом к БД."""

    row = await fetch_document_row(db, document_id)
    if not row:
        return None

    storage = resolve_document_storage(row)
    return {
        **storage,
        "content_type": row.get("content_type"),
    }
