"""鉴权：P0 阶段使用静态 admin Token；P3 阶段对接律智荟 OA。

注意：此处预置 OA 调用占位逻辑，P3 联调时只需替换 `_authenticate_oa` 实现。
"""
from typing import Optional

from fastapi import Depends, Header, HTTPException, status

from app.core.config import Settings, get_settings


class CurrentUser:
    """当前登录用户（极简实现，P3 替换为 OA 用户模型）。"""

    def __init__(self, username: str, is_admin: bool = False):
        self.username = username
        self.is_admin = is_admin


def _authenticate_admin(token: str, settings: Settings) -> Optional[CurrentUser]:
    if token == settings.app_admin_token:
        return CurrentUser(username="admin", is_admin=True)
    return None


def _authenticate_oa(access_token: str, settings: Settings) -> Optional[CurrentUser]:  # noqa: ARG001
    """P3 阶段实现：调律智荟 getUserInfo 接口换取用户信息。

    当前 P0 阶段不启用，仅保留接入位。
    """
    return None


def get_current_user(
    authorization: str = Header(default=""),
    settings: Settings = Depends(get_settings),
) -> CurrentUser:
    """从 Authorization 头提取 Token 并解析当前用户。"""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="缺少 Token")

    token = authorization.split(" ", 1)[1].strip()

    user = _authenticate_admin(token, settings) or _authenticate_oa(token, settings)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token 无效")
    return user


def require_admin(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    return user
