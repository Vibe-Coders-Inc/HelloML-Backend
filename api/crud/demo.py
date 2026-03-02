"""
Voice demo endpoint — generates ephemeral OpenAI Realtime API tokens
for the landing page live demo widget.
"""

import os
import time
import httpx
from collections import defaultdict
from typing import Optional
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/demo", tags=["demo"])

# ---------------------------------------------------------------------------
# Rate limiting (in-memory, IP-based)
# ---------------------------------------------------------------------------
_rate_limit: dict[str, list[float]] = defaultdict(list)
_MAX_PER_HOUR = 5


def _check_rate_limit(ip: str) -> None:
    now = time.time()
    window = now - 3600  # 1 hour

    # Cleanup old entries for this IP
    _rate_limit[ip] = [t for t in _rate_limit[ip] if t > window]

    if len(_rate_limit[ip]) >= _MAX_PER_HOUR:
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded — max 5 demo sessions per hour. Please try again later.",
        )

    _rate_limit[ip].append(now)


# Periodic cleanup of stale IPs (called lazily)
_last_cleanup = 0.0


def _maybe_cleanup() -> None:
    global _last_cleanup
    now = time.time()
    if now - _last_cleanup < 600:  # every 10 min at most
        return
    _last_cleanup = now
    window = now - 3600
    stale = [ip for ip, ts in _rate_limit.items() if not ts or ts[-1] < window]
    for ip in stale:
        del _rate_limit[ip]


# ---------------------------------------------------------------------------
# Demo system prompt
# ---------------------------------------------------------------------------
DEMO_INSTRUCTIONS = """\
You are an AI phone agent demo for HelloML. Be warm, concise, and natural — like a real phone conversation.

Rules:
- Respond in 1-2 SHORT sentences per turn. Never monologue.
- Sound professional but not salesy.
- Only share HelloML facts when asked.

HelloML facts (use only when relevant):
- AI phone agent: answers calls 24/7, books appointments, handles inquiries
- $29/mo per agent, 200 min included, $0.10/extra min. No credit card needed.
- 2-min setup. Google Calendar integration. Works for any business type.
- Built by engineers from Apple, LLNL, and Disney.

Greeting: "Hey! I'm a HelloML demo agent. Ask me anything or pretend you're calling a business — I'll show you how I handle it."

If someone roleplays a customer, play the business receptionist naturally. If asked to do unrelated tasks, redirect to the demo.\
"""

# Match the model used in RealtimeSession (realtime_manager.py default)
DEMO_MODEL = "gpt-realtime-1.5"
# Verified working with gpt-realtime-1.5 + client_secrets endpoint (tested 2026-02-26)
# fable, onyx, nova are TTS-only — they fail on the Realtime API
ALLOWED_VOICES = ["alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse", "marin"]
DEMO_VOICE = "ash"


class DemoSessionRequest(BaseModel):
    voice: Optional[str] = None


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------
@router.post("/session")
async def create_demo_session(request: Request, body: Optional[DemoSessionRequest] = None):
    """
    Create an ephemeral OpenAI Realtime API session for the landing-page
    voice demo. No authentication required.
    """
    # Resolve client IP (behind reverse proxy)
    client_ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    if not client_ip:
        client_ip = request.client.host if request.client else "unknown"

    _maybe_cleanup()
    _check_rate_limit(client_ip)

    # Resolve voice selection
    voice = DEMO_VOICE
    if body and body.voice:
        if body.voice not in ALLOWED_VOICES:
            raise HTTPException(status_code=400, detail=f"Invalid voice. Allowed: {', '.join(ALLOWED_VOICES)}")
        voice = body.voice

    openai_key = os.getenv("OPENAI_API_KEY")
    if not openai_key:
        raise HTTPException(status_code=500, detail="OpenAI API key not configured")

    # Request ephemeral token from OpenAI (new client_secrets endpoint)
    # Docs: https://developers.openai.com/api/docs/guides/realtime-webrtc
    session_config = {
        "session": {
            "type": "realtime",
            "model": DEMO_MODEL,
            "instructions": DEMO_INSTRUCTIONS,
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "transcription": {"model": "gpt-4o-mini-transcribe"},
                    "turn_detection": {"type": "semantic_vad"},
                },
                "output": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "voice": voice,
                },
            },
        }
    }

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            "https://api.openai.com/v1/realtime/client_secrets",
            headers={
                "Authorization": f"Bearer {openai_key}",
                "Content-Type": "application/json",
            },
            json=session_config,
        )

    if resp.status_code != 200:
        print(f"[Demo] OpenAI session error {resp.status_code}: {resp.text}")
        raise HTTPException(status_code=502, detail="Failed to create demo session")

    data = resp.json()

    return {
        "ephemeral_key": data.get("value", data.get("client_secret", {}).get("value")),
        "model": DEMO_MODEL,
        "voice": voice,
    }
