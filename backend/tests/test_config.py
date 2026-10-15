"""Configuration guards and account promotion."""

from __future__ import annotations

import base64

import pytest

from app.config import Settings, assert_production_ready


def _key() -> str:
    return base64.b64encode(bytes(range(32))).decode()


def test_production_requires_an_explicit_master_key():
    with pytest.raises(RuntimeError, match="MASTER_KEY"):
        assert_production_ready(Settings(environment="production", master_key=""))


def test_production_accepts_a_real_master_key():
    assert_production_ready(Settings(environment="production", master_key=_key()))


def test_development_allows_the_fallback_key():
    assert_production_ready(Settings(environment="development", master_key=""))


def test_production_alias_is_recognised():
    settings = Settings(environment="prod", master_key="")
    assert settings.is_production is True
    with pytest.raises(RuntimeError):
        assert_production_ready(settings)


def test_master_key_bytes_are_32_bytes_and_stable():
    settings = Settings(master_key=_key())
    assert len(settings.master_key_bytes) == 32
    assert settings.master_key_bytes == Settings(master_key=_key()).master_key_bytes


def test_short_master_keys_are_hashed_not_rejected():
    """A passphrase should work locally instead of crashing at startup."""
    settings = Settings(master_key="a-short-passphrase")
    assert len(settings.master_key_bytes) == 32


def test_fallback_key_is_flagged_and_derived_from_jwt_secret():
    settings = Settings(master_key="", jwt_secret="a-jwt-secret")
    assert settings.using_ephemeral_master_key is True
    assert len(settings.master_key_bytes) == 32
    assert settings.master_key_bytes == Settings(
        master_key="", jwt_secret="a-jwt-secret"
    ).master_key_bytes


def test_admin_email_list_parsing():
    settings = Settings(admin_emails=" Boss@Example.com , ops@example.com , ")
    assert settings.admin_email_list == ["boss@example.com", "ops@example.com"]
    assert Settings(admin_emails="").admin_email_list == []


def test_promote_configured_admins_promotes_known_accounts(app_client, cluster):
    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models import User
    from app.services.accounts import promote_configured_admins

    token = app_client.post(
        "/api/auth/register",
        json={"email": "promote-me@example.com", "password": "supersecret123"},
    ).json()["access_token"]
    assert app_client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"}).json()[
        "is_admin"
    ] is False

    db = SessionLocal()
    try:
        promoted = promote_configured_admins(
            db, ["promote-me@example.com", "nobody@example.com"]
        )
        assert promoted == ["promote-me@example.com"]
        assert db.scalar(select(User).where(User.email == "promote-me@example.com")).is_admin is True

        # Idempotent: running again promotes nobody.
        assert promote_configured_admins(db, ["promote-me@example.com"]) == []
    finally:
        db.close()

    # The existing token now carries admin rights, because the user is
    # reloaded from the database on every request.
    assert app_client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {token}"}
    ).json()["is_admin"] is True


def test_ensure_admin_exists_promotes_the_oldest_account(app_client, cluster):
    """A cluster must never be left without an administrator."""
    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models import User
    from app.services.accounts import ensure_admin_exists

    db = SessionLocal()
    try:
        # Simulate a database that predates roles entirely.
        for user in db.scalars(select(User)).all():
            user.is_admin = False
        db.commit()

        promoted = ensure_admin_exists(db)
        assert promoted is not None
        oldest = db.scalars(select(User).order_by(User.id)).first()
        assert oldest.email == promoted
        assert oldest.is_admin is True

        # Idempotent once an administrator exists.
        assert ensure_admin_exists(db) is None
    finally:
        db.close()
