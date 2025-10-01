from fastapi import FastAPI, HTTPException
from twilio.rest import Client
from pydantic import BaseModel
import os

app = FastAPI(title="Phone Number Provisioning API")

# Pydantic models for requests
class PhoneNumberRequest(BaseModel):
    twilio_account_sid: str
    twilio_auth_token: str
    area_code: str = "415"
    webhook_url: str

@app.get("/")
def index():
    """Health check endpoint"""
    return "Phone Number Provisioning API is running!"

@app.post("/provision-phone-number")
async def provision_phone_number(request_data: PhoneNumberRequest):
    """Provision a new phone number"""
    try:
        # Initialize Twilio client
        client = Client(request_data.twilio_account_sid, request_data.twilio_auth_token)
        
        # Search for available numbers
        available = client.available_phone_numbers('US').local.list(
            area_code=request_data.area_code,
            limit=1
        )
        
        if not available:
            raise HTTPException(status_code=400, detail=f"No numbers available in area code {request_data.area_code}")
        
        # Purchase the number with webhook
        number = client.incoming_phone_numbers.create(
            phone_number=available[0].phone_number,
            voice_url=request_data.webhook_url,
            voice_method='POST'
        )
        
        return {
            "phone_number": number.phone_number,
            "webhook_url": request_data.webhook_url,
            "status": "active",
            "message": f"Phone number {number.phone_number} provisioned successfully"
        }
        
    except Exception as e:
        if "Twilio" in str(e):
            raise HTTPException(status_code=400, detail=f"Twilio error: {str(e)}")
        else:
            raise HTTPException(status_code=500, detail=f"Error provisioning number: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)