"""Export and restore the data that must survive a clean deployment.

The bundle is intentionally small and auditable: UTF-8 CSV files plus a JSON
manifest containing checksums and inventory information.  It is not an
encrypted backup; keep the directory under the same access controls as the
database because local password hashes and user profile data are included.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
import uuid
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import Engine, create_engine, inspect, text


FORMAT_NAME = "translation-preserved-data"
FORMAT_VERSION = 1

USERS_FILE = "users.csv"
TERMS_FILE = "term_entries.csv"
TM_FILE = "translation_memories.csv"
MODEL_CONFIGS_FILE = "model_configs.csv"
LEGACY_MODEL_CONFIG_FILE = "legacy_model_system_config.csv"
CONFLICTS_FILE = "conflicts.csv"
MANIFEST_FILE = "manifest.json"

ENCRYPTED_PREFIX = "enc:v1:"
LEGACY_MODEL_KEYS = frozenset(
    {"translation_model", "vl_model", "api_base_url", "api_key"}
)

USERS_FIELDS = (
    "id",
    "username",
    "password_hash",
    "auth_source",
    "is_admin",
    "is_active",
    "display_name",
    "email",
    "phone",
    "department",
    "oa_id",
    "partner_id",
    "partner_name",
    "oa_employed",
    "created_at",
    "updated_at",
)
TERM_FIELDS = (
    "id",
    "source_term",
    "source_normalized",
    "target_term",
    "lang_pair",
    "domain",
    "priority",
    "note",
    "updated_by",
    "created_at",
    "updated_at",
)
TM_FIELDS = (
    "id",
    "source_text",
    "source_normalized",
    "target_text",
    "lang_pair",
    "source",
    "domain",
    "task_id",
    "updated_by",
    "created_at",
    "updated_at",
)
MODEL_CONFIG_FIELDS = (
    "id",
    "name",
    "model_type",
    "model_id",
    "api_base_url",
    "api_key",
    "is_active",
    "updated_by",
    "created_at",
    "updated_at",
)
LEGACY_MODEL_CONFIG_FIELDS = (
    "key",
    "value",
    "updated_by",
    "created_at",
    "updated_at",
)
CONFLICT_FIELDS = (
    "dataset",
    "lang_pair",
    "source_normalized",
    "kept_id",
    "dropped_id",
    "kept_updated_at",
    "dropped_updated_at",
    "differing_fields",
)

CSV_SCHEMAS: dict[str, tuple[str, ...]] = {
    USERS_FILE: USERS_FIELDS,
    TERMS_FILE: TERM_FIELDS,
    TM_FILE: TM_FIELDS,
    MODEL_CONFIGS_FILE: MODEL_CONFIG_FIELDS,
    LEGACY_MODEL_CONFIG_FILE: LEGACY_MODEL_CONFIG_FIELDS,
    CONFLICTS_FILE: CONFLICT_FIELDS,
}

ALL_APPLICATION_TABLES = (
    "quality_feedbacks",
    "translation_memories",
    "translation_tasks",
    "term_entries",
    "model_configs",
    "system_config",
    "users",
)


class PreservedDataError(RuntimeError):
    """Raised when a bundle or database is unsafe to process."""


def _serialize(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    enum_value = getattr(value, "value", None)
    if isinstance(enum_value, str):
        return enum_value
    return str(value)


def _normalize_source(row: Mapping[str, Any], source_field: str) -> str:
    stored = row.get("source_normalized")
    if stored is not None:
        return str(stored)
    source = row.get(source_field)
    return "" if source is None else str(source).strip().lower()


def _parse_timestamp(value: Any, field: str, *, required: bool = True) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
        # The legacy deployment used PostgreSQL `timestamp without time zone`
        # while the application consistently wrote UTC values.  Treat only
        # native naive datetime objects read from that database as UTC.  Naive
        # timestamp strings in an import bundle remain invalid below.
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    elif value in (None, ""):
        if required:
            raise PreservedDataError(f"字段 {field} 不能为空")
        return None
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise PreservedDataError(f"字段 {field} 不是合法 ISO-8601 时间") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PreservedDataError(f"字段 {field} 必须包含时区")
    return parsed.astimezone(timezone.utc)


def _winner_key(row: Mapping[str, Any]) -> tuple[datetime, str]:
    updated_at = _parse_timestamp(row.get("updated_at"), "updated_at", required=False)
    return (updated_at or datetime.min.replace(tzinfo=timezone.utc), str(row.get("id", "")))


def _different_fields(kept: Mapping[str, Any], dropped: Mapping[str, Any]) -> str:
    ignored = {"id", "created_at", "updated_at", "source_normalized"}
    fields = sorted((set(kept) | set(dropped)) - ignored)
    return ",".join(
        field for field in fields if _serialize(kept.get(field)) != _serialize(dropped.get(field))
    )


def deduplicate_latest(
    rows: Iterable[Mapping[str, Any]],
    *,
    dataset: str,
    source_field: str,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Keep the newest row for each language-pair/normalized-source key."""

    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for original in rows:
        row = dict(original)
        row["source_normalized"] = _normalize_source(row, source_field)
        key = (str(row.get("lang_pair", "")), row["source_normalized"])
        groups.setdefault(key, []).append(row)

    winners: list[dict[str, Any]] = []
    conflicts: list[dict[str, str]] = []
    for (lang_pair, normalized), candidates in sorted(groups.items()):
        ordered = sorted(candidates, key=_winner_key, reverse=True)
        kept = ordered[0]
        winners.append(kept)
        for dropped in ordered[1:]:
            conflicts.append(
                {
                    "dataset": dataset,
                    "lang_pair": lang_pair,
                    "source_normalized": normalized,
                    "kept_id": _serialize(kept.get("id")),
                    "dropped_id": _serialize(dropped.get("id")),
                    "kept_updated_at": _serialize(kept.get("updated_at")),
                    "dropped_updated_at": _serialize(dropped.get("updated_at")),
                    "differing_fields": _different_fields(kept, dropped),
                }
            )

    winners.sort(
        key=lambda row: (
            str(row.get("lang_pair", "")),
            str(row.get("source_normalized", "")),
            str(row.get("id", "")),
        )
    )
    conflicts.sort(
        key=lambda row: (
            row["dataset"],
            row["lang_pair"],
            row["source_normalized"],
            row["dropped_id"],
        )
    )
    return winners, conflicts


def _prepare_output_directory(path: Path) -> None:
    if path.is_symlink():
        raise PreservedDataError(f"拒绝写入符号链接目录：{path}")
    if path.exists():
        if not path.is_dir():
            raise PreservedDataError(f"输出路径不是目录：{path}")
        if any(path.iterdir()):
            raise PreservedDataError(f"输出目录必须为空：{path}")
    else:
        path.mkdir(parents=True, mode=0o700)
    os.chmod(path, 0o700)


def _write_csv(path: Path, fields: Sequence[str], rows: Iterable[Mapping[str, Any]]) -> int:
    count = 0
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: _serialize(row.get(field)) for field in fields})
            count += 1
    os.chmod(path, 0o600)
    return count


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _language_pairs(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    counts = Counter(str(row.get("lang_pair", "")) for row in rows)
    return dict(sorted(counts.items()))


def _as_bool(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"true", "1"}:
        return True
    if normalized in {"false", "0"}:
        return False
    raise PreservedDataError(f"字段 {field} 必须为 true 或 false")


def _fernet(key: str) -> Fernet:
    if not key:
        raise PreservedDataError("解密现有密文需要 APP_DATA_ENCRYPTION_KEY")
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise PreservedDataError("APP_DATA_ENCRYPTION_KEY 格式无效") from exc


def _plaintext_secret(value: Any, encryption_key: str) -> str:
    """Return a provider key as plaintext for a key-rotation-safe export."""

    serialized = _serialize(value)
    if not serialized.startswith(ENCRYPTED_PREFIX):
        return serialized
    token = serialized[len(ENCRYPTED_PREFIX) :]
    try:
        plaintext = _fernet(encryption_key).decrypt(token.encode("ascii"))
        return plaintext.decode("utf-8")
    except (InvalidToken, UnicodeDecodeError, UnicodeEncodeError) as exc:
        raise PreservedDataError(
            "模型 API Key 无法解密；APP_DATA_ENCRYPTION_KEY 不匹配或数据已损坏"
        ) from exc


def _encrypt_plaintext_secret(value: str, encryption_key: str) -> str:
    if not encryption_key or not value:
        return value
    return ENCRYPTED_PREFIX + _fernet(encryption_key).encrypt(value.encode("utf-8")).decode(
        "ascii"
    )


def _transform_export_user(original: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(original)
    for field in ("is_admin", "is_active", "oa_employed"):
        row[field] = _as_bool(row.get(field), field)
    is_oa = str(row.get("auth_source") or "").lower() == "oa" or bool(row.get("oa_id"))
    if is_oa:
        row["auth_source"] = "oa"
        row["password_hash"] = None
    else:
        row["auth_source"] = "local"
    return row


def _account_inventory(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    inventory = [
        {
            "username": str(row.get("username", "")),
            "auth_source": str(row.get("auth_source", "")),
            "is_admin": _as_bool(row.get("is_admin"), "is_admin"),
            "is_active": _as_bool(row.get("is_active"), "is_active"),
            "oa_id": _serialize(row.get("oa_id")),
        }
        for row in rows
    ]
    return sorted(inventory, key=lambda account: account["username"])


def _fetch_export_rows(connection: Any) -> tuple[list[dict[str, Any]], ...]:
    schema = inspect(connection)
    user_columns = {column["name"] for column in schema.get_columns("users")}
    term_columns = {column["name"] for column in schema.get_columns("term_entries")}
    memory_columns = {
        column["name"] for column in schema.get_columns("translation_memories")
    }
    system_config_columns = {
        column["name"] for column in schema.get_columns("system_config")
    }

    auth_source_expression = (
        "auth_source"
        if "auth_source" in user_columns
        else "CASE WHEN oa_id IS NULL THEN 'local' ELSE 'oa' END AS auth_source"
    )
    preserved_user_filter = (
        "auth_source = 'local' OR is_admin = true"
        if "auth_source" in user_columns
        else "oa_id IS NULL OR is_admin = true"
    )
    oa_employed_expression = (
        "oa_employed" if "oa_employed" in user_columns else "true AS oa_employed"
    )
    term_normalized_expression = (
        "source_normalized"
        if "source_normalized" in term_columns
        else "lower(trim(source_term)) AS source_normalized"
    )
    memory_normalized_expression = (
        "source_normalized"
        if "source_normalized" in memory_columns
        else "lower(trim(source_text)) AS source_normalized"
    )
    # The legacy system_config table only had updated_at.  Alias it as
    # created_at so even an empty legacy table can be exported without
    # referencing a column that does not exist.
    legacy_created_at_expression = (
        "created_at"
        if "created_at" in system_config_columns
        else "updated_at AS created_at"
    )

    users = connection.execute(
        text(
            "SELECT id, username, password_hash, "
            f"{auth_source_expression}, is_admin, is_active, "
            "display_name, email, phone, department, oa_id, partner_id, partner_name, "
            f"{oa_employed_expression}, created_at, updated_at FROM users "
            f"WHERE {preserved_user_filter}"
        )
    ).mappings().all()
    terms = connection.execute(
        text(
            "SELECT id, source_term, "
            f"{term_normalized_expression}, target_term, lang_pair, domain, "
            "priority, note, updated_by, created_at, updated_at FROM term_entries"
        )
    ).mappings().all()
    memories = connection.execute(
        text(
            "SELECT id, source_text, "
            f"{memory_normalized_expression}, target_text, lang_pair, source, "
            "domain, task_id, updated_by, created_at, updated_at FROM translation_memories"
        )
    ).mappings().all()
    model_configs = connection.execute(
        text(
            "SELECT id, name, model_type, model_id, api_base_url, api_key, is_active, "
            "updated_by, created_at, updated_at FROM model_configs"
        )
    ).mappings().all()
    legacy_model_config = connection.execute(
        text(
            "SELECT key, value, updated_by, "
            f"{legacy_created_at_expression}, updated_at FROM system_config "
            "WHERE key IN ('translation_model', 'vl_model', 'api_base_url', 'api_key')"
        )
    ).mappings().all()
    return (
        [dict(row) for row in users],
        [dict(row) for row in terms],
        [dict(row) for row in memories],
        [dict(row) for row in model_configs],
        [dict(row) for row in legacy_model_config],
    )


def export_preserved_data(
    engine: Engine,
    output_dir: str | Path,
    *,
    app_data_encryption_key: str | None = None,
) -> dict[str, Any]:
    """Create a checked, least-data export bundle and return its manifest."""

    destination = Path(output_dir)
    _prepare_output_directory(destination)

    with engine.connect() as connection:
        if engine.dialect.name == "postgresql":
            connection = connection.execution_options(isolation_level="REPEATABLE READ")
        with connection.begin():
            if engine.dialect.name == "postgresql":
                connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            user_rows, term_rows, memory_rows, model_rows, legacy_rows = _fetch_export_rows(
                connection
            )

    encryption_key = (
        os.environ.get("APP_DATA_ENCRYPTION_KEY", "")
        if app_data_encryption_key is None
        else app_data_encryption_key
    )
    if encryption_key:
        _fernet(encryption_key)

    users = [_transform_export_user(row) for row in user_rows]
    users.sort(key=lambda row: (str(row.get("username", "")), str(row.get("id", ""))))
    terms, term_conflicts = deduplicate_latest(
        term_rows, dataset="term_entries", source_field="source_term"
    )
    memories, memory_conflicts = deduplicate_latest(
        memory_rows, dataset="translation_memories", source_field="source_text"
    )
    for memory in memories:
        memory["task_id"] = None
    model_configs = [dict(row) for row in model_rows]
    for model in model_configs:
        model["is_active"] = _as_bool(model.get("is_active"), "is_active")
        model["api_key"] = _plaintext_secret(model.get("api_key"), encryption_key)
    model_configs.sort(
        key=lambda row: (
            str(row.get("model_type", "")),
            str(row.get("name", "")),
            str(row.get("id", "")),
        )
    )
    legacy_model_config = [dict(row) for row in legacy_rows]
    for entry in legacy_model_config:
        if entry.get("key") == "api_key":
            entry["value"] = _plaintext_secret(entry.get("value"), encryption_key)
    legacy_model_config.sort(key=lambda row: str(row.get("key", "")))
    conflicts = sorted(
        [*term_conflicts, *memory_conflicts],
        key=lambda row: (
            row["dataset"], row["lang_pair"], row["source_normalized"], row["dropped_id"]
        ),
    )

    file_rows: dict[str, list[dict[str, Any]]] = {
        USERS_FILE: users,
        TERMS_FILE: terms,
        TM_FILE: memories,
        MODEL_CONFIGS_FILE: model_configs,
        LEGACY_MODEL_CONFIG_FILE: legacy_model_config,
        CONFLICTS_FILE: conflicts,
    }
    file_manifest: dict[str, dict[str, Any]] = {}
    for filename, rows in file_rows.items():
        path = destination / filename
        row_count = _write_csv(path, CSV_SCHEMAS[filename], rows)
        file_manifest[filename] = {"rows": row_count, "sha256": _sha256(path)}

    language_pair_distribution = {
        "term_entries": _language_pairs(terms),
        "translation_memories": _language_pairs(memories),
    }
    manifest: dict[str, Any] = {
        "format": FORMAT_NAME,
        "version": FORMAT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "files": file_manifest,
        "language_pair_distribution": language_pair_distribution,
        "accounts": _account_inventory(users),
        "conflict_rows": len(conflicts),
    }
    manifest_path = destination / MANIFEST_FILE
    with manifest_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    os.chmod(manifest_path, 0o600)
    return manifest


def _read_csv(path: Path, expected_fields: Sequence[str]) -> list[dict[str, str]]:
    if path.is_symlink() or not path.is_file():
        raise PreservedDataError(f"数据文件缺失或不是普通文件：{path.name}")
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != list(expected_fields):
            raise PreservedDataError(f"CSV 表头不匹配：{path.name}")
        rows = list(reader)
    if any(None in row for row in rows):
        raise PreservedDataError(f"CSV 存在多余列：{path.name}")
    return rows


def _load_verified_bundle(source: Path) -> tuple[dict[str, Any], dict[str, list[dict[str, str]]]]:
    if source.is_symlink() or not source.is_dir():
        raise PreservedDataError(f"导入路径不是普通目录：{source}")
    manifest_path = source / MANIFEST_FILE
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise PreservedDataError("manifest.json 缺失或不是普通文件")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PreservedDataError("manifest.json 无法读取") from exc
    if manifest.get("format") != FORMAT_NAME or manifest.get("version") != FORMAT_VERSION:
        raise PreservedDataError("不支持的数据包格式或版本")
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) != set(CSV_SCHEMAS):
        raise PreservedDataError("manifest 文件清单不完整")

    loaded: dict[str, list[dict[str, str]]] = {}
    for filename, fields in CSV_SCHEMAS.items():
        metadata = files.get(filename)
        if not isinstance(metadata, dict):
            raise PreservedDataError(f"manifest 缺少文件元数据：{filename}")
        path = source / filename
        if path.is_symlink() or not path.is_file():
            raise PreservedDataError(f"数据文件缺失或不是普通文件：{filename}")
        if _sha256(path) != metadata.get("sha256"):
            raise PreservedDataError(f"SHA256 校验失败：{filename}")
        rows = _read_csv(path, fields)
        expected_rows = metadata.get("rows")
        if isinstance(expected_rows, bool) or not isinstance(expected_rows, int):
            raise PreservedDataError(f"manifest 行数无效：{filename}")
        if len(rows) != expected_rows:
            raise PreservedDataError(f"行数校验失败：{filename}")
        loaded[filename] = rows

    expected_pairs = manifest.get("language_pair_distribution")
    actual_pairs = {
        "term_entries": _language_pairs(loaded[TERMS_FILE]),
        "translation_memories": _language_pairs(loaded[TM_FILE]),
    }
    if expected_pairs != actual_pairs:
        raise PreservedDataError("语对分布校验失败")
    if manifest.get("accounts") != _account_inventory(loaded[USERS_FILE]):
        raise PreservedDataError("账号清单校验失败")
    if manifest.get("conflict_rows") != len(loaded[CONFLICTS_FILE]):
        raise PreservedDataError("冲突报告行数校验失败")
    return manifest, loaded


def _required(row: Mapping[str, str], field: str) -> str:
    value = row.get(field, "")
    if value == "":
        raise PreservedDataError(f"字段 {field} 不能为空")
    return value


def _optional(row: Mapping[str, str], field: str) -> str | None:
    value = row.get(field, "")
    return None if value == "" else value


def _uuid(value: str, field: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError) as exc:
        raise PreservedDataError(f"字段 {field} 不是合法 UUID") from exc


def _base_values(row: Mapping[str, str]) -> dict[str, Any]:
    return {
        "id": _uuid(_required(row, "id"), "id"),
        "created_at": _parse_timestamp(row.get("created_at"), "created_at"),
        "updated_at": _parse_timestamp(row.get("updated_at"), "updated_at"),
    }


def _prepare_user(row: Mapping[str, str]) -> dict[str, Any]:
    values = _base_values(row)
    auth_source = _required(row, "auth_source").lower()
    if auth_source not in {"local", "oa"}:
        raise PreservedDataError("字段 auth_source 取值无效")
    oa_id = _optional(row, "oa_id")
    is_oa = auth_source == "oa" or oa_id is not None
    password_hash = _optional(row, "password_hash")
    if is_oa:
        auth_source = "oa"
        password_hash = None
    elif password_hash is None:
        raise PreservedDataError("本地账号必须保留 password_hash")
    values.update(
        {
            "username": _required(row, "username"),
            "password_hash": password_hash,
            "auth_source": auth_source,
            "is_admin": _as_bool(row.get("is_admin"), "is_admin"),
            "is_active": _as_bool(row.get("is_active"), "is_active"),
            "display_name": _optional(row, "display_name"),
            "email": _optional(row, "email"),
            "phone": _optional(row, "phone"),
            "department": _optional(row, "department"),
            "oa_id": oa_id,
            "partner_id": _optional(row, "partner_id"),
            "partner_name": _optional(row, "partner_name"),
            "oa_employed": _as_bool(row.get("oa_employed"), "oa_employed"),
        }
    )
    return values


def _prepare_term(row: Mapping[str, str]) -> dict[str, Any]:
    values = _base_values(row)
    priority = _required(row, "priority")
    if priority not in {"strict", "preferred"}:
        raise PreservedDataError("字段 priority 取值无效")
    values.update(
        {
            "source_term": _required(row, "source_term"),
            "target_term": _required(row, "target_term"),
            "lang_pair": _required(row, "lang_pair"),
            "domain": _optional(row, "domain"),
            "priority": priority,
            "note": _optional(row, "note"),
            "updated_by": _optional(row, "updated_by"),
        }
    )
    return values


def _prepare_memory(row: Mapping[str, str]) -> dict[str, Any]:
    values = _base_values(row)
    values.update(
        {
            "source_text": _required(row, "source_text"),
            "target_text": _required(row, "target_text"),
            "lang_pair": _required(row, "lang_pair"),
            "source": _required(row, "source"),
            "domain": _optional(row, "domain"),
            # Task rows are intentionally not part of this bundle.
            "task_id": None,
            "updated_by": _optional(row, "updated_by"),
        }
    )
    return values


def _prepare_model_config(
    row: Mapping[str, str], encryption_key: str
) -> dict[str, Any]:
    values = _base_values(row)
    model_type = _required(row, "model_type")
    if model_type not in {"translation", "vl"}:
        raise PreservedDataError("字段 model_type 取值无效")
    values.update(
        {
            "name": _required(row, "name"),
            "model_type": model_type,
            "model_id": _required(row, "model_id"),
            "api_base_url": _required(row, "api_base_url"),
            "api_key": _encrypt_plaintext_secret(row.get("api_key", ""), encryption_key),
            "is_active": _as_bool(row.get("is_active"), "is_active"),
            "updated_by": _optional(row, "updated_by"),
        }
    )
    return values


def _prepare_legacy_model_config(
    row: Mapping[str, str], encryption_key: str
) -> dict[str, Any]:
    key = _required(row, "key")
    if key not in LEGACY_MODEL_KEYS:
        raise PreservedDataError(f"旧版模型配置键不在允许清单中：{key}")
    value = row.get("value", "")
    if key == "api_key":
        value = _encrypt_plaintext_secret(value, encryption_key)
    return {
        "key": key,
        "value": value,
        "updated_by": _optional(row, "updated_by"),
        "created_at": _parse_timestamp(row.get("created_at"), "created_at"),
        "updated_at": _parse_timestamp(row.get("updated_at"), "updated_at"),
    }


def _reject_duplicate_keys(rows: list[dict[str, str]], dataset: str, source_field: str) -> None:
    _winners, conflicts = deduplicate_latest(rows, dataset=dataset, source_field=source_field)
    if conflicts:
        raise PreservedDataError(f"导入文件包含重复的语对/归一化源文本：{dataset}")


def _validate_model_configs(rows: Sequence[Mapping[str, Any]]) -> None:
    active_types: set[str] = set()
    for row in rows:
        if not row["is_active"]:
            continue
        model_type = str(row["model_type"])
        if model_type in active_types:
            raise PreservedDataError(f"同一模型类型存在多个启用配置：{model_type}")
        active_types.add(model_type)


def _lock_and_assert_empty(connection: Any, dialect_name: str) -> None:
    if dialect_name == "postgresql":
        table_list = ", ".join(f'"{table}"' for table in ALL_APPLICATION_TABLES)
        connection.exec_driver_sql(f"LOCK TABLE {table_list} IN ACCESS EXCLUSIVE MODE")
    nonempty = []
    for table in ALL_APPLICATION_TABLES:
        exists = connection.execute(
            text(f'SELECT EXISTS (SELECT 1 FROM "{table}" LIMIT 1)')
        ).scalar_one()
        if bool(exists):
            nonempty.append(table)
    if nonempty:
        raise PreservedDataError("目标数据库不是空库，拒绝导入：" + ", ".join(nonempty))


USER_INSERT = text(
    "INSERT INTO users (id, username, password_hash, auth_source, is_admin, is_active, "
    "display_name, email, phone, department, oa_id, partner_id, partner_name, oa_employed, "
    "created_at, updated_at) VALUES (:id, :username, :password_hash, :auth_source, :is_admin, "
    ":is_active, :display_name, :email, :phone, :department, :oa_id, :partner_id, "
    ":partner_name, :oa_employed, :created_at, :updated_at)"
)
TERM_INSERT = text(
    "INSERT INTO term_entries (id, source_term, target_term, lang_pair, domain, priority, note, "
    "updated_by, created_at, updated_at) VALUES (:id, :source_term, :target_term, :lang_pair, "
    ":domain, :priority, :note, :updated_by, :created_at, :updated_at)"
)
TM_INSERT = text(
    "INSERT INTO translation_memories (id, source_text, target_text, lang_pair, source, domain, "
    "task_id, updated_by, created_at, updated_at) VALUES (:id, :source_text, :target_text, "
    ":lang_pair, :source, :domain, :task_id, :updated_by, :created_at, :updated_at)"
)
MODEL_CONFIG_INSERT = text(
    "INSERT INTO model_configs (id, name, model_type, model_id, api_base_url, api_key, "
    "is_active, updated_by, created_at, updated_at) VALUES (:id, :name, :model_type, "
    ":model_id, :api_base_url, :api_key, :is_active, :updated_by, :created_at, :updated_at)"
)
LEGACY_MODEL_CONFIG_INSERT = text(
    "INSERT INTO system_config (key, value, updated_by, created_at, updated_at) "
    "VALUES (:key, :value, :updated_by, :created_at, :updated_at)"
)


def import_preserved_data(
    engine: Engine,
    input_dir: str | Path,
    *,
    app_data_encryption_key: str | None = None,
) -> dict[str, Any]:
    """Verify and restore a bundle, refusing any non-empty application database."""

    manifest, loaded = _load_verified_bundle(Path(input_dir))
    _reject_duplicate_keys(loaded[TERMS_FILE], "term_entries", "source_term")
    _reject_duplicate_keys(loaded[TM_FILE], "translation_memories", "source_text")
    encryption_key = (
        os.environ.get("APP_DATA_ENCRYPTION_KEY", "")
        if app_data_encryption_key is None
        else app_data_encryption_key
    )
    if encryption_key:
        _fernet(encryption_key)
    users = [_prepare_user(row) for row in loaded[USERS_FILE]]
    terms = [_prepare_term(row) for row in loaded[TERMS_FILE]]
    memories = [_prepare_memory(row) for row in loaded[TM_FILE]]
    model_configs = [
        _prepare_model_config(row, encryption_key) for row in loaded[MODEL_CONFIGS_FILE]
    ]
    _validate_model_configs(model_configs)
    legacy_model_config = [
        _prepare_legacy_model_config(row, encryption_key)
        for row in loaded[LEGACY_MODEL_CONFIG_FILE]
    ]
    legacy_keys = [row["key"] for row in legacy_model_config]
    if len(legacy_keys) != len(set(legacy_keys)):
        raise PreservedDataError("旧版模型配置包含重复键")

    with engine.begin() as connection:
        _lock_and_assert_empty(connection, engine.dialect.name)
        if users:
            connection.execute(USER_INSERT, users)
        if terms:
            connection.execute(TERM_INSERT, terms)
        if memories:
            connection.execute(TM_INSERT, memories)
        if model_configs:
            connection.execute(MODEL_CONFIG_INSERT, model_configs)
        if legacy_model_config:
            connection.execute(LEGACY_MODEL_CONFIG_INSERT, legacy_model_config)
    return {
        "users": len(users),
        "term_entries": len(terms),
        "translation_memories": len(memories),
        "model_configs": len(model_configs),
        "legacy_model_system_config": len(legacy_model_config),
        "conflicts_reported": manifest["conflict_rows"],
    }


def _default_database_url() -> str:
    backend_root = str(Path(__file__).resolve().parents[1])
    if backend_root not in sys.path:
        sys.path.insert(0, backend_root)
    from app.core.config import get_settings

    return get_settings().database_url


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="导出或恢复翻译系统的保留数据",
        epilog=(
            "模型 API Key 换钥：导出时 APP_DATA_ENCRYPTION_KEY 应为旧密钥；"
            "导入时应为新密钥。OA 凭据不属于此数据包。"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    export_parser = subparsers.add_parser("export", help="导出术语、翻译记忆和允许保留的账号")
    export_parser.add_argument("--output", required=True, type=Path, help="必须为空的输出目录")
    export_parser.add_argument("--database-url", help="数据库 URL；默认读取应用配置")

    import_parser = subparsers.add_parser("import", help="校验数据包并导入空数据库")
    import_parser.add_argument("--input", required=True, type=Path, help="导出数据包目录")
    import_parser.add_argument("--database-url", help="数据库 URL；默认读取应用配置")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    database_url = args.database_url or _default_database_url()
    engine = create_engine(database_url, future=True)
    try:
        if args.command == "export":
            result = export_preserved_data(engine, args.output)
            summary = {
                "output": str(args.output),
                "rows": {name: metadata["rows"] for name, metadata in result["files"].items()},
            }
        else:
            summary = import_preserved_data(engine, args.input)
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 0
    except (PreservedDataError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
