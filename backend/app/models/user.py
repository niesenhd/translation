"""用户 ORM 模型（本地账密登录 + OA 律智荟同步）。"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # 登录用户名（唯一）
    username: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    # 密码哈希：格式 pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>
    password_hash: Mapped[str] = mapped_column(String(256))
    # 是否管理员（可进后台、看全部数据）
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    # 是否启用（禁用后无法登录）
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # 显示名（可选，用于界面展示）
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # OA 同步字段（律智荟 getemployees 返回）
    email: Mapped[str | None] = mapped_column(String(256), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    department: Mapped[str | None] = mapped_column(String(128), nullable=True)
    oa_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
