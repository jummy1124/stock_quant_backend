import os

# JWT_SECRET has no default (app.config fails fast without it), so the test
# environment must supply one BEFORE app.config is imported below. setdefault,
# not assignment, so a developer with a real .env / exported value keeps it.
os.environ.setdefault("JWT_SECRET", "test-secret-not-for-production-0123456789")
# Never open an SMTP connection from the suite, whatever the developer's .env
# says. app.email's console backend just logs.
os.environ["EMAIL_BACKEND"] = "console"
# Rate limits are per-process and would leak across tests; the dedicated
# rate-limit tests turn this back on for themselves.
os.environ["RATE_LIMIT_ENABLED"] = "false"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402
from sqlmodel import Session, SQLModel, create_engine  # noqa: E402

from app.db import get_session  # noqa: E402
from app.main import app  # noqa: E402

# Import models so their tables register on SQLModel.metadata.
from app import models  # noqa: F401,E402


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


@pytest.fixture(name="outbox")
def outbox_fixture():
    """Capture outgoing mail instead of logging it.

    Yields a list of (to, subject, text_body, html_body) tuples, so a test can
    pull the verification / reset link straight out of the message the endpoint
    would have sent.
    """
    from app import email as email_mod

    sent: list[tuple[str, str, str, str | None]] = []

    class _Capture:
        def send(self, to, subject, text_body, html_body=None):
            sent.append((to, subject, text_body, html_body))

    email_mod.set_email_sender(_Capture())
    yield sent
    email_mod.set_email_sender(None)


def token_from_email(text_body: str, param: str) -> str:
    """Extract the raw token from a link like `http://host/?verify=<token>`."""
    marker = f"?{param}="
    start = text_body.index(marker) + len(marker)
    end = start
    while end < len(text_body) and not text_body[end].isspace():
        end += 1
    return text_body[start:end]


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
