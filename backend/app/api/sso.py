"""律智荟 OA 单点登录：签名请求换取 60 秒一次性浏览器 code。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import time
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.core.database import SessionLocal
from app.core.security import create_token
from app.models.user import (
    AUTH_SOURCE_OA,
    RESERVED_LOCAL_ADMIN_USERNAME,
    User,
    is_effectively_active,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sso", tags=["sso"])

_SSO_TIMESTAMP_WINDOW = 300
_SSO_REPLAY_TTL = 300
_SSO_CODE_TTL = 60


class SSOLoginRequest(BaseModel):
    loginName: str = Field(min_length=1, max_length=128)
    timestamp: int
    sign: str = Field(min_length=1, max_length=512)


class SSOLoginResponse(BaseModel):
    success: bool = True
    redirect_url: str


class SSOExchangeRequest(BaseModel):
    code: str = Field(min_length=20, max_length=256)


class SSOExchangeResponse(BaseModel):
    token: str
    username: str
    is_admin: bool
    display_name: str | None = None


def _verify_sso_sign(login_name: str, timestamp: int, sign: str, secret: str) -> bool:
    """验证 HMAC-SHA256 签名。签名串 = loginName + timestamp。"""
    sign_str = f"{login_name}{timestamp}"
    expected = base64.b64encode(
        hmac.new(secret.encode("utf-8"), sign_str.encode("utf-8"), hashlib.sha256).digest()
    ).decode("ascii")
    return hmac.compare_digest(expected, sign)


def _redis_client(settings: Settings):
    import redis
    return redis.from_url(settings.redis_url, decode_responses=True)


def _replay_key(payload: SSOLoginRequest) -> str:
    canonical = f"{payload.loginName}\0{payload.timestamp}\0{payload.sign}".encode("utf-8")
    return "sso:replay:" + hashlib.sha256(canonical).hexdigest()


def _code_key(code: str) -> str:
    return "sso:code:" + hashlib.sha256(code.encode("utf-8")).hexdigest()


def _claim_sso_request(payload: SSOLoginRequest, settings: Settings) -> None:
    """原子登记已使用签名；Redis 故障时 fail-closed。"""
    try:
        claimed = _redis_client(settings).set(
            _replay_key(payload), "1", nx=True, ex=_SSO_REPLAY_TTL
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("SSO 防重放缓存不可用：%s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="SSO 服务暂不可用，请稍后重试",
        ) from exc
    if not claimed:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="SSO 请求已使用或正在处理",
        )


def _store_sso_code(username: str, settings: Settings) -> str:
    code = secrets.token_urlsafe(32)
    try:
        stored = _redis_client(settings).set(
            _code_key(code), username, nx=True, ex=_SSO_CODE_TTL
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("SSO 一次性 code 缓存不可用：%s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="SSO 服务暂不可用，请稍后重试",
        ) from exc
    if not stored:  # 256-bit 随机 code 碰撞；拒绝而不是覆盖既有登录
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="SSO code 生成失败，请重试",
        )
    return code


def _consume_sso_code(code: str, settings: Settings) -> str | None:
    """GETDEL 原子读取并删除，保证 code 最多成功使用一次。"""
    try:
        value = _redis_client(settings).getdel(_code_key(code))
    except Exception as exc:  # noqa: BLE001
        logger.error("SSO 一次性 code 缓存不可用：%s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="SSO 服务暂不可用，请稍后重试",
        ) from exc
    return value if isinstance(value, str) and value else None


def _get_active_oa_user(username: str) -> User | None:
    # Defense in depth: even malformed/restored data must never make the
    # reserved local administrator eligible for OA SSO.
    if username == RESERVED_LOCAL_ADMIN_USERNAME:
        return None
    db = SessionLocal()
    try:
        user = db.scalar(select(User).where(User.username == username))
        if (
            user is None
            or (user.auth_source != AUTH_SOURCE_OA and not user.oa_id)
            or not is_effectively_active(user)
        ):
            return None
        # 避免 Session 关闭后再触发属性懒加载。
        for attr in ("username", "is_admin", "display_name"):
            getattr(user, attr)
        return user
    finally:
        db.close()


@router.post("/login", response_model=SSOLoginResponse)
def sso_login(payload: SSOLoginRequest, settings: Settings = Depends(get_settings)):
    secret = settings.oa_sso_secret
    if not secret:
        raise HTTPException(status_code=500, detail="SSO 密钥未配置")
    if not settings.public_base_url:
        raise HTTPException(status_code=500, detail="PUBLIC_BASE_URL 未配置")

    if payload.timestamp < 1_000_000_000_000 or payload.timestamp > 9_999_999_999_999:
        raise HTTPException(status_code=400, detail="时间戳格式错误，需为 13 位毫秒级")
    now_ms = int(time.time() * 1000)
    if abs(now_ms - payload.timestamp) > _SSO_TIMESTAMP_WINDOW * 1000:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="时间戳已过期")

    if not _verify_sso_sign(payload.loginName, payload.timestamp, payload.sign, secret):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="签名校验失败")

    _claim_sso_request(payload, settings)
    user = _get_active_oa_user(payload.loginName)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"用户不存在或已停用：{payload.loginName}",
        )

    code = _store_sso_code(user.username, settings)
    redirect_url = f"{settings.public_base_url}/?sso_code={quote(code, safe='')}"
    logger.info("SSO 登录请求验证成功：%s", payload.loginName)
    return SSOLoginResponse(redirect_url=redirect_url)


@router.post("/exchange", response_model=SSOExchangeResponse)
def exchange_sso_code(
    payload: SSOExchangeRequest,
    settings: Settings = Depends(get_settings),
):
    username = _consume_sso_code(payload.code, settings)
    if not username:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="SSO code 无效、已使用或已过期",
        )

    user = _get_active_oa_user(username)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="SSO code 无效、已使用或已过期",
        )

    token = create_token(user.username, settings)
    logger.info("SSO code 兑换成功：%s", user.username)
    return SSOExchangeResponse(
        token=token,
        username=user.username,
        is_admin=user.is_admin,
        display_name=user.display_name,
    )
