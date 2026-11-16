from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from app.services.credentials import CredentialsUnavailableError, decrypt, encrypt
from tests.conftest import make_connector_settings, make_settings


def test_round_trip_and_ciphertext_hides_the_token() -> None:
    settings = make_connector_settings()
    blob = encrypt(settings, {"token": "xoxp-super-secret", "n": 1})
    assert b"xoxp-super-secret" not in blob
    assert decrypt(settings, blob) == {"token": "xoxp-super-secret", "n": 1}


def test_wrong_key_asks_to_reconnect() -> None:
    blob = encrypt(make_connector_settings(), {"token": "t"})
    other = make_connector_settings(credentials_encryption_key=Fernet.generate_key().decode())
    with pytest.raises(CredentialsUnavailableError, match="reconnect"):
        decrypt(other, blob)


def test_missing_or_invalid_key() -> None:
    with pytest.raises(CredentialsUnavailableError, match="not set"):
        encrypt(make_settings(), {"token": "t"})
    with pytest.raises(CredentialsUnavailableError, match="not a valid Fernet key"):
        encrypt(make_connector_settings(credentials_encryption_key="short"), {"token": "t"})
