"""模型配置 ORM 模型。"""
from __future__ import annotations

import enum
import uuid

from sqlalchemy import Boolean, Enum, Index, String, Text, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin


class ModelType(str, enum.Enum):
    """模型类型。"""
    TRANSLATION = "translation"  # 文本翻译模型
    VL = "vl"                    # 视觉语言模型（OCR）


class ModelConfig(TimestampMixin, Base):
    __tablename__ = "model_configs"
    __table_args__ = (
        Index(
            "uq_model_active_type",
            "model_type",
            unique=True,
            postgresql_where=text("is_active"),
            sqlite_where=text("is_active = 1"),
        ),
    )

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))

    # 模型名称（显示用）
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # 模型类型
    model_type: Mapped[ModelType] = mapped_column(
        Enum(ModelType, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )
    # 模型 ID（如 qwen-plus, qwen-vl-max 等）
    model_id: Mapped[str] = mapped_column(String(256), nullable=False)
    # API 地址
    api_base_url: Mapped[str] = mapped_column(String(512), nullable=False)
    # API Key
    api_key: Mapped[str] = mapped_column(Text, nullable=False)
    # 是否为当前选中
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))

    updated_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
