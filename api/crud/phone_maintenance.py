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
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
    <body style="margin: 0; padding: 0; background-color: #FAF6F0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color: #FAF6F0;">
        <tr><td align="center" style="padding: 40px 20px;">
          <table role="presentation" width="560" cellpadding="0" cellspacing="0" style="max-width: 560px; width: 100%; background-color: #FFFFFF; border-radius: 12px; overflow: hidden; box-shadow: 0 1px 3px rgba(93, 78, 55, 0.08);">
            <tr><td style="padding: 32px 40px 24px; text-align: center; border-bottom: 1px solid #F5F0E8;">
              <span style="font-size: 24px; font-weight: 700; color: #5D4E37; letter-spacing: -0.5px;">HelloML</span>
            </td></tr>
            <tr><td style="padding: 32px 40px;">
              <h1 style="margin: 0 0 16px; font-size: 20px; font-weight: 600; color: #3D2E1F;">Phone Number Inactivity Notice</h1>
              <p style="margin: 0 0 16px; font-size: 15px; line-height: 1.6; color: #5D4E37;">
                Your phone number <strong style="color: #8B6F47;">{phone_number}</strong> for
                <strong>{business_name}</strong> has not received any calls in the past 11 days.
              </p>
              <p style="margin: 0 0 16px; font-size: 15px; line-height: 1.6; color: #5D4E37;">
                To help manage resources, we automatically release phone numbers that do not receive calls
                for 14 days. <strong>Your number will be released in 3 days</strong> unless it receives a call.
              </p>
              <div style="background: #F5F0E8; border-radius: 8px; padding: 20px; margin: 24px 0;">
                <p style="color: #5D4E37; margin: 0 0 10px; font-weight: 600; font-size: 15px;">To keep your number:</p>
                <ul style="color: #5D4E37; margin: 0; padding-left: 20px; font-size: 15px; line-height: 1.6;">
                  <li>Make a test call to your agent</li>
                  <li>Or ensure the number receives at least one call</li>
                </ul>
              </div>
              <p style="margin: 0 0 24px; font-size: 15px; line-height: 1.6; color: #5D4E37;">
                If your number is released, you can provision a new one from your dashboard (though it may be a different number).
              </p>
              <table role="presentation" cellpadding="0" cellspacing="0" style="margin: 0 auto;">
                <tr><td style="border-radius: 8px; background-color: #8B6F47;">
                  <a href="https://helloml.app/dashboard" target="_blank" style="display: inline-block; padding: 12px 28px; font-size: 15px; font-weight: 600; color: #FFFFFF; text-decoration: none;">Go to Dashboard</a>
                </td></tr>
              </table>
            </td></tr>
            <tr><td style="padding: 24px 40px; text-align: center; border-top: 1px solid #F5F0E8;">
              <p style="margin: 0; font-size: 12px; color: #A67A5B;">HelloML. This is an automated message.</p>
            </td></tr>
          </table>
        </td></tr>
      </table>
    </body>
    </html>
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
                    "subject": f"Your phone number {phone_number} will be released in 3 days",
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
