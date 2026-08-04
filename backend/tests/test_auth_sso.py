from __future__ import annotations

import base64
import hashlib
import hmac
import time
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException

from app.api import auth, feedback, sso
from app.core import database, security
from app.core.config import Settings
from app.models.user import AUTH_SOURCE_LOCAL, AUTH_SOURCE_OA
from app.services import oa_sync


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expirations: dict[str, int] = {}

    def set(self, key: str, value: str, *, nx: bool, ex: int):
        if nx and key in self.values:
            return False
        self.values[key] = value
        self.expirations[key] = ex
        return True

    def getdel(self, key: str):
        self.expirations.pop(key, None)
        return self.values.pop(key, None)


class FakeSession:
    def __init__(self, value) -> None:
        self.value = value

    def scalar(self, _statement):
        return self.value

    def get(self, _model, _key):
        return self.value

    def close(self) -> None:
        pass


class FakeOASession(FakeSession):
    def __init__(self, value=None) -> None:
        super().__init__(value)
        self.added = []

    def add(self, value) -> None:
        self.added.append(value)

    def scalars(self, _statement):
        return [] if self.value is None else [self.value]

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        app_secret_key="test-app-secret",
        app_admin_token="test-admin-token",
        app_data_encryption_key=Fernet.generate_key().decode("ascii"),
        public_base_url="https://translation.example.com:8080/",
        oa_app_key="test-oa-app-key",
        oa_app_secret="test-oa-app-secret",
        oa_sso_secret="test-sso-secret",
    )


def _signed_payload(settings: Settings, username: str = "zhangsan") -> sso.SSOLoginRequest:
    timestamp = int(time.time() * 1000)
    digest = hmac.new(
        settings.oa_sso_secret.encode(),
        f"{username}{timestamp}".encode(),
        hashlib.sha256,
    ).digest()
    return sso.SSOLoginRequest(
        loginName=username,
        timestamp=timestamp,
        sign=base64.b64encode(digest).decode(),
    )


def test_sso_request_is_replay_protected_for_300_seconds(monkeypatch) -> None:
    settings = _settings()
    redis = FakeRedis()
    monkeypatch.setattr(sso, "_redis_client", lambda _settings: redis)
    payload = _signed_payload(settings)

    sso._claim_sso_request(payload, settings)
    replay_key = sso._replay_key(payload)
    assert redis.expirations[replay_key] == 300

    with pytest.raises(HTTPException) as exc_info:
        sso._claim_sso_request(payload, settings)
    assert exc_info.value.status_code == 401


def test_sso_code_is_single_use_and_expires_in_60_seconds(monkeypatch) -> None:
    settings = _settings()
    redis = FakeRedis()
    monkeypatch.setattr(sso, "_redis_client", lambda _settings: redis)

    code = sso._store_sso_code("zhangsan", settings)
    code_key = sso._code_key(code)
    assert redis.expirations[code_key] == 60
    assert sso._consume_sso_code(code, settings) == "zhangsan"
    assert sso._consume_sso_code(code, settings) is None


def test_sso_login_returns_absolute_code_url_without_session_token(monkeypatch) -> None:
    settings = _settings()
    redis = FakeRedis()
    monkeypatch.setattr(sso, "_redis_client", lambda _settings: redis)
    monkeypatch.setattr(
        sso,
        "_get_active_oa_user",
        lambda _username: SimpleNamespace(username="zhangsan", is_admin=False, display_name="张三"),
    )

    result = sso.sso_login(_signed_payload(settings), settings)

    assert set(result.model_dump()) == {"success", "redirect_url"}
    assert result.redirect_url.startswith("https://translation.example.com:8080/?sso_code=")
    assert "token=" not in result.redirect_url


def test_exchange_consumes_code_before_issuing_session(monkeypatch) -> None:
    settings = _settings()
    redis = FakeRedis()
    user = SimpleNamespace(username="zhangsan", is_admin=False, display_name="张三")
    monkeypatch.setattr(sso, "_redis_client", lambda _settings: redis)
    monkeypatch.setattr(sso, "_get_active_oa_user", lambda _username: user)
    code = sso._store_sso_code(user.username, settings)

    result = sso.exchange_sso_code(sso.SSOExchangeRequest(code=code), settings)

    assert result.username == "zhangsan"
    assert result.token.count(".") == 1
    with pytest.raises(HTTPException) as exc_info:
        sso.exchange_sso_code(sso.SSOExchangeRequest(code=code), settings)
    assert exc_info.value.status_code == 401


def test_oa_account_cannot_use_local_password_login(monkeypatch) -> None:
    user = SimpleNamespace(
        username="zhangsan",
        auth_source=AUTH_SOURCE_OA,
        is_active=True,
        password_hash=security.hash_password("known-password"),
        oa_id="oa-123",
    )
    monkeypatch.setattr(auth, "SessionLocal", lambda: FakeSession(user))
    monkeypatch.setattr(auth, "_check_login_locked", lambda _ip: None)
    monkeypatch.setattr(auth, "_record_login_failure", lambda _ip: None)
    request = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))

    with pytest.raises(HTTPException) as exc_info:
        auth.login(auth.LoginRequest(username="zhangsan", password="known-password"), request)
    assert exc_info.value.status_code == 401


def test_reserved_admin_can_use_only_its_existing_local_password(monkeypatch) -> None:
    settings = _settings()
    existing_hash = security.hash_password("existing-admin-password")
    user = SimpleNamespace(
        username="admin",
        auth_source=AUTH_SOURCE_LOCAL,
        is_active=True,
        password_hash=existing_hash,
        oa_id=None,
        is_admin=True,
        display_name="管理员",
    )
    monkeypatch.setattr(auth, "SessionLocal", lambda: FakeSession(user))
    monkeypatch.setattr(auth, "get_settings", lambda: settings)
    monkeypatch.setattr(auth, "_check_login_locked", lambda _ip: None)
    monkeypatch.setattr(auth, "_record_login_failure", lambda _ip: None)
    monkeypatch.setattr(auth, "_clear_login_failures", lambda _ip: None)
    request = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))

    result = auth.login(
        auth.LoginRequest(username="admin", password="existing-admin-password"),
        request,
    )

    assert result.username == "admin"
    assert result.is_admin is True
    assert result.token.count(".") == 1


def test_reserved_local_admin_is_rejected_by_oa_sso(monkeypatch) -> None:
    settings = _settings()
    redis = FakeRedis()
    user = SimpleNamespace(
        username="admin",
        # SSO must reject the reserved name even if malformed/restored data
        # still carries an OA binding and would otherwise pass OA checks.
        auth_source=AUTH_SOURCE_OA,
        is_active=True,
        password_hash="existing-hash",
        oa_id="oa-admin",
        oa_employed=True,
        active_override=None,
        is_admin=True,
        display_name="管理员",
    )
    monkeypatch.setattr(sso, "SessionLocal", lambda: FakeSession(user))
    monkeypatch.setattr(sso, "_redis_client", lambda _settings: redis)

    with pytest.raises(HTTPException) as exc_info:
        sso.sso_login(_signed_payload(settings, "admin"), settings)

    assert exc_info.value.status_code == 404
    assert not any(key.startswith("sso:code:") for key in redis.values)


@pytest.mark.parametrize(
    ("auth_source", "is_active", "oa_employed", "active_override", "accepted"),
    [
        (AUTH_SOURCE_OA, True, True, None, True),
        (AUTH_SOURCE_OA, True, False, None, False),
        (AUTH_SOURCE_OA, False, False, True, True),
        (AUTH_SOURCE_OA, True, True, False, False),
        (AUTH_SOURCE_LOCAL, True, False, None, True),
        (AUTH_SOURCE_LOCAL, False, True, True, False),
    ],
)
def test_session_rechecks_effective_active_state(
    monkeypatch,
    auth_source: str,
    is_active: bool,
    oa_employed: bool,
    active_override: bool | None,
    accepted: bool,
) -> None:
    user = SimpleNamespace(
        username="zhangsan",
        is_admin=False,
        is_active=is_active,
        auth_source=auth_source,
        oa_employed=oa_employed,
        active_override=active_override,
        oa_id="oa-123" if auth_source == AUTH_SOURCE_OA else None,
    )
    monkeypatch.setattr(security, "_verify_token", lambda _token, _settings: "zhangsan")
    monkeypatch.setattr(database, "SessionLocal", lambda: FakeSession(user))

    result = security._authenticate_session("token", _settings())
    assert (result is not None) is accepted


@pytest.mark.parametrize(
    ("auth_source", "oa_employed", "active_override", "accepted"),
    [
        (AUTH_SOURCE_OA, False, None, False),
        (AUTH_SOURCE_OA, False, True, True),
        (AUTH_SOURCE_OA, True, False, False),
        (AUTH_SOURCE_LOCAL, True, True, False),
    ],
)
def test_sso_rechecks_effective_active_state(
    monkeypatch,
    auth_source: str,
    oa_employed: bool,
    active_override: bool | None,
    accepted: bool,
) -> None:
    user = SimpleNamespace(
        username="zhangsan",
        is_admin=False,
        display_name="张三",
        is_active=True,
        auth_source=auth_source,
        oa_employed=oa_employed,
        active_override=active_override,
        oa_id="oa-123" if auth_source == AUTH_SOURCE_OA else None,
    )
    monkeypatch.setattr(sso, "SessionLocal", lambda: FakeSession(user))

    result = sso._get_active_oa_user("zhangsan")

    assert (result is not None) is accepted


def test_feedback_rejects_another_users_task(monkeypatch) -> None:
    class CapturingSession(FakeSession):
        statement = None

        def scalar(self, statement):
            self.statement = statement
            return None

    db = CapturingSession(None)
    monkeypatch.setattr(feedback, "SessionLocal", lambda: db)

    with pytest.raises(HTTPException) as exc_info:
        feedback.create_feedback(
            feedback.FeedbackCreate(task_id="task-id", rating=5),
            security.CurrentUser("zhangsan"),
        )
    assert exc_info.value.status_code == 404
    assert "translation_tasks.owner" in str(db.statement)


def test_oa_sync_creates_sso_only_account_without_local_password(monkeypatch) -> None:
    db = FakeOASession()
    monkeypatch.setattr(oa_sync, "SessionLocal", lambda: db)
    monkeypatch.setattr(oa_sync, "get_settings", _settings)
    monkeypatch.setattr(oa_sync, "_generate_token", lambda *_args: ("oa-token", 123))
    monkeypatch.setattr(
        oa_sync,
        "_fetch_all_employees",
        lambda *_args: [{"loginName": "zhangsan", "name": "张三", "status": "A", "inServiceStatus": "在职"}],
    )
    result = oa_sync.sync_users_from_oa()

    assert result == {
        "synced": 1,
        "created": 1,
        "updated": 0,
        "employed_changed": 0,
        "conflicts": 0,
        "conflict_usernames": [],
    }
    assert len(db.added) == 1
    assert db.added[0].auth_source == AUTH_SOURCE_OA
    assert db.added[0].password_hash is None
    assert db.added[0].active_override is None


def test_oa_sync_converts_existing_oa_admin_to_sso_only(monkeypatch) -> None:
    existing = SimpleNamespace(
        username="zhangsan",
        auth_source=AUTH_SOURCE_LOCAL,
        password_hash="legacy-local-hash",
        display_name="张三",
        email=None,
        phone=None,
        department=None,
        oa_id="oa-123",
        partner_id=None,
        partner_name=None,
        oa_employed=True,
        active_override=None,
        is_active=True,
        is_admin=True,
    )
    db = FakeOASession(existing)
    monkeypatch.setattr(oa_sync, "SessionLocal", lambda: db)
    monkeypatch.setattr(oa_sync, "get_settings", _settings)
    monkeypatch.setattr(oa_sync, "_generate_token", lambda *_args: ("oa-token", 123))
    monkeypatch.setattr(
        oa_sync,
        "_fetch_all_employees",
        lambda *_args: [{"loginName": "zhangsan", "name": "张三", "id": "oa-123", "status": "A", "inServiceStatus": "在职"}],
    )

    result = oa_sync.sync_users_from_oa()

    assert result["updated"] == 1
    assert existing.auth_source == AUTH_SOURCE_OA
    assert existing.password_hash is None
