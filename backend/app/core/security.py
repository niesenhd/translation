"""鉴权：本地账号/OA SSO 会话 + 静态 admin Token 兜底。

- 用户名/密码登录：POST /api/auth/login 校验密码后签发 HMAC 签名 token（无状态，
  载荷含 username + 过期时间，用 app_secret_key 签名）。
- 兼容：原静态 admin token 仍可用作紧急超级管理员入口。
- OA 用户仅能通过 SSO 换取本站会话 token；每次鉴权都复查最终启用状态。
"""
import base64
import hashlib
import hmac
import json
import os
import time
from typing import Optional

from fastapi import Depends, Header, HTTPException, status

from app.core.config import Settings, get_settings


class CurrentUser:
    """当前登录用户。"""

    def __init__(self, username: str, is_admin: bool = False):
        self.username = username
        self.is_admin = is_admin


# ── 密码哈希（pbkdf2_hmac，stdlib，无需第三方依赖）─────────────────────

_PBKDF2_ITER = 200_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITER)
    return f"pbkdf2_sha256${_PBKDF2_ITER}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        algo, iter_s, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iter_s))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


# ── 签名 token（无状态，HMAC）──────────────────────────────────────────

_TOKEN_TTL = 7 * 24 * 3600  # 7 天


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _ub64u(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def create_token(username: str, settings: Settings) -> str:
    payload = {"u": username, "exp": int(time.time()) + _TOKEN_TTL}
    payload_b = _b64u(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = _b64u(hmac.new(settings.app_secret_key.encode("utf-8"), payload_b.encode("ascii"), hashlib.sha256).digest())
    return f"{payload_b}.{sig}"


def _verify_token(token: str, settings: Settings) -> Optional[str]:
    """校验签名 token，返回 username；失败返回 None。"""
    if "." not in token:
        return None
    payload_b, sig = token.rsplit(".", 1)
    expected = _b64u(hmac.new(settings.app_secret_key.encode("utf-8"), payload_b.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(expected, sig):
        return None
    try:
        payload = json.loads(_ub64u(payload_b).decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("exp", 0) < time.time():
        return None
    return payload.get("u")


# ── 用户解析 ──────────────────────────────────────────────────────────

def _authenticate_admin(token: str, settings: Settings) -> Optional[CurrentUser]:
    """兼容：静态 admin token 直接作为超级管理员。"""
    if token == settings.app_admin_token:
        return CurrentUser(username="admin", is_admin=True)
    return None


def _authenticate_session(token: str, settings: Settings) -> Optional[CurrentUser]:
    """签名 token → 查 users 表，并实时校验账号的最终启用状态。"""
    username = _verify_token(token, settings)
    if not username:
        return None
    from app.models.user import User, is_effectively_active
    from app.core.database import SessionLocal
    from sqlalchemy import select as _sel
    db = SessionLocal()
    try:
        user = db.scalar(_sel(User).where(User.username == username))
    finally:
        db.close()
    if user is None or not is_effectively_active(user):
        return None
    return CurrentUser(username=user.username, is_admin=user.is_admin)


def get_current_user(
    authorization: str = Header(default=""),
    settings: Settings = Depends(get_settings),
) -> CurrentUser:
    """从 Authorization 头解析当前用户（本站签名 token / 静态 admin token）。"""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="缺少 Token")

    token = authorization.split(" ", 1)[1].strip()
    user = (
        _authenticate_session(token, settings)
        or _authenticate_admin(token, settings)
    )
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token 无效或已过期")
    return user


def require_admin(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    return user
