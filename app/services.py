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

OFFICE_EXTENSIONS = {".doc", ".docx", ".rtf", ".odt"}
TEXT_EXTENSIONS = {".txt", ".md", ".html", ".csv", ".json", ".xml", ".log"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}


@lru_cache(maxsize=1)
def get_minio_client() -> Optional[Minio]:
    endpoint = os.getenv("MINIO_ENDPOINT") or os.getenv("S3_ENDPOINT")
    access_key = os.getenv("MINIO_ACCESS_KEY") or os.getenv("MINIO_ROOT_USER") or os.getenv("AWS_ACCESS_KEY_ID")
    secret_key = os.getenv("MINIO_SECRET_KEY") or os.getenv("MINIO_ROOT_PASSWORD") or os.getenv("AWS_SECRET_ACCESS_KEY")

    if not endpoint or not access_key or not secret_key:
        return None

    secure = str(os.getenv("MINIO_SECURE", "false")).lower() in {"1", "true", "yes", "on"}
    return Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=secure)


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


async def fetch_document_row(db: AsyncSession, document_id: UUID) -> Optional[Mapping[str, Any]]:
    query = text(
        """
        SELECT d.*, t.name AS topic_name
        FROM documents d
        LEFT JOIN topics t ON t.id = d.topic_id
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



def convert_office_bytes_to_pdf(file_bytes: bytes, source_filename: Optional[str]) -> tuple[bytes, str]:
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

        process = subprocess.run(
            [
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
            ],
            capture_output=True,
            text=True,
        )

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
