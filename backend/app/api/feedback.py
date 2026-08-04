"""翻译质量反馈接口。"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.core.database import SessionLocal
from app.core.security import CurrentUser, get_current_user, require_admin
from app.models.feedback import FeedbackStatus, FeedbackType, QualityFeedback
from app.models.task import TranslationTask

router = APIRouter(prefix="/feedback", tags=["feedback"])


# ── Schemas ──────────────────────────────────────────────────────────

class FeedbackCreate(BaseModel):
    task_id: str
    rating: int | None = Field(default=None, ge=1, le=5)
    feedback_type: FeedbackType | None = None
    suggestion: str | None = None


class FeedbackReview(BaseModel):
    status: FeedbackStatus
    reject_reason: str | None = None


class FeedbackRead(BaseModel):
    id: str
    task_id: str
    username: str
    rating: int | None
    feedback_type: str | None
    suggestion: str | None
    status: str
    reject_reason: str | None
    reviewed_by: str | None
    created_at: str
    updated_at: str
    # 附加信息
    original_filename: str | None = None


class FeedbackPage(BaseModel):
    items: list[FeedbackRead]
    total: int


# ── 用户端：提交反馈 ─────────────────────────────────────────────────

@router.post("", response_model=FeedbackRead, status_code=status.HTTP_201_CREATED)
def create_feedback(payload: FeedbackCreate, user: CurrentUser = Depends(get_current_user)):
    db = SessionLocal()
    try:
        task_query = select(TranslationTask).where(TranslationTask.id == payload.task_id)
        if not user.is_admin:
            task_query = task_query.where(TranslationTask.owner == user.username)
        task = db.scalar(task_query)
        # 权限条件与 task_id 在同一 SQL 中执行；统一返回 404，避免泄露任务是否存在。
        if task is None:
            raise HTTPException(status_code=404, detail="任务不存在")

        entry = QualityFeedback(
            id=str(uuid.uuid4()),
            task_id=payload.task_id,
            username=user.username,
            rating=payload.rating,
            feedback_type=payload.feedback_type,
            suggestion=payload.suggestion,
        )
        db.add(entry)
        db.commit()
        db.refresh(entry)
        return _to_read(entry, task.original_filename)
    finally:
        db.close()


# ── 用户端：查看自己的反馈 ──────────────────────────────────────────

@router.get("/my", response_model=FeedbackPage)
def list_my_feedback(
    user: CurrentUser = Depends(get_current_user),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    db = SessionLocal()
    try:
        stmt = select(QualityFeedback).where(QualityFeedback.username == user.username)
        count_stmt = select(func.count()).select_from(QualityFeedback).where(
            QualityFeedback.username == user.username
        )
        total = db.scalar(count_stmt) or 0
        items = list(
            db.scalars(
                stmt.order_by(QualityFeedback.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        result = []
        for fb in items:
            task = db.get(TranslationTask, fb.task_id)
            result.append(_to_read(fb, task.original_filename if task else None))
        return FeedbackPage(items=result, total=total)
    finally:
        db.close()


# ── 管理员：查看所有反馈 ─────────────────────────────────────────────

@router.get("", response_model=FeedbackPage, dependencies=[Depends(require_admin)])
def list_all_feedback(
    status_filter: str = Query(default="", alias="status"),
    feedback_type: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    db = SessionLocal()
    try:
        stmt = select(QualityFeedback)
        count_stmt = select(func.count()).select_from(QualityFeedback)

        if status_filter:
            stmt = stmt.where(QualityFeedback.status == status_filter)
            count_stmt = count_stmt.where(QualityFeedback.status == status_filter)
        if feedback_type:
            stmt = stmt.where(QualityFeedback.feedback_type == feedback_type)
            count_stmt = count_stmt.where(QualityFeedback.feedback_type == feedback_type)

        total = db.scalar(count_stmt) or 0
        items = list(
            db.scalars(
                stmt.order_by(QualityFeedback.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        result = []
        for fb in items:
            task = db.get(TranslationTask, fb.task_id)
            result.append(_to_read(fb, task.original_filename if task else None))
        return FeedbackPage(items=result, total=total)
    finally:
        db.close()


# ── 管理员：审核反馈 ─────────────────────────────────────────────────

@router.put("/{feedback_id}", response_model=FeedbackRead)
def review_feedback(
    feedback_id: str,
    payload: FeedbackReview,
    user: CurrentUser = Depends(require_admin),
):
    db = SessionLocal()
    try:
        entry = db.get(QualityFeedback, feedback_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="反馈不存在")

        entry.status = payload.status
        entry.reviewed_by = user.username
        if payload.status == FeedbackStatus.REJECTED and payload.reject_reason:
            entry.reject_reason = payload.reject_reason
        db.commit()
        db.refresh(entry)

        task = db.get(TranslationTask, entry.task_id)
        return _to_read(entry, task.original_filename if task else None)
    finally:
        db.close()


# ── 管理员：采纳反馈并录入术语库/TM（功能2，补需求 2.11 缺口）──────────

class AdoptTermPayload(BaseModel):
    source_term: str
    target_term: str
    lang_pair: str = Field(max_length=32)
    domain: str | None = None


class AdoptTmpPayload(BaseModel):
    source_text: str
    target_text: str
    lang_pair: str = Field(max_length=32)


class FeedbackAdopt(BaseModel):
    """采纳反馈时可选择录入术语库和/或 TM。"""
    to_term: AdoptTermPayload | None = None
    to_tm: AdoptTmpPayload | None = None


@router.post("/{feedback_id}/adopt", response_model=FeedbackRead)
def adopt_feedback(
    feedback_id: str,
    payload: FeedbackAdopt,
    user: CurrentUser = Depends(require_admin),
):
    """采纳反馈：可选把建议录入术语库(strict) / TM(source=feedback)，并置 ADOPTED。"""
    from app.models.term import TermPriority
    from app.services.terminology_repository import upsert_term, upsert_translation_memory

    db = SessionLocal()
    try:
        entry = db.get(QualityFeedback, feedback_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="反馈不存在")

        if payload.to_term:
            t = payload.to_term
            upsert_term(
                db,
                source_term=t.source_term,
                target_term=t.target_term,
                lang_pair=t.lang_pair,
                domain=t.domain,
                priority=TermPriority.STRICT,
                note=None,
                username=user.username,
            )

        if payload.to_tm:
            m = payload.to_tm
            upsert_translation_memory(
                db,
                source_text=m.source_text,
                target_text=m.target_text,
                lang_pair=m.lang_pair,
                source="feedback",
                domain=None,
                task_id=None,
                username=user.username,
            )

        entry.status = FeedbackStatus.ADOPTED
        entry.reviewed_by = user.username
        db.commit()
        db.refresh(entry)

        task = db.get(TranslationTask, entry.task_id)
        return _to_read(entry, task.original_filename if task else None)
    finally:
        db.close()


# ── 辅助 ─────────────────────────────────────────────────────────────

def _to_read(fb: QualityFeedback, filename: str | None = None) -> FeedbackRead:
    return FeedbackRead(
        id=fb.id,
        task_id=fb.task_id,
        username=fb.username,
        rating=fb.rating,
        feedback_type=fb.feedback_type.value if fb.feedback_type else None,
        suggestion=fb.suggestion,
        status=fb.status.value,
        reject_reason=fb.reject_reason,
        reviewed_by=fb.reviewed_by,
        created_at=fb.created_at.isoformat() if fb.created_at else "",
        updated_at=fb.updated_at.isoformat() if fb.updated_at else "",
        original_filename=filename,
    )
