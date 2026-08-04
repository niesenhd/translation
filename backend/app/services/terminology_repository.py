"""术语与翻译记忆的并发安全写入操作。"""
from __future__ import annotations

import uuid

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.models.term import TermEntry, TermPriority
from app.models.translation_memory import TranslationMemory


def _insert_for(db, model):
    insert_fn = sqlite_insert if db.bind.dialect.name == "sqlite" else postgresql_insert
    return insert_fn(model)


def upsert_term(
    db,
    *,
    source_term: str,
    target_term: str,
    lang_pair: str,
    domain: str | None,
    priority: TermPriority,
    note: str | None,
    username: str,
) -> None:
    stmt = _insert_for(db, TermEntry).values(
        id=str(uuid.uuid4()),
        source_term=source_term,
        target_term=target_term,
        lang_pair=lang_pair,
        domain=domain,
        priority=priority,
        note=note,
        updated_by=username,
    )
    excluded = stmt.excluded
    db.execute(
        stmt.on_conflict_do_update(
            index_elements=[TermEntry.lang_pair, TermEntry.source_normalized],
            set_={
                "source_term": excluded.source_term,
                "target_term": excluded.target_term,
                "domain": excluded.domain,
                "priority": excluded.priority,
                "note": excluded.note,
                "updated_by": excluded.updated_by,
                "updated_at": func.now(),
            },
        )
    )


def upsert_translation_memory(
    db,
    *,
    source_text: str,
    target_text: str,
    lang_pair: str,
    source: str,
    domain: str | None,
    task_id: str | None,
    username: str,
) -> None:
    stmt = _insert_for(db, TranslationMemory).values(
        id=str(uuid.uuid4()),
        source_text=source_text,
        target_text=target_text,
        lang_pair=lang_pair,
        source=source,
        domain=domain,
        task_id=task_id,
        updated_by=username,
    )
    excluded = stmt.excluded
    db.execute(
        stmt.on_conflict_do_update(
            index_elements=[TranslationMemory.lang_pair, TranslationMemory.source_normalized],
            set_={
                "source_text": excluded.source_text,
                "target_text": excluded.target_text,
                "source": excluded.source,
                "domain": func.coalesce(excluded.domain, TranslationMemory.domain),
                "task_id": func.coalesce(excluded.task_id, TranslationMemory.task_id),
                "updated_by": excluded.updated_by,
                "updated_at": func.now(),
            },
        )
    )
