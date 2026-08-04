from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from app.core.config import Settings
from app.core.crypto import decrypt_secret, encrypt_secret, is_encrypted_secret


def _settings(**overrides) -> Settings:
    values = {
        "app_env": "development",
        "app_secret_key": "test-app-secret",
        "app_admin_token": "test-admin-token",
        "public_base_url": "https://translation.example.com/",
        "app_data_encryption_key": Fernet.generate_key().decode("ascii"),
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_crypto_round_trip_and_plaintext_compatibility() -> None:
    settings = _settings()
    encrypted = encrypt_secret("provider-secret", settings)

    assert is_encrypted_secret(encrypted)
    assert "provider-secret" not in encrypted
    assert decrypt_secret(encrypted, settings) == "provider-secret"
    assert decrypt_secret("legacy-plaintext", settings) == "legacy-plaintext"


def test_crypto_rejects_wrong_key() -> None:
    encrypted = encrypt_secret("provider-secret", _settings())

    with pytest.raises(ValueError, match="无法解密"):
        decrypt_secret(encrypted, _settings())


def test_production_requires_data_key_and_public_base_url() -> None:
    with pytest.raises(ValidationError) as exc_info:
        _settings(
            app_env="production",
            app_data_encryption_key="",
            public_base_url="",
        )

    message = str(exc_info.value)
    assert "APP_DATA_ENCRYPTION_KEY" in message
    assert "PUBLIC_BASE_URL" in message


@pytest.mark.parametrize(
    "url",
    ["/relative", "javascript:alert(1)", "https://user:pass@example.com", "https://example.com/?x=1"],
)
def test_public_base_url_must_be_safe_absolute_http_url(url: str) -> None:
    with pytest.raises(ValidationError, match="PUBLIC_BASE_URL"):
        _settings(public_base_url=url)
