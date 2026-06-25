"""模型配置 ORM 模型。"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ModelType(str, enum.Enum):
    """模型类型。"""
    TRANSLATION = "translation"  # 文本翻译模型
    VL = "vl"                    # 视觉语言模型（OCR）


class ModelConfig(Base):
    __tablename__ = "model_configs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))

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
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
