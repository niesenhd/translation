#!/usr/bin/env python3
"""Prepare a production upgrade .env without exposing retained credentials.

The source file is never modified.  OA credentials and model-provider settings
are copied byte-for-byte, while application/database/cache/storage credentials
are regenerated for the clean deployment.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import secrets
import tempfile
from pathlib import Path


ASSIGNMENT_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
OA_KEYS = ("OA_APP_KEY", "OA_APP_SECRET", "OA_SSO_SECRET")
MODEL_KEYS = (
    "DASHSCOPE_API_KEY",
    "DASHSCOPE_BASE_URL",
    "DASHSCOPE_MODEL",
    "DASHSCOPE_VL_MODEL",
)


def _parse(lines: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in lines:
        match = ASSIGNMENT_RE.match(line.rstrip("\n"))
        if match:
            values[match.group(1)] = match.group(2)
    return values


def _fernet_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")


def _replacement_values(public_base_url: str, cors_origins: str) -> dict[str, str]:
    return {
        "APP_ENV": "production",
        "APP_SECRET_KEY": secrets.token_hex(32),
        "APP_ADMIN_TOKEN": secrets.token_hex(24),
        "APP_DATA_ENCRYPTION_KEY": _fernet_key(),
        "PUBLIC_BASE_URL": public_base_url.rstrip("/"),
        "CORS_ORIGINS": cors_origins,
        "POSTGRES_PASSWORD": secrets.token_hex(24),
        "REDIS_PASSWORD": secrets.token_hex(24),
        "MINIO_ACCESS_KEY": secrets.token_hex(12),
        "MINIO_SECRET_KEY": secrets.token_hex(32),
        "CELERY_WORKER_CONCURRENCY": "4",
    }


def prepare(source: Path, output: Path, public_base_url: str, cors_origins: str) -> None:
    if source.resolve() == output.resolve():
        raise ValueError("source 与 output 必须是不同文件")
    if source.is_symlink() or not source.is_file():
        raise ValueError("source 必须是现有普通文件")
    if output.exists() and output.is_symlink():
        raise ValueError("拒绝覆盖符号链接")

    source_lines = source.read_text(encoding="utf-8").splitlines(keepends=True)
    source_values = _parse(source_lines)
    missing_oa = [key for key in OA_KEYS if not source_values.get(key)]
    if missing_oa:
        raise ValueError("现有 OA 配置缺失，拒绝生成升级环境：" + ", ".join(missing_oa))

    replacements = _replacement_values(public_base_url, cors_origins)
    rendered: list[str] = []
    replaced: set[str] = set()
    for line in source_lines:
        match = ASSIGNMENT_RE.match(line.rstrip("\n"))
        if match and match.group(1) in replacements:
            key = match.group(1)
            rendered.append(f"{key}={replacements[key]}\n")
            replaced.add(key)
        else:
            rendered.append(line if line.endswith("\n") else line + "\n")
    for key, value in replacements.items():
        if key not in replaced:
            rendered.append(f"{key}={value}\n")

    output.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", dir=output.parent
    )
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.writelines(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, output)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise

    output_values = _parse(output.read_text(encoding="utf-8").splitlines(keepends=True))
    retained_keys = [*OA_KEYS, *(key for key in MODEL_KEYS if key in source_values)]
    changed_retained = [
        key for key in retained_keys if output_values.get(key) != source_values.get(key)
    ]
    if changed_retained:
        output.unlink(missing_ok=True)
        raise RuntimeError("保留配置发生变化，已删除输出：" + ", ".join(changed_retained))
    if (output.stat().st_mode & 0o077) != 0:
        raise RuntimeError("输出文件权限不是 600")

    print(
        json.dumps(
            {
                "output": str(output),
                "retained_keys_verified": retained_keys,
                "rotated_keys": sorted(replacements),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--public-base-url", required=True)
    parser.add_argument("--cors-origins", required=True)
    args = parser.parse_args()
    prepare(args.source, args.output, args.public_base_url, args.cors_origins)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
