"""Tests for registration, login and token handling."""

from __future__ import annotations

import uuid


def _email() -> str:
    return f"auth-{uuid.uuid4().hex[:10]}@example.com"


def test_register_returns_token_and_profile(app_client, cluster):
    email = _email()
    response = app_client.post(
        "/api/auth/register",
        json={"email": email, "password": "supersecret123", "full_name": "Ada Lovelace"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] > 0
    assert body["user"]["email"] == email
    assert body["user"]["full_name"] == "Ada Lovelace"
    assert "hashed_password" not in body["user"]


def test_register_rejects_duplicate_email(app_client, cluster):
    email = _email()
    payload = {"email": email, "password": "supersecret123"}
    assert app_client.post("/api/auth/register", json=payload).status_code == 201
    duplicate = app_client.post("/api/auth/register", json=payload)
    assert duplicate.status_code == 409


def test_register_rejects_short_password(app_client, cluster):
    response = app_client.post(
        "/api/auth/register", json={"email": _email(), "password": "short"}
    )
    assert response.status_code == 422


def test_register_rejects_invalid_email(app_client, cluster):
    response = app_client.post(
        "/api/auth/register", json={"email": "not-an-email", "password": "supersecret123"}
    )
    assert response.status_code == 422


def test_login_with_valid_credentials(app_client, cluster):
    email = _email()
    password = "supersecret123"
    app_client.post("/api/auth/register", json={"email": email, "password": password})

    response = app_client.post("/api/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200
    assert response.json()["access_token"]


def test_login_rejects_wrong_password(app_client, cluster):
    email = _email()
    app_client.post(
        "/api/auth/register", json={"email": email, "password": "supersecret123"}
    )
    response = app_client.post(
        "/api/auth/login", json={"email": email, "password": "wrong-password"}
    )
    assert response.status_code == 401


def test_me_requires_a_token(app_client, cluster):
    assert app_client.get("/api/auth/me").status_code == 401
    assert app_client.get("/api/auth/me", headers={"Authorization": "Bearer nonsense"}).status_code == 401


def test_me_returns_current_user(app_client, cluster, auth, user_token):
    response = app_client.get("/api/auth/me", headers=auth)
    assert response.status_code == 200
    assert response.json()["email"].startswith("user-")


def test_long_passwords_are_supported(app_client, cluster):
    """bcrypt truncates at 72 bytes; our pre-hash must allow longer secrets."""
    email = _email()
    long_password = "p" * 200
    created = app_client.post(
        "/api/auth/register", json={"email": email, "password": long_password}
    )
    assert created.status_code == 201
    login = app_client.post("/api/auth/login", json={"email": email, "password": long_password})
    assert login.status_code == 200
