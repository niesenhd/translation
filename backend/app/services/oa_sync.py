"""律智荟 OA 用户数据同步。

定时调 generateToken + getemployees，同步在职人员到本地 users 表。
"""
from __future__ import annotations

import logging
import time

import httpx
from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models.user import (
    AUTH_SOURCE_OA,
    RESERVED_LOCAL_ADMIN_USERNAME,
    User,
    is_effectively_active,
)

logger = logging.getLogger(__name__)


def _generate_token(base_url: str, app_key: str, app_secret: str) -> tuple[str, int]:
    """调律智荟 generateToken 获取 Saury token，返回 (token, timestamp)。"""
    ts = int(time.time() * 1000)
    resp = httpx.post(
        f"{base_url}/api/generateToken",
        json={"AppKey": app_key, "AppSecret": app_secret, "Timestamp": ts},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"generateToken 失败：{data.get('error')}")
    return data["result"], ts


def _fetch_all_employees(base_url: str, token: str, ts: int, app_key: str) -> list[dict]:
    """分页拉取全部人员。"""
    headers = {
        "S-App-Key": app_key,
        "S-Auth-Token": token,
        "S-Timestamp": str(ts),
        "Content-Type": "application/json",
    }
    all_items: list[dict] = []
    page = 1
    while True:
        resp = httpx.post(
            f"{base_url}/api/getemployees",
            json={"PageNumber": page, "PageSize": 1000},
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success"):
            raise RuntimeError(f"getemployees 失败：{data.get('error')}")
        result = data["result"]
        items = result.get("items", [])
        all_items.extend(items)
        if len(all_items) >= result.get("totalCount", 0) or not items:
            break
        page += 1
    return all_items


def sync_users_from_oa() -> dict:
    """从律智荟同步人员数据到本地 users 表。

    Returns:
        {"synced": int, "created": int, "updated": int, "employed_changed": int,
         "conflicts": int, "conflict_usernames": list[str]}
    """
    settings = get_settings()
    base_url = settings.oa_base_url
    app_key = settings.oa_app_key
    app_secret = settings.oa_app_secret

    if not app_key or not app_secret:
        logger.warning("OA 同步跳过：AppKey/AppSecret 未配置")
        return {
            "synced": 0,
            "created": 0,
            "updated": 0,
            "employed_changed": 0,
            "conflicts": 0,
            "conflict_usernames": [],
        }

    # 1. 获取 Saury token
    token, ts = _generate_token(base_url, app_key, app_secret)
    logger.info("OA 同步：已获取 Saury token")

    # 2. 拉取全部人员
    employees = _fetch_all_employees(base_url, token, ts, app_key)
    logger.info("OA 同步：拉取到 %d 条人员记录", len(employees))

    # 3. 构建全员映射：loginName -> (employee_data, is_employed)
    all_map: dict[str, tuple[dict, bool]] = {}
    reserved_conflict_usernames: list[str] = []
    for emp in employees:
        login_name = (emp.get("loginName") or "").strip()
        if not login_name:
            continue
        # admin 是翻译系统保留的本地管理员。即使 OA 存在同名人员、数据库尚无该账号，
        # 也不得创建或更新 OA 账号，避免覆盖本地密码和管理员设置。
        if login_name == RESERVED_LOCAL_ADMIN_USERNAME:
            reserved_conflict_usernames.append(login_name)
            logger.warning("OA 同步冲突：%s 是保留的本地管理员用户名，已跳过且未覆盖", login_name)
            continue
        is_employed = emp.get("status") == "A" and emp.get("inServiceStatus") == "在职"
        all_map[login_name] = (emp, is_employed)

    created = 0
    updated = 0
    employed_changed = 0
    conflict_usernames: list[str] = reserved_conflict_usernames.copy()

    db = SessionLocal()
    try:
        existing_users = list(db.scalars(select(User)))
        users_by_username = {user.username: user for user in existing_users}
        users_by_oa_id = {user.oa_id: user for user in existing_users if user.oa_id}

        # 4a. 全量同步：在职 + 离职都写入
        for login_name, (emp, is_employed) in all_map.items():
            user = users_by_username.get(login_name)
            oa_id = str(emp.get("id")) if emp.get("id") else None

            # 本地账号命名空间优先。OA 同名人员不得接管本地密码、管理员权限或禁用状态。
            if user is not None and user.auth_source != AUTH_SOURCE_OA and not user.oa_id:
                conflict_usernames.append(login_name)
                logger.warning("OA 同步冲突：用户名 %s 已属于本地账号，已跳过且未覆盖", login_name)
                continue
            oa_owner = users_by_oa_id.get(oa_id) if oa_id else None
            if oa_owner is not None and oa_owner.username != login_name:
                conflict_usernames.append(login_name)
                logger.warning(
                    "OA 同步冲突：OA ID 已绑定其他用户名（incoming=%s, existing=%s），已跳过",
                    login_name,
                    oa_owner.username,
                )
                continue

            if user is None:
                # 新人员：自动创建账号，新用户默认启用
                user = User(
                    username=login_name,
                    # OA 账号仅通过 SSO 登录，不创建任何可用的本地密码。
                    password_hash=None,
                    auth_source=AUTH_SOURCE_OA,
                    display_name=emp.get("name") or login_name,
                    email=emp.get("email") or None,
                    phone=emp.get("phone") or None,
                    department=emp.get("department") or None,
                    oa_id=oa_id,
                    partner_id=str(emp.get("qyPartner")) if emp.get("qyPartner") else None,
                    partner_name=emp.get("qyPartnerName") or None,
                    oa_employed=is_employed,
                    is_active=is_employed,
                    active_override=None,
                )
                db.add(user)
                users_by_username[login_name] = user
                if oa_id:
                    users_by_oa_id[oa_id] = user
                created += 1
            else:
                changed = False
                if user.auth_source != AUTH_SOURCE_OA:
                    user.auth_source = AUTH_SOURCE_OA
                    changed = True
                if user.password_hash is not None:
                    user.password_hash = None
                    changed = True
                if emp.get("name") and user.display_name != emp["name"]:
                    user.display_name = emp["name"]
                    changed = True
                if emp.get("email") is not None and user.email != emp.get("email"):
                    user.email = emp.get("email") or None
                    changed = True
                if emp.get("phone") is not None and user.phone != emp.get("phone"):
                    user.phone = emp.get("phone") or None
                    changed = True
                if emp.get("department") is not None and user.department != emp.get("department"):
                    user.department = emp.get("department") or None
                    changed = True
                if oa_id and user.oa_id != oa_id:
                    user.oa_id = oa_id
                    users_by_oa_id[oa_id] = user
                    changed = True
                partner_name = emp.get("qyPartnerName") or None
                if partner_name != user.partner_name:
                    user.partner_name = partner_name
                    changed = True
                partner_id = str(emp.get("qyPartner")) if emp.get("qyPartner") else None
                if partner_id != user.partner_id:
                    user.partner_id = partner_id
                    changed = True
                # OA 在职状态始终同步；管理员强制状态优先于同步结果。
                if user.oa_employed != is_employed:
                    user.oa_employed = is_employed
                    changed = True
                    employed_changed += 1
                effective_active = is_effectively_active(user)
                if user.is_active != effective_active:
                    user.is_active = effective_active
                    changed = True
                if changed:
                    updated += 1

        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    result = {
        "synced": len(all_map),
        "created": created,
        "updated": updated,
        "employed_changed": employed_changed,
        "conflicts": len(conflict_usernames),
        "conflict_usernames": sorted(set(conflict_usernames))[:100],
    }
    logger.info("OA 同步完成：%s", result)
    return result
