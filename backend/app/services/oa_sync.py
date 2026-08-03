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
from app.core.security import hash_password
from app.models.user import User

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
        {"synced": int, "created": int, "updated": int, "deactivated": int}
    """
    settings = get_settings()
    base_url = settings.oa_base_url
    app_key = settings.oa_app_key
    app_secret = settings.oa_app_secret

    if not app_key or not app_secret:
        logger.warning("OA 同步跳过：AppKey/AppSecret 未配置")
        return {"synced": 0, "created": 0, "updated": 0, "deactivated": 0}

    # 1. 获取 Saury token
    token, ts = _generate_token(base_url, app_key, app_secret)
    logger.info("OA 同步：已获取 Saury token")

    # 2. 拉取全部人员
    employees = _fetch_all_employees(base_url, token, ts, app_key)
    logger.info("OA 同步：拉取到 %d 条人员记录", len(employees))

    # 3. 构建全员映射：loginName -> (employee_data, is_active)
    all_map: dict[str, tuple[dict, bool]] = {}
    for emp in employees:
        login_name = (emp.get("loginName") or "").strip()
        if not login_name:
            continue
        is_active = emp.get("status") == "A" and emp.get("inServiceStatus") == "在职"
        all_map[login_name] = (emp, is_active)

    created = 0
    updated = 0
    deactivated = 0

    db = SessionLocal()
    try:
        # 4a. 全量同步：在职 + 离职都写入
        for login_name, (emp, is_active) in all_map.items():
            user = db.scalar(select(User).where(User.username == login_name))
            if user is None:
                # 新人员：自动创建账号
                user = User(
                    username=login_name,
                    password_hash=hash_password(str(time.time())),
                    display_name=emp.get("name") or login_name,
                    email=emp.get("email") or None,
                    phone=emp.get("phone") or None,
                    department=emp.get("department") or None,
                    oa_id=str(emp.get("id")) if emp.get("id") else None,
                    partner_id=str(emp.get("qyPartner")) if emp.get("qyPartner") else None,
                    partner_name=emp.get("qyPartnerName") or None,
                    is_active=is_active,
                )
                db.add(user)
                created += 1
            else:
                changed = False
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
                oa_id = str(emp.get("id")) if emp.get("id") else None
                if oa_id and user.oa_id != oa_id:
                    user.oa_id = oa_id
                    changed = True
                partner_name = emp.get("qyPartnerName") or None
                if partner_name != user.partner_name:
                    user.partner_name = partner_name
                    changed = True
                partner_id = str(emp.get("qyPartner")) if emp.get("qyPartner") else None
                if partner_id != user.partner_id:
                    user.partner_id = partner_id
                    changed = True
                if user.is_active != is_active:
                    user.is_active = is_active
                    changed = True
                    if not is_active:
                        deactivated += 1
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
        "deactivated": deactivated,
    }
    logger.info("OA 同步完成：%s", result)
    return result
