#!/usr/bin/env python3
import httpx
import os

RESEND_API_KEY = os.getenv('RESEND_API_KEY', '')

if not RESEND_API_KEY:
    print('ERROR: RESEND_API_KEY not set')
    exit(1)

# Test 1: Contact form submission email
contact_html = """
<div style="font-family: sans-serif; max-width: 600px; margin: 0 auto;">
  <h2 style="color: #5D4E37;">New Support Request</h2>
  <div style="background: #FAF8F3; padding: 20px; border-radius: 8px; margin: 20px 0;">
    <p><strong>From:</strong> John Doe</p>
    <p><strong>Email:</strong> <a href="mailto:test@example.com">test@example.com</a></p>
  </div>
  <div style="background: #fff; padding: 20px; border: 1px solid #E8DCC8; border-radius: 8px;">
    <p><strong>Message:</strong></p>
    <p style="white-space: pre-wrap;">This is a test message from the contact form. I have a question about your service.</p>
  </div>
  <p style="color: #888; font-size: 12px; margin-top: 20px;">
    This message was sent from the HelloML support form.
  </p>
</div>
"""

# Test 2: Phone number warning email
warning_html = """
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
    Your phone number <strong style="color: #8B6F47;">+1 (555) 123-4567</strong> for
    <strong>Test Business</strong> hasn't received any calls in the past 11 days.
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

# Send to Resend account email since domain not verified
recipient = 'noahgallego394@gmail.com'

# Send contact form test
resp1 = httpx.post(
    'https://api.resend.com/emails',
    headers={
        'Authorization': f'Bearer {RESEND_API_KEY}',
        'Content-Type': 'application/json',
    },
    json={
        'from': 'HelloML <onboarding@resend.dev>',
        'to': [recipient],
        'subject': '[TEST] Support Request from John Doe',
        'html': contact_html,
    },
)
print(f'Contact form email: {resp1.status_code} - {resp1.text}')

# Send warning test
resp2 = httpx.post(
    'https://api.resend.com/emails',
    headers={
        'Authorization': f'Bearer {RESEND_API_KEY}',
        'Content-Type': 'application/json',
    },
    json={
        'from': 'HelloML <onboarding@resend.dev>',
        'to': [recipient],
        'subject': '[TEST] ⚠️ Your phone number +1 (555) 123-4567 will be released in 3 days',
        'html': warning_html,
    },
)
print(f'Warning email: {resp2.status_code} - {resp2.text}')
