"""术语库 ORM 模型。"""
from __future__ import annotations

import enum
import uuid

from sqlalchemy import Computed, Enum, Index, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin


class TermPriority(str, enum.Enum):
    """术语优先级。"""
    STRICT = "strict"      # 强制：必须使用此翻译
    PREFERRED = "preferred"  # 优先：建议使用此翻译


class TermEntry(TimestampMixin, Base):
    __tablename__ = "term_entries"
    __table_args__ = (
        UniqueConstraint("lang_pair", "source_normalized", name="uq_term_lang_source_norm"),
        Index("ix_term_lang_source", "lang_pair", "source_term"),
    )

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))

    # 中文术语（源语言术语）
    source_term: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    source_normalized: Mapped[str] = mapped_column(
        String(512), Computed("lower(trim(source_term))", persisted=True), nullable=False
    )
    # 目标语言术语
    target_term: Mapped[str] = mapped_column(String(512), nullable=False)
    # 语种方向，如 "zh→en"
    lang_pair: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # 领域标签，如 "合同"、"侵权"
    domain: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # 优先级
    priority: Mapped[TermPriority] = mapped_column(
        Enum(TermPriority, values_callable=lambda x: [e.value for e in x]),
        default=TermPriority.PREFERRED,
        server_default="preferred",
    )
    # 备注
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    updated_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
