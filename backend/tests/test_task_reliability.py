from __future__ import annotations

import asyncio
from io import BytesIO
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, UploadFile

from app.api import tasks as tasks_api
from app.core import storage
from app.models.task import (
    FootnoteMode,
    OutputMode,
    PdfOutputFormat,
    RefineMode,
    TaskStatus,
    TranslateImagesOption,
)
from app.tasks import celery_app as worker


class _ObjectResponse:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.offset = 0
        self.closed = False
        self.released = False

    def read(self, size: int) -> bytes:
        chunk = self.data[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk

    def close(self) -> None:
        self.closed = True

    def release_conn(self) -> None:
        self.released = True


def test_minio_download_stream_releases_connection(monkeypatch) -> None:
    response = _ObjectResponse(b"abcdef")
    client = SimpleNamespace(get_object=lambda _bucket, _name: response)
    monkeypatch.setattr(storage, "get_client", lambda: client)

    assert list(storage.download_stream("object", chunk_size=2)) == [b"ab", b"cd", b"ef"]
    assert response.closed is True
    assert response.released is True


def test_minio_download_stream_closes_when_consumer_stops_early(monkeypatch) -> None:
    response = _ObjectResponse(b"abcdef")
    client = SimpleNamespace(get_object=lambda _bucket, _name: response)
    monkeypatch.setattr(storage, "get_client", lambda: client)

    stream = storage.download_stream("object", chunk_size=2)
    assert next(stream) == b"ab"
    stream.close()

    assert response.closed is True
    assert response.released is True


def test_streaming_response_wrapper_closes_iterator_on_disconnect() -> None:
    class ClosableIterator:
        def __init__(self) -> None:
            self.closed = False
            self.sent = False

        def __iter__(self):
            return self

        def __next__(self) -> bytes:
            if self.sent:
                raise StopIteration
            self.sent = True
            return b"chunk"

        def close(self) -> None:
            self.closed = True

    iterator = ClosableIterator()

    async def disconnect() -> None:
        response_stream = tasks_api._iterate_download_with_close(iterator)
        assert await anext(response_stream) == b"chunk"
        await response_stream.aclose()

    asyncio.run(disconnect())
    assert iterator.closed is True


def test_upload_limit_is_checked_before_minio_or_database(monkeypatch) -> None:
    monkeypatch.setattr(tasks_api, "MAX_UPLOAD_SIZE", 3)
    monkeypatch.setattr(tasks_api, "UPLOAD_CHUNK_SIZE", 2)
    upload_called = False
    removed: list[str] = []

    class FakeDB:
        def __init__(self) -> None:
            self.added = None
            self.statements = []

        def add(self, value) -> None:
            self.added = value

        def commit(self) -> None:
            pass

        def rollback(self) -> None:
            pass

        def execute(self, statement):
            self.statements.append(statement)
            return SimpleNamespace(rowcount=1)

    db = FakeDB()

    def unexpected_upload(*_args, **_kwargs) -> None:
        nonlocal upload_called
        upload_called = True

    monkeypatch.setattr(tasks_api, "upload_stream", unexpected_upload)
    monkeypatch.setattr(
        tasks_api,
        "secure_remove_object",
        lambda object_name: removed.append(object_name),
    )
    file = UploadFile(file=BytesIO(b"1234"), filename="sample.txt")

    async def invoke_upload() -> None:
        await tasks_api.upload_and_translate(
            file=file,
            target_lang="zh",
            source_lang="auto",
            output_mode=OutputMode.PLAIN,
            pdf_output_format=PdfOutputFormat.PDF,
            translate_images=TranslateImagesOption.NO,
            refine_mode=RefineMode.NONE,
            footnote_mode=FootnoteMode.BILINGUAL,
            user=SimpleNamespace(username="tester"),
            db=db,
        )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(invoke_upload())

    assert exc_info.value.status_code == 413
    assert upload_called is False
    assert db.added.status == TaskStatus.UPLOADING
    assert len(db.statements) == 1  # _fail_upload 的 UPLOADING -> FAILED 条件更新
    assert removed == [db.added.source_object]


def test_batch_zip_limit_removes_partial_temp_file(monkeypatch, tmp_path) -> None:
    original_named_temporary_file = tasks_api.tempfile.NamedTemporaryFile
    monkeypatch.setattr(tasks_api, "MAX_BATCH_ZIP_SIZE", 3)
    monkeypatch.setattr(tasks_api, "download_stream", lambda _name: iter([b"1234"]))
    monkeypatch.setattr(
        tasks_api.tempfile,
        "NamedTemporaryFile",
        lambda **kwargs: original_named_temporary_file(dir=tmp_path, **kwargs),
    )

    with pytest.raises(HTTPException) as exc_info:
        tasks_api._build_batch_zip_response([("object", "document.txt")], "files.zip")

    assert exc_info.value.status_code == 413
    assert list(tmp_path.iterdir()) == []


def test_batch_download_rejects_more_than_100_unique_files() -> None:
    with pytest.raises(HTTPException) as exc_info:
        tasks_api._validate_batch_file_count([str(index) for index in range(101)])

    assert exc_info.value.status_code == 413


def test_non_owner_gets_same_404_as_missing_task() -> None:
    task = SimpleNamespace(status=TaskStatus.SUCCEEDED, owner="alice")
    db = SimpleNamespace(get=lambda _model, _task_id: task)

    with pytest.raises(HTTPException) as exc_info:
        tasks_api._get_task_for_user(
            "task-id",
            SimpleNamespace(username="bob", is_admin=False),
            db,
        )

    assert exc_info.value.status_code == 404


def test_queue_eta_accounts_for_running_tasks_and_free_slots() -> None:
    assert tasks_api._estimate_wait_seconds(1, 4, 2, 100.0) == 0
    assert tasks_api._estimate_wait_seconds(2, 4, 2, 100.0) == 0
    assert tasks_api._estimate_wait_seconds(3, 4, 2, 100.0) == 100
    assert tasks_api._estimate_wait_seconds(5, 4, 4, 100.0) == 200
    assert tasks_api._estimate_wait_seconds(1, 4, 4, None) is None


def test_redis_client_is_reused_within_process(monkeypatch) -> None:
    created: list[object] = []

    def make_client(*_args, **_kwargs):
        client = object()
        created.append(client)
        return client

    import redis

    monkeypatch.setattr(redis, "from_url", make_client)
    monkeypatch.setattr(worker.os, "getpid", lambda: 100)
    monkeypatch.setattr(worker, "_redis_process_client", None)
    monkeypatch.setattr(worker, "_redis_process_pid", None)

    assert worker._redis_client() is worker._redis_client()
    assert len(created) == 1


def test_concurrency_gate_stays_closed_until_redis_recovers(monkeypatch) -> None:
    calls = 0

    def acquire(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("redis unavailable")
        return 1

    client = SimpleNamespace(register_script=lambda _script: acquire)
    monkeypatch.setattr(worker, "_redis_client", lambda: client)
    monkeypatch.setattr(worker, "_get_max_concurrency", lambda: 2)
    monkeypatch.setattr(worker, "_check_task_alive", lambda _task_id: None)
    monkeypatch.setattr(worker.time, "sleep", lambda _seconds: None)

    worker._acquire_concurrency_slot("task", "attempt", poll_interval=0)

    assert calls == 2


def test_celery_worker_loss_settings_are_enabled() -> None:
    assert worker.celery_app.conf.task_acks_late is True
    assert worker.celery_app.conf.task_reject_on_worker_lost is True
    assert worker.run_translation_task.acks_late is True
    assert worker.run_translation_task.reject_on_worker_lost is True
