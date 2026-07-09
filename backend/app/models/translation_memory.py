"""翻译记忆库 ORM 模型。"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class TranslationMemory(Base):
    __tablename__ = "translation_memories"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))

    # 源语言文本
    source_text: Mapped[str] = mapped_column(Text, nullable=False)
    # 目标语言文本
    target_text: Mapped[str] = mapped_column(Text, nullable=False)
    # 语种方向，如 "zh→en"
    lang_pair: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # 来源：manual（手动录入）/ feedback（反馈审核）/ auto（自动采集）
    source: Mapped[str] = mapped_column(String(32), default="manual")
    # 领域标签
    domain: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # 来源任务（从任务导入时记录，便于追溯/重导），nullable
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
