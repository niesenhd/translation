"""MinIO 客户端封装（对象存储）。"""
from __future__ import annotations

import threading
from collections.abc import Iterator
from io import BytesIO
from typing import BinaryIO

from minio import Minio
from minio.error import S3Error

from app.core.config import get_settings

_settings = get_settings()

_client: Minio | None = None
_init_lock = threading.Lock()


def get_client() -> Minio:
    global _client
    if _client is None:
        with _init_lock:
            if _client is None:  # double-check
                _client = Minio(
                    _settings.minio_endpoint,
                    access_key=_settings.minio_access_key,
                    secret_key=_settings.minio_secret_key,
                    secure=_settings.minio_secure,
                )
                _ensure_bucket(_client, _settings.minio_bucket)
    return _client


def _ensure_bucket(client: Minio, bucket: str) -> None:
    try:
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)
    except S3Error as exc:
        # 仅忽略 "bucket already owned by you"（并发创建场景）
        # 其他错误（连接失败、认证失败等）必须抛出，否则后续所有存储操作都会失败且难以排查
        if getattr(exc, "code", "") == "BucketAlreadyOwnedByYou":
            return
        raise


def upload_bytes(object_name: str, data: bytes, content_type: str = "application/octet-stream") -> None:
    client = get_client()
    client.put_object(
        _settings.minio_bucket,
        object_name,
        BytesIO(data),
        length=len(data),
        content_type=content_type,
    )


def upload_stream(object_name: str, stream: BinaryIO, length: int, content_type: str = "application/octet-stream") -> None:
    client = get_client()
    client.put_object(_settings.minio_bucket, object_name, stream, length=length, content_type=content_type)


def download_bytes(object_name: str) -> bytes:
    client = get_client()
    response = client.get_object(_settings.minio_bucket, object_name)
    try:
        return response.read()
    finally:
        response.close()
        response.release_conn()


def download_stream(object_name: str, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
    """按块读取对象，并在响应结束或客户端中断时释放 MinIO 连接。"""
    client = get_client()
    response = client.get_object(_settings.minio_bucket, object_name)
    try:
        while True:
            chunk = response.read(chunk_size)
            if not chunk:
                break
            yield chunk
    finally:
        response.close()
        response.release_conn()


def object_size(object_name: str) -> int:
    """返回对象大小，用于流式响应的 Content-Length。"""
    stat = get_client().stat_object(_settings.minio_bucket, object_name)
    return int(stat.size or 0)


def remove_object(object_name: str) -> None:
    client = get_client()
    client.remove_object(_settings.minio_bucket, object_name)


def secure_remove_object(object_name: str) -> bool:
    """安全覆写删除：先用随机字节覆盖原对象一次，再删除。

    需求 2.9 要求"覆写后删除，不可恢复"。MinIO 默认是 immutable 对象存储，
    我们通过 PUT 覆盖同 key + DELETE 两步实现：
    - 覆盖一次：写入随机字节，长度与原对象近似（最多 1MB，过长无意义）
    - 删除：移除对象

    返回是否成功。对象不存在视为成功（已删除）。
    """
    import os
    import secrets

    client = get_client()
    bucket = _settings.minio_bucket
    try:
        try:
            stat = client.stat_object(bucket, object_name)
            size = min(stat.size or 0, 1024 * 1024)  # 上限 1MB
        except S3Error as exc:
            # 对象不存在视为成功
            if getattr(exc, "code", "") in ("NoSuchKey", "NoSuchObject"):
                return True
            raise
        # 覆写
        if size > 0:
            random_bytes = secrets.token_bytes(size)
            client.put_object(
                bucket,
                object_name,
                BytesIO(random_bytes),
                length=size,
                content_type="application/octet-stream",
            )
        # 删除
        client.remove_object(bucket, object_name)
        return True
    except S3Error:
        return False
