from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.api import admin, user_exports
from app.core.security import CurrentUser
from app.models.user import AUTH_SOURCE_LOCAL, AUTH_SOURCE_OA


class _AdminSession:
    def __init__(self, user) -> None:
        self.user = user
        self.commits = 0

    def get(self, _model, _user_id):
        return self.user

    def commit(self) -> None:
        self.commits += 1

    def refresh(self, _user) -> None:
        return None

    def close(self) -> None:
        return None


def _user(**overrides):
    values = {
        "id": "00000000-0000-0000-0000-000000000001",
        "username": "oa-user",
        "password_hash": "PASSWORD-HASH-MUST-NOT-BE-EXPORTED",
        "auth_source": AUTH_SOURCE_OA,
        "display_name": "OA User",
        "email": "user@example.com",
        "phone": "13800000000",
        "department": "Legal",
        "oa_id": "oa-1",
        "partner_id": "partner-1",
        "partner_name": "Partner",
        "is_admin": False,
        "is_active": True,
        "active_override": None,
        "oa_employed": True,
        "created_at": datetime(2026, 8, 4, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 8, 4, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.parametrize(
    ("oa_employed", "requested_active"),
    [(False, True), (True, False)],
)
def test_admin_force_state_overrides_oa_and_survives_in_response(
    monkeypatch, oa_employed: bool, requested_active: bool
) -> None:
    user = _user(
        oa_employed=oa_employed,
        is_active=not requested_active,
    )
    db = _AdminSession(user)
    monkeypatch.setattr(admin, "SessionLocal", lambda: db)

    result = admin.toggle_user_active(
        user.id,
        admin.UserToggleActive(is_active=requested_active),
        CurrentUser(username="another-admin", is_admin=True),
    )

    assert user.active_override is requested_active
    assert user.is_active is requested_active
    assert result.active_override is requested_active
    assert result.is_active is requested_active
    assert result.oa_employed is oa_employed
    assert db.commits == 1


def test_local_user_toggle_does_not_create_oa_override(monkeypatch) -> None:
    user = _user(
        username="local-user",
        auth_source=AUTH_SOURCE_LOCAL,
        oa_id=None,
        oa_employed=False,
        is_active=True,
        active_override=None,
    )
    db = _AdminSession(user)
    monkeypatch.setattr(admin, "SessionLocal", lambda: db)

    result = admin.toggle_user_active(
        user.id,
        admin.UserToggleActive(is_active=False),
        CurrentUser(username="another-admin", is_admin=True),
    )

    assert user.active_override is None
    assert user.is_active is False
    assert result.is_active is False
    assert db.commits == 1


class _ExportSession:
    def __init__(self, users) -> None:
        self.users = users
        self.statement = None
        self.closed = False

    def scalars(self, statement):
        self.statement = statement
        return self.users

    def close(self) -> None:
        self.closed = True


def test_all_users_csv_is_complete_safe_and_not_cached(monkeypatch) -> None:
    users = [
        _user(
            id="00000000-0000-0000-0000-000000000001",
            username="a-local",
            auth_source=AUTH_SOURCE_LOCAL,
            oa_id=None,
            display_name="=2+2",
            email="local,user@example.com",
            phone="+8613800000000",
            department="@department",
            oa_employed=True,
            active_override=None,
        ),
        _user(
            id="00000000-0000-0000-0000-000000000002",
            username="b-left-forced-on",
            oa_employed=False,
            is_active=False,
            active_override=True,
        ),
        _user(
            id="00000000-0000-0000-0000-000000000003",
            username="c-employed-forced-off",
            oa_employed=True,
            is_active=True,
            active_override=False,
        ),
    ]
    db = _ExportSession(users)
    monkeypatch.setattr(user_exports, "SessionLocal", lambda: db)

    content = "".join(user_exports._iter_user_csv())
    rows = list(csv.DictReader(io.StringIO(content.removeprefix("\ufeff"))))

    assert content.encode("utf-8").startswith(b"\xef\xbb\xbf")
    assert content.count("\ufeff") == 1
    assert [row["登录名"] for row in rows] == [
        "a-local",
        "b-left-forced-on",
        "c-employed-forced-off",
    ]
    assert rows[0]["姓名"] == "'=2+2"
    assert rows[0]["邮箱"] == "local,user@example.com"
    assert rows[0]["手机"] == "'+8613800000000"
    assert rows[0]["部门"] == "'@department"
    assert rows[1]["OA在职状态"] == "离职"
    assert rows[1]["最终启用状态"] == "启用"
    assert rows[1]["管理员覆盖状态"] == "强制启用"
    assert rows[2]["最终启用状态"] == "停用"
    assert rows[2]["管理员覆盖状态"] == "强制停用"
    assert "password_hash" not in rows[0]
    assert "PASSWORD-HASH-MUST-NOT-BE-EXPORTED" not in content
    assert "LIMIT" not in str(db.statement).upper()
    assert "OFFSET" not in str(db.statement).upper()
    assert db.closed is True

    response = user_exports.export_all_users_csv(
        CurrentUser(username="admin", is_admin=True)
    )
    assert response.media_type == "text/csv; charset=utf-8"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-disposition"].startswith(
        'attachment; filename="users-'
    )
