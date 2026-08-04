"""创建/补齐本地登录账号（幂等）。

用法（容器内）：
  python /scripts/seed_users.py /path/to/creds.txt [--admin-pass <pw>] [--reset]

creds.txt 每行 "username password [is_admin]"（is_admin 为可选，值 admin 表示管理员）。
- 默认仅创建缺失用户（已存在则跳过，不改密码），可重复运行。
- --reset：对已存在用户也重置密码。
- --admin-pass <pw>：额外确保存在一个管理员账号（名为 admin），用给定密码。

注意：密码仅用于此处计算哈希入库，不落盘、不入 git。
"""
import argparse
from pathlib import Path

from sqlalchemy import select

from app.core.database import SessionLocal
from app.core.security import hash_password
from app.models.user import (
    AUTH_SOURCE_LOCAL,
    RESERVED_LOCAL_ADMIN_USERNAME,
    User,
)


def upsert(db, username: str, password: str, is_admin: bool, reset: bool) -> str:
    is_reserved_admin = username == RESERVED_LOCAL_ADMIN_USERNAME
    is_admin = is_admin or is_reserved_admin
    existing = db.scalar(select(User).where(User.username == username))
    if existing:
        if is_reserved_admin:
            existing.auth_source = AUTH_SOURCE_LOCAL
            existing.is_admin = True
            existing.oa_id = None
            existing.active_override = None
        if reset:
            existing.password_hash = hash_password(password)
            existing.is_admin = is_admin
            existing.is_active = True
            return f"reset  {username}"
        return f"skip   {username} (已存在，加 --reset 可重置密码)"
    db.add(User(
        username=username,
        password_hash=hash_password(password),
        auth_source=AUTH_SOURCE_LOCAL,
        is_admin=is_admin,
        is_active=True,
        display_name=username,
    ))
    return f"create {username}"


def main() -> int:
    parser = argparse.ArgumentParser(description="创建或补齐本地登录账号")
    parser.add_argument("credential_files", nargs="*")
    parser.add_argument("--admin-pass", default="")
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()

    db = SessionLocal()
    created = 0
    try:
        # 额外管理员账号
        if args.admin_pass:
            msg = upsert(db, "admin", args.admin_pass, is_admin=True, reset=args.reset)
            print(msg)
            created += 1

        for path in args.credential_files:
            for line in Path(path).read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                if len(parts) < 2:
                    continue
                username, password = parts[0], parts[1]
                is_admin = (len(parts) > 2 and parts[2].lower() == "admin")
                msg = upsert(db, username, password, is_admin, args.reset)
                print(msg)
                created += 1
        db.commit()
    finally:
        db.close()
    print(f"完成（处理 {created} 条）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
