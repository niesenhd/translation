"""翻译记忆库 ORM 模型。"""
from __future__ import annotations

import uuid

from sqlalchemy import Computed, ForeignKey, Index, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin


class TranslationMemory(TimestampMixin, Base):
    __tablename__ = "translation_memories"
    __table_args__ = (
        UniqueConstraint("lang_pair", "source_normalized", name="uq_tm_lang_source_norm"),
        Index("ix_tm_lang_source", "lang_pair", "source_normalized"),
        Index(
            "ix_tm_source_trgm",
            "source_normalized",
            postgresql_using="gin",
            postgresql_ops={"source_normalized": "gin_trgm_ops"},
        ),
    )

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))

    # 源语言文本
    source_text: Mapped[str] = mapped_column(Text, nullable=False)
    source_normalized: Mapped[str] = mapped_column(
        Text, Computed("lower(trim(source_text))", persisted=True), nullable=False
    )
    # 目标语言文本
    target_text: Mapped[str] = mapped_column(Text, nullable=False)
    # 语种方向，如 "zh→en"
    lang_pair: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # 来源：manual（手动录入）/ feedback（反馈审核）/ auto（自动采集）
    source: Mapped[str] = mapped_column(String(32), default="manual", server_default="manual")
    # 领域标签
    domain: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # 来源任务（从任务导入时记录，便于追溯/重导），nullable
    task_id: Mapped[str | None] = mapped_column(
        Uuid(as_uuid=False),
        ForeignKey("translation_tasks.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    updated_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
