"""认证接口：本地账密登录 + 当前用户信息。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.security import CurrentUser, create_token, get_current_user, verify_password
from app.models.user import User

router = APIRouter(prefix="/auth", tags=["auth"])


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
def login(payload: LoginRequest):
    settings = get_settings()
    db = SessionLocal()
    try:
        user = db.scalar(select(User).where(User.username == payload.username))
    finally:
        db.close()
    # 用户名/密码错误统一返回同一提示，避免账号枚举
    if user is None or not user.is_active or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
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
