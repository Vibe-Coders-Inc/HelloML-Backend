"""
Tests for the Twilio WebSocket voice handler — /conversation/{agent_id}/media-stream
"""

import json
import pytest
from unittest.mock import patch, MagicMock, AsyncMock


# ---------------------------------------------------------------------------
# Twilio voice webhook (TwiML)
# ---------------------------------------------------------------------------

class TestVoiceWebhook:

    def test_incoming_call_returns_twiml(self, client):
        """POST /{agent_id}/voice should return TwiML with <Stream>."""
        agent_row = {
            "id": 1, "business_id": 10, "name": "A", "greeting": "Hi",
            "goodbye": "Bye", "voice_model": "ash", "model_type": "gpt-realtime-1.5",
        }

        mock_db = MagicMock()

        # agent lookup
        agent_qb = MagicMock()
        agent_qb.select.return_value = agent_qb
        agent_qb.eq.return_value = agent_qb
        agent_qb.single.return_value = agent_qb
        agent_resp = MagicMock(); agent_resp.data = agent_row
        agent_qb.execute.return_value = agent_resp

        # subscription check - no active sub
        sub_qb = MagicMock()
        sub_qb.select.return_value = sub_qb
        sub_qb.eq.return_value = sub_qb
        sub_qb.in_.return_value = sub_qb
        sub_qb.limit.return_value = sub_qb
        sub_resp = MagicMock(); sub_resp.data = [{"status": "active"}]
        sub_qb.execute.return_value = sub_resp

        # conversation insert
        conv_qb = MagicMock()
        conv_qb.insert.return_value = conv_qb
        conv_resp = MagicMock(); conv_resp.data = [{"id": 77}]
        conv_qb.execute.return_value = conv_resp

        call_n = {"n": 0}
        def _table(name):
            call_n["n"] += 1
            if name == "agent":
                return agent_qb
            if name == "subscription":
                return sub_qb
            if name == "conversation":
                return conv_qb
            return MagicMock()

        mock_db.table = _table

        with patch("api.crud.realtime_voice.get_service_client", return_value=mock_db):
            resp = client.post(
                "/conversation/1/voice",
                data={"From": "+15551234567"},
            )
            assert resp.status_code == 200
            assert "application/xml" in resp.headers["content-type"]
            assert "<Stream" in resp.text
            assert "media-stream" in resp.text

    def test_agent_not_found_returns_hangup(self, client):
        mock_db = MagicMock()
        qb = MagicMock()
        qb.select.return_value = qb; qb.eq.return_value = qb; qb.single.return_value = qb
        r = MagicMock(); r.data = None; qb.execute.return_value = r
        mock_db.table.return_value = qb

        with patch("api.crud.realtime_voice.get_service_client", return_value=mock_db):
            resp = client.post("/conversation/999/voice", data={"From": "+15551234567"})
            assert resp.status_code == 200
            assert "Agent not found" in resp.text
            assert "<Hangup" in resp.text

    def test_trial_exhausted_returns_hangup(self, client):
        agent_row = {
            "id": 1, "business_id": 10, "name": "A", "greeting": "Hi",
            "goodbye": "Bye", "voice_model": "ash",
        }
        mock_db = MagicMock()

        agent_qb = MagicMock()
        agent_qb.select.return_value = agent_qb; agent_qb.eq.return_value = agent_qb
        agent_qb.single.return_value = agent_qb
        agent_resp = MagicMock(); agent_resp.data = agent_row
        agent_qb.execute.return_value = agent_resp

        sub_qb = MagicMock()
        sub_qb.select.return_value = sub_qb; sub_qb.eq.return_value = sub_qb
        sub_qb.in_.return_value = sub_qb; sub_qb.limit.return_value = sub_qb
        sub_resp = MagicMock(); sub_resp.data = []  # no active sub
        sub_qb.execute.return_value = sub_resp

        conv_qb = MagicMock()
        conv_qb.select.return_value = conv_qb; conv_qb.eq.return_value = conv_qb
        conv_qb.not_ = MagicMock()
        conv_qb.not_.is_.return_value = conv_qb
        conv_resp = MagicMock()
        conv_resp.data = [
            {"started_at": "2025-01-01T00:00:00Z", "ended_at": "2025-01-01T00:06:00Z"}
        ]  # 6 min > 5 min trial
        conv_qb.execute.return_value = conv_resp

        def _table(name):
            if name == "agent": return agent_qb
            if name == "subscription": return sub_qb
            if name == "conversation": return conv_qb
            return MagicMock()

        mock_db.table = _table

        with patch("api.crud.realtime_voice.get_service_client", return_value=mock_db):
            resp = client.post("/conversation/1/voice", data={"From": "+15551234567"})
            assert "free trial has ended" in resp.text.lower() or "trial" in resp.text.lower()


# ---------------------------------------------------------------------------
# μ-law format handling (audio passthrough)
# ---------------------------------------------------------------------------

class TestAudioPassthrough:

    def test_passthrough_no_corruption(self):
        """Passthrough helpers should return input unchanged."""
        from api.audio_utils import twilio_to_openai_passthrough, openai_to_twilio_passthrough
        import base64

        raw = b'\x80\x7f\x00\xff' * 100
        b64 = base64.b64encode(raw).decode()

        assert twilio_to_openai_passthrough(b64) == b64
        assert openai_to_twilio_passthrough(b64) == b64
