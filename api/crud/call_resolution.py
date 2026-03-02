# api/crud/call_resolution.py

"""
Call Resolution Analysis — classifies calls as legitimate, spam, or no-activity
and credits minutes back for unfair charges.

Heuristics:
  - Calls <= 15 seconds: auto-flagged for AI review
  - GPT-4o-mini classifies transcript for telemarketing / spam / no-activity

Resolution statuses:
  - "legitimate" — normal call, counts toward minutes
  - "spam" — telemarketing / robocall / spam, credited back
  - "no_activity" — silence / immediate hangup, credited back
  - "pending" — awaiting analysis
"""

import os
import json
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from ..auth import get_current_user, AuthenticatedUser
from ..database import get_service_client

router = APIRouter(prefix="/resolution", tags=["Call Resolution"])

# Threshold: calls this short (seconds) get auto-flagged for AI review
SHORT_CALL_THRESHOLD = 15

CLASSIFICATION_PROMPT = """\
You are a call classification system. Analyze this phone call transcript and classify it.

Classify as ONE of:
- "spam": Telemarketing, robocalls, scam calls, automated messages, sales pitches the business didn't request
- "no_activity": No meaningful conversation happened — silence, immediate hangup, wrong number with no interaction, only a greeting with no response
- "legitimate": A real customer or person with a genuine inquiry, appointment request, question, or business interaction

Transcript:
{transcript}

Call duration: {duration_seconds} seconds

Respond with ONLY a JSON object:
{{"classification": "spam"|"no_activity"|"legitimate", "reason": "brief explanation"}}\
"""


class ResolutionResult(BaseModel):
    classification: str
    reason: str


def _get_openai_client():
    """Get a standard (non-realtime) OpenAI client for cheap classification."""
    from openai import OpenAI
    return OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


def _calculate_duration_seconds(started_at: str, ended_at: str) -> float:
    """Calculate call duration in seconds from ISO timestamps."""
    start = datetime.fromisoformat(started_at.replace('Z', '+00:00'))
    end = datetime.fromisoformat(ended_at.replace('Z', '+00:00'))
    return max(0, (end - start).total_seconds())


def _build_transcript_text(messages: list) -> str:
    """Build a readable transcript from message records."""
    if not messages:
        return "(no messages recorded)"

    lines = []
    for msg in messages:
        role = msg.get('role', 'unknown')
        content = msg.get('content', '')
        if content:
            speaker = "Customer" if role == 'user' else "Agent"
            lines.append(f"{speaker}: {content}")

    return "\n".join(lines) if lines else "(no messages recorded)"


async def analyze_call(conversation_id: int, db=None) -> ResolutionResult:
    """
    Analyze a completed call and classify it.
    Called automatically after call ends, or manually via API.
    """
    if db is None:
        db = get_service_client()

    # Get conversation
    conv_result = db.table('conversation').select('*').eq('id', conversation_id).execute()
    if not conv_result.data:
        raise ValueError(f"Conversation {conversation_id} not found")

    conv = conv_result.data[0]

    # Only analyze completed conversations
    if conv.get('status') != 'completed':
        return ResolutionResult(classification="legitimate", reason="Call not completed, skipping analysis")

    # Calculate duration
    started_at = conv.get('started_at')
    ended_at = conv.get('ended_at')

    if not started_at or not ended_at:
        return ResolutionResult(classification="legitimate", reason="Missing timestamps")

    duration = _calculate_duration_seconds(started_at, ended_at)

    # Get messages/transcript
    messages_result = db.table('message').select('role, content').eq(
        'conversation_id', conversation_id
    ).order('created_at').execute()

    transcript = _build_transcript_text(messages_result.data)

    # Heuristic: very short calls with no/minimal transcript
    if duration <= SHORT_CALL_THRESHOLD and transcript == "(no messages recorded)":
        result = ResolutionResult(
            classification="no_activity",
            reason=f"Call lasted {duration:.0f}s with no messages recorded"
        )
        _save_resolution(db, conversation_id, result)
        return result

    # For short calls OR any call with transcript, use AI classification
    if duration <= SHORT_CALL_THRESHOLD or len(messages_result.data) <= 2:
        try:
            result = _classify_with_ai(transcript, duration)
            _save_resolution(db, conversation_id, result)
            return result
        except Exception as e:
            print(f"[Resolution] AI classification failed for {conversation_id}: {e}", flush=True)
            # Default to legitimate if AI fails — don't wrongly credit
            result = ResolutionResult(classification="legitimate", reason=f"AI classification failed: {str(e)}")
            _save_resolution(db, conversation_id, result)
            return result

    # Longer calls with substantial transcript — still classify but likely legitimate
    try:
        result = _classify_with_ai(transcript, duration)
        _save_resolution(db, conversation_id, result)
        return result
    except Exception as e:
        print(f"[Resolution] AI classification failed for {conversation_id}: {e}", flush=True)
        result = ResolutionResult(classification="legitimate", reason=f"AI classification failed: {str(e)}")
        _save_resolution(db, conversation_id, result)
        return result


def _classify_with_ai(transcript: str, duration_seconds: float) -> ResolutionResult:
    """Use GPT-4o-mini to classify the call transcript."""
    client = _get_openai_client()

    prompt = CLASSIFICATION_PROMPT.format(
        transcript=transcript[:3000],  # Limit to 3k chars to keep costs low
        duration_seconds=f"{duration_seconds:.0f}"
    )

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        max_tokens=150,
        response_format={"type": "json_object"}
    )

    content = response.choices[0].message.content
    parsed = json.loads(content)

    classification = parsed.get("classification", "legitimate")
    reason = parsed.get("reason", "No reason provided")

    # Validate classification
    valid_classifications = {"spam", "no_activity", "legitimate"}
    if classification not in valid_classifications:
        classification = "legitimate"

    return ResolutionResult(classification=classification, reason=reason)


def _save_resolution(db, conversation_id: int, result: ResolutionResult):
    """Save the resolution result to the conversation record."""
    try:
        db.table('conversation').update({
            'resolution_status': result.classification,
            'resolution_reason': result.reason,
        }).eq('id', conversation_id).execute()
        print(f"[Resolution] Conversation {conversation_id}: {result.classification} — {result.reason}", flush=True)
    except Exception as e:
        print(f"[Resolution] Failed to save resolution for {conversation_id}: {e}", flush=True)


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------

@router.get("/{conversation_id}", summary="Get call resolution status")
async def get_resolution(
    conversation_id: int,
    current_user: AuthenticatedUser = Depends(get_current_user)
):
    """Get the resolution status of a specific conversation."""
    try:
        db = current_user.get_db()
        conv = db.table('conversation').select(
            'id, resolution_status, resolution_reason, started_at, ended_at, status'
        ).eq('id', conversation_id).single().execute()

        if not conv.data:
            raise HTTPException(status_code=404, detail="Conversation not found")

        data = conv.data
        duration = None
        if data.get('started_at') and data.get('ended_at'):
            duration = _calculate_duration_seconds(data['started_at'], data['ended_at'])

        return {
            "conversation_id": conversation_id,
            "resolution_status": data.get('resolution_status', 'pending'),
            "resolution_reason": data.get('resolution_reason'),
            "duration_seconds": duration,
            "call_status": data.get('status'),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{conversation_id}/analyze", summary="Manually trigger resolution analysis")
async def trigger_analysis(
    conversation_id: int,
    current_user: AuthenticatedUser = Depends(get_current_user)
):
    """Manually trigger resolution analysis for a conversation."""
    try:
        db = current_user.get_db()
        result = await analyze_call(conversation_id, db)
        return {
            "conversation_id": conversation_id,
            "classification": result.classification,
            "reason": result.reason,
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/business/{business_id}/summary", summary="Get resolution summary for business")
async def get_resolution_summary(
    business_id: int,
    current_user: AuthenticatedUser = Depends(get_current_user)
):
    """Get a summary of call resolutions for the current billing period."""
    try:
        db = current_user.get_db()

        # Get agent
        agent_result = db.table('agent').select('id').eq('business_id', business_id).execute()
        if not agent_result.data:
            return {"total_calls": 0, "legitimate": 0, "spam": 0, "no_activity": 0, "credited_minutes": 0}

        agent_id = agent_result.data[0]['id']

        # Get all completed conversations
        convs = db.table('conversation').select(
            'resolution_status, started_at, ended_at'
        ).eq('agent_id', agent_id).eq('status', 'completed').execute()

        summary = {"total_calls": 0, "legitimate": 0, "spam": 0, "no_activity": 0, "pending": 0, "credited_minutes": 0.0}

        for conv in convs.data:
            summary["total_calls"] += 1
            status = conv.get('resolution_status', 'pending')

            if status in summary:
                summary[status] += 1

            # Calculate credited minutes
            if status in ('spam', 'no_activity'):
                if conv.get('started_at') and conv.get('ended_at'):
                    duration_min = _calculate_duration_seconds(conv['started_at'], conv['ended_at']) / 60
                    summary["credited_minutes"] += duration_min

        summary["credited_minutes"] = round(summary["credited_minutes"], 1)
        return summary

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
