from __future__ import annotations

import sqlite3

import pytest
from fastapi import HTTPException

from app.api import terms


def test_sqlite_identifier_is_quoted_as_one_identifier() -> None:
    malicious = 'conceptid, secret FROM secrets --"'
    quoted = terms._quote_sqlite_identifier(malicious)

    assert quoted == '"conceptid, secret FROM secrets --"""'


def test_malicious_schema_identifier_cannot_rewrite_select() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        cursor = conn.cursor()
        cursor.execute("CREATE TABLE secrets(conceptid TEXT, secret TEXT)")
        cursor.execute("INSERT INTO secrets VALUES ('same-id', 'must-not-be-imported')")
        malicious = "conceptid, secret FROM secrets --"
        cursor.execute(f'CREATE TABLE I_en ("{malicious}" TEXT, origterm TEXT)')
        cursor.execute(f'INSERT INTO I_en ("{malicious}", origterm) VALUES (?, ?)', ("same-id", "contract"))
        cursor.execute("CREATE TABLE I_zh (conceptid TEXT, origterm TEXT)")
        cursor.execute("INSERT INTO I_zh VALUES ('same-id', '合同')")

        entries = terms._extract_sdltb_sqlite_entries(cursor, ["I_en", "I_zh", "secrets"])
    finally:
        conn.close()

    assert entries == [{"source_term": "合同", "target_term": "contract", "lang_pair": "zh→en"}]
    assert all("must-not-be-imported" not in str(entry) for entry in entries)


def test_sqlite_import_enforces_table_limit(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "too-many.sdltb"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE I_en(conceptid TEXT, origterm TEXT)")
        conn.execute("CREATE TABLE I_zh(conceptid TEXT, origterm TEXT)")
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(terms, "_MAX_SDLTB_TABLES", 1)
    with pytest.raises(HTTPException) as exc_info:
        terms._import_sdltb_sqlite(db_path.read_bytes(), object())

    assert exc_info.value.status_code == 400
    assert "表数量" in exc_info.value.detail


def test_sqlite_import_connection_is_query_only(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "readonly.sdltb"
    conn = sqlite3.connect(db_path)
    conn.close()
    observed = {}

    def fake_extract(cursor, _tables):
        observed["query_only"] = cursor.connection.execute("PRAGMA query_only").fetchone()[0]
        with pytest.raises(sqlite3.OperationalError):
            cursor.execute("CREATE TABLE should_fail(value TEXT)")
        return [{"source_term": "合同", "target_term": "contract", "lang_pair": "zh→en"}]

    monkeypatch.setattr(terms, "_extract_sdltb_sqlite_entries", fake_extract)
    monkeypatch.setattr(terms, "_import_rows", lambda rows, _user, default_domain=None: {"rows": rows})

    result = terms._import_sdltb_sqlite(db_path.read_bytes(), object())

    assert observed["query_only"] == 1
    assert result["rows"][0][:3] == ["合同", "contract", "zh→en"]
