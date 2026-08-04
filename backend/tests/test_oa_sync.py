from types import SimpleNamespace

from app.models.user import AUTH_SOURCE_LOCAL, User
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


def test_oa_sync_never_overwrites_same_named_local_account(monkeypatch) -> None:
    local = User(
        username="local-admin",
        password_hash="pbkdf2_sha256$hash",
        auth_source=AUTH_SOURCE_LOCAL,
        is_admin=True,
        is_active=True,
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
                "loginName": "local-admin",
                "id": "oa-1",
                "name": "OA Name",
                "status": "A",
                "inServiceStatus": "在职",
            }
        ],
    )
    monkeypatch.setattr(oa_sync, "SessionLocal", lambda: db)

    result = oa_sync.sync_users_from_oa()

    assert result["conflicts"] == 1
    assert result["conflict_usernames"] == ["local-admin"]
    assert local.auth_source == AUTH_SOURCE_LOCAL
    assert local.password_hash == "pbkdf2_sha256$hash"
    assert local.is_admin is True
    assert local.oa_id is None
    assert db.added == []
