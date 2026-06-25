"""翻译记忆库管理接口。"""
from __future__ import annotations

import io
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select

from app.core.database import SessionLocal
from app.core.security import CurrentUser, require_admin
from app.models.translation_memory import TranslationMemory

router = APIRouter(prefix="/admin/tm", tags=["translation-memory"], dependencies=[Depends(require_admin)])


# ── Schemas ──────────────────────────────────────────────────────────

class TMCreate(BaseModel):
    source_text: str
    target_text: str
    lang_pair: str = Field(max_length=32)
    source: str = Field(default="manual", max_length=32)
    domain: str | None = None


class TMUpdate(BaseModel):
    source_text: str | None = None
    target_text: str | None = None
    lang_pair: str | None = Field(default=None, max_length=32)
    source: str | None = Field(default=None, max_length=32)
    domain: str | None = None


class TMRead(BaseModel):
    id: str
    source_text: str
    target_text: str
    lang_pair: str
    source: str
    domain: str | None
    created_at: str
    updated_at: str


class TMPage(BaseModel):
    items: list[TMRead]
    total: int


# ── CRUD ─────────────────────────────────────────────────────────────

@router.get("", response_model=TMPage)
def list_tm(
    keyword: str = Query(default=""),
    lang_pair: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    db = SessionLocal()
    try:
        stmt = select(TranslationMemory)
        count_stmt = select(func.count()).select_from(TranslationMemory)

        if keyword:
            stmt = stmt.where(TranslationMemory.source_text.ilike(f"%{keyword}%"))
            count_stmt = count_stmt.where(TranslationMemory.source_text.ilike(f"%{keyword}%"))
        if lang_pair:
            stmt = stmt.where(TranslationMemory.lang_pair == lang_pair)
            count_stmt = count_stmt.where(TranslationMemory.lang_pair == lang_pair)

        total = db.scalar(count_stmt) or 0
        items = list(
            db.scalars(
                stmt.order_by(TranslationMemory.updated_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        return TMPage(
            items=[
                TMRead(
                    id=t.id,
                    source_text=t.source_text,
                    target_text=t.target_text,
                    lang_pair=t.lang_pair,
                    source=t.source,
                    domain=t.domain,
                    created_at=t.created_at.isoformat() if t.created_at else "",
                    updated_at=t.updated_at.isoformat() if t.updated_at else "",
                )
                for t in items
            ],
            total=total,
        )
    finally:
        db.close()


@router.post("", response_model=TMRead, status_code=status.HTTP_201_CREATED)
def create_tm(payload: TMCreate, user: CurrentUser = Depends(require_admin)):
    db = SessionLocal()
    try:
        entry = TranslationMemory(
            id=str(uuid.uuid4()),
            source_text=payload.source_text,
            target_text=payload.target_text,
            lang_pair=payload.lang_pair,
            source=payload.source,
            domain=payload.domain,
            updated_by=user.username,
        )
        db.add(entry)
        db.commit()
        db.refresh(entry)
        return TMRead(
            id=entry.id,
            source_text=entry.source_text,
            target_text=entry.target_text,
            lang_pair=entry.lang_pair,
            source=entry.source,
            domain=entry.domain,
            created_at=entry.created_at.isoformat() if entry.created_at else "",
            updated_at=entry.updated_at.isoformat() if entry.updated_at else "",
        )
    finally:
        db.close()


@router.put("/{tm_id}", response_model=TMRead)
def update_tm(tm_id: str, payload: TMUpdate, user: CurrentUser = Depends(require_admin)):
    db = SessionLocal()
    try:
        entry = db.get(TranslationMemory, tm_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="记录不存在")
        for field, value in payload.model_dump(exclude_unset=True).items():
            setattr(entry, field, value)
        entry.updated_by = user.username
        db.commit()
        db.refresh(entry)
        return TMRead(
            id=entry.id,
            source_text=entry.source_text,
            target_text=entry.target_text,
            lang_pair=entry.lang_pair,
            source=entry.source,
            domain=entry.domain,
            created_at=entry.created_at.isoformat() if entry.created_at else "",
            updated_at=entry.updated_at.isoformat() if entry.updated_at else "",
        )
    finally:
        db.close()


@router.delete("/{tm_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_tm(tm_id: str):
    db = SessionLocal()
    try:
        entry = db.get(TranslationMemory, tm_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="记录不存在")
        db.delete(entry)
        db.commit()
    finally:
        db.close()


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
def batch_delete_tm(ids: list[str]):
    db = SessionLocal()
    try:
        db.execute(delete(TranslationMemory).where(TranslationMemory.id.in_(ids)))
        db.commit()
    finally:
        db.close()
