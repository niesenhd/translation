"""管理员用户数据导出接口。"""
from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from app.core.database import SessionLocal
from app.core.security import CurrentUser, require_admin
from app.models.user import AUTH_SOURCE_OA, User, is_effectively_active


router = APIRouter(prefix="/admin", tags=["admin"])

_CSV_HEADERS = (
    "用户ID",
    "登录名",
    "姓名",
    "账号来源",
    "OA用户ID",
    "邮箱",
    "手机",
    "部门",
    "主管合伙人",
    "管理员",
    "OA在职状态",
    "最终启用状态",
    "管理员覆盖状态",
    "创建时间",
    "更新时间",
)


def _safe_csv_cell(value: object | None) -> str:
    """Prevent spreadsheet formula execution without changing normal values."""
    if value is None:
        return ""
    text = str(value)
    if text.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")):
        return f"'{text}"
    return text


def _csv_row(values: tuple[object | None, ...]) -> str:
    buffer = io.StringIO(newline="")
    csv.writer(buffer).writerow([_safe_csv_cell(value) for value in values])
    return buffer.getvalue()


def _override_label(user: User) -> str:
    if user.auth_source != AUTH_SOURCE_OA and not user.oa_id:
        return "不适用"
    if user.active_override is None:
        return "跟随OA"
    return "强制启用" if user.active_override else "强制停用"


def _iter_user_csv() -> Iterator[str]:
    # Excel can identify the UTF-8 encoding reliably when the BOM is present.
    yield "\ufeff"
    yield _csv_row(_CSV_HEADERS)

    db = SessionLocal()
    try:
        users = db.scalars(select(User).order_by(User.username))
        for user in users:
            is_oa = user.auth_source == AUTH_SOURCE_OA or bool(user.oa_id)
            yield _csv_row(
                (
                    user.id,
                    user.username,
                    user.display_name,
                    "OA" if is_oa else "本地",
                    user.oa_id,
                    user.email,
                    user.phone,
                    user.department,
                    user.partner_name,
                    "是" if user.is_admin else "否",
                    ("在职" if user.oa_employed else "离职") if is_oa else "不适用",
                    "启用" if is_effectively_active(user) else "停用",
                    _override_label(user),
                    user.created_at.isoformat() if user.created_at else "",
                    user.updated_at.isoformat() if user.updated_at else "",
                )
            )
    finally:
        db.close()


@router.get("/users/export")
def export_all_users_csv(
    _current: CurrentUser = Depends(require_admin),
) -> StreamingResponse:
    filename = f"users-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.csv"
    return StreamingResponse(
        _iter_user_csv(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )
