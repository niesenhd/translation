"""翻译任务 API 路由。"""
from __future__ import annotations

import io
import os
import uuid
import zipfile
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db
from app.core.security import CurrentUser, get_current_user
from app.core.storage import download_bytes, secure_remove_object, upload_bytes
from app.models.task import OutputMode, PdfOutputFormat, TaskStatus, TranslateImagesOption, TranslationTask
from app.schemas.task import TaskCreateResponse, TaskRead
from app.tasks.celery_app import run_translation_task

router = APIRouter(prefix="/tasks", tags=["tasks"])

ALLOWED_EXT = {"docx", "doc", "pdf", "txt", "md", "xlsx", "xls", "csv", "pptx", "ppt"}
MAX_UPLOAD_SIZE = 200 * 1024 * 1024  # 200MB，nginx 的 500m 作为最后防线


@router.post("/upload", response_model=TaskCreateResponse, status_code=status.HTTP_201_CREATED)
async def upload_and_translate(
    file: Annotated[UploadFile, File()],
    target_lang: Annotated[str, Form()],
    source_lang: Annotated[str, Form()] = "auto",
    output_mode: Annotated[OutputMode, Form()] = OutputMode.PLAIN,
    pdf_output_format: Annotated[PdfOutputFormat, Form()] = PdfOutputFormat.PDF,
    # 默认"仅翻译文档文字，图片保持原样"（需求 2.2 默认项），与模型默认一致
    translate_images: Annotated[TranslateImagesOption, Form()] = TranslateImagesOption.NO,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TaskCreateResponse:
    if not file.filename:
        raise HTTPException(status_code=400, detail="文件名为空")
    ext = os.path.splitext(file.filename)[1].lower().lstrip(".")
    if ext not in ALLOWED_EXT:
        raise HTTPException(status_code=400, detail=f"暂不支持的格式：{ext}。当前支持：{', '.join(sorted(ALLOWED_EXT))}")

    content = await file.read()
    # 文件大小校验：nginx 的 client_max_body_size 是最后防线，应用层提前拦截给出明确错误
    if len(content) > MAX_UPLOAD_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"文件过大（{len(content) / 1024 / 1024:.1f}MB），最大支持 {MAX_UPLOAD_SIZE / 1024 / 1024:.0f}MB"
        )

    task_id = str(uuid.uuid4())
    source_object = f"sources/{task_id}.{ext}"
    upload_bytes(source_object, content, content_type=file.content_type or "application/octet-stream")

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
        source_object=source_object,
        status=TaskStatus.QUEUED,
    )
    db.add(task)
    db.commit()

    _enqueue_or_fail(task, db)
    return TaskCreateResponse(id=task_id, status=TaskStatus.QUEUED)


def _enqueue_or_fail(task: TranslationTask, db: Session) -> None:
    """入队 Celery 任务；broker 不可用时把任务标 FAILED 而非永久卡在"排队中"。"""
    try:
        run_translation_task.delay(task.id)
    except Exception as exc:  # noqa: BLE001
        task.status = TaskStatus.FAILED
        task.error_message = f"任务入队失败（消息队列不可用）：{str(exc)[:200]}"
        db.commit()
        raise HTTPException(status_code=503, detail="任务入队失败，请稍后重试") from exc


@router.get("", response_model=list[TaskRead])
def list_tasks(
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[TaskRead]:
    stmt = select(TranslationTask).where(TranslationTask.status != TaskStatus.DELETED)
    if not user.is_admin:
        stmt = stmt.where(TranslationTask.owner == user.username)
    stmt = stmt.order_by(TranslationTask.created_at.desc())
    tasks = list(db.scalars(stmt))

    # 为 QUEUED 任务批量计算排队位置和 ETA
    queue_info = _compute_queue_info_batch(tasks, db)
    result = []
    for t in tasks:
        item = TaskRead.model_validate(t)
        if t.status == TaskStatus.QUEUED and t.id in queue_info:
            item.queue_position, item.estimated_wait_seconds = queue_info[t.id]
        result.append(item)
    return result


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
    data = download_bytes(task.result_object)
    base, _ = os.path.splitext(task.original_filename)
    out_ext = task.result_object.rsplit(".", 1)[-1]
    filename = f"{base}.{task.target_lang}.{out_ext}"
    return _build_download_response(data, filename)


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
    data = download_bytes(task.source_object)
    return _build_download_response(data, task.original_filename)


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
        .values(status=TaskStatus.QUEUED, progress=0, error_message=None, result_object=None)
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
        raise HTTPException(status_code=403, detail="无权访问该任务")
    return task


def _build_download_response(data: bytes, filename: str) -> StreamingResponse:
    """构造带 RFC 5987 文件名编码的下载响应，兼容中文等非 ASCII 文件名。"""
    ascii_fallback = filename.encode("ascii", "ignore").decode("ascii") or "download.bin"
    encoded = quote(filename, safe="")
    return StreamingResponse(
        iter([data]),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{ascii_fallback}"; filename*=UTF-8\'\'{encoded}'
            )
        },
    )


@router.post("/batch/download-source")
def batch_download_source(
    task_ids: list[str],
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """批量下载原文，打包为 ZIP。同名文件自动加序号避免覆盖。"""
    tasks = _get_tasks_for_user(task_ids, user, db)
    buf = io.BytesIO()
    used_names: dict[str, int] = {}  # filename -> count
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for task in tasks:
            if not task.source_object:
                continue
            data = download_bytes(task.source_object)
            name = task.original_filename
            # 处理同名文件
            if name in used_names:
                used_names[name] += 1
                base, ext = os.path.splitext(name)
                name = f"{base}_{used_names[name]}{ext}"
            else:
                used_names[name] = 0
            zf.writestr(name, data)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=sources.zip"},
    )


@router.post("/batch/download-result")
def batch_download_result(
    task_ids: list[str],
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """批量下载译文，打包为 ZIP。同名文件自动加序号避免覆盖。"""
    tasks = _get_tasks_for_user(task_ids, user, db)
    buf = io.BytesIO()
    used_names: dict[str, int] = {}
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for task in tasks:
            if task.status != TaskStatus.SUCCEEDED or not task.result_object:
                continue
            data = download_bytes(task.result_object)
            base, _ = os.path.splitext(task.original_filename)
            out_ext = task.result_object.rsplit(".", 1)[-1]
            name = f"{base}.{task.target_lang}.{out_ext}"
            if name in used_names:
                used_names[name] += 1
                base2, ext2 = os.path.splitext(name)
                name = f"{base2}_{used_names[name]}{ext2}"
            else:
                used_names[name] = 0
            zf.writestr(name, data)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=translations.zip"},
    )


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
    stmt = select(TranslationTask).where(
        TranslationTask.id.in_(task_ids),
        TranslationTask.status != TaskStatus.DELETED,
    )
    tasks = list(db.scalars(stmt))
    if not user.is_admin:
        for t in tasks:
            if t.owner != user.username:
                raise HTTPException(status_code=403, detail="无权访问该任务")
    return tasks


# ====== 排队位置与预计等待时间计算 ======

def _avg_task_duration_seconds(db: Session) -> float | None:
    """计算近期已完成任务的平均耗时（秒），取最近 50 条。"""
    avg = db.scalar(
        select(
            func.avg(
                func.extract("epoch", TranslationTask.updated_at)
                - func.extract("epoch", TranslationTask.created_at)
            )
        ).where(TranslationTask.status == TaskStatus.SUCCEEDED)
    )
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
    # 排队位置 = 在该任务之前（含自身）的 QUEUED 任务数（按 created_at 升序）
    pos = db.scalar(
        select(func.count()).select_from(TranslationTask).where(
            TranslationTask.status == TaskStatus.QUEUED,
            TranslationTask.created_at <= task.created_at,
        )
    ) or 1

    max_c = _get_max_concurrency_value(db)
    avg_dur = _avg_task_duration_seconds(db)

    # 前面有 (pos - max_c) 个任务需要等待（已在处理的 max_c 个不计算在内）
    pending_before = max(0, pos - max_c)
    if avg_dur and pending_before > 0:
        eta = int(pending_before * avg_dur / max_c)
    elif avg_dur:
        eta = int(avg_dur * 0.3)  # 即将轮到，给出一个粗略估计
    else:
        eta = None
    return pos, eta


def _compute_queue_info_batch(
    tasks: list[TranslationTask], db: Session
) -> dict[str, tuple[int | None, int | None]]:
    """为一批任务中的 QUEUED 任务批量计算排队信息。"""
    queued = [t for t in tasks if t.status == TaskStatus.QUEUED]
    if not queued:
        return {}

    max_c = _get_max_concurrency_value(db)
    avg_dur = _avg_task_duration_seconds(db)

    # 全局 QUEUED 计数（用于确定每个任务的全局位次）
    # 简化：用每个任务的 created_at 做一次查询
    result: dict[str, tuple[int | None, int | None]] = {}
    for t in queued:
        pos = db.scalar(
            select(func.count()).select_from(TranslationTask).where(
                TranslationTask.status == TaskStatus.QUEUED,
                TranslationTask.created_at <= t.created_at,
            )
        ) or 1
        pending_before = max(0, pos - max_c)
        if avg_dur and pending_before > 0:
            eta = int(pending_before * avg_dur / max_c)
        elif avg_dur:
            eta = int(avg_dur * 0.3)
        else:
            eta = None
        result[t.id] = (pos, eta)
    return result
