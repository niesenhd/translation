"""翻译任务 ORM 模型。"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class TaskStatus(str, enum.Enum):
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


class TranslationTask(Base):
    __tablename__ = "translation_tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner: Mapped[str] = mapped_column(String(128), index=True)

    original_filename: Mapped[str] = mapped_column(String(512))
    file_ext: Mapped[str] = mapped_column(String(16))
    source_lang: Mapped[str] = mapped_column(String(16), default="auto")
    target_lang: Mapped[str] = mapped_column(String(16))
    output_mode: Mapped[OutputMode] = mapped_column(Enum(OutputMode), default=OutputMode.PLAIN)
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

    source_object: Mapped[str] = mapped_column(String(512))  # MinIO 中原文对象 key
    result_object: Mapped[str | None] = mapped_column(String(512), nullable=True)

    status: Mapped[TaskStatus] = mapped_column(Enum(TaskStatus), default=TaskStatus.QUEUED, index=True)
    progress: Mapped[int] = mapped_column(Integer, default=0)  # 0-100
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
