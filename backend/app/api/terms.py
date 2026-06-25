"""术语库管理接口。"""
from __future__ import annotations

import io
import uuid

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select

from app.core.database import SessionLocal
from app.core.security import CurrentUser, require_admin
from app.models.term import TermEntry, TermPriority

router = APIRouter(prefix="/admin/terms", tags=["terms"], dependencies=[Depends(require_admin)])


# ── Schemas ──────────────────────────────────────────────────────────

class TermCreate(BaseModel):
    source_term: str = Field(max_length=512)
    target_term: str = Field(max_length=512)
    lang_pair: str = Field(max_length=32)
    domain: str | None = None
    priority: TermPriority = TermPriority.PREFERRED
    note: str | None = None


class TermUpdate(BaseModel):
    source_term: str | None = Field(default=None, max_length=512)
    target_term: str | None = Field(default=None, max_length=512)
    lang_pair: str | None = Field(default=None, max_length=32)
    domain: str | None = None
    priority: TermPriority | None = None
    note: str | None = None


class TermRead(BaseModel):
    id: str
    source_term: str
    target_term: str
    lang_pair: str
    domain: str | None
    priority: str
    note: str | None
    created_at: str
    updated_at: str

    class Config:
        from_attributes = True


class TermPage(BaseModel):
    items: list[TermRead]
    total: int


# ── CRUD ─────────────────────────────────────────────────────────────

@router.get("", response_model=TermPage)
def list_terms(
    keyword: str = Query(default="", description="按中文术语关键词搜索"),
    lang_pair: str = Query(default="", description="按语种方向筛选"),
    domain: str = Query(default="", description="按领域筛选"),
    sort_by: str = Query(default="updated_at", description="排序字段：source_term/target_term/lang_pair/domain/created_at/updated_at"),
    sort_order: str = Query(default="desc", description="排序方向：asc/desc"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    db = SessionLocal()
    try:
        stmt = select(TermEntry)
        count_stmt = select(func.count()).select_from(TermEntry)

        if keyword:
            stmt = stmt.where(TermEntry.source_term.ilike(f"%{keyword}%"))
            count_stmt = count_stmt.where(TermEntry.source_term.ilike(f"%{keyword}%"))
        if lang_pair:
            stmt = stmt.where(TermEntry.lang_pair == lang_pair)
            count_stmt = count_stmt.where(TermEntry.lang_pair == lang_pair)
        if domain:
            stmt = stmt.where(TermEntry.domain == domain)
            count_stmt = count_stmt.where(TermEntry.domain == domain)

        # 排序字段白名单
        sort_col_map = {
            "source_term": TermEntry.source_term,
            "target_term": TermEntry.target_term,
            "lang_pair": TermEntry.lang_pair,
            "domain": TermEntry.domain,
            "created_at": TermEntry.created_at,
            "updated_at": TermEntry.updated_at,
        }
        sort_col = sort_col_map.get(sort_by, TermEntry.updated_at)
        order_clause = sort_col.desc() if sort_order.lower() == "desc" else sort_col.asc()

        total = db.scalar(count_stmt) or 0
        items = list(
            db.scalars(
                stmt.order_by(order_clause)
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        return TermPage(
            items=[
                TermRead(
                    id=t.id,
                    source_term=t.source_term,
                    target_term=t.target_term,
                    lang_pair=t.lang_pair,
                    domain=t.domain,
                    priority=t.priority.value,
                    note=t.note,
                    created_at=t.created_at.isoformat() if t.created_at else "",
                    updated_at=t.updated_at.isoformat() if t.updated_at else "",
                )
                for t in items
            ],
            total=total,
        )
    finally:
        db.close()


@router.post("", response_model=TermRead, status_code=status.HTTP_201_CREATED)
def create_term(payload: TermCreate, user: CurrentUser = Depends(require_admin)):
    db = SessionLocal()
    try:
        entry = TermEntry(
            id=str(uuid.uuid4()),
            source_term=payload.source_term,
            target_term=payload.target_term,
            lang_pair=payload.lang_pair,
            domain=payload.domain,
            priority=payload.priority,
            note=payload.note,
            updated_by=user.username,
        )
        db.add(entry)
        db.commit()
        db.refresh(entry)
        return TermRead(
            id=entry.id,
            source_term=entry.source_term,
            target_term=entry.target_term,
            lang_pair=entry.lang_pair,
            domain=entry.domain,
            priority=entry.priority.value,
            note=entry.note,
            created_at=entry.created_at.isoformat() if entry.created_at else "",
            updated_at=entry.updated_at.isoformat() if entry.updated_at else "",
        )
    finally:
        db.close()


@router.put("/{term_id}", response_model=TermRead)
def update_term(term_id: str, payload: TermUpdate, user: CurrentUser = Depends(require_admin)):
    db = SessionLocal()
    try:
        entry = db.get(TermEntry, term_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="术语不存在")
        for field, value in payload.model_dump(exclude_unset=True).items():
            setattr(entry, field, value)
        entry.updated_by = user.username
        db.commit()
        db.refresh(entry)
        return TermRead(
            id=entry.id,
            source_term=entry.source_term,
            target_term=entry.target_term,
            lang_pair=entry.lang_pair,
            domain=entry.domain,
            priority=entry.priority.value,
            note=entry.note,
            created_at=entry.created_at.isoformat() if entry.created_at else "",
            updated_at=entry.updated_at.isoformat() if entry.updated_at else "",
        )
    finally:
        db.close()


@router.delete("/{term_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_term(term_id: str):
    db = SessionLocal()
    try:
        entry = db.get(TermEntry, term_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="术语不存在")
        db.delete(entry)
        db.commit()
    finally:
        db.close()


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
def batch_delete_terms(ids: list[str]):
    """批量删除术语。"""
    db = SessionLocal()
    try:
        db.execute(delete(TermEntry).where(TermEntry.id.in_(ids)))
        db.commit()
    finally:
        db.close()


@router.post("/batch-delete", status_code=status.HTTP_200_OK)
def batch_delete_terms_post(payload: dict):
    """批量删除术语（POST 兼容版本，参数为 {"ids": [...]}）"""
    ids = payload.get("ids", [])
    if not ids:
        return {"deleted": 0}
    db = SessionLocal()
    try:
        result = db.execute(delete(TermEntry).where(TermEntry.id.in_(ids)))
        db.commit()
        return {"deleted": result.rowcount}
    finally:
        db.close()


# ── 文件导入/导出 ────────────────────────────────────────────────────

@router.post("/import", response_model=dict)
def import_terms(
    file: UploadFile = File(...),
    domain: str = Form(default=""),
    user: CurrentUser = Depends(require_admin),
):
    """从 Excel/CSV/SDLTB 批量导入术语。

    支持格式：
    - Excel (.xlsx / .xls): 列顺序：中文术语、目标语言术语、语种方向、领域标签、优先级、备注
    - CSV (.csv): 同 Excel 列顺序
    - SDLTB (.sdltb): SDL Trados 术语库格式（Access/SQLite 数据库）

    同语种方向下中文术语重复则覆盖。

    可选参数 domain：当传入时，作为本次导入的领域。文件中没有领域字段或为空时使用此值。
    """
    filename = (file.filename or "").lower()
    content = file.file.read()
    default_domain = (domain or "").strip() or None

    if filename.endswith(".sdltb"):
        return _import_sdltb(content, user, default_domain=default_domain)
    elif filename.endswith(".csv"):
        import csv
        reader = csv.reader(io.StringIO(content.decode("utf-8-sig")))
        rows = list(reader)
    elif filename.endswith(".xls"):
        # 老版 Excel 用 xlrd 解析
        try:
            import xlrd
            book = xlrd.open_workbook(file_contents=content)
            sheet = book.sheet_by_index(0)
            rows = [sheet.row_values(r) for r in range(sheet.nrows)]
        except Exception as e:
            raise HTTPException(status_code=400, detail=f".xls 文件解析失败：{e}")
    else:
        # 默认按 .xlsx Excel 处理
        try:
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True)
            ws = wb.active
            rows = [list(row) for row in ws.iter_rows(values_only=True)]
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"文件解析失败：{e}")

    return _import_rows(rows, user, default_domain=default_domain)


def _import_rows(rows: list, user, default_domain: str | None = None) -> dict:
    """从行数据导入术语（Excel/CSV 解析后的行列表）。

    default_domain：当行内 domain 为空时使用的默认领域。
    """
    db = SessionLocal()
    try:
        imported = 0
        skipped = 0
        for i, row in enumerate(rows):
            # 跳过表头（第一行包含"中文"等字样）
            if i == 0 and row and any(str(c).strip() in ("中文术语", "source_term", "源语言") for c in row if c):
                continue
            if len(row) < 2:
                skipped += 1
                continue

            source_term = str(row[0] or "").strip()
            target_term = str(row[1] or "").strip()
            if not source_term or not target_term:
                skipped += 1
                continue

            lang_pair = str(row[2] or "zh→en").strip() if len(row) > 2 else "zh→en"
            row_domain = str(row[3] or "").strip() if len(row) > 3 else ""
            domain = row_domain or default_domain
            priority_str = str(row[4] or "preferred").strip().lower() if len(row) > 4 else "preferred"
            note = str(row[5] or "").strip() if len(row) > 5 else None

            priority = TermPriority.STRICT if priority_str == "strict" else TermPriority.PREFERRED

            # 去重：同语种方向下中文术语重复则覆盖
            existing = db.scalar(
                select(TermEntry).where(
                    TermEntry.source_term == source_term,
                    TermEntry.lang_pair == lang_pair,
                )
            )
            if existing:
                existing.target_term = target_term
                existing.domain = domain
                existing.priority = priority
                existing.note = note
                existing.updated_by = user.username
            else:
                db.add(TermEntry(
                    id=str(uuid.uuid4()),
                    source_term=source_term,
                    target_term=target_term,
                    lang_pair=lang_pair,
                    domain=domain,
                    priority=priority,
                    note=note,
                    updated_by=user.username,
                ))
            imported += 1
        db.commit()
        return {"imported": imported, "skipped": skipped}
    finally:
        db.close()


def _import_sdltb(content: bytes, user, default_domain: str | None = None) -> dict:
    """从 SDLTB 文件导入术语。

    SDLTB 是 SDL Trados 的术语库格式，本质是 Microsoft Access (Jet) 数据库。
    典型表结构（MultiTerm 格式）：
    - I_English / I_Chinese / I_xxx: 各语言术语表
      列: conceptid, origterm, termstatus 等
    - GLOSSARY: 术语库元信息

    使用 access-parser 纯 Python 库读取，无需 ODBC/Jet 驱动。
    """
    import tempfile
    import os
    import logging

    logger = logging.getLogger(__name__)

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".sdltb", delete=False) as tmp:
            tmp.write(content)
            tmp_path = tmp.name

        # 尝试使用 access-parser 读取 Access 数据库
        try:
            from access_parser import AccessParser
            db = AccessParser(tmp_path)
            tables = db.catalog
            logger.info("SDLTB 表列表: %s", list(tables.keys()))
        except ImportError:
            raise HTTPException(
                status_code=400,
                detail="缺少 access-parser 库，请运行: pip install access-parser"
            )
        except Exception as e:
            # 如果 access-parser 失败，尝试 SQLite 方式（部分新版 SDLTB 可能用 SQLite）
            logger.warning("access-parser 解析失败，尝试 SQLite: %s", e)
            return _import_sdltb_sqlite(content, user, default_domain=default_domain)

        # 提取术语数据
        entries = _extract_sdltb_access(db, tables)

        if not entries:
            raise HTTPException(status_code=400, detail="SDLTB 文件中未找到术语数据，或表结构不被支持")

        # 转换为行格式并复用 _import_rows
        rows = []
        for entry in entries:
            rows.append([
                entry.get("source_term", ""),
                entry.get("target_term", ""),
                entry.get("lang_pair", "zh→en"),
                entry.get("domain", ""),
                "preferred",
                entry.get("note", ""),
            ])

        return _import_rows(rows, user, default_domain=default_domain)

    except HTTPException:
        raise
    except Exception as e:
        logger.error("SDLTB 解析失败: %s", e)
        raise HTTPException(status_code=400, detail=f"SDLTB 文件解析失败：{e}")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _extract_sdltb_access(db, tables: dict) -> list[dict]:
    """从 Access 格式的 SDLTB 中提取术语。

    MultiTerm SDLTB 的典型表结构：
    - I_English: 英文术语表，包含 conceptid, origterm 等列
    - I_Chinese: 中文术语表
    - I_Japanese: 日文术语表
    - 其他 I_xxx 表对应不同语言

    每个 I_xxx 表中的 conceptid 关联同一概念在不同语言中的术语。
    """
    import logging
    logger = logging.getLogger(__name__)

    # 找出所有语言表（以 I_ 开头的表）
    lang_tables = {}
    for table_name in tables:
        if table_name.startswith("I_"):
            lang_code = table_name[2:]  # 去掉 "I_" 前缀
            lang_tables[lang_code] = table_name

    if not lang_tables:
        # 尝试其他命名模式
        for table_name in tables:
            name_lower = table_name.lower()
            if any(kw in name_lower for kw in ["term", "lang", "entry", "concept"]):
                lang_tables[table_name] = table_name

    logger.info("发现语言表: %s", lang_tables)

    if not lang_tables:
        return []

    # 语言代码映射
    lang_name_map = {
        "english": "en", "chinese": "zh", "japanese": "ja", "korean": "ko",
        "french": "fr", "german": "de", "spanish": "es", "italian": "it",
        "portuguese": "pt", "russian": "ru", "arabic": "ar", "hindi": "hi",
        "thai": "th", "vietnamese": "vi", "indonesian": "id", "malay": "ms",
        "dutch": "nl", "polish": "pl", "turkish": "tr", "swedish": "sv",
        "danish": "da", "finnish": "fi", "norwegian": "no", "czech": "cs",
        "hungarian": "hu", "romanian": "ro", "greek": "el", "hebrew": "he",
        "ukrainian": "uk", "bengali": "bn", "tamil": "ta",
    }

    # 读取每个语言表的数据，按 conceptid 分组
    # conceptid -> {lang_code: term_text}
    concept_map: dict[str, dict[str, str]] = {}

    for lang_name, table_name in lang_tables.items():
        try:
            table_data = db.parse_table(table_name)
            if not table_data:
                logger.warning("表 %s 解析结果为空", table_name)
                continue

            # 获取列名
            columns = list(table_data.keys())
            logger.info("表 %s 列: %s", table_name, columns)

            # 查找 conceptid 和 origterm 列
            concept_col = None
            term_col = None
            for col in columns:
                col_lower = col.lower()
                if "conceptid" in col_lower or "concept_id" in col_lower or col_lower == "id":
                    concept_col = col
                if "origterm" in col_lower or "term" in col_lower or col_lower == "termtext":
                    if term_col is None or "origterm" in col_lower:
                        term_col = col

            if not concept_col or not term_col:
                logger.warning("表 %s 中未找到 conceptid 或 term 列 (concept_col=%s, term_col=%s)", table_name, concept_col, term_col)
                continue

            # 标准化语言代码
            lang_code = lang_name_map.get(lang_name.lower(), lang_name.lower())

            # 读取数据
            row_count = len(table_data[concept_col])
            logger.info("表 %s 共 %d 行数据, concept_col=%s, term_col=%s", table_name, row_count, concept_col, term_col)

            # 调试：打印前3行
            for i in range(min(3, row_count)):
                cid = table_data[concept_col][i]
                term = table_data[term_col][i]
                logger.info("  行%d: conceptid=%s (type=%s), origterm=%s (type=%s)", i, cid, type(cid).__name__, term, type(term).__name__)

            for i in range(row_count):
                raw_cid = table_data[concept_col][i]
                # 统一 conceptid 格式：int/float/str 都转为整数字符串
                try:
                    concept_id = str(int(float(raw_cid)))
                except (ValueError, TypeError):
                    concept_id = str(raw_cid or "")
                term_text = str(table_data[term_col][i] or "").strip()

                if not concept_id or not term_text:
                    continue

                if concept_id not in concept_map:
                    concept_map[concept_id] = {}
                concept_map[concept_id][lang_code] = term_text

        except Exception as exc:
            logger.warning("解析表 %s 失败: %s", table_name, exc)
            continue

    if not concept_map:
        logger.warning("concept_map 为空，未提取到任何术语数据")
        return []

    logger.info("concept_map 共 %d 个概念", len(concept_map))

    # 调试：检查前几个概念的语言分布
    for i, (cid, lt) in enumerate(concept_map.items()):
        if i < 5:
            logger.info("  概念 %s: 语言=%s", cid, list(lt.keys()))

    # 配对术语
    entries = []
    no_pair_count = 0
    for concept_id, lang_terms in concept_map.items():
        if len(lang_terms) < 2:
            no_pair_count += 1
            continue

        # 找中文作为源语言
        zh_terms = [t for l, t in lang_terms.items() if l == "zh"]
        other_terms = [(l, t) for l, t in lang_terms.items() if l != "zh"]

        if zh_terms and other_terms:
            source = zh_terms[0]
            for target_lang, target_text in other_terms:
                entries.append({
                    "source_term": source,
                    "target_term": target_text,
                    "lang_pair": f"zh→{target_lang}",
                })
        else:
            # 没有中文，取排序最前的语言作为源
            sorted_langs = sorted(lang_terms.items())
            source_lang, source_text = sorted_langs[0]
            for target_lang, target_text in sorted_langs[1:]:
                entries.append({
                    "source_term": source_text,
                    "target_term": target_text,
                    "lang_pair": f"{source_lang}→{target_lang}",
                })

    logger.info("配对结果: %d 条术语, %d 个概念无配对", len(entries), no_pair_count)
    return entries


def _import_sdltb_sqlite(content: bytes, user, default_domain: str | None = None) -> dict:
    """从 SQLite 格式的 SDLTB 导入术语（部分新版 SDLTB 可能使用 SQLite）。"""
    import sqlite3
    import tempfile
    import os
    import logging

    logger = logging.getLogger(__name__)

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".sdltb", delete=False) as tmp:
            tmp.write(content)
            tmp_path = tmp.name

        conn = sqlite3.connect(tmp_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row["name"] for row in cursor.fetchall()]
        logger.info("SDLTB (SQLite) 表: %s", tables)

        entries = _extract_sdltb_sqlite_entries(cursor, tables)
        conn.close()

        if not entries:
            raise HTTPException(status_code=400, detail="SDLTB 文件中未找到术语数据，或表结构不被支持")

        rows = []
        for entry in entries:
            rows.append([
                entry.get("source_term", ""),
                entry.get("target_term", ""),
                entry.get("lang_pair", "zh→en"),
                entry.get("domain", ""),
                "preferred",
                entry.get("note", ""),
            ])

        return _import_rows(rows, user, default_domain=default_domain)

    except HTTPException:
        raise
    except Exception as e:
        logger.error("SDLTB (SQLite) 解析失败: %s", e)
        raise HTTPException(status_code=400, detail=f"SDLTB 文件解析失败：{e}")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _extract_sdltb_sqlite_entries(cursor, tables: list[str]) -> list[dict]:
    """从 SQLite 格式的 SDLTB 提取术语。"""
    import logging
    logger = logging.getLogger(__name__)

    entries = []

    # 策略1：尝试 mt_* 表结构
    if "mt_term" in tables and "mt_lang" in tables:
        try:
            cursor.execute("""
                SELECT e.entry_id, l.lang, t.term
                FROM mt_term t
                JOIN mt_lang l ON t.lang_id = l.id
                JOIN mt_concept e ON l.concept_id = e.id
                ORDER BY e.entry_id, l.lang
            """)
            entry_map: dict[int, list[tuple[str, str]]] = {}
            for row in cursor.fetchall():
                entry_id = row[0]
                lang_code = row[1]
                term_text = row[2]
                if entry_id not in entry_map:
                    entry_map[entry_id] = []
                entry_map[entry_id].append((lang_code, term_text))
            entries = _pair_terms_by_entry(entry_map)
            if entries:
                return entries
        except Exception as exc:
            logger.debug("mt_* 表提取失败: %s", exc)

    # 策略2：尝试 tbx_* 表结构
    if "tbx_term" in tables and "tbx_lang" in tables:
        try:
            cursor.execute("""
                SELECT e.id, l.lang, t.term
                FROM tbx_term t
                JOIN tbx_lang l ON t.lang_id = l.id
                JOIN tbx_entry e ON l.entry_id = e.id
                ORDER BY e.id, l.lang
            """)
            entry_map: dict[int, list[tuple[str, str]]] = {}
            for row in cursor.fetchall():
                entry_id = row[0]
                lang_code = row[1]
                term_text = row[2]
                if entry_id not in entry_map:
                    entry_map[entry_id] = []
                entry_map[entry_id].append((lang_code, term_text))
            entries = _pair_terms_by_entry(entry_map)
            if entries:
                return entries
        except Exception as exc:
            logger.debug("tbx_* 表提取失败: %s", exc)

    # 策略3：扫描 I_ 开头的表（Access 兼容模式）
    i_tables = [t for t in tables if t.startswith("I_")]
    if i_tables:
        logger.info("发现 I_ 开头的表: %s", i_tables)
        # SQLite 中不太可能有 I_ 表，但以防万一
        entry_map: dict[str, list[tuple[str, str]]] = {}
        for table in i_tables:
            lang_code = table[2:].lower()
            try:
                cursor.execute(f"PRAGMA table_info({table})")
                columns = [row[1] for row in cursor.fetchall()]
                term_col = None
                concept_col = None
                for col in columns:
                    col_l = col.lower()
                    if "origterm" in col_l or col_l == "term":
                        term_col = col
                    if "conceptid" in col_l or col_l == "id":
                        concept_col = col
                if term_col and concept_col:
                    cursor.execute(f"SELECT {concept_col}, {term_col} FROM {table}")
                    for row in cursor.fetchall():
                        cid = str(row[0])
                        term = str(row[1]).strip()
                        if cid not in entry_map:
                            entry_map[cid] = []
                        entry_map[cid].append((lang_code, term))
            except Exception as exc:
                logger.debug("表 %s 提取失败: %s", table, exc)

        if entry_map:
            return _pair_terms_by_entry(entry_map)

    return entries


def _pair_terms_by_entry(entry_map: dict[int, list[tuple[str, str]]]) -> list[dict]:
    """将按 entry_id 分组的术语配对为源语言→目标语言。"""
    # 语言代码映射
    lang_map = {
        "zh": "zh", "zh-CN": "zh", "zh-TW": "zh", "zh-Hans": "zh", "zh-Hant": "zh",
        "en": "en", "en-US": "en", "en-GB": "en",
        "ja": "ja", "ja-JP": "ja",
        "ko": "ko", "ko-KR": "ko",
        "fr": "fr", "fr-FR": "fr",
        "de": "de", "de-DE": "de",
        "es": "es", "es-ES": "es",
        "ru": "ru", "ru-RU": "ru",
        "pt": "pt", "pt-BR": "pt", "pt-PT": "pt",
        "it": "it", "it-IT": "it",
        "ar": "ar", "ar-SA": "ar",
    }

    entries = []
    for entry_id, terms in entry_map.items():
        if len(terms) < 2:
            continue

        # 标准化语言代码
        normalized = []
        for lang_code, term_text in terms:
            base_lang = lang_map.get(lang_code, lang_code.split("-")[0].lower())
            normalized.append((base_lang, term_text))

        # 找中文作为源语言，其他语言作为目标
        zh_terms = [t for l, t in normalized if l == "zh"]
        other_terms = [(l, t) for l, t in normalized if l != "zh"]

        if zh_terms and other_terms:
            source = zh_terms[0]
            for target_lang, target_text in other_terms:
                entries.append({
                    "source_term": source,
                    "target_term": target_text,
                    "lang_pair": f"zh→{target_lang}",
                })
        elif len(normalized) >= 2:
            # 没有中文，取前两个语言配对
            source_lang, source_text = normalized[0]
            for target_lang, target_text in normalized[1:]:
                entries.append({
                    "source_term": source_text,
                    "target_term": target_text,
                    "lang_pair": f"{source_lang}→{target_lang}",
                })

    return entries


@router.get("/export")
def export_terms(
    lang_pair: str = Query(default=""),
    domain: str = Query(default=""),
):
    """导出术语库为 Excel 文件。"""
    import openpyxl

    db = SessionLocal()
    try:
        stmt = select(TermEntry).order_by(TermEntry.lang_pair, TermEntry.source_term)
        if lang_pair:
            stmt = stmt.where(TermEntry.lang_pair == lang_pair)
        if domain:
            stmt = stmt.where(TermEntry.domain == domain)
        items = list(db.scalars(stmt))
    finally:
        db.close()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "术语库"
    ws.append(["中文术语", "目标语言术语", "语种方向", "领域标签", "优先级", "备注"])
    for t in items:
        ws.append([t.source_term, t.target_term, t.lang_pair, t.domain, t.priority.value, t.note])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    from fastapi.responses import StreamingResponse
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=terms.xlsx"},
    )


# ── 领域列表 ─────────────────────────────────────────────────────────

@router.get("/domains", response_model=list[str])
def list_domains():
    """获取所有已使用的领域标签。"""
    db = SessionLocal()
    try:
        rows = db.scalars(
            select(TermEntry.domain).distinct().where(TermEntry.domain.isnot(None))
        ).all()
        return sorted(rows)
    finally:
        db.close()


# ── 语种方向列表 ──────────────────────────────────────────────────────

@router.get("/lang-pairs", response_model=list[str])
def list_lang_pairs():
    """获取所有已使用的语种方向。"""
    db = SessionLocal()
    try:
        rows = db.scalars(select(TermEntry.lang_pair).distinct()).all()
        return sorted(rows)
    finally:
        db.close()
