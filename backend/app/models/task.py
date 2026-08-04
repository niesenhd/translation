"""翻译任务 ORM 模型。"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Enum, ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin


class TaskStatus(str, enum.Enum):
    UPLOADING = "uploading"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DELETED = "deleted"


class OutputMode(str, enum.Enum):
    PLAIN = "plain"  # 纯译文
    BILINGUAL = "bilingual"  # 中外对照


class PdfOutputFormat(str, enum.Enum):
    """PDF 翻译的输出格式选择（仅当源文件为 PDF 时生效）。"""

    PDF = "pdf"  # 原版 PDF 就地替换文字（PyMuPDF），最大限度保留版面/页眉页脚/图片
    DOCX = "docx"  # 转 Word（pdf2docx 路径），便于二次编辑


class TranslateImagesOption(str, enum.Enum):
    """是否翻译图片中的文字。"""

    YES = "yes"  # 图片中的文字也翻译（OCR + 就地替换）
    NO = "no"  # 仅翻译文档文字，图片保持原样


class RefineMode(str, enum.Enum):
    """两遍法精译模式（功能C）。"""

    NONE = "none"  # 单遍翻译（默认）
    DOUBLE_PASS = "double_pass"  # 翻译后再用法律译审复核一遍（成本约 2 倍，重要文书用）


class FootnoteMode(str, enum.Enum):
    """脚注处理方式（仅双语对照模式生效；文档无脚注时本选项无影响）。"""

    BILINGUAL = "bilingual"          # 脚注双语：英文原文 + 中文译文（默认）
    TRANSLATION_ONLY = "translation_only"  # 脚注仅译文：原文替换为中文，体积减半
    SKIP = "skip"                    # 脚注不翻译：保持英文原文


class TranslationTask(TimestampMixin, Base):
    __tablename__ = "translation_tasks"
    __table_args__ = (
        CheckConstraint(
            "progress >= 0 AND progress <= 100",
            name="ck_translation_tasks_progress",
        ),
    )

    id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    owner: Mapped[str] = mapped_column(
        String(128), ForeignKey("users.username"), index=True
    )

    original_filename: Mapped[str] = mapped_column(String(512))
    file_ext: Mapped[str] = mapped_column(String(16))
    source_lang: Mapped[str] = mapped_column(String(16), default="auto", server_default="auto")
    target_lang: Mapped[str] = mapped_column(String(16))
    output_mode: Mapped[OutputMode] = mapped_column(
        Enum(OutputMode, values_callable=lambda x: [e.value for e in x]),
        default=OutputMode.PLAIN,
        server_default=OutputMode.PLAIN.value,
    )
    # 仅 PDF 文件使用；其他格式忽略此字段
    pdf_output_format: Mapped[PdfOutputFormat] = mapped_column(
        Enum(PdfOutputFormat, values_callable=lambda x: [e.value for e in x]),
        default=PdfOutputFormat.PDF,
        server_default="pdf",
    )
    # 是否翻译图片中的文字
    translate_images: Mapped[TranslateImagesOption] = mapped_column(
        Enum(TranslateImagesOption, values_callable=lambda x: [e.value for e in x]),
        default=TranslateImagesOption.NO,
        server_default="no",
    )
    # 两遍法精译模式（功能C）：none=单遍（默认）；double_pass=翻译+法律译审复核
    refine_mode: Mapped[RefineMode] = mapped_column(
        Enum(RefineMode, values_callable=lambda x: [e.value for e in x]),
        default=RefineMode.NONE,
        server_default="none",
    )
    # 脚注处理方式（仅双语对照模式生效；文档无脚注时无影响）：bilingual=脚注双语(默认)；
    # translation_only=脚注仅译文；skip=脚注不翻译
    footnote_mode: Mapped[FootnoteMode] = mapped_column(
        Enum(FootnoteMode, values_callable=lambda x: [e.value for e in x]),
        default=FootnoteMode.BILINGUAL,
        server_default="bilingual",
    )

    source_object: Mapped[str] = mapped_column(String(512))  # MinIO 中原文对象 key
    result_object: Mapped[str | None] = mapped_column(String(512), nullable=True)

    status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus, values_callable=lambda x: [e.value for e in x]),
        default=TaskStatus.QUEUED,
        server_default=TaskStatus.QUEUED.value,
        index=True,
    )
    progress: Mapped[int] = mapped_column(Integer, default=0, server_default="0")  # 0-100
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 每次 worker 抢占任务都会生成新 attempt_id；所有心跳和收尾更新均须匹配它，
    # 防止旧 worker 在重试或孤儿对账后覆盖新一轮执行结果。
    attempt_id: Mapped[str | None] = mapped_column(
        Uuid(as_uuid=False), nullable=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
