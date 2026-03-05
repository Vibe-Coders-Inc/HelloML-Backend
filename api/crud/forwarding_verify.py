# api/crud/forwarding_verify.py

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Literal
import os
import random
import time
from twilio.rest import Client
from ..database import get_service_client
from ..auth import get_current_user, AuthenticatedUser

router = APIRouter(tags=["Forwarding Verification"])

# In-memory store for verification codes
# Format: { "phone_number": { "code": "123456", "expires_at": timestamp, "attempts": int, "first_attempt_at": timestamp } }
_verification_codes: dict[str, dict] = {}


class SendCodeRequest(BaseModel):
    method: Literal["sms", "call"]


class VerifyCodeRequest(BaseModel):
    code: str


def _clean_phone(phone: str) -> str:
    """Strip formatting, return +1XXXXXXXXXX"""
    digits = ''.join(c for c in phone if c.isdigit())
    if len(digits) == 10:
        digits = '1' + digits
    if not digits.startswith('1'):
        digits = '1' + digits
    return '+' + digits


def _get_agent_for_business(db, business_id: int) -> dict:
    """Get the agent for a business, raise 404 if not found."""
    agent = db.table('agent').select('*').eq('business_id', business_id).execute()
    if not agent.data:
        raise HTTPException(status_code=404, detail="No agent found for this business")
    return agent.data[0]


@router.post("/{business_id}/forwarding/send-code", summary="Send forwarding verification code")
async def send_verification_code(
    business_id: int,
    request: SendCodeRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
):
    db = current_user.get_db()

    # Verify business ownership via RLS
    biz = db.table('business').select('id').eq('id', business_id).execute()
    if not biz.data:
        raise HTTPException(status_code=404, detail="Business not found")

    agent = _get_agent_for_business(db, business_id)
    phone = agent.get('forwarding_number')
    if not phone or not phone.strip():
        raise HTTPException(status_code=400, detail="No forwarding number set on agent")

    cleaned = _clean_phone(phone)
    now = time.time()

    # Rate limit: max 3 attempts per number per 10 minutes
    entry = _verification_codes.get(cleaned)
    if entry:
        window_start = entry.get('first_attempt_at', 0)
        if now - window_start < 600:  # within 10 min window
            if entry.get('attempts', 0) >= 3:
                raise HTTPException(status_code=429, detail="Too many attempts. Please wait 10 minutes.")
        else:
            # Reset window
            entry = None

    # Generate 6-digit code
    code = f"{random.randint(0, 999999):06d}"

    _verification_codes[cleaned] = {
        'code': code,
        'expires_at': now + 600,  # 10 minutes
        'attempts': (entry['attempts'] + 1) if entry else 1,
        'first_attempt_at': entry['first_attempt_at'] if entry else now,
    }

    # Send via Twilio
    twilio_client = Client(os.getenv("ACCOUNT_SID"), os.getenv("AUTH_TOKEN"))
    from_number = os.getenv("TWILIO_FROM_NUMBER", os.getenv("TWILIO_PHONE_NUMBER"))

    # If no dedicated from number, find one from Twilio account
    if not from_number:
        numbers = twilio_client.incoming_phone_numbers.list(limit=1)
        if numbers:
            from_number = numbers[0].phone_number
        else:
            raise HTTPException(status_code=500, detail="No Twilio phone number available to send from")

    try:
        if request.method == "sms":
            twilio_client.messages.create(
                body=f"Your HelloML verification code is: {code}",
                from_=from_number,
                to=cleaned,
            )
        else:
            # Voice call with TwiML
            twiml = f'<Response><Say voice="alice">Your HelloML verification code is: {" ".join(code)}. Again, your code is: {" ".join(code)}.</Say></Response>'
            twilio_client.calls.create(
                twiml=twiml,
                from_=from_number,
                to=cleaned,
            )
    except Exception as e:
        print(f"[Forwarding Verify] Twilio error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to send verification: {str(e)}")

    print(f"[Forwarding Verify] Sent {request.method} code to {cleaned} for business {business_id}")
    return {"success": True, "method": request.method}


@router.post("/{business_id}/forwarding/verify-code", summary="Verify forwarding code")
async def verify_code(
    business_id: int,
    request: VerifyCodeRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
):
    db = current_user.get_db()

    biz = db.table('business').select('id').eq('id', business_id).execute()
    if not biz.data:
        raise HTTPException(status_code=404, detail="Business not found")

    agent = _get_agent_for_business(db, business_id)
    phone = agent.get('forwarding_number')
    if not phone or not phone.strip():
        raise HTTPException(status_code=400, detail="No forwarding number set on agent")

    cleaned = _clean_phone(phone)
    now = time.time()

    entry = _verification_codes.get(cleaned)
    if not entry:
        raise HTTPException(status_code=400, detail="No verification code sent. Please request a new code.")

    if now > entry['expires_at']:
        del _verification_codes[cleaned]
        raise HTTPException(status_code=400, detail="Verification code expired. Please request a new code.")

    if entry['code'] != request.code.strip():
        raise HTTPException(status_code=400, detail="Invalid verification code.")

    # Code is correct - mark as verified
    del _verification_codes[cleaned]

    from datetime import datetime, timezone
    db.table('agent').update({
        'forwarding_verified': True,
        'updated_at': datetime.now(timezone.utc).isoformat(),
    }).eq('id', agent['id']).execute()

    print(f"[Forwarding Verify] Number {cleaned} verified for business {business_id}")
    return {"success": True, "verified": True}
