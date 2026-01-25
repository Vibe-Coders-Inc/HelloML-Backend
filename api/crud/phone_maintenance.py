# api/crud/phone_maintenance.py

from fastapi import APIRouter, HTTPException, Header
from twilio.rest import Client
import os
import logging
import httpx
from datetime import datetime, timezone
from ..database import get_service_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal", tags=["Internal"])

# Internal API key for cron jobs - should be set in environment
INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY", "")

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
WARNING_DAYS = 11  # Send warning after 11 days of inactivity
RELEASE_DAYS = 14  # Release after 14 days of inactivity


async def send_warning_email(to_email: str, phone_number: str, business_name: str):
    """Send warning email via Resend API."""
    if not RESEND_API_KEY:
        logger.warning("RESEND_API_KEY not configured, skipping email")
        return False

    html_content = f"""
    <div style="font-family: sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
      <div style="text-align: center; margin-bottom: 30px;">
        <h1 style="color: #8B6F47; margin: 0;">HelloML</h1>
      </div>

      <div style="background: #FEF3C7; border: 1px solid #F59E0B; border-radius: 8px; padding: 20px; margin-bottom: 20px;">
        <h2 style="color: #92400E; margin: 0 0 10px 0; font-size: 18px;">⚠️ Phone Number Inactivity Warning</h2>
        <p style="color: #92400E; margin: 0;">Your phone number will be released in 3 days due to inactivity.</p>
      </div>

      <p style="color: #374151; line-height: 1.6;">
        Hi there,
      </p>

      <p style="color: #374151; line-height: 1.6;">
        Your phone number <strong style="color: #8B6F47;">{phone_number}</strong> for
        <strong>{business_name}</strong> hasn't received any calls in the past 11 days.
      </p>

      <p style="color: #374151; line-height: 1.6;">
        To help manage resources, we automatically release phone numbers that don't receive calls
        for 14 days. <strong>Your number will be released in 3 days</strong> unless it receives a call.
      </p>

      <div style="background: #F5F0E8; border-radius: 8px; padding: 20px; margin: 20px 0;">
        <p style="color: #5D4E37; margin: 0 0 10px 0; font-weight: 600;">To keep your number:</p>
        <ul style="color: #5D4E37; margin: 0; padding-left: 20px;">
          <li>Make a test call to your agent</li>
          <li>Or simply ensure the number receives at least one call</li>
        </ul>
      </div>

      <p style="color: #374151; line-height: 1.6;">
        If your number is released, you can always provision a new one from your dashboard
        (though it may be a different number).
      </p>

      <p style="color: #6B7280; font-size: 14px; margin-top: 30px;">
        — The HelloML Team
      </p>

      <div style="border-top: 1px solid #E5E7EB; margin-top: 30px; padding-top: 20px; text-align: center;">
        <p style="color: #9CA3AF; font-size: 12px; margin: 0;">
          This is an automated message from HelloML. If you have questions, reply to this email.
        </p>
      </div>
    </div>
    """

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {RESEND_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "from": "HelloML <support@helloml.app>",
                    "to": [to_email],
                    "subject": f"⚠️ Your phone number {phone_number} will be released in 3 days",
                    "html": html_content,
                },
            )

            if response.status_code == 200:
                logger.info(f"Warning email sent to {to_email} for {phone_number}")
                return True
            else:
                logger.error(f"Failed to send email: {response.status_code} - {response.text}")
                return False
    except Exception as e:
        logger.error(f"Error sending warning email: {e}")
        return False


async def release_twilio_number(phone_number: str) -> bool:
    """Release a phone number from Twilio."""
    try:
        client = Client(os.getenv("ACCOUNT_SID"), os.getenv("AUTH_TOKEN"))
        numbers = client.incoming_phone_numbers.list(phone_number=phone_number)

        if numbers:
            numbers[0].delete()
            logger.info(f"Released Twilio number: {phone_number}")
            return True
        else:
            logger.warning(f"Number not found in Twilio: {phone_number}")
            return True  # Consider it released if not found
    except Exception as e:
        logger.error(f"Failed to release Twilio number {phone_number}: {e}")
        return False


@router.post("/phone/check-inactive", summary="Check and handle inactive phone numbers")
async def check_inactive_phones(x_internal_key: str = Header(None, alias="X-Internal-Key")):
    """
    Internal endpoint called by cron job to:
    1. Send warning emails for phones inactive 11+ days
    2. Release phones inactive 14+ days

    Requires X-Internal-Key header for authentication.
    """
    # Verify internal API key
    if not INTERNAL_API_KEY:
        raise HTTPException(status_code=500, detail="Internal API key not configured")

    if x_internal_key != INTERNAL_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid internal API key")

    db = get_service_client()
    now = datetime.now(timezone.utc)

    warnings_sent = 0
    phones_released = 0
    errors = []

    try:
        # Get all active phones with their owner info
        # Join through agent -> business to get owner_user_id, then get email from auth.users
        phones_query = db.table('phone_number').select(
            '*, agent:agent_id(id, business:business_id(id, name, owner_user_id))'
        ).eq('status', 'active').execute()

        if not phones_query.data:
            return {
                "success": True,
                "message": "No active phones to check",
                "warnings_sent": 0,
                "phones_released": 0
            }

        for phone in phones_query.data:
            if not phone.get('last_call_at'):
                continue

            last_call = datetime.fromisoformat(phone['last_call_at'].replace('Z', '+00:00'))
            days_inactive = (now - last_call).days

            agent = phone.get('agent')
            if not agent:
                continue

            business = agent.get('business')
            if not business:
                continue

            owner_user_id = business.get('owner_user_id')
            business_name = business.get('name', 'Your Business')

            # Get user email from auth.users
            user_query = db.auth.admin.get_user_by_id(owner_user_id)
            user_email = user_query.user.email if user_query and user_query.user else None

            if not user_email:
                logger.warning(f"Could not find email for user {owner_user_id}")
                continue

            # Check if needs to be released (14+ days)
            if days_inactive >= RELEASE_DAYS:
                logger.info(f"Releasing phone {phone['phone_number']} - {days_inactive} days inactive")

                # Release from Twilio
                released = await release_twilio_number(phone['phone_number'])

                if released:
                    # Update status in database
                    db.table('phone_number').update({
                        'status': 'released',
                    }).eq('id', phone['id']).execute()
                    phones_released += 1

                    # TODO: Could send a "your number was released" email here
                else:
                    errors.append(f"Failed to release {phone['phone_number']}")

            # Check if needs warning (11+ days and no warning sent yet)
            elif days_inactive >= WARNING_DAYS and not phone.get('warning_sent_at'):
                logger.info(f"Sending warning for phone {phone['phone_number']} - {days_inactive} days inactive")

                email_sent = await send_warning_email(
                    to_email=user_email,
                    phone_number=phone['phone_number'],
                    business_name=business_name
                )

                if email_sent:
                    # Mark warning as sent
                    db.table('phone_number').update({
                        'warning_sent_at': now.isoformat()
                    }).eq('id', phone['id']).execute()
                    warnings_sent += 1
                else:
                    errors.append(f"Failed to send warning for {phone['phone_number']}")

        return {
            "success": True,
            "warnings_sent": warnings_sent,
            "phones_released": phones_released,
            "errors": errors if errors else None
        }

    except Exception as e:
        logger.error(f"Error in check_inactive_phones: {e}")
        raise HTTPException(status_code=500, detail=str(e))
