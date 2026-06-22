import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.db import get_session
from app.main import app

# Import models so their tables register on SQLModel.metadata.
from app import models  # noqa: F401


@pytest.fixture(name="engine")
def engine_fixture():
    # In-memory SQLite shared across connections via StaticPool.
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    yield engine
    SQLModel.metadata.drop_all(engine)


@pytest.fixture(name="client")
def client_fixture(engine):
    def get_session_override():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = get_session_override
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def register(client, email="a@example.com", password="secret123", display_name=None):
    body = {"email": email, "password": password}
    if display_name is not None:
        body["display_name"] = display_name
    return client.post("/userapi/auth/register", json=body)


def auth_header(client, **kwargs):
    resp = register(client, **kwargs)
    assert resp.status_code == 201, resp.text
    token = resp.json()["token"]
    return {"Authorization": f"Bearer {token}"}
