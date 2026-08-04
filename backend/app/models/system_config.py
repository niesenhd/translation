"""系统配置表（key/value），管理员可动态调整。

P0 阶段仅用于「文件保留天数」一项，结构通用便于后续扩展（并发上限、模型路由等）。
"""
from __future__ import annotations

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin


class SystemConfig(TimestampMixin, Base):
    __tablename__ = "system_config"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(512))
    updated_by: Mapped[str | None] = mapped_column(String(128), nullable=True)


# 已知的配置键（约束 + 默认值由调用方提供）
KEY_FILE_RETENTION_DAYS = "file_retention_days"
