"""文件保留策略：过期文件自动覆写删除。

需求 2.9：超过保留期的任务原文 + 译文均彻底删除（覆写后删除，不可恢复）。

触发方式：
- Celery Beat 定时任务（celery_app.py 中已配置）
- 也可通过 POST /api/admin/cleanup 手动触发

保留天数：
- 默认值由 settings.file_retention_days（环境变量 FILE_RETENTION_DAYS，默认 180）
- 管理员可通过 system_config 表动态调整，DB 优先，回退到 env
- 设为 0 表示永不过期
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, delete as sa_delete

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.storage import secure_remove_object
from app.models.system_config import KEY_FILE_RETENTION_DAYS, SystemConfig
from app.models.task import TaskStatus, TranslationTask

logger = logging.getLogger(__name__)


def get_retention_days() -> int:
    """读取当前生效的保留天数：DB > 环境变量。"""
    db = SessionLocal()
    try:
        cfg = db.get(SystemConfig, KEY_FILE_RETENTION_DAYS)
        if cfg is not None:
            try:
                return max(0, int(cfg.value))
            except ValueError:
                pass
    finally:
        db.close()
    return int(get_settings().file_retention_days or 0)


def set_retention_days(days: int, updated_by: str | None = None) -> int:
    """写入新的保留天数（管理员）。返回写入后的值。"""
    days = max(0, int(days))
    db = SessionLocal()
    try:
        cfg = db.get(SystemConfig, KEY_FILE_RETENTION_DAYS)
        if cfg is None:
            cfg = SystemConfig(key=KEY_FILE_RETENTION_DAYS, value=str(days), updated_by=updated_by)
            db.add(cfg)
        else:
            cfg.value = str(days)
            cfg.updated_by = updated_by
        db.commit()
        return days
    finally:
        db.close()


@dataclass
class CleanupResult:
    scanned: int = 0  # 扫描到的过期任务数
    deleted: int = 0  # 成功删除的任务数
    failed: int = 0  # 删除失败的任务数

    def as_dict(self) -> dict:
        return {"scanned": self.scanned, "deleted": self.deleted, "failed": self.failed}


def cleanup_expired_files() -> CleanupResult:
    """扫描并删除过期文件。

    保留天数读自 get_retention_days()；为 0 则跳过（永不过期）。
    仅清理终态任务（succeeded / failed），队列中或正在跑的任务不动。
    """
    result = CleanupResult()
    days = get_retention_days()
    if days <= 0:
        logger.info("file_retention_days<=0，跳过清理")
        return result

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    db = SessionLocal()
    try:
        stmt = select(TranslationTask).where(
            TranslationTask.created_at < cutoff,
            TranslationTask.status.in_([TaskStatus.SUCCEEDED, TaskStatus.FAILED]),
        )
        tasks = list(db.scalars(stmt))
        result.scanned = len(tasks)

        deleted_ids: list[str] = []

        for task in tasks:
            ok = True
            if task.source_object:
                ok = secure_remove_object(task.source_object) and ok
            if task.result_object:
                ok = secure_remove_object(task.result_object) and ok

            if ok:
                # 先收集成功删除的任务 ID，最后批量删除 DB 记录
                # 避免"MinIO 文件已删但 DB 记录还在"的不一致状态
                deleted_ids.append(task.id)
                result.deleted += 1
                logger.info("已清理过期任务 %s（用户=%s 文件=%s）", task.id, task.owner, task.original_filename)
            else:
                result.failed += 1
                logger.warning("清理任务 %s 失败：MinIO 对象删除异常", task.id)

        # 批量删除 DB 记录，一次性 commit
        if deleted_ids:
            db.execute(
                sa_delete(TranslationTask).where(TranslationTask.id.in_(deleted_ids))
            )
            db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.exception("cleanup_expired_files 发生异常：%s", exc)
        result.failed += 1
    finally:
        db.close()

    logger.info(
        "文件清理完成：scanned=%d deleted=%d failed=%d (保留 %d 天)",
        result.scanned,
        result.deleted,
        result.failed,
        days,
    )
    return result
