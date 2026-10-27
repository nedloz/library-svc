import asyncio
import os
import uuid
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")

import pytest  # noqa: E402
from prometheus_client import REGISTRY  # noqa: E402

from app import main, services  # noqa: E402


def sample(name: str, **labels) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def run_office_preview(monkeypatch, *, enabled, cached):
    monkeypatch.setattr(main.anyio.to_thread, "run_sync", lambda func, *args: asyncio.sleep(0, func(*args)))
    monkeypatch.setattr(main, "preview_cache_enabled", lambda: enabled)
    monkeypatch.setattr(main, "preview_cache_key", lambda *_a: "key")
    monkeypatch.setattr(main, "preview_cache_get", lambda _key: Path("/cached.pdf") if cached else None)
    monkeypatch.setattr(main, "preview_cache_put", lambda _key, _bytes: None)
    monkeypatch.setattr(main, "download_minio_object", lambda *_a: b"docx")
    monkeypatch.setattr(main, "convert_office_bytes_to_pdf", lambda *_a: (b"%PDF", "x.pdf"))
    return asyncio.run(main._office_preview_pdf("bucket", "object", "report.docx"))


def test_cache_hit_is_counted_and_skips_conversion(monkeypatch):
    before = sample("pochemuchnik_preview_cache_requests_total", outcome="hit")
    path, data = run_office_preview(monkeypatch, enabled=True, cached=True)
    assert path == Path("/cached.pdf") and data is None
    assert sample("pochemuchnik_preview_cache_requests_total", outcome="hit") == before + 1


def test_cache_miss_is_counted_and_converts(monkeypatch):
    before = sample("pochemuchnik_preview_cache_requests_total", outcome="miss")
    path, data = run_office_preview(monkeypatch, enabled=True, cached=False)
    assert path is None and data == b"%PDF"
    assert sample("pochemuchnik_preview_cache_requests_total", outcome="miss") == before + 1


def test_disabled_cache_is_counted_separately(monkeypatch):
    before = sample("pochemuchnik_preview_cache_requests_total", outcome="disabled")
    run_office_preview(monkeypatch, enabled=False, cached=False)
    assert sample("pochemuchnik_preview_cache_requests_total", outcome="disabled") == before + 1


def test_eviction_over_the_size_limit_is_counted(monkeypatch, tmp_path):
    monkeypatch.setattr(services, "PREVIEW_CACHE_DIR", tmp_path)
    monkeypatch.setattr(services, "PREVIEW_CACHE_MAX_BYTES", 10)
    old = tmp_path / "old.pdf"
    old.write_bytes(b"x" * 8)
    os.utime(old, (1, 1))
    before = sample("pochemuchnik_preview_cache_evictions_total")

    services.preview_cache_put("new", b"y" * 8)

    assert not old.exists()
    assert sample("pochemuchnik_preview_cache_evictions_total") == before + 1


def test_libreoffice_run_time_is_observed(monkeypatch):
    def fake_run(command, **_kwargs):
        outdir = Path(command[command.index("--outdir") + 1])
        (outdir / "source.pdf").write_bytes(b"%PDF")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(services.shutil, "which", lambda _name: "/usr/bin/soffice")
    monkeypatch.setattr(services.subprocess, "run", fake_run)
    before = sample("pochemuchnik_preview_convert_duration_seconds_count")

    pdf, _name = services.convert_office_bytes_to_pdf(b"docx", "report.docx")

    assert pdf == b"%PDF"
    assert sample("pochemuchnik_preview_convert_duration_seconds_count") == before + 1


def preview(monkeypatch, filename, content_type):
    async def file_info(_db, _document_id):
        return {"bucket_name": "b", "object_key": "k", "filename": filename, "content_type": content_type}

    async def office_pdf(*_args):
        return None, b"%PDF"

    monkeypatch.setattr(main, "build_document_file_info", file_info)
    monkeypatch.setattr(main, "_office_preview_pdf", office_pdf)
    return asyncio.run(main.preview_document(uuid.uuid4(), db=None, _user_id=uuid.uuid4()))


def test_office_preview_is_counted_as_office_pdf(monkeypatch):
    before = sample("pochemuchnik_preview_requests_total", mode="office_pdf")
    preview(monkeypatch, "report.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    assert sample("pochemuchnik_preview_requests_total", mode="office_pdf") == before + 1


def test_download_is_counted_only_after_a_successful_response(monkeypatch):
    async def file_info(_db, _document_id):
        return {"bucket_name": "b", "object_key": "k", "filename": "a.pdf", "content_type": "application/pdf"}

    async def broken(*_args):
        raise RuntimeError("minio down")

    monkeypatch.setattr(main, "build_document_file_info", file_info)
    monkeypatch.setattr(main, "_build_file_response_from_minio", broken)
    before = sample("pochemuchnik_downloads_total")
    with pytest.raises(RuntimeError):
        asyncio.run(main.download_document(uuid.uuid4(), db=None, _user_id=uuid.uuid4()))
    assert sample("pochemuchnik_downloads_total") == before

    async def ok(*_args):
        return "response"

    monkeypatch.setattr(main, "_build_file_response_from_minio", ok)
    asyncio.run(main.download_document(uuid.uuid4(), db=None, _user_id=uuid.uuid4()))
    assert sample("pochemuchnik_downloads_total") == before + 1


def test_document_counts_cover_all_statuses_even_when_absent():
    class Db:
        async def execute(self, _query):
            return SimpleNamespace(all=lambda: [("published", 7)])

    asyncio.run(services.refresh_document_counts(Db()))

    assert sample("pochemuchnik_documents", status="published") == 7
    assert sample("pochemuchnik_documents", status="draft") == 0
    assert sample("pochemuchnik_documents", status="archived") == 0
