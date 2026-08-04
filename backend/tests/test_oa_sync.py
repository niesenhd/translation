from types import SimpleNamespace

import pytest

from app.models.user import (
    AUTH_SOURCE_LOCAL,
    AUTH_SOURCE_OA,
    User,
    is_effectively_active,
)
from app.services import oa_sync


class _FakeDb:
    def __init__(self, users):
        self.users = users
        self.added = []

    def scalars(self, _statement):
        return self.users

    def add(self, value):
        self.added.append(value)

    def commit(self):
        return None

    def rollback(self):
        return None

    def close(self):
        return None


def test_oa_sync_never_updates_reserved_local_admin(monkeypatch) -> None:
    local = User(
        username="admin",
        password_hash="pbkdf2_sha256$hash",
        auth_source=AUTH_SOURCE_LOCAL,
        is_admin=True,
        is_active=True,
        display_name="Local Admin",
        email="local-admin@example.test",
        oa_employed=False,
        oa_id=None,
    )
    db = _FakeDb([local])
    monkeypatch.setattr(
        oa_sync,
        "get_settings",
        lambda: SimpleNamespace(oa_base_url="https://oa.example", oa_app_key="key", oa_app_secret="secret"),
    )
    monkeypatch.setattr(oa_sync, "_generate_token", lambda *_args: ("token", 1))
    monkeypatch.setattr(
        oa_sync,
        "_fetch_all_employees",
        lambda *_args: [
            {
                "loginName": "admin",
                "id": "oa-1",
                "name": "OA Name",
                "email": "oa-admin@example.test",
                "status": "A",
                "inServiceStatus": "在职",
            }
        ],
    )
    monkeypatch.setattr(oa_sync, "SessionLocal", lambda: db)

    result = oa_sync.sync_users_from_oa()

    assert result["conflicts"] == 1
    assert result["conflict_usernames"] == ["admin"]
    assert local.auth_source == AUTH_SOURCE_LOCAL
    assert local.password_hash == "pbkdf2_sha256$hash"
    assert local.is_admin is True
    assert local.oa_id is None
    assert local.is_active is True
    assert local.display_name == "Local Admin"
    assert local.email == "local-admin@example.test"
    assert local.oa_employed is False
    assert db.added == []


def test_oa_sync_never_creates_reserved_admin_from_oa(monkeypatch) -> None:
    db = _FakeDb([])
    monkeypatch.setattr(
        oa_sync,
        "get_settings",
        lambda: SimpleNamespace(
            oa_base_url="https://oa.example",
            oa_app_key="key",
            oa_app_secret="secret",
        ),
    )
    monkeypatch.setattr(oa_sync, "_generate_token", lambda *_args: ("token", 1))
    monkeypatch.setattr(
        oa_sync,
        "_fetch_all_employees",
        lambda *_args: [
            {
                "loginName": "admin",
                "id": "oa-admin",
                "name": "OA Admin",
                "status": "A",
                "inServiceStatus": "在职",
            }
        ],
    )
    monkeypatch.setattr(oa_sync, "SessionLocal", lambda: db)

    result = oa_sync.sync_users_from_oa()

    assert result["created"] == 0
    assert result["updated"] == 0
    assert result["conflicts"] == 1
    assert result["conflict_usernames"] == ["admin"]
    assert db.added == []


@pytest.mark.parametrize(
    ("active_override", "is_employed", "expected_active"),
    [
        (None, True, True),
        (None, False, False),
        (True, False, True),
        (False, True, False),
    ],
)
def test_oa_sync_preserves_admin_override_and_updates_employment(
    monkeypatch,
    active_override: bool | None,
    is_employed: bool,
    expected_active: bool,
) -> None:
    user = User(
        username="oa-user",
        password_hash=None,
        auth_source=AUTH_SOURCE_OA,
        is_admin=True,
        is_active=not expected_active,
        active_override=active_override,
        display_name="OA User",
        email=None,
        phone=None,
        department=None,
        oa_id="oa-1",
        partner_id=None,
        partner_name=None,
        oa_employed=not is_employed,
    )
    db = _FakeDb([user])
    monkeypatch.setattr(
        oa_sync,
        "get_settings",
        lambda: SimpleNamespace(
            oa_base_url="https://oa.example",
            oa_app_key="key",
            oa_app_secret="secret",
        ),
    )
    monkeypatch.setattr(oa_sync, "_generate_token", lambda *_args: ("token", 1))
    monkeypatch.setattr(
        oa_sync,
        "_fetch_all_employees",
        lambda *_args: [
            {
                "loginName": "oa-user",
                "id": "oa-1",
                "name": "OA User",
                "status": "A" if is_employed else "I",
                "inServiceStatus": "在职" if is_employed else "离职",
            }
        ],
    )
    monkeypatch.setattr(oa_sync, "SessionLocal", lambda: db)

    oa_sync.sync_users_from_oa()

    assert user.oa_employed is is_employed
    assert user.active_override is active_override
    assert user.is_active is expected_active
    assert is_effectively_active(user) is expected_active
    assert user.is_admin is True


@pytest.mark.parametrize(("is_employed", "expected_active"), [(True, True), (False, False)])
def test_new_oa_user_starts_by_following_employment(
    monkeypatch, is_employed: bool, expected_active: bool
) -> None:
    db = _FakeDb([])
    monkeypatch.setattr(
        oa_sync,
        "get_settings",
        lambda: SimpleNamespace(
            oa_base_url="https://oa.example",
            oa_app_key="key",
            oa_app_secret="secret",
        ),
    )
    monkeypatch.setattr(oa_sync, "_generate_token", lambda *_args: ("token", 1))
    monkeypatch.setattr(
        oa_sync,
        "_fetch_all_employees",
        lambda *_args: [
            {
                "loginName": "new-oa-user",
                "id": "oa-2",
                "name": "New OA User",
                "status": "A" if is_employed else "I",
                "inServiceStatus": "在职" if is_employed else "离职",
            }
        ],
    )
    monkeypatch.setattr(oa_sync, "SessionLocal", lambda: db)

    oa_sync.sync_users_from_oa()

    assert len(db.added) == 1
    created = db.added[0]
    assert created.active_override is None
    assert created.oa_employed is is_employed
    assert created.is_active is expected_active
