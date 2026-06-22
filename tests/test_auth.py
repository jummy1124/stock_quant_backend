import jwt

from app.config import settings
from tests.conftest import register


def test_register_returns_token_and_user(client):
    resp = register(client, email="new@example.com", display_name="Newbie")
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert "token" in data
    user = data["user"]
    assert user["email"] == "new@example.com"
    assert user["display_name"] == "Newbie"
    assert "id" in user
    assert "password_hash" not in user

    # JWT payload contains sub and exp.
    payload = jwt.decode(
        data["token"], settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM]
    )
    assert payload["sub"] == user["id"]
    assert "exp" in payload


def test_register_duplicate_email_returns_409(client):
    assert register(client, email="dup@example.com").status_code == 201
    resp = register(client, email="dup@example.com")
    assert resp.status_code == 409
    assert "detail" in resp.json()


def test_register_invalid_email_returns_422(client):
    resp = client.post(
        "/userapi/auth/register", json={"email": "not-an-email", "password": "x"}
    )
    assert resp.status_code == 422


def test_login_success(client):
    register(client, email="login@example.com", password="pw12345")
    resp = client.post(
        "/userapi/auth/login",
        json={"email": "login@example.com", "password": "pw12345"},
    )
    assert resp.status_code == 200
    assert "token" in resp.json()
    assert resp.json()["user"]["email"] == "login@example.com"


def test_login_wrong_password_returns_401(client):
    register(client, email="wp@example.com", password="correct")
    resp = client.post(
        "/userapi/auth/login",
        json={"email": "wp@example.com", "password": "wrong"},
    )
    assert resp.status_code == 401
    assert "detail" in resp.json()


def test_login_unknown_email_returns_401(client):
    resp = client.post(
        "/userapi/auth/login",
        json={"email": "ghost@example.com", "password": "whatever"},
    )
    assert resp.status_code == 401


def test_logout_returns_204(client):
    resp = client.post("/userapi/auth/logout")
    assert resp.status_code == 204


def test_me_with_token(client):
    reg = register(client, email="me@example.com", display_name="Me")
    token = reg.json()["token"]
    resp = client.get("/userapi/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["email"] == "me@example.com"
    assert resp.json()["display_name"] == "Me"


def test_me_without_token_returns_401(client):
    assert client.get("/userapi/me").status_code == 401


def test_me_with_bad_token_returns_401(client):
    resp = client.get(
        "/userapi/me", headers={"Authorization": "Bearer not.a.valid.jwt"}
    )
    assert resp.status_code == 401


def test_me_with_expired_token_returns_401(client):
    import uuid
    from datetime import datetime, timedelta, timezone

    expired = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "exp": datetime.now(timezone.utc) - timedelta(minutes=1),
        },
        settings.JWT_SECRET,
        algorithm=settings.JWT_ALGORITHM,
    )
    resp = client.get(
        "/userapi/me", headers={"Authorization": f"Bearer {expired}"}
    )
    assert resp.status_code == 401


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
