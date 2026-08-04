from __future__ import annotations

import csv
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, text

from scripts.preserved_data import (
    CONFLICTS_FILE,
    ENCRYPTED_PREFIX,
    LEGACY_MODEL_CONFIG_FILE,
    MANIFEST_FILE,
    MODEL_CONFIGS_FILE,
    TERMS_FILE,
    TM_FILE,
    USERS_FILE,
    PreservedDataError,
    export_preserved_data,
    import_preserved_data,
)


NOW = datetime(2026, 8, 4, 8, 0, tzinfo=timezone.utc)
OLDER = datetime(2026, 8, 3, 8, 0, tzinfo=timezone.utc)


def _id() -> str:
    return str(uuid.uuid4())


def _encrypt(value: str, key: str) -> str:
    return ENCRYPTED_PREFIX + Fernet(key.encode("ascii")).encrypt(value.encode()).decode()


def _decrypt(value: str, key: str) -> str:
    assert value.startswith(ENCRYPTED_PREFIX)
    return Fernet(key.encode("ascii")).decrypt(
        value.removeprefix(ENCRYPTED_PREFIX).encode()
    ).decode()


def _create_schema(engine) -> None:
    statements = (
        """CREATE TABLE users (
            id TEXT PRIMARY KEY, username TEXT, password_hash TEXT, auth_source TEXT,
            is_admin BOOLEAN, is_active BOOLEAN, display_name TEXT, email TEXT, phone TEXT,
            department TEXT, oa_id TEXT, partner_id TEXT, partner_name TEXT,
            oa_employed BOOLEAN, created_at TIMESTAMP, updated_at TIMESTAMP
        )""",
        """CREATE TABLE term_entries (
            id TEXT PRIMARY KEY, source_term TEXT, source_normalized TEXT, target_term TEXT,
            lang_pair TEXT, domain TEXT, priority TEXT, note TEXT, updated_by TEXT,
            created_at TIMESTAMP, updated_at TIMESTAMP
        )""",
        """CREATE TABLE translation_memories (
            id TEXT PRIMARY KEY, source_text TEXT, source_normalized TEXT, target_text TEXT,
            lang_pair TEXT, source TEXT, domain TEXT, task_id TEXT, updated_by TEXT,
            created_at TIMESTAMP, updated_at TIMESTAMP
        )""",
        """CREATE TABLE model_configs (
            id TEXT PRIMARY KEY, name TEXT, model_type TEXT, model_id TEXT,
            api_base_url TEXT, api_key TEXT, is_active BOOLEAN, updated_by TEXT,
            created_at TIMESTAMP, updated_at TIMESTAMP
        )""",
        """CREATE TABLE system_config (
            key TEXT PRIMARY KEY, value TEXT, updated_by TEXT,
            created_at TIMESTAMP, updated_at TIMESTAMP
        )""",
        "CREATE TABLE translation_tasks (id TEXT PRIMARY KEY)",
        "CREATE TABLE quality_feedbacks (id TEXT PRIMARY KEY)",
    )
    with engine.begin() as connection:
        for statement in statements:
            connection.exec_driver_sql(statement)


def _seed_source(engine, old_key: str) -> dict[str, str]:
    ids = {name: _id() for name in ("local", "oa_admin", "oa_user", "term_old", "term_new", "tm_old", "tm_new", "text", "vl")}
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users VALUES (:id, :username, :password_hash, :auth_source, "
                ":is_admin, :is_active, :display_name, :email, :phone, :department, :oa_id, "
                ":partner_id, :partner_name, :oa_employed, :created_at, :updated_at)"
            ),
            [
                {
                    "id": ids["local"], "username": "local-admin", "password_hash": "hash-local",
                    "auth_source": "local", "is_admin": True, "is_active": True,
                    "display_name": "Local", "email": None, "phone": None, "department": None,
                    "oa_id": None, "partner_id": None, "partner_name": None,
                    "oa_employed": True, "created_at": OLDER, "updated_at": NOW,
                },
                {
                    "id": ids["oa_admin"], "username": "oa-admin", "password_hash": "must-clear",
                    "auth_source": "local", "is_admin": True, "is_active": True,
                    "display_name": "OA Admin", "email": "admin@example.com", "phone": "1",
                    "department": "Legal", "oa_id": "oa-1", "partner_id": "p-1",
                    "partner_name": "Partner", "oa_employed": True,
                    "created_at": OLDER, "updated_at": NOW,
                },
                {
                    "id": ids["oa_user"], "username": "oa-user", "password_hash": "excluded",
                    "auth_source": "oa", "is_admin": False, "is_active": True,
                    "display_name": "OA User", "email": None, "phone": None, "department": None,
                    "oa_id": "oa-2", "partner_id": None, "partner_name": None,
                    "oa_employed": True, "created_at": OLDER, "updated_at": NOW,
                },
            ],
        )
        connection.execute(
            text(
                "INSERT INTO term_entries VALUES (:id, :source_term, :source_normalized, "
                ":target_term, :lang_pair, :domain, :priority, :note, :updated_by, "
                ":created_at, :updated_at)"
            ),
            [
                {"id": ids["term_old"], "source_term": " Contract ", "source_normalized": "contract", "target_term": "旧合同", "lang_pair": "en→zh", "domain": None, "priority": "preferred", "note": None, "updated_by": "old", "created_at": OLDER, "updated_at": OLDER},
                {"id": ids["term_new"], "source_term": "contract", "source_normalized": "contract", "target_term": "合同", "lang_pair": "en→zh", "domain": "law", "priority": "strict", "note": "new", "updated_by": "new", "created_at": OLDER, "updated_at": NOW},
            ],
        )
        connection.execute(
            text(
                "INSERT INTO translation_memories VALUES (:id, :source_text, :source_normalized, "
                ":target_text, :lang_pair, :source, :domain, :task_id, :updated_by, "
                ":created_at, :updated_at)"
            ),
            [
                {"id": ids["tm_old"], "source_text": "Hello", "source_normalized": "hello", "target_text": "旧译文", "lang_pair": "en→zh", "source": "manual", "domain": None, "task_id": _id(), "updated_by": "old", "created_at": OLDER, "updated_at": OLDER},
                {"id": ids["tm_new"], "source_text": " hello ", "source_normalized": "hello", "target_text": "你好", "lang_pair": "en→zh", "source": "auto", "domain": "law", "task_id": _id(), "updated_by": "new", "created_at": OLDER, "updated_at": NOW},
            ],
        )
        connection.execute(
            text(
                "INSERT INTO model_configs VALUES (:id, :name, :model_type, :model_id, "
                ":api_base_url, :api_key, :is_active, :updated_by, :created_at, :updated_at)"
            ),
            [
                {"id": ids["text"], "name": "文本模型", "model_type": "translation", "model_id": "qwen-text", "api_base_url": "https://model.example/v1", "api_key": _encrypt("model-text-key", old_key), "is_active": True, "updated_by": "local-admin", "created_at": OLDER, "updated_at": NOW},
                {"id": ids["vl"], "name": "视觉模型", "model_type": "vl", "model_id": "qwen-vl", "api_base_url": "https://model.example/v1", "api_key": _encrypt("model-vl-key", old_key), "is_active": True, "updated_by": "local-admin", "created_at": OLDER, "updated_at": NOW},
            ],
        )
        connection.execute(
            text(
                "INSERT INTO system_config VALUES (:key, :value, :updated_by, :created_at, :updated_at)"
            ),
            [
                {"key": "translation_model", "value": "legacy-text", "updated_by": "admin", "created_at": OLDER, "updated_at": NOW},
                {"key": "vl_model", "value": "legacy-vl", "updated_by": "admin", "created_at": OLDER, "updated_at": NOW},
                {"key": "api_base_url", "value": "https://legacy.example/v1", "updated_by": "admin", "created_at": OLDER, "updated_at": NOW},
                {"key": "api_key", "value": _encrypt("legacy-key", old_key), "updated_by": "admin", "created_at": OLDER, "updated_at": NOW},
                {"key": "oa_app_secret", "value": "OA-MUST-NEVER-MOVE", "updated_by": "admin", "created_at": OLDER, "updated_at": NOW},
                {"key": "file_retention_days", "value": "180", "updated_by": "admin", "created_at": OLDER, "updated_at": NOW},
            ],
        )
    return ids


def _csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def test_round_trip_is_filtered_deduplicated_and_rotates_model_keys(tmp_path: Path) -> None:
    old_key = Fernet.generate_key().decode()
    new_key = Fernet.generate_key().decode()
    source = create_engine("sqlite+pysqlite:///:memory:")
    target = create_engine("sqlite+pysqlite:///:memory:")
    _create_schema(source)
    _create_schema(target)
    _seed_source(source, old_key)

    bundle = tmp_path / "bundle"
    manifest = export_preserved_data(
        source, bundle, app_data_encryption_key=old_key
    )

    assert set(manifest["files"]) == {
        USERS_FILE, TERMS_FILE, TM_FILE, MODEL_CONFIGS_FILE,
        LEGACY_MODEL_CONFIG_FILE, CONFLICTS_FILE,
    }
    assert manifest["files"][USERS_FILE]["rows"] == 2
    assert manifest["files"][TERMS_FILE]["rows"] == 1
    assert manifest["files"][TM_FILE]["rows"] == 1
    assert manifest["files"][MODEL_CONFIGS_FILE]["rows"] == 2
    assert manifest["files"][LEGACY_MODEL_CONFIG_FILE]["rows"] == 4
    assert manifest["conflict_rows"] == 2
    assert manifest["language_pair_distribution"] == {
        "term_entries": {"en→zh": 1},
        "translation_memories": {"en→zh": 1},
    }
    assert [account["username"] for account in manifest["accounts"]] == [
        "local-admin", "oa-admin"
    ]
    assert os.stat(bundle).st_mode & 0o777 == 0o700
    for filename, metadata in manifest["files"].items():
        path = bundle / filename
        assert os.stat(path).st_mode & 0o777 == 0o600
        assert hashlib.sha256(path.read_bytes()).hexdigest() == metadata["sha256"]

    users = {row["username"]: row for row in _csv_rows(bundle / USERS_FILE)}
    assert users["local-admin"]["password_hash"] == "hash-local"
    assert users["oa-admin"]["password_hash"] == ""
    assert users["oa-admin"]["auth_source"] == "oa"
    assert _csv_rows(bundle / TERMS_FILE)[0]["target_term"] == "合同"
    memory = _csv_rows(bundle / TM_FILE)[0]
    assert memory["target_text"] == "你好"
    assert memory["task_id"] == ""
    assert {row["api_key"] for row in _csv_rows(bundle / MODEL_CONFIGS_FILE)} == {
        "model-text-key", "model-vl-key"
    }
    legacy = {row["key"]: row["value"] for row in _csv_rows(bundle / LEGACY_MODEL_CONFIG_FILE)}
    assert legacy["api_key"] == "legacy-key"
    assert "OA-MUST-NEVER-MOVE" not in "".join(
        path.read_text(encoding="utf-8") for path in bundle.iterdir()
    )

    result = import_preserved_data(
        target, bundle, app_data_encryption_key=new_key
    )
    assert result == {
        "users": 2,
        "term_entries": 1,
        "translation_memories": 1,
        "model_configs": 2,
        "legacy_model_system_config": 4,
        "conflicts_reported": 2,
    }
    with target.connect() as connection:
        restored_users = {
            row.username: row
            for row in connection.execute(text("SELECT * FROM users")).mappings()
        }
        assert restored_users["local-admin"].password_hash == "hash-local"
        assert restored_users["oa-admin"].password_hash is None
        assert restored_users["oa-admin"].auth_source == "oa"
        assert connection.execute(text("SELECT target_term FROM term_entries")).scalar_one() == "合同"
        restored_memory = connection.execute(
            text("SELECT target_text, task_id FROM translation_memories")
        ).one()
        assert tuple(restored_memory) == ("你好", None)
        restored_models = connection.execute(
            text("SELECT model_id, api_key FROM model_configs")
        ).all()
        assert {_decrypt(row.api_key, new_key) for row in restored_models} == {
            "model-text-key", "model-vl-key"
        }
        restored_legacy = dict(
            connection.execute(text("SELECT key, value FROM system_config")).all()
        )
        assert _decrypt(restored_legacy["api_key"], new_key) == "legacy-key"
        assert set(restored_legacy) == {
            "translation_model", "vl_model", "api_base_url", "api_key"
        }

    with pytest.raises(PreservedDataError, match="不是空库"):
        import_preserved_data(target, bundle, app_data_encryption_key=new_key)


def test_import_rejects_tampered_csv_before_writing(tmp_path: Path) -> None:
    key = Fernet.generate_key().decode()
    source = create_engine("sqlite+pysqlite:///:memory:")
    target = create_engine("sqlite+pysqlite:///:memory:")
    _create_schema(source)
    _create_schema(target)
    _seed_source(source, key)
    bundle = tmp_path / "bundle"
    export_preserved_data(source, bundle, app_data_encryption_key=key)

    with (bundle / TERMS_FILE).open("a", encoding="utf-8") as handle:
        handle.write("tampered\n")
    with pytest.raises(PreservedDataError, match="SHA256"):
        import_preserved_data(target, bundle, app_data_encryption_key=key)

    with target.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM users")).scalar_one() == 0


def test_export_refuses_nonempty_destination(tmp_path: Path) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    _create_schema(engine)
    destination = tmp_path / "existing"
    destination.mkdir()
    (destination / "keep.txt").write_text("do not overwrite", encoding="utf-8")

    with pytest.raises(PreservedDataError, match="必须为空"):
        export_preserved_data(engine, destination)
