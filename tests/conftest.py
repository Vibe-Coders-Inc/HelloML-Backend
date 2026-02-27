"""
Shared test fixtures — mocks for Supabase, OpenAI, Twilio, and FastAPI test client.
All external services are mocked so tests run without real credentials.
"""

import os
import sys
import pytest
from unittest.mock import MagicMock, patch, AsyncMock

# Set required env vars BEFORE any app imports
os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
os.environ.setdefault("SUPABASE_PUBLISHED_KEY", "fake-anon-key")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake-service-key")
os.environ.setdefault("OPENAI_API_KEY", "sk-fake-key")
os.environ.setdefault("OPENAI_WEBHOOK_SECRET", "whsec_fake_secret")
os.environ.setdefault("ACCOUNT_SID", "ACfake")
os.environ.setdefault("AUTH_TOKEN", "fake-auth-token")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_fake")
os.environ.setdefault("API_BASE_URL", "https://api.test.helloml.app")


# ---------------------------------------------------------------------------
# Mock Supabase helpers
# ---------------------------------------------------------------------------

class MockQueryBuilder:
    """Fluent query builder that returns configurable data."""

    def __init__(self, data=None):
        self._data = data if data is not None else []

    def select(self, *a, **kw):
        return self

    def insert(self, data):
        # Return the inserted data with an id
        if isinstance(data, dict):
            row = {"id": 1, **data}
            self._data = [row]
        return self

    def update(self, data):
        if self._data:
            self._data = [{**self._data[0], **data}]
        return self

    def delete(self):
        return self

    def eq(self, *a, **kw):
        return self

    def in_(self, *a, **kw):
        return self

    def not_(self):
        return self

    @property
    def is_(self):
        return lambda *a, **kw: self

    def limit(self, *a):
        return self

    def single(self):
        return self

    def execute(self):
        resp = MagicMock()
        resp.data = self._data
        return resp


class MockSupabaseClient:
    """Minimal mock for supabase.Client used in tests."""

    def __init__(self):
        self._tables: dict[str, list[dict]] = {}
        self.auth = MagicMock()

    def table(self, name: str):
        return MockQueryBuilder(self._tables.get(name, []))

    def set_table_data(self, name: str, data: list[dict]):
        self._tables[name] = data


@pytest.fixture()
def mock_db():
    """Return a fresh MockSupabaseClient and patch get_service_client."""
    db = MockSupabaseClient()
    with patch("api.database.get_service_client", return_value=db), \
         patch("api.database._service_client", db):
        yield db


@pytest.fixture()
def mock_openai():
    """Patch OpenAI client globally."""
    client = MagicMock()
    with patch("openai.OpenAI", return_value=client):
        yield client


# ---------------------------------------------------------------------------
# FastAPI test client
# ---------------------------------------------------------------------------

@pytest.fixture()
def app():
    """Import and return the raw FastAPI app (not wrapped in FlyReplayMiddleware)."""
    from api.main import _app
    return _app


@pytest.fixture()
def client(app):
    """Synchronous TestClient (httpx-backed)."""
    from fastapi.testclient import TestClient
    return TestClient(app)


@pytest.fixture()
def async_client(app):
    """Async test client for async tests."""
    import httpx
    from httpx import ASGITransport
    transport = ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def auth_headers():
    """Return a fake Bearer token header."""
    return {"Authorization": "Bearer fake-jwt-token"}


@pytest.fixture()
def mock_auth(app):
    """Override get_current_user dependency to return a fake authenticated user."""
    from api.auth import AuthenticatedUser, get_current_user

    fake_user = AuthenticatedUser(
        id="user-uuid-123",
        email="test@example.com",
        access_token="fake-jwt-token",
    )
    fake_db = MockSupabaseClient()
    fake_user._db_client = fake_db

    app.dependency_overrides[get_current_user] = lambda: fake_user
    yield fake_user, fake_db
    app.dependency_overrides.pop(get_current_user, None)
