"""用户 ORM 模型（本地账密登录 + OA 律智荟同步）。"""
from __future__ import annotations

import uuid

from sqlalchemy import Boolean, CheckConstraint, Index, String, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin

AUTH_SOURCE_LOCAL = "local"
AUTH_SOURCE_OA = "oa"
RESERVED_LOCAL_ADMIN_USERNAME = "admin"


class User(TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "auth_source IN ('local', 'oa')",
            name="ck_users_auth_source",
        ),
        Index(
            "uq_users_oa_id_not_null",
            "oa_id",
            unique=True,
            postgresql_where=text("oa_id IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    # 登录用户名（唯一）
    username: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    # 密码哈希：格式 pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>
    password_hash: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # local=仅本地密码登录；oa=仅通过律智荟 SSO 登录
    auth_source: Mapped[str] = mapped_column(
        String(16), nullable=False, default=AUTH_SOURCE_LOCAL, server_default=AUTH_SOURCE_LOCAL
    )
    # 是否管理员（可进后台、看全部数据）
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    # 是否启用（禁用后无法登录）
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    # OA 账号的管理员强制状态；NULL 表示跟随 OA 在职状态。
    active_override: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # 显示名（可选，用于界面展示）
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # OA 同步字段（律智荟 getemployees 返回）
    email: Mapped[str | None] = mapped_column(String(256), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    department: Mapped[str | None] = mapped_column(String(128), nullable=True)
    oa_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # 主管合伙人（律智荟 getemployees 返回）
    partner_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    partner_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # OA 在职状态；同步时用于计算未被管理员覆盖的最终启用状态。
    oa_employed: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))


def is_effectively_active(user: User) -> bool:
    """返回用户的实时最终启用状态。"""
    is_oa_account = user.auth_source == AUTH_SOURCE_OA or bool(user.oa_id)
    if not is_oa_account:
        return user.is_active
    if user.active_override is not None:
        return user.active_override
    return user.oa_employed
