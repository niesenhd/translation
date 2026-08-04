"""翻译任务 API 路由。"""
from __future__ import annotations

import os
import tempfile
import uuid
import zipfile
from collections.abc import AsyncIterator, Iterable
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask
from starlette.concurrency import iterate_in_threadpool

from app.core.config import get_settings
from app.core.database import get_db
from app.core.languages import SUPPORTED_LANGUAGES, SUPPORTED_SOURCE_LANGUAGES
from app.core.security import CurrentUser, get_current_user
from app.core.storage import download_stream, object_size, secure_remove_object, upload_stream
from app.models.task import FootnoteMode, OutputMode, PdfOutputFormat, RefineMode, TaskStatus, TranslateImagesOption, TranslationTask
from app.schemas.task import TaskCreateResponse, TaskPage, TaskRead
from app.tasks.celery_app import run_translation_task

router = APIRouter(prefix="/tasks", tags=["tasks"])

ALLOWED_EXT = {"docx", "doc", "pdf", "txt", "md", "xlsx", "xls", "csv", "pptx", "ppt"}
MAX_UPLOAD_SIZE = 200 * 1024 * 1024  # 200MB，nginx 的 500m 作为最后防线
UPLOAD_CHUNK_SIZE = 1024 * 1024
MAX_BATCH_FILES = 100
MAX_BATCH_ZIP_SIZE = 2 * 1024 * 1024 * 1024  # ZIP 磁盘文件与逻辑内容均限制为 2GiB


@router.post("/upload", response_model=TaskCreateResponse, status_code=status.HTTP_201_CREATED)
async def upload_and_translate(
    file: Annotated[UploadFile, File()],
    target_lang: Annotated[str, Form()],
    source_lang: Annotated[str, Form()] = "auto",
    output_mode: Annotated[OutputMode, Form()] = OutputMode.PLAIN,
    pdf_output_format: Annotated[PdfOutputFormat, Form()] = PdfOutputFormat.PDF,
    # 默认"仅翻译文档文字，图片保持原样"（需求 2.2 默认项），与模型默认一致
    translate_images: Annotated[TranslateImagesOption, Form()] = TranslateImagesOption.NO,
    # 两遍法精译（功能C）：默认关，重要文书可选 double_pass
    refine_mode: Annotated[RefineMode, Form()] = RefineMode.NONE,
    # 脚注处理（仅双语模式生效；无脚注的文档不受影响）：默认脚注双语
    footnote_mode: Annotated[FootnoteMode, Form()] = FootnoteMode.BILINGUAL,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TaskCreateResponse:
    if not file.filename:
        raise HTTPException(status_code=400, detail="文件名为空")
    ext = os.path.splitext(file.filename)[1].lower().lstrip(".")
    if ext not in ALLOWED_EXT:
        raise HTTPException(status_code=400, detail=f"暂不支持的格式：{ext}。当前支持：{', '.join(sorted(ALLOWED_EXT))}")
    source_lang = source_lang.strip()
    target_lang = target_lang.strip()
    if source_lang not in SUPPORTED_SOURCE_LANGUAGES:
        raise HTTPException(status_code=400, detail="不支持的源语言")
    if target_lang not in SUPPORTED_LANGUAGES:
        raise HTTPException(status_code=400, detail="不支持的目标语言")

    task_id = str(uuid.uuid4())
    source_object = f"sources/{task_id}.{ext}"

    # 先持久化 UPLOADING，再读取请求体和写 MinIO；后续任一阶段失败都有可对账记录。
    task = TranslationTask(
        id=task_id,
        owner=user.username,
        original_filename=file.filename,
        file_ext=ext,
        source_lang=source_lang,
        target_lang=target_lang,
        output_mode=output_mode,
        pdf_output_format=pdf_output_format,
        translate_images=translate_images,
        refine_mode=refine_mode,
        footnote_mode=footnote_mode,
        source_object=source_object,
        status=TaskStatus.UPLOADING,
    )
    db.add(task)
    try:
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        raise HTTPException(status_code=503, detail="无法创建上传任务，请稍后重试") from exc

    try:
        # UploadFile 本身会落盘，但仍需逐块读取才能执行可信的应用层大小限制，
        # 避免 await file.read() 再复制一份最多 200MB 的 bytes 到进程内存。
        with tempfile.SpooledTemporaryFile(max_size=8 * UPLOAD_CHUNK_SIZE, mode="w+b") as staged:
            total_size = 0
            while chunk := await file.read(UPLOAD_CHUNK_SIZE):
                total_size += len(chunk)
                if total_size > MAX_UPLOAD_SIZE:
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            f"文件过大（已超过 {MAX_UPLOAD_SIZE / 1024 / 1024:.0f}MB），"
                            f"最大支持 {MAX_UPLOAD_SIZE / 1024 / 1024:.0f}MB"
                        ),
                    )
                staged.write(chunk)
            staged.seek(0)
            upload_stream(
                source_object,
                staged,
                total_size,
                content_type=file.content_type or "application/octet-stream",
            )
        queued = db.execute(
            update(TranslationTask)
            .where(
                TranslationTask.id == task_id,
                TranslationTask.status == TaskStatus.UPLOADING,
            )
            .values(status=TaskStatus.QUEUED)
        )
        db.commit()
        if queued.rowcount != 1:
            raise HTTPException(status_code=409, detail="上传期间任务状态已变化")
        task.status = TaskStatus.QUEUED
    except HTTPException as exc:
        _fail_upload(task_id, source_object, db, str(exc.detail))
        raise
    except Exception as exc:  # noqa: BLE001
        _fail_upload(task_id, source_object, db, "文件上传失败，请稍后重试")
        raise HTTPException(status_code=503, detail="文件上传失败，请稍后重试") from exc

    _enqueue_or_fail(task, db)
    return TaskCreateResponse(id=task_id, status=task.status)


def _fail_upload(task_id: str, source_object: str, db: Session, message: str) -> None:
    """尽力清理对象，并只把仍处于 UPLOADING 的同一任务标为失败。"""
    db.rollback()
    try:
        secure_remove_object(source_object)
    except Exception:  # noqa: BLE001
        pass
    try:
        db.execute(
            update(TranslationTask)
            .where(
                TranslationTask.id == task_id,
                TranslationTask.status == TaskStatus.UPLOADING,
            )
            .values(status=TaskStatus.FAILED, error_message=message[:1000])
        )
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()


def _enqueue_or_fail(task: TranslationTask, db: Session) -> None:
    """入队 Celery 任务；broker 不可用时把任务标 FAILED 而非永久卡在"排队中"。"""
    try:
        run_translation_task.delay(task.id)
    except Exception as exc:  # noqa: BLE001
        task.status = TaskStatus.FAILED
        task.error_message = f"任务入队失败（消息队列不可用）：{str(exc)[:200]}"
        db.commit()
        raise HTTPException(status_code=503, detail="任务入队失败，请稍后重试") from exc


@router.get("", response_model=TaskPage)
def list_tasks(
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TaskPage:
    filters = [TranslationTask.status != TaskStatus.DELETED]
    if not user.is_admin:
        filters.append(TranslationTask.owner == user.username)
    total = db.scalar(
        select(func.count()).select_from(TranslationTask).where(*filters)
    ) or 0
    stmt = (
        select(TranslationTask)
        .where(*filters)
        .order_by(TranslationTask.created_at.desc(), TranslationTask.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    tasks = list(db.scalars(stmt))

    # 为 QUEUED 任务批量计算排队位置和 ETA
    queue_info = _compute_queue_info_batch(tasks, db)
    result = []
    for t in tasks:
        item = TaskRead.model_validate(t)
        if t.status == TaskStatus.QUEUED and t.id in queue_info:
            item.queue_position, item.estimated_wait_seconds = queue_info[t.id]
        result.append(item)
    return TaskPage(items=result, total=total, page=page, page_size=page_size)


@router.get("/{task_id}", response_model=TaskRead)
def get_task(
    task_id: str,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TaskRead:
    task = _get_task_for_user(task_id, user, db)
    item = TaskRead.model_validate(task)
    if task.status == TaskStatus.QUEUED:
        pos, eta = _compute_queue_position(task, db)
        item.queue_position = pos
        item.estimated_wait_seconds = eta
    return item


@router.get("/{task_id}/download")
def download_result(
    task_id: str,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """下载翻译结果文件。文件名格式：{原文件名}.{target_lang}.{ext}"""
    task = _get_task_for_user(task_id, user, db)
    if task.status != TaskStatus.SUCCEEDED or task.result_object is None:
        raise HTTPException(status_code=400, detail="任务尚未完成")
    base, _ = os.path.splitext(task.original_filename)
    out_ext = task.result_object.rsplit(".", 1)[-1]
    filename = f"{base}.{task.target_lang}.{out_ext}"
    return _build_download_response(
        download_stream(task.result_object),
        filename,
        content_length=object_size(task.result_object),
    )


@router.get("/{task_id}/download/source")
def download_source(
    task_id: str,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """下载用户上传的原始文件。"""
    task = _get_task_for_user(task_id, user, db)
    if not task.source_object:
        raise HTTPException(status_code=404, detail="原始文件不存在")
    return _build_download_response(
        download_stream(task.source_object),
        task.original_filename,
        content_length=object_size(task.source_object),
    )


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_task(
    task_id: str,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    task = _get_task_for_user(task_id, user, db)
    # 先标记 DELETED：worker 启动 / 进度回调时会检测到并立即退出，避免对已删任务继续调用 LLM
    task.status = TaskStatus.DELETED
    db.commit()
    # 安全删除：用户主动删除走与定时清理同样的"覆写 + 删除"流程
    for obj in (task.source_object, task.result_object):
        if obj:
            try:
                secure_remove_object(obj)
            except Exception:  # noqa: BLE001
                pass


@router.post("/{task_id}/retry", response_model=TaskRead)
def retry_task(
    task_id: str,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TranslationTask:
    """重试失败的任务：重新入队，遵循 FIFO 排队规则。"""
    task = _get_task_for_user(task_id, user, db)
    if task.status not in (TaskStatus.FAILED, TaskStatus.SUCCEEDED):
        raise HTTPException(status_code=400, detail="仅失败或已完成的任务可重试")
    if not task.source_object:
        raise HTTPException(status_code=400, detail="原文已被清理，无法重试")
    # 条件更新"抢占"：并发的两个 retry 只有一个能把状态置回 QUEUED，
    # 避免重复入队导致同一任务被翻译两次（worker 侧也有 QUEUED→RUNNING 抢占兜底）
    claimed = db.execute(
        update(TranslationTask)
        .where(
            TranslationTask.id == task_id,
            TranslationTask.status.in_([TaskStatus.FAILED, TaskStatus.SUCCEEDED]),
        )
        .values(
            status=TaskStatus.QUEUED,
            progress=0,
            error_message=None,
            result_object=None,
            attempt_id=None,
            heartbeat_at=None,
        )
    )
    db.commit()
    if claimed.rowcount != 1:
        raise HTTPException(status_code=409, detail="任务状态已变化，请刷新后重试")
    db.refresh(task)
    _enqueue_or_fail(task, db)
    return task


def _get_task_for_user(task_id: str, user: CurrentUser, db: Session) -> TranslationTask:
    task = db.get(TranslationTask, task_id)
    if task is None or task.status == TaskStatus.DELETED:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not user.is_admin and task.owner != user.username:
        # 与不存在统一返回 404，避免通过 ID 探测其他用户的任务。
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


def _build_download_response(
    data: Iterable[bytes], filename: str, content_length: int | None = None
) -> StreamingResponse:
    """构造带 RFC 5987 文件名编码的下载响应，兼容中文等非 ASCII 文件名。"""
    ascii_fallback = filename.encode("ascii", "ignore").decode("ascii") or "download.bin"
    encoded = quote(filename, safe="")
    headers = {
        "Content-Disposition": (
            f'attachment; filename="{ascii_fallback}"; filename*=UTF-8\'\'{encoded}'
        )
    }
    if content_length is not None:
        headers["Content-Length"] = str(content_length)
    return StreamingResponse(
        _iterate_download_with_close(data),
        media_type="application/octet-stream",
        headers=headers,
    )


async def _iterate_download_with_close(data: Iterable[bytes]) -> AsyncIterator[bytes]:
    """在线程池读取同步 MinIO 流，并在响应完成或断开时显式关闭迭代器。"""
    iterator = iter(data)
    try:
        async for chunk in iterate_in_threadpool(iterator):
            yield chunk
    finally:
        close = getattr(iterator, "close", None)
        if close is not None:
            close()


@router.post("/batch/download-source")
def batch_download_source(
    task_ids: list[str],
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FileResponse:
    """批量下载原文，打包为 ZIP。同名文件自动加序号避免覆盖。"""
    _validate_batch_file_count(task_ids)
    tasks = _get_tasks_for_user(task_ids, user, db)
    entries = [
        (task.source_object, task.original_filename)
        for task in tasks
        if task.source_object
    ]
    return _build_batch_zip_response(
        entries,
        archive_name="sources.zip",
    )


@router.post("/batch/download-result")
def batch_download_result(
    task_ids: list[str],
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FileResponse:
    """批量下载译文，打包为 ZIP。同名文件自动加序号避免覆盖。"""
    _validate_batch_file_count(task_ids)
    tasks = _get_tasks_for_user(task_ids, user, db)
    entries: list[tuple[str, str]] = []
    for task in tasks:
        if task.status != TaskStatus.SUCCEEDED or not task.result_object:
            continue
        base, _ = os.path.splitext(task.original_filename)
        out_ext = task.result_object.rsplit(".", 1)[-1]
        entries.append(
            (task.result_object, f"{base}.{task.target_lang}.{out_ext}")
        )
    return _build_batch_zip_response(
        entries,
        archive_name="translations.zip",
    )


class _BatchZipTooLarge(Exception):
    pass


def _build_batch_zip_response(
    entries: list[tuple[str, str]], archive_name: str
) -> FileResponse:
    """在磁盘构建有上限的 ZIP，并在响应结束后删除临时文件。"""
    temp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix="translation-", suffix=".zip", delete=False) as temp_file:
            temp_path = temp_file.name
            uncompressed_size = 0
            used_names: dict[str, int] = {}
            with zipfile.ZipFile(temp_file, "w", zipfile.ZIP_DEFLATED) as zf:
                for object_name, original_name in entries:
                    safe_name = os.path.basename(original_name.replace("\\", "/")) or "file"
                    name = _deduplicate_filename(safe_name, used_names)
                    with zf.open(name, "w") as target:
                        for chunk in download_stream(object_name):
                            uncompressed_size += len(chunk)
                            if uncompressed_size > MAX_BATCH_ZIP_SIZE:
                                raise _BatchZipTooLarge
                            target.write(chunk)
                            if temp_file.tell() > MAX_BATCH_ZIP_SIZE:
                                raise _BatchZipTooLarge
        if os.path.getsize(temp_path) > MAX_BATCH_ZIP_SIZE:
            raise _BatchZipTooLarge
    except Exception as exc:
        if temp_path:
            _unlink_temp_file(temp_path)
        if isinstance(exc, _BatchZipTooLarge):
            raise HTTPException(
                status_code=413,
                detail=f"批量下载内容超过 {MAX_BATCH_ZIP_SIZE / 1024 / 1024:.0f}MB 上限",
            ) from exc
        raise

    return FileResponse(
        temp_path,
        media_type="application/zip",
        filename=archive_name,
        background=BackgroundTask(_unlink_temp_file, temp_path),
    )


def _validate_batch_file_count(task_ids: list[str]) -> None:
    if len(set(task_ids)) > MAX_BATCH_FILES:
        raise HTTPException(
            status_code=413,
            detail=f"批量下载最多支持 {MAX_BATCH_FILES} 个文件",
        )


def _deduplicate_filename(name: str, used_names: dict[str, int]) -> str:
    count = used_names.get(name, 0)
    used_names[name] = count + 1
    if count == 0:
        return name
    base, ext = os.path.splitext(name)
    return f"{base}_{count}{ext}"


def _unlink_temp_file(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


@router.post("/batch/delete", status_code=status.HTTP_204_NO_CONTENT)
def batch_delete_tasks(
    task_ids: list[str],
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    """批量删除任务。"""
    tasks = _get_tasks_for_user(task_ids, user, db)
    # 先全部标记 DELETED 并提交：让 worker 尽快感知到并退出
    for task in tasks:
        task.status = TaskStatus.DELETED
    db.commit()
    # 再做安全擦除
    for task in tasks:
        for obj in (task.source_object, task.result_object):
            if obj:
                try:
                    secure_remove_object(obj)
                except Exception:  # noqa: BLE001
                    pass
    db.commit()


def _get_tasks_for_user(task_ids: list[str], user, db: Session) -> list[TranslationTask]:
    """根据 ID 列表获取任务，校验权限。"""
    unique_ids = list(dict.fromkeys(task_ids))
    stmt = select(TranslationTask).where(
        TranslationTask.id.in_(unique_ids),
        TranslationTask.status != TaskStatus.DELETED,
    )
    if not user.is_admin:
        stmt = stmt.where(TranslationTask.owner == user.username)
    tasks = list(db.scalars(stmt))
    if len(tasks) != len(unique_ids):
        raise HTTPException(status_code=404, detail="任务不存在")
    return tasks


# ====== 排队位置与预计等待时间计算 ======

def _avg_task_duration_seconds(db: Session) -> float | None:
    """计算近期已完成任务的平均耗时（秒），取最近 50 条。

    使用子查询先筛选最近 50 条 SUCCEEDED 任务，再由外层求平均，
    避免全量历史数据稀释近期趋势。
    """
    duration_expr = (
        func.extract("epoch", TranslationTask.updated_at)
        - func.extract("epoch", TranslationTask.created_at)
    )
    # 子查询：按创建时间倒序取最近 50 条成功任务的耗时
    subq = (
        select(duration_expr.label("duration"))
        .where(TranslationTask.status == TaskStatus.SUCCEEDED)
        .order_by(TranslationTask.created_at.desc())
        .limit(50)
        .subquery()
    )
    # 外层：对子查询结果求平均
    avg = db.scalar(select(func.avg(subq.c.duration)))
    return float(avg) if avg else None


def _get_max_concurrency_value(db: Session) -> int:
    """读取管理员配置的并发上限，回退到环境变量默认值。"""
    from app.api.admin import KEY_MAX_CONCURRENCY
    from app.models.system_config import SystemConfig

    cfg = db.get(SystemConfig, KEY_MAX_CONCURRENCY)
    if cfg is not None:
        try:
            return max(1, int(cfg.value))
        except (ValueError, TypeError):
            pass
    return max(1, get_settings().translation_max_concurrency)


def _compute_queue_position(task: TranslationTask, db: Session) -> tuple[int | None, int | None]:
    """计算单个 QUEUED 任务的排队位置（从 1 开始）和预计等待时间（秒）。"""
    positions = _queued_positions([task.id], db)
    pos = positions.get(task.id)
    if pos is None:
        return None, None
    max_c = _get_max_concurrency_value(db)
    running = db.scalar(
        select(func.count()).select_from(TranslationTask).where(
            TranslationTask.status == TaskStatus.RUNNING
        )
    ) or 0
    return pos, _estimate_wait_seconds(pos, max_c, running, _avg_task_duration_seconds(db))


def _compute_queue_info_batch(
    tasks: list[TranslationTask], db: Session
) -> dict[str, tuple[int | None, int | None]]:
    """为一批任务中的 QUEUED 任务批量计算排队信息。"""
    queued = [t for t in tasks if t.status == TaskStatus.QUEUED]
    if not queued:
        return {}

    max_c = _get_max_concurrency_value(db)
    avg_dur = _avg_task_duration_seconds(db)
    running = db.scalar(
        select(func.count()).select_from(TranslationTask).where(
            TranslationTask.status == TaskStatus.RUNNING
        )
    ) or 0
    positions = _queued_positions([task.id for task in queued], db)

    result: dict[str, tuple[int | None, int | None]] = {}
    for t in queued:
        pos = positions.get(t.id)
        if pos is not None:
            result[t.id] = (
                pos,
                _estimate_wait_seconds(pos, max_c, running, avg_dur),
            )
    return result


def _queued_positions(task_ids: list[str], db: Session) -> dict[str, int]:
    """单次窗口查询返回全局 FIFO 位次；id 用于 created_at 相同时稳定排序。"""
    if not task_ids:
        return {}
    ranked = (
        select(
            TranslationTask.id.label("task_id"),
            func.row_number()
            .over(order_by=(TranslationTask.created_at.asc(), TranslationTask.id.asc()))
            .label("position"),
        )
        .where(TranslationTask.status == TaskStatus.QUEUED)
        .subquery()
    )
    rows = db.execute(
        select(ranked.c.task_id, ranked.c.position).where(
            ranked.c.task_id.in_(task_ids)
        )
    )
    return {task_id: int(position) for task_id, position in rows}


def _estimate_wait_seconds(
    position: int,
    max_concurrency: int,
    running_count: int,
    average_duration: float | None,
) -> int | None:
    """按空闲槽位与后续处理批次估算等待时间。"""
    available_slots = max(0, max_concurrency - running_count)
    if position <= available_slots:
        return 0
    if not average_duration:
        return None
    waiting_rank = position - available_slots
    waves = (waiting_rank + max_concurrency - 1) // max_concurrency
    return int(waves * average_duration)
