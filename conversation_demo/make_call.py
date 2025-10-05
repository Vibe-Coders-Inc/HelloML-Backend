

import os
from dotenv import load_dotenv
from twilio.rest import Client

load_dotenv()

account_sid = os.getenv("ACCOUNT_SID")
auth_token = os.getenv("AUTH_TOKEN")
client = Client(account_sid, auth_token)

# Make an outbound call
call = client.calls.create(
    from_=os.getenv("PHONE_NUMBER"),
    to="+16618855171",  # My number
    url="http://demo.twilio.com/docs/voice.xml"  # Demo TwiML for testing
)

print(f"Call SID: {call.sid}")
print(f"Call Status: {call.status}")