"""翻译质量反馈 ORM 模型。"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class FeedbackStatus(str, enum.Enum):
    PENDING = "pending"      # 待处理
    ADOPTED = "adopted"      # 已采纳
    REJECTED = "rejected"    # 已驳回


class FeedbackType(str, enum.Enum):
    TERMINOLOGY = "terminology"  # 术语错误
    GRAMMAR = "grammar"          # 语法问题
    FORMAT = "format"            # 格式错乱
    OMISSION = "omission"        # 漏译
    OTHER = "other"              # 其他


class QualityFeedback(Base):
    __tablename__ = "quality_feedbacks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))

    # 关联的翻译任务
    task_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    # 提交用户
    username: Mapped[str] = mapped_column(String(128), nullable=False)
    # 评分 1-5
    rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 问题类型
    feedback_type: Mapped[FeedbackType | None] = mapped_column(
        Enum(FeedbackType, values_callable=lambda x: [e.value for e in x]),
        nullable=True,
    )
    # 修改建议
    suggestion: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 处理状态
    status: Mapped[FeedbackStatus] = mapped_column(
        Enum(FeedbackStatus, values_callable=lambda x: [e.value for e in x]),
        default=FeedbackStatus.PENDING,
        server_default="pending",
    )
    # 驳回原因
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 审核人
    reviewed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
