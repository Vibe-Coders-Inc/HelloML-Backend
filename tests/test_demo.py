"""
Tests for demo endpoint — /demo/session
"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock


class TestDemoSession:

    def test_create_session_success(self, client):
        """Should return ephemeral key, model, and voice."""
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {
            "client_secret": {"value": "ek_test_abc123"}
        }

        with patch("api.crud.demo.httpx.AsyncClient") as mock_cls:
            mock_ctx = AsyncMock()
            mock_ctx.post.return_value = fake_resp
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            resp = client.post("/demo/session", json={})
            assert resp.status_code == 200
            data = resp.json()
            assert data["ephemeral_key"] == "ek_test_abc123"
            assert data["model"] == "gpt-realtime-1.5"
            assert data["voice"] == "ash"

    def test_invalid_voice_returns_400(self, client):
        resp = client.post("/demo/session", json={"voice": "invalid_voice"})
        assert resp.status_code == 400
        assert "Invalid voice" in resp.json()["detail"]

    def test_rate_limiting(self, client):
        """After 5 requests from same IP, should get 429."""
        # Reset rate limiter
        import api.crud.demo as demo_mod
        demo_mod._rate_limit.clear()

        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {"value": "ek_test"}

        with patch("api.crud.demo.httpx.AsyncClient") as mock_cls:
            mock_ctx = AsyncMock()
            mock_ctx.post.return_value = fake_resp
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            for i in range(5):
                r = client.post("/demo/session", json={})
                assert r.status_code == 200, f"Request {i+1} failed: {r.text}"

            # 6th should be rate limited
            r = client.post("/demo/session", json={})
            assert r.status_code == 429

        # Cleanup
        demo_mod._rate_limit.clear()

    def test_custom_voice_selection(self, client):
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {"value": "ek_test"}

        with patch("api.crud.demo.httpx.AsyncClient") as mock_cls:
            mock_ctx = AsyncMock()
            mock_ctx.post.return_value = fake_resp
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            resp = client.post("/demo/session", json={"voice": "coral"})
            assert resp.status_code == 200
            assert resp.json()["voice"] == "coral"

    def test_openai_failure_returns_502(self, client):
        """If OpenAI returns non-200, we should get 502."""
        import api.crud.demo as demo_mod
        demo_mod._rate_limit.clear()

        fake_resp = MagicMock()
        fake_resp.status_code = 500
        fake_resp.text = "Internal Server Error"

        with patch("api.crud.demo.httpx.AsyncClient") as mock_cls:
            mock_ctx = AsyncMock()
            mock_ctx.post.return_value = fake_resp
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            resp = client.post("/demo/session", json={})
            assert resp.status_code == 502

        demo_mod._rate_limit.clear()
