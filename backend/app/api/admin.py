"""管理员后台接口。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.security import CurrentUser, require_admin
from app.models.system_config import SystemConfig
from app.models.task import TaskStatus, TranslationTask
from app.services.retention import (
    cleanup_expired_files,
    get_retention_days,
    set_retention_days,
)

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


# ══════════════════════════════════════════════════════════════════════
# 文件保留策略
# ══════════════════════════════════════════════════════════════════════

class RetentionRead(BaseModel):
    days: int


class RetentionUpdate(BaseModel):
    days: int = Field(ge=0, le=3650, description="0=永不过期，最多 10 年")


class CleanupResultRead(BaseModel):
    scanned: int
    deleted: int
    failed: int


@router.get("/retention", response_model=RetentionRead)
def read_retention() -> RetentionRead:
    return RetentionRead(days=get_retention_days())


@router.put("/retention", response_model=RetentionRead)
def update_retention(payload: RetentionUpdate, user: CurrentUser = Depends(require_admin)) -> RetentionRead:
    days = set_retention_days(payload.days, updated_by=user.username)
    return RetentionRead(days=days)


@router.post("/cleanup", response_model=CleanupResultRead)
def trigger_cleanup() -> CleanupResultRead:
    result = cleanup_expired_files()
    return CleanupResultRead(**result.as_dict())


# ══════════════════════════════════════════════════════════════════════
# 统计看板
# ══════════════════════════════════════════════════════════════════════

class DashboardStats(BaseModel):
    total_tasks: int
    succeeded_tasks: int
    failed_tasks: int
    running_tasks: int
    queued_tasks: int
    total_users: int
    # 按文件类型分布
    file_type_distribution: dict[str, int]
    # 最近 7 天每日翻译量
    daily_trend: list[dict]
    # 平均耗时（秒）
    avg_duration_seconds: float | None


@router.get("/stats", response_model=DashboardStats)
def get_stats():
    db = SessionLocal()
    try:
        # 基本统计
        total = db.scalar(select(func.count()).select_from(TranslationTask)) or 0
        succeeded = db.scalar(
            select(func.count()).select_from(TranslationTask).where(TranslationTask.status == TaskStatus.SUCCEEDED)
        ) or 0
        failed = db.scalar(
            select(func.count()).select_from(TranslationTask).where(TranslationTask.status == TaskStatus.FAILED)
        ) or 0
        running = db.scalar(
            select(func.count()).select_from(TranslationTask).where(TranslationTask.status == TaskStatus.RUNNING)
        ) or 0
        queued = db.scalar(
            select(func.count()).select_from(TranslationTask).where(TranslationTask.status == TaskStatus.QUEUED)
        ) or 0

        # 用户数
        total_users = db.scalar(
            select(func.count(func.distinct(TranslationTask.owner)))
        ) or 0

        # 文件类型分布
        type_rows = db.execute(
            select(TranslationTask.file_ext, func.count())
            .group_by(TranslationTask.file_ext)
        ).all()
        file_type_distribution = {row[0]: row[1] for row in type_rows}

        # 最近 7 天趋势
        daily_trend = []
        for i in range(6, -1, -1):
            day = datetime.now(timezone.utc) - timedelta(days=i)
            day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
            day_end = day_start + timedelta(days=1)
            count = db.scalar(
                select(func.count()).select_from(TranslationTask).where(
                    TranslationTask.created_at >= day_start,
                    TranslationTask.created_at < day_end,
                )
            ) or 0
            daily_trend.append({"date": day_start.strftime("%Y-%m-%d"), "count": count})

        # 平均耗时（succeeded 任务，从 created_at 到 updated_at）
        avg_duration = db.scalar(
            select(func.avg(
                func.extract("epoch", TranslationTask.updated_at) - func.extract("epoch", TranslationTask.created_at)
            )).where(TranslationTask.status == TaskStatus.SUCCEEDED)
        )

        return DashboardStats(
            total_tasks=total,
            succeeded_tasks=succeeded,
            failed_tasks=failed,
            running_tasks=running,
            queued_tasks=queued,
            total_users=total_users,
            file_type_distribution=file_type_distribution,
            daily_trend=daily_trend,
            avg_duration_seconds=round(avg_duration, 1) if avg_duration else None,
        )
    finally:
        db.close()


# ══════════════════════════════════════════════════════════════════════
# 模型配置管理
# ══════════════════════════════════════════════════════════════════════

KEY_TRANSLATION_MODEL = "translation_model"
KEY_VL_MODEL = "vl_model"
KEY_API_BASE_URL = "api_base_url"
KEY_API_KEY = "api_key"


class ModelConfigRead(BaseModel):
    translation_model: str
    vl_model: str
    api_base_url: str
    api_key_set: bool  # 不返回实际 key，只返回是否已设置


class ModelConfigUpdate(BaseModel):
    translation_model: str | None = None
    vl_model: str | None = None
    api_base_url: str | None = None
    api_key: str | None = None


def _get_config_value(key: str, default: str = "") -> str:
    """从 system_config 读取值，不存在则返回 default。"""
    db = SessionLocal()
    try:
        cfg = db.get(SystemConfig, key)
        if cfg is not None:
            return cfg.value
    finally:
        db.close()
    return default


def _set_config_value(key: str, value: str, updated_by: str | None = None) -> None:
    """写入 system_config。"""
    db = SessionLocal()
    try:
        cfg = db.get(SystemConfig, key)
        if cfg is None:
            cfg = SystemConfig(key=key, value=value, updated_by=updated_by)
            db.add(cfg)
        else:
            cfg.value = value
            cfg.updated_by = updated_by
        db.commit()
    finally:
        db.close()


@router.get("/model-config", response_model=ModelConfigRead)
def read_model_config():
    settings = get_settings()
    return ModelConfigRead(
        translation_model=_get_config_value(KEY_TRANSLATION_MODEL, settings.dashscope_model),
        vl_model=_get_config_value(KEY_VL_MODEL, settings.dashscope_vl_model),
        api_base_url=_get_config_value(KEY_API_BASE_URL, settings.dashscope_base_url),
        api_key_set=bool(_get_config_value(KEY_API_KEY, settings.dashscope_api_key)),
    )


@router.put("/model-config", response_model=ModelConfigRead)
def update_model_config(payload: ModelConfigUpdate, user: CurrentUser = Depends(require_admin)):
    if payload.translation_model is not None:
        _set_config_value(KEY_TRANSLATION_MODEL, payload.translation_model, user.username)
    if payload.vl_model is not None:
        _set_config_value(KEY_VL_MODEL, payload.vl_model, user.username)
    if payload.api_base_url is not None:
        _set_config_value(KEY_API_BASE_URL, payload.api_base_url, user.username)
    if payload.api_key is not None:
        _set_config_value(KEY_API_KEY, payload.api_key, user.username)

    # 重置翻译器单例，使新配置生效
    from app.services.translator import reset_translator
    reset_translator()

    return read_model_config()


# ══════════════════════════════════════════════════════════════════════
# 并发控制
# ══════════════════════════════════════════════════════════════════════

KEY_MAX_CONCURRENCY = "max_concurrency"


class ConcurrencyRead(BaseModel):
    max_concurrency: int
    current_running: int


class ConcurrencyUpdate(BaseModel):
    max_concurrency: int = Field(ge=1, le=20)


@router.get("/concurrency", response_model=ConcurrencyRead)
def read_concurrency():
    settings = get_settings()
    max_c = int(_get_config_value(KEY_MAX_CONCURRENCY, str(settings.translation_max_concurrency)))

    db = SessionLocal()
    try:
        current = db.scalar(
            select(func.count()).select_from(TranslationTask).where(
                TranslationTask.status == TaskStatus.RUNNING
            )
        ) or 0
    finally:
        db.close()

    return ConcurrencyRead(max_concurrency=max_c, current_running=current)


@router.put("/concurrency", response_model=ConcurrencyRead)
def update_concurrency(payload: ConcurrencyUpdate, user: CurrentUser = Depends(require_admin)):
    _set_config_value(KEY_MAX_CONCURRENCY, str(payload.max_concurrency), user.username)
    return read_concurrency()
