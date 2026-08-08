"""Config: test-mode-only enforcement of STRIPE_SECRET_KEY."""

from __future__ import annotations

import pytest

from ledgerproof.config import Settings, get_settings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Keep the ambient environment / .env out of these assertions.
    for var in (
        "STRIPE_SECRET_KEY",
        "STRIPE_WEBHOOK_SECRET",
        "DATABASE_URL",
        "TEST_DATABASE_URL",
        "APP_DATABASE_URL",
        "REDIS_URL",
    ):
        monkeypatch.delenv(var, raising=False)


def test_live_key_is_a_hard_error() -> None:
    with pytest.raises(ValueError) as excinfo:
        Settings(stripe_secret_key="sk_live_abc123", _env_file=None)
    msg = str(excinfo.value)
    assert "live" in msg.lower()
    assert "sk_test_" in msg  # the refusal tells you what to use instead


def test_live_key_from_environment_is_also_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_abc123")
    with pytest.raises(ValueError):
        Settings(_env_file=None)


def test_non_test_non_live_key_rejected() -> None:
    with pytest.raises(ValueError):
        Settings(stripe_secret_key="rk_test_abc123", _env_file=None)


def test_test_key_accepted() -> None:
    s = Settings(stripe_secret_key="sk_test_abc123", _env_file=None)
    assert s.stripe_secret_key == "sk_test_abc123"


def test_empty_key_accepted() -> None:
    s = Settings(_env_file=None)
    assert s.stripe_secret_key == ""


def test_defaults_point_at_local_services() -> None:
    s = Settings(_env_file=None)
    assert s.database_url.endswith("/ledgerproof")
    assert s.test_database_url.endswith("/ledgerproof_test")
    assert s.app_database_url.startswith("postgresql://ledger_app:")
    assert s.redis_url.startswith("redis://")


def test_get_settings_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_cached")
    get_settings.cache_clear()
    try:
        assert get_settings() is get_settings()
    finally:
        get_settings.cache_clear()
