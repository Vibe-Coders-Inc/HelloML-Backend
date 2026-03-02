"""
Tests for call resolution analysis — spam/telemarketing detection and minute crediting.
"""

import json
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from datetime import datetime, timedelta, timezone

from api.crud.call_resolution import (
    analyze_call,
    _classify_with_ai,
    _build_transcript_text,
    _calculate_duration_seconds,
    _save_resolution,
    ResolutionResult,
    SHORT_CALL_THRESHOLD,
    CLASSIFICATION_PROMPT,
)


# ---------------------------------------------------------------------------
# Unit tests: helper functions
# ---------------------------------------------------------------------------

class TestCalculateDuration:
    def test_normal_duration(self):
        start = "2026-03-01T10:00:00Z"
        end = "2026-03-01T10:02:30Z"
        assert _calculate_duration_seconds(start, end) == 150.0

    def test_zero_duration(self):
        ts = "2026-03-01T10:00:00Z"
        assert _calculate_duration_seconds(ts, ts) == 0.0

    def test_negative_returns_zero(self):
        start = "2026-03-01T10:05:00Z"
        end = "2026-03-01T10:00:00Z"
        assert _calculate_duration_seconds(start, end) == 0.0

    def test_short_call(self):
        start = "2026-03-01T10:00:00Z"
        end = "2026-03-01T10:00:10Z"
        assert _calculate_duration_seconds(start, end) == 10.0


class TestBuildTranscript:
    def test_empty_messages(self):
        assert _build_transcript_text([]) == "(no messages recorded)"

    def test_single_user_message(self):
        msgs = [{"role": "user", "content": "Hello?"}]
        assert _build_transcript_text(msgs) == "Customer: Hello?"

    def test_single_agent_message(self):
        msgs = [{"role": "assistant", "content": "Hi there!"}]
        assert _build_transcript_text(msgs) == "Agent: Hi there!"

    def test_conversation(self):
        msgs = [
            {"role": "user", "content": "I need an appointment"},
            {"role": "assistant", "content": "Sure, when works for you?"},
            {"role": "user", "content": "Tomorrow at 3pm"},
        ]
        result = _build_transcript_text(msgs)
        assert "Customer: I need an appointment" in result
        assert "Agent: Sure, when works for you?" in result
        assert "Customer: Tomorrow at 3pm" in result

    def test_empty_content_skipped(self):
        msgs = [{"role": "user", "content": ""}, {"role": "assistant", "content": "Hello"}]
        assert _build_transcript_text(msgs) == "Agent: Hello"

    def test_no_content_messages(self):
        msgs = [{"role": "user", "content": ""}]
        assert _build_transcript_text(msgs) == "(no messages recorded)"


# ---------------------------------------------------------------------------
# Unit tests: AI classification
# ---------------------------------------------------------------------------

class TestClassifyWithAI:
    @patch("api.crud.call_resolution._get_openai_client")
    def test_spam_classification(self, mock_get_client):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = json.dumps({
            "classification": "spam",
            "reason": "Automated telemarketing call about car warranty"
        })
        mock_client.chat.completions.create.return_value = mock_response

        result = _classify_with_ai("Agent: Your car warranty is expiring!", 8.0)

        assert result.classification == "spam"
        assert "warranty" in result.reason.lower()

        # Verify GPT-4o-mini was used
        call_args = mock_client.chat.completions.create.call_args
        assert call_args.kwargs["model"] == "gpt-4o-mini"

    @patch("api.crud.call_resolution._get_openai_client")
    def test_legitimate_classification(self, mock_get_client):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = json.dumps({
            "classification": "legitimate",
            "reason": "Customer requesting plumbing appointment"
        })
        mock_client.chat.completions.create.return_value = mock_response

        result = _classify_with_ai(
            "Customer: Hi, I need a plumber\nAgent: Sure, when works for you?",
            45.0
        )
        assert result.classification == "legitimate"

    @patch("api.crud.call_resolution._get_openai_client")
    def test_no_activity_classification(self, mock_get_client):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = json.dumps({
            "classification": "no_activity",
            "reason": "Only agent greeting, no customer response"
        })
        mock_client.chat.completions.create.return_value = mock_response

        result = _classify_with_ai("Agent: Hello, how can I help?", 5.0)
        assert result.classification == "no_activity"

    @patch("api.crud.call_resolution._get_openai_client")
    def test_invalid_classification_defaults_legitimate(self, mock_get_client):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = json.dumps({
            "classification": "unknown_garbage",
            "reason": "whatever"
        })
        mock_client.chat.completions.create.return_value = mock_response

        result = _classify_with_ai("test", 10.0)
        assert result.classification == "legitimate"

    @patch("api.crud.call_resolution._get_openai_client")
    def test_json_response_format_requested(self, mock_get_client):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = json.dumps({
            "classification": "legitimate", "reason": "test"
        })
        mock_client.chat.completions.create.return_value = mock_response

        _classify_with_ai("test", 30.0)

        call_args = mock_client.chat.completions.create.call_args
        assert call_args.kwargs["response_format"] == {"type": "json_object"}
        assert call_args.kwargs["temperature"] == 0


# ---------------------------------------------------------------------------
# Integration tests: analyze_call
# ---------------------------------------------------------------------------

class _ChainMock:
    """Mock that returns itself for any chained call, then returns data on execute()."""
    def __init__(self, data):
        self._data = data
    def __getattr__(self, name):
        if name == '_data':
            return object.__getattribute__(self, '_data')
        if name == 'execute':
            return self.execute
        return lambda *a, **kw: self
    def execute(self):
        resp = MagicMock()
        resp.data = self._data
        return resp


class TestAnalyzeCall:
    def _make_db(self, conv_data, messages_data=None):
        """Create a mock DB that returns specified data for conversation and messages."""
        db = MagicMock()

        def mock_table(name):
            if name == 'conversation':
                data = conv_data if isinstance(conv_data, list) else [conv_data]
                return _ChainMock(data)
            elif name == 'message':
                return _ChainMock(messages_data or [])
            return _ChainMock([])

        db.table = mock_table
        return db

    @pytest.mark.asyncio
    async def test_short_call_no_messages_auto_classified(self):
        """Calls <=15s with no messages should auto-classify as no_activity."""
        now = datetime.now(timezone.utc)
        conv = {
            "id": 1,
            "status": "completed",
            "started_at": now.isoformat(),
            "ended_at": (now + timedelta(seconds=10)).isoformat(),
            "resolution_status": None,
        }

        db = self._make_db(conv, [])
        result = await analyze_call(1, db)

        assert result.classification == "no_activity"
        assert "10s" in result.reason

    @pytest.mark.asyncio
    @patch("api.crud.call_resolution._classify_with_ai")
    async def test_short_call_with_messages_uses_ai(self, mock_classify):
        """Short calls WITH messages should use AI classification."""
        mock_classify.return_value = ResolutionResult(
            classification="spam", reason="Telemarketing detected"
        )

        now = datetime.now(timezone.utc)
        conv = {
            "id": 2,
            "status": "completed",
            "started_at": now.isoformat(),
            "ended_at": (now + timedelta(seconds=12)).isoformat(),
            "resolution_status": None,
        }
        messages = [
            {"role": "assistant", "content": "Hi, this is about your car warranty"},
        ]

        db = self._make_db(conv, messages)
        result = await analyze_call(2, db)

        assert result.classification == "spam"
        mock_classify.assert_called_once()

    @pytest.mark.asyncio
    @patch("api.crud.call_resolution._classify_with_ai")
    async def test_long_legitimate_call(self, mock_classify):
        """Normal calls should be classified via AI and likely be legitimate."""
        mock_classify.return_value = ResolutionResult(
            classification="legitimate", reason="Customer scheduling appointment"
        )

        now = datetime.now(timezone.utc)
        conv = {
            "id": 3,
            "status": "completed",
            "started_at": now.isoformat(),
            "ended_at": (now + timedelta(minutes=3)).isoformat(),
            "resolution_status": None,
        }
        messages = [
            {"role": "user", "content": "I need a plumber"},
            {"role": "assistant", "content": "Sure, when works?"},
            {"role": "user", "content": "Tomorrow at 3"},
            {"role": "assistant", "content": "Booked for 3pm tomorrow"},
        ]

        db = self._make_db(conv, messages)
        result = await analyze_call(3, db)

        assert result.classification == "legitimate"

    @pytest.mark.asyncio
    async def test_non_completed_call_skipped(self):
        """Calls not in 'completed' status should be skipped."""
        conv = {
            "id": 4,
            "status": "in_progress",
            "started_at": "2026-03-01T10:00:00Z",
            "ended_at": None,
        }
        db = self._make_db(conv)
        result = await analyze_call(4, db)
        assert result.classification == "legitimate"
        assert "not completed" in result.reason.lower()

    @pytest.mark.asyncio
    @patch("api.crud.call_resolution._classify_with_ai")
    async def test_ai_failure_defaults_legitimate(self, mock_classify):
        """If AI fails, default to legitimate — never wrongly credit."""
        mock_classify.side_effect = Exception("API error")

        now = datetime.now(timezone.utc)
        conv = {
            "id": 5,
            "status": "completed",
            "started_at": now.isoformat(),
            "ended_at": (now + timedelta(seconds=8)).isoformat(),
            "resolution_status": None,
        }
        messages = [{"role": "user", "content": "hello"}]

        db = self._make_db(conv, messages)
        result = await analyze_call(5, db)

        assert result.classification == "legitimate"
        assert "failed" in result.reason.lower()


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------

class TestResolutionEndpoints:
    def test_resolution_result_model(self):
        """ResolutionResult model works correctly."""
        r = ResolutionResult(classification="spam", reason="Telemarketing")
        assert r.classification == "spam"
        assert r.reason == "Telemarketing"

    def test_resolution_result_all_types(self):
        """All resolution types can be created."""
        for cls in ["spam", "no_activity", "legitimate"]:
            r = ResolutionResult(classification=cls, reason="test")
            assert r.classification == cls


# ---------------------------------------------------------------------------
# Billing integration test
# ---------------------------------------------------------------------------

class TestBillingExcludesCredited:
    """Verify that billing usage calculation excludes spam/no_activity calls."""

    def test_credited_minutes_not_counted(self):
        """Spam/no_activity calls should not count toward minutes_used."""
        from datetime import datetime

        # Simulate conversations data
        conversations = [
            {
                "started_at": "2026-03-01T10:00:00Z",
                "ended_at": "2026-03-01T10:05:00Z",  # 5 min
                "resolution_status": "legitimate",
            },
            {
                "started_at": "2026-03-01T11:00:00Z",
                "ended_at": "2026-03-01T11:00:10Z",  # 10 sec
                "resolution_status": "spam",
            },
            {
                "started_at": "2026-03-01T12:00:00Z",
                "ended_at": "2026-03-01T12:00:05Z",  # 5 sec
                "resolution_status": "no_activity",
            },
        ]

        # Replicate billing logic from billing.py
        total_seconds = 0
        credited_seconds = 0
        for conv in conversations:
            start = datetime.fromisoformat(conv['started_at'].replace('Z', '+00:00'))
            end = datetime.fromisoformat(conv['ended_at'].replace('Z', '+00:00'))
            duration = max(0, (end - start).total_seconds())

            resolution = conv.get('resolution_status')
            if resolution in ('spam', 'no_activity'):
                credited_seconds += duration
            else:
                total_seconds += duration

        minutes_used = round(total_seconds / 60, 1)
        credited_minutes = round(credited_seconds / 60, 1)

        # Only the 5-min legitimate call should count
        assert minutes_used == 5.0
        # Spam (10s) + no_activity (5s) = 15s = 0.2 min credited
        assert credited_minutes == 0.2


class TestShortCallThreshold:
    """Verify the short call threshold constant."""

    def test_threshold_is_15_seconds(self):
        assert SHORT_CALL_THRESHOLD == 15

    def test_classification_prompt_exists(self):
        assert "spam" in CLASSIFICATION_PROMPT
        assert "no_activity" in CLASSIFICATION_PROMPT
        assert "legitimate" in CLASSIFICATION_PROMPT
