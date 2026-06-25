"""Pydantic schema：API 请求/响应模型。"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict

from app.models.task import OutputMode, PdfOutputFormat, TranslateImagesOption, TaskStatus


class TaskCreateResponse(BaseModel):
    id: str
    status: TaskStatus


class TaskRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    owner: str
    original_filename: str
    file_ext: str
    source_lang: str
    target_lang: str
    output_mode: OutputMode
    pdf_output_format: PdfOutputFormat
    translate_images: TranslateImagesOption
    status: TaskStatus
    progress: int
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    # 排队位置（仅 QUEUED 状态有值，1 表示下一个处理）
    queue_position: Optional[int] = None
    # 预计等待时间（秒，仅 QUEUED 状态有值）
    estimated_wait_seconds: Optional[int] = None
