"""SSO 单点登录接口（律智荟 OA 对接）。

律智荟服务器 POST 调用本接口，传入 loginName + timestamp + sign，
验证签名后返回登录 token，律智荟拿 token 跳转浏览器进入翻译系统。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.core.database import SessionLocal
from app.core.security import create_token
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sso", tags=["sso"])

_SSO_TIMESTAMP_WINDOW = 300  # 5 分钟


class SSOLoginRequest(BaseModel):
    loginName: str = Field(min_length=1, max_length=128)
    timestamp: int
    sign: str = Field(min_length=1)


class SSOLoginResponse(BaseModel):
    success: bool = True
    token: str
    redirect_url: str


def _verify_sso_sign(login_name: str, timestamp: int, sign: str, secret: str) -> bool:
    """验证 HMAC-SHA256 签名。签名串 = loginName + timestamp。"""
    sign_str = f"{login_name}{timestamp}"
    expected = base64.b64encode(
        hmac.new(secret.encode("utf-8"), sign_str.encode("utf-8"), hashlib.sha256).digest()
    ).decode("ascii")
    return hmac.compare_digest(expected, sign)


@router.post("/login", response_model=SSOLoginResponse)
def sso_login(payload: SSOLoginRequest, settings: Settings = Depends(get_settings)):
    secret = settings.oa_sso_secret
    if not secret:
        raise HTTPException(status_code=500, detail="SSO 密钥未配置")

    # 时间窗口校验
    now_ms = int(time.time() * 1000)
    if abs(now_ms - payload.timestamp) > _SSO_TIMESTAMP_WINDOW * 1000:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="时间戳已过期")

    # 签名校验
    if not _verify_sso_sign(payload.loginName, payload.timestamp, payload.sign, secret):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="签名校验失败")

    # 查本地用户
    db = SessionLocal()
    try:
        user = db.scalar(select(User).where(User.username == payload.loginName))
    finally:
        db.close()

    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"用户不存在或已离职：{payload.loginName}",
        )

    token = create_token(user.username, settings)
    logger.info("SSO 登录成功：%s", payload.loginName)
    return SSOLoginResponse(
        token=token,
        redirect_url=f"/?token={token}",
    )
