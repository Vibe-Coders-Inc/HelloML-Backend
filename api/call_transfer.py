"""Call transfer/forwarding functionality for live SIP calls."""
import os
from twilio.rest import Client
from datetime import datetime
import pytz

TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID") or os.getenv("ACCOUNT_SID")
TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN") or os.getenv("AUTH_TOKEN")


def should_transfer(agent_data: dict, business_data: dict) -> tuple[bool, str]:
    """Check if call transfer is allowed right now.
    Returns (allowed, reason)."""
    if not agent_data.get('forwarding_enabled'):
        return False, "Call forwarding is not enabled"
    if not agent_data.get('forwarding_number'):
        return False, "No forwarding number configured"
    if not agent_data.get('forwarding_verified'):
        return False, "Forwarding number has not been verified"

    # Check business hours
    tz_name = business_data.get('business_timezone', 'America/Los_Angeles')
    hours_start = business_data.get('business_hours_start', '09:00')
    hours_end = business_data.get('business_hours_end', '17:00')

    try:
        tz = pytz.timezone(tz_name)
        now = datetime.now(tz)
        start_h, start_m = map(int, hours_start.split(':'))
        end_h, end_m = map(int, hours_end.split(':'))

        current_minutes = now.hour * 60 + now.minute
        start_minutes = start_h * 60 + start_m
        end_minutes = end_h * 60 + end_m

        if current_minutes < start_minutes or current_minutes >= end_minutes:
            return False, "Outside business hours"

        # Check day of week (Mon-Fri only by default)
        if now.weekday() >= 5:  # Saturday or Sunday
            return False, "Outside business days"
    except Exception as e:
        print(f"[Transfer] Error checking hours: {e}")
        return False, f"Error checking hours: {e}"

    return True, "Transfer allowed"


def transfer_call(call_sid: str, forwarding_number: str) -> dict:
    """Transfer an active Twilio call to the forwarding number."""
    try:
        client = Client(TWILIO_SID, TWILIO_TOKEN)

        call = client.calls(call_sid).update(
            twiml=f'<Response><Dial>{forwarding_number}</Dial></Response>'
        )

        return {"success": True, "status": call.status}
    except Exception as e:
        print(f"[Transfer] Error transferring call: {e}")
        return {"success": False, "error": str(e)}
