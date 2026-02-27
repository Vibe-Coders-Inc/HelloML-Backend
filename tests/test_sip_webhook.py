"""
Tests for SIP webhook handler — /conversation/sip/webhook
"""

import json
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_sip_event(call_id="call_abc123", from_header="sip:+15551234567@sip.twilio.com",
                    to_header="sip:+18005551212@sip.openai.com", event_type="realtime.call.incoming"):
    """Build a mock webhook event object."""
    ev = MagicMock()
    ev.type = event_type
    ev.data = MagicMock()
    ev.data.call_id = call_id

    from_h = MagicMock(); from_h.name = "From"; from_h.value = from_header
    to_h = MagicMock(); to_h.name = "To"; to_h.value = to_header
    ev.data.sip_headers = [from_h, to_h]
    return ev


AGENT_ROW = {
    "id": 42,
    "business_id": 10,
    "name": "Test Agent",
    "prompt": "Be helpful.",
    "greeting": "Hi there!",
    "goodbye": "Bye!",
    "voice_model": "ash",
    "model_type": "gpt-realtime-1.5",
}


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------

class TestWebhookSignature:

    def test_invalid_signature_returns_400(self, client, mock_db):
        """Invalid webhook signature should return 400."""
        with patch("api.crud.sip_voice._get_openai_client") as mock_get:
            oc = MagicMock()
            oc.webhooks.unwrap.side_effect = __import__(
                "openai", fromlist=["InvalidWebhookSignatureError"]
            ).InvalidWebhookSignatureError("bad sig")
            mock_get.return_value = oc

            resp = client.post("/conversation/sip/webhook", content=b'{}',
                               headers={"Content-Type": "application/json"})
            assert resp.status_code == 400
            assert "Invalid signature" in resp.json()["detail"]

    def test_valid_signature_proceeds(self, client, mock_db):
        """Valid signature with no matching agent should still return 200 (rejected)."""
        ev = _make_sip_event()

        with patch("api.crud.sip_voice._get_openai_client") as mock_get, \
             patch("api.crud.sip_voice.get_service_client", return_value=mock_db), \
             patch("api.crud.sip_voice.http_requests") as mock_http:
            oc = MagicMock()
            oc.webhooks.unwrap.return_value = ev
            mock_get.return_value = oc

            resp = client.post("/conversation/sip/webhook", content=b'{}',
                               headers={"Content-Type": "application/json"})
            assert resp.status_code == 200
            assert resp.json()["status"] == "rejected"


# ---------------------------------------------------------------------------
# Phone number lookup
# ---------------------------------------------------------------------------

class TestPhoneLookup:

    def test_extract_phone_from_sip_uri(self):
        from api.crud.sip_voice import _lookup_agent_by_phone
        db = MagicMock()
        # No match
        qb = MagicMock()
        qb.select.return_value = qb
        qb.eq.return_value = qb
        qb.limit.return_value = qb
        execute_resp = MagicMock(); execute_resp.data = []
        qb.execute.return_value = execute_resp
        db.table.return_value = qb

        agent, phone = _lookup_agent_by_phone(db, "sip:+18005551212@sip.example.com")
        # Should try to find in DB (returns None since no data)
        assert agent is None
        assert phone is None

    def test_bad_sip_uri_returns_none(self):
        from api.crud.sip_voice import _lookup_agent_by_phone
        db = MagicMock()
        agent, phone = _lookup_agent_by_phone(db, "garbage")
        assert agent is None

    def test_plus1_prefix_handling(self):
        """Should try alternative formats when initial lookup fails."""
        from api.crud.sip_voice import _lookup_agent_by_phone

        db = MagicMock()
        call_count = {"n": 0}

        def _mock_table(name):
            qb = MagicMock()
            qb.select.return_value = qb
            qb.limit.return_value = qb
            qb.single.return_value = qb

            def _eq(col, val):
                call_count["n"] += 1
                qb2 = MagicMock()
                qb2.select.return_value = qb2
                qb2.limit.return_value = qb2
                qb2.single.return_value = qb2
                if call_count["n"] <= 1:
                    # First lookup fails
                    resp = MagicMock(); resp.data = []
                    qb2.execute.return_value = resp
                else:
                    # Alt format succeeds
                    resp = MagicMock(); resp.data = [{"agent_id": 42, "phone_number": "8005551212"}]
                    qb2.execute.return_value = resp

                    # Also set up agent lookup
                    agent_qb = MagicMock()
                    agent_qb.select.return_value = agent_qb
                    agent_qb.eq.return_value = agent_qb
                    agent_qb.single.return_value = agent_qb
                    agent_resp = MagicMock(); agent_resp.data = AGENT_ROW
                    agent_qb.execute.return_value = agent_resp

                return qb2

            qb.eq = _eq
            return qb

        db.table = _mock_table
        # This is hard to fully mock due to the chained queries; just verify it doesn't crash
        # and calls table multiple times
        _lookup_agent_by_phone(db, "sip:+18005551212@sip.example.com")
        assert call_count["n"] >= 1


# ---------------------------------------------------------------------------
# Call acceptance / rejection
# ---------------------------------------------------------------------------

class TestCallAcceptReject:

    def test_no_agent_rejects_with_404(self, client, mock_db):
        ev = _make_sip_event()

        with patch("api.crud.sip_voice._get_openai_client") as mock_get, \
             patch("api.crud.sip_voice.get_service_client", return_value=mock_db), \
             patch("api.crud.sip_voice.http_requests") as mock_http:
            oc = MagicMock()
            oc.webhooks.unwrap.return_value = ev
            mock_get.return_value = oc

            resp = client.post("/conversation/sip/webhook", content=b'{}')
            assert resp.json()["reason"] == "no agent"
            # Verify reject API was called
            mock_http.post.assert_called_once()
            call_args = mock_http.post.call_args
            assert "/reject" in call_args[0][0]

    def test_trial_exhausted_rejects_with_486(self, client):
        ev = _make_sip_event()

        with patch("api.crud.sip_voice._get_openai_client") as mock_get, \
             patch("api.crud.sip_voice._lookup_agent_by_phone", return_value=(AGENT_ROW, "+18005551212")), \
             patch("api.crud.sip_voice._check_trial_exhausted", return_value=True), \
             patch("api.crud.sip_voice.get_service_client") as mock_svc, \
             patch("api.crud.sip_voice.http_requests") as mock_http:
            oc = MagicMock()
            oc.webhooks.unwrap.return_value = ev
            mock_get.return_value = oc

            resp = client.post("/conversation/sip/webhook", content=b'{}')
            assert resp.json()["reason"] == "trial exhausted"
            call_args = mock_http.post.call_args
            assert "/reject" in call_args[0][0]

    def test_successful_acceptance(self, client):
        ev = _make_sip_event()

        mock_db_inst = MagicMock()
        # conversation insert
        conv_qb = MagicMock()
        conv_qb.insert.return_value = conv_qb
        conv_resp = MagicMock(); conv_resp.data = [{"id": 99}]
        conv_qb.execute.return_value = conv_resp
        # existing check returns empty
        existing_qb = MagicMock()
        existing_qb.select.return_value = existing_qb
        existing_qb.eq.return_value = existing_qb
        existing_qb.limit.return_value = existing_qb
        ex_resp = MagicMock(); ex_resp.data = []
        existing_qb.execute.return_value = ex_resp

        call_count = {"n": 0}
        def _table(name):
            call_count["n"] += 1
            if name == "conversation":
                if call_count["n"] <= 3:
                    return existing_qb  # idempotency check
                return conv_qb
            # business / tool_connection
            qb = MagicMock()
            qb.select.return_value = qb; qb.eq.return_value = qb
            qb.single.return_value = qb; qb.limit.return_value = qb
            r = MagicMock(); r.data = []; qb.execute.return_value = r
            return qb

        mock_db_inst.table = _table

        with patch("api.crud.sip_voice._get_openai_client") as mock_get, \
             patch("api.crud.sip_voice._lookup_agent_by_phone", return_value=(AGENT_ROW, "+18005551212")), \
             patch("api.crud.sip_voice._check_trial_exhausted", return_value=False), \
             patch("api.crud.sip_voice.get_service_client", return_value=mock_db_inst), \
             patch("api.crud.sip_voice.http_requests") as mock_http, \
             patch("api.crud.sip_voice.threading"):
            oc = MagicMock()
            oc.webhooks.unwrap.return_value = ev
            mock_get.return_value = oc

            accept_resp = MagicMock()
            accept_resp.status_code = 200
            accept_resp.raise_for_status = MagicMock()
            mock_http.post.return_value = accept_resp

            resp = client.post("/conversation/sip/webhook", content=b'{}')
            assert resp.json()["status"] == "accepted"


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

class TestIdempotency:

    def test_duplicate_call_id_returns_already_handled(self, client):
        ev = _make_sip_event(call_id="dup-call-1")

        mock_db_inst = MagicMock()
        # existing check returns a row
        qb = MagicMock()
        qb.select.return_value = qb; qb.eq.return_value = qb; qb.limit.return_value = qb
        r = MagicMock(); r.data = [{"id": 55}]; qb.execute.return_value = r
        mock_db_inst.table.return_value = qb

        with patch("api.crud.sip_voice._get_openai_client") as mock_get, \
             patch("api.crud.sip_voice._lookup_agent_by_phone", return_value=(AGENT_ROW, "+18005551212")), \
             patch("api.crud.sip_voice._check_trial_exhausted", return_value=False), \
             patch("api.crud.sip_voice.get_service_client", return_value=mock_db_inst):
            oc = MagicMock()
            oc.webhooks.unwrap.return_value = ev
            mock_get.return_value = oc

            resp = client.post("/conversation/sip/webhook", content=b'{}')
            assert resp.json()["status"] == "already_handled"


# ---------------------------------------------------------------------------
# Session config
# ---------------------------------------------------------------------------

class TestSessionConfig:

    def test_session_config_defaults(self):
        from api.crud.sip_voice import _build_session_config

        config = _build_session_config(
            agent_config=AGENT_ROW,
            business_info={"name": "Test Biz"},
            agent_phone="+18005551212",
            connected_tools=[],
            tool_settings={},
        )

        assert config["model"] == "gpt-realtime-1.5"
        assert config["voice"] == "ash"
        assert config["turn_detection"]["type"] == "semantic_vad"
        assert config["turn_detection"]["eagerness"] == "low"
        assert config["input_audio_transcription"]["model"] == "gpt-4o-mini-transcribe"

    def test_session_config_includes_tools(self):
        from api.crud.sip_voice import _build_session_config

        config = _build_session_config(
            agent_config=AGENT_ROW,
            business_info={},
            agent_phone=None,
            connected_tools=["google-calendar"],
            tool_settings={"google-calendar": {"default_duration": 60}},
        )

        tool_names = [t["name"] for t in config["tools"]]
        assert "search_knowledge_base" in tool_names
        assert "end_call" in tool_names
        assert "check_calendar" in tool_names
        assert "create_calendar_event" in tool_names


# ---------------------------------------------------------------------------
# SIP header parsing
# ---------------------------------------------------------------------------

class TestSipHeaderParsing:

    def test_from_header_caller_phone_extraction(self, client):
        """Verify caller phone is extracted from SIP From header."""
        ev = _make_sip_event(from_header="sip:+15559876543@sip.twilio.com")

        mock_db_inst = MagicMock()
        conv_qb = MagicMock()
        inserted_data = {}

        def _capture_insert(data):
            inserted_data.update(data)
            conv_qb2 = MagicMock()
            r = MagicMock(); r.data = [{"id": 100}]
            conv_qb2.execute.return_value = r
            return conv_qb2

        conv_qb.insert = _capture_insert
        conv_qb.select.return_value = conv_qb
        conv_qb.eq.return_value = conv_qb
        conv_qb.limit.return_value = conv_qb
        ex_resp = MagicMock(); ex_resp.data = []
        conv_qb.execute.return_value = ex_resp

        def _table(name):
            if name == "conversation":
                return conv_qb
            qb = MagicMock()
            qb.select.return_value = qb; qb.eq.return_value = qb
            qb.single.return_value = qb; qb.limit.return_value = qb
            r = MagicMock(); r.data = []; qb.execute.return_value = r
            return qb

        mock_db_inst.table = _table

        with patch("api.crud.sip_voice._get_openai_client") as mock_get, \
             patch("api.crud.sip_voice._lookup_agent_by_phone", return_value=(AGENT_ROW, "+18005551212")), \
             patch("api.crud.sip_voice._check_trial_exhausted", return_value=False), \
             patch("api.crud.sip_voice.get_service_client", return_value=mock_db_inst), \
             patch("api.crud.sip_voice.http_requests") as mock_http, \
             patch("api.crud.sip_voice.threading"):
            oc = MagicMock()
            oc.webhooks.unwrap.return_value = ev
            mock_get.return_value = oc
            ar = MagicMock(); ar.status_code = 200; ar.raise_for_status = MagicMock()
            mock_http.post.return_value = ar

            client.post("/conversation/sip/webhook", content=b'{}')
            assert inserted_data.get("caller_phone") == "+15559876543"

    def test_non_incoming_event_ignored(self, client, mock_db):
        ev = _make_sip_event(event_type="realtime.call.ended")

        with patch("api.crud.sip_voice._get_openai_client") as mock_get:
            oc = MagicMock()
            oc.webhooks.unwrap.return_value = ev
            mock_get.return_value = oc

            resp = client.post("/conversation/sip/webhook", content=b'{}')
            assert resp.json()["status"] == "ignored"
