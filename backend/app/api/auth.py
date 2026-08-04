"""认证接口：本地账密登录 + 当前用户信息。"""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.security import CurrentUser, create_token, get_current_user, verify_password
from app.models.user import AUTH_SOURCE_LOCAL, User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

# 登录防爆破（基于 Redis，跨 worker 共享；Redis 不可用时 fail-open 不阻断登录）
_LOGIN_MAX_FAILURES = 5        # 窗口内允许的最大失败次数
_LOGIN_WINDOW_SECONDS = 300    # 失败计数窗口（5 分钟）
_LOGIN_LOCKOUT_SECONDS = 900   # 触发上限后锁定时长（15 分钟）


def _client_ip(request: Request) -> str:
    """取真实客户端 IP：优先 X-Forwarded-For（nginx 反代后），否则连接地址。"""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _login_redis():
    try:
        import redis
        return redis.from_url(get_settings().redis_url, decode_responses=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("登录限速：Redis 不可用，本次不做限速（fail-open）：%s", exc)
        return None


def _check_login_locked(ip: str) -> None:
    """若该 IP 处于锁定期，抛 429。Redis 不可用时放行。"""
    r = _login_redis()
    if r is None:
        return
    try:
        if r.get(f"login:lock:{ip}"):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="登录尝试过于频繁，请稍后再试。",
            )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("登录限速检查失败，放行：%s", exc)


def _record_login_failure(ip: str) -> None:
    """记录一次失败；达到上限则设置锁定。"""
    r = _login_redis()
    if r is None:
        return
    try:
        key = f"login:fail:{ip}"
        count = r.incr(key)
        if count == 1:
            r.expire(key, _LOGIN_WINDOW_SECONDS)
        if count >= _LOGIN_MAX_FAILURES:
            r.setex(f"login:lock:{ip}", _LOGIN_LOCKOUT_SECONDS, "1")
            r.delete(key)
            logger.warning("登录限速：IP %s 失败 %d 次，锁定 %ds", ip, count, _LOGIN_LOCKOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001
        logger.warning("登录限速记录失败，忽略：%s", exc)


def _clear_login_failures(ip: str) -> None:
    r = _login_redis()
    if r is None:
        return
    try:
        r.delete(f"login:fail:{ip}", f"login:lock:{ip}")
    except Exception:  # noqa: BLE001
        pass


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class LoginResponse(BaseModel):
    token: str
    username: str
    is_admin: bool
    display_name: str | None = None


class MeResponse(BaseModel):
    username: str
    is_admin: bool
    display_name: str | None = None


@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest, request: Request):
    settings = get_settings()
    ip = _client_ip(request)
    # 先检查是否处于锁定期
    _check_login_locked(ip)
    db = SessionLocal()
    try:
        user = db.scalar(select(User).where(User.username == payload.username))
    finally:
        db.close()
    # 用户名/密码错误统一返回同一提示，避免账号枚举
    if (
        user is None
        or user.auth_source != AUTH_SOURCE_LOCAL
        or bool(user.oa_id)
        or not user.is_active
        or not verify_password(payload.password, user.password_hash)
    ):
        _record_login_failure(ip)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    # 登录成功：清空该 IP 的失败计数
    _clear_login_failures(ip)
    token = create_token(user.username, settings)
    return LoginResponse(
        token=token,
        username=user.username,
        is_admin=user.is_admin,
        display_name=user.display_name,
    )


@router.get("/me", response_model=MeResponse)
def me(user: CurrentUser = Depends(get_current_user)):
    db = SessionLocal()
    try:
        u = db.scalar(select(User).where(User.username == user.username))
    finally:
        db.close()
    return MeResponse(
        username=user.username,
        is_admin=user.is_admin,
        display_name=u.display_name if u else None,
    )
