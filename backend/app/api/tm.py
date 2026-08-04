"""翻译记忆库管理接口。"""
from __future__ import annotations

import io

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from app.core.database import SessionLocal
from app.core.security import CurrentUser, require_admin
from app.models.translation_memory import TranslationMemory
from app.models.task import TaskStatus, TranslationTask
from app.services.terminology_repository import upsert_translation_memory as _upsert_tm_record

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


class ImportResult(BaseModel):
    imported: int
    skipped: int


def _to_read(t: TranslationMemory) -> TMRead:
    return TMRead(
        id=t.id,
        source_text=t.source_text,
        target_text=t.target_text,
        lang_pair=t.lang_pair,
        source=t.source,
        domain=t.domain,
        created_at=t.created_at.isoformat() if t.created_at else "",
        updated_at=t.updated_at.isoformat() if t.updated_at else "",
    )


def _upsert_tm(db, source_text: str, target_text: str, lang_pair: str,
               source: str, domain: str | None, task_id: str | None,
               username: str) -> bool:
    """使用唯一约束原子 UPSERT，避免并发 check-then-act 产生重复记录。"""
    _upsert_tm_record(
        db,
        source_text=source_text,
        target_text=target_text,
        lang_pair=lang_pair,
        source=source,
        domain=domain,
        task_id=task_id,
        username=username,
    )
    return True


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
        _upsert_tm(db, payload.source_text, payload.target_text, payload.lang_pair,
                   payload.source, payload.domain, None, user.username)
        db.commit()
        norm = payload.source_text.strip().lower()
        entry = db.scalar(
            select(TranslationMemory).where(
                TranslationMemory.lang_pair == payload.lang_pair,
                TranslationMemory.source_normalized == norm,
            )
        )
        return _to_read(entry)
    finally:
        db.close()


# ── 从已完成任务导入 TM（功能1）──────────────────────────────────────

def _extract_paragraphs(ext: str, data: bytes) -> list[str]:
    """抽取段落列表（用于原文/译文按索引对齐）。支持 docx/doc/txt/md，其余返回空。"""
    ext = ext.lower().lstrip(".")
    try:
        if ext in ("txt", "md"):
            from app.services.document_engine import _decode_text_bytes
            return [p for p in _decode_text_bytes(data).split("\n") if p.strip()]
        if ext in ("docx", "doc"):
            from docx import Document
            from app.services.document_engine import (
                _collect_docx_paragraphs, _extract_docx_paragraph_text, _libreoffice_convert,
            )
            docx_data = _libreoffice_convert(data, "doc", "docx") if ext == "doc" else data
            doc = Document(io.BytesIO(docx_data))
            return [_extract_docx_paragraph_text(p) for p in _collect_docx_paragraphs(doc)]
    except Exception:
        return []
    return []


@router.post("/import-from-task/{task_id}", response_model=ImportResult)
def import_from_task(task_id: str, user: CurrentUser = Depends(require_admin)):
    """把一个已完成任务的原文-译文段落对齐写入 TM（source=auto，带去重）。

    仅支持 docx/doc/txt/md（段落对齐清晰）。对照模式结果按 \n 拆出译文。
    """
    from app.core.storage import download_bytes

    db = SessionLocal()
    try:
        task = db.get(TranslationTask, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        if task.status not in (TaskStatus.SUCCEEDED,):
            raise HTTPException(status_code=400, detail="仅已成功的任务可导入 TM")
        if not task.source_object or not task.result_object:
            raise HTTPException(status_code=400, detail="原文或译文已被清理")
        ext = task.file_ext.lower().lstrip(".")
        if ext not in ("docx", "doc", "txt", "md"):
            raise HTTPException(status_code=400, detail=f"暂不支持 {ext} 格式从任务导入 TM（支持 docx/doc/txt/md）")

        src_paras = _extract_paragraphs(ext, download_bytes(task.source_object))
        res_paras = _extract_paragraphs(ext, download_bytes(task.result_object))
        if not src_paras or not res_paras:
            raise HTTPException(status_code=400, detail="无法从文件抽取段落")

        lang_pair = f"{(task.source_lang if task.source_lang != 'auto' else 'src')}→{task.target_lang}"
        # 若源语言是 auto，尽力推断：英文字母占比高则 en，否则 zh
        if task.source_lang == "auto":
            sample = " ".join(src_paras[:20])
            en_ratio = sum(c.isascii() and c.isalpha() for c in sample) / max(len(sample), 1)
            lang_pair = f"{'en' if en_ratio > 0.5 else 'zh'}→{task.target_lang}"

        imported = skipped = 0
        n = min(len(src_paras), len(res_paras))
        for i in range(n):
            s = src_paras[i].strip()
            r = res_paras[i].strip()
            if not s or len(s) < 8:
                continue
            # 对照模式：取换行后的译文部分
            t = r.split("\n", 1)[1].strip() if "\n" in r else r
            if not t or len(t) < 4 or t == s:
                skipped += 1
                continue
            _upsert_tm(db, s, t, lang_pair, "auto", None, task.id, user.username)
            imported += 1
        db.commit()
        return ImportResult(imported=imported, skipped=skipped)
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
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail="同一语种方向下已存在相同的归一化源文本",
            ) from exc
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
