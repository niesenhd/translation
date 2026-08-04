"""敏感配置值的应用层加解密。"""
from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import Settings, get_settings

_ENCRYPTED_PREFIX = "enc:v1:"


@lru_cache(maxsize=4)
def _fernet_for_key(key: str) -> Fernet:
    if not key:
        raise RuntimeError("APP_DATA_ENCRYPTION_KEY 未配置")
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise RuntimeError("APP_DATA_ENCRYPTION_KEY 格式无效") from exc


def is_encrypted_secret(value: str) -> bool:
    return value.startswith(_ENCRYPTED_PREFIX)


def encrypt_secret(value: str, settings: Settings | None = None) -> str:
    """加密明文；已是当前格式的密文则原样返回。"""
    if not value or is_encrypted_secret(value):
        return value
    cfg = settings or get_settings()
    token = _fernet_for_key(cfg.app_data_encryption_key).encrypt(value.encode("utf-8"))
    return _ENCRYPTED_PREFIX + token.decode("ascii")


def decrypt_secret(value: str, settings: Settings | None = None) -> str:
    """解密当前格式密文；未迁移的历史明文保持兼容并原样返回。"""
    if not value or not is_encrypted_secret(value):
        return value
    cfg = settings or get_settings()
    token = value[len(_ENCRYPTED_PREFIX):]
    try:
        plaintext = _fernet_for_key(cfg.app_data_encryption_key).decrypt(token.encode("ascii"))
        return plaintext.decode("utf-8")
    except (InvalidToken, UnicodeDecodeError, UnicodeEncodeError) as exc:
        raise ValueError("敏感配置密文无法解密；密钥不匹配或数据已损坏") from exc
