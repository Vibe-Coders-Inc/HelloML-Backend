from fastapi import FastAPI, HTTPException
from twilio.rest import Client
from pydantic import BaseModel
import os
from .database import supabase

app = FastAPI(title="Phone Number Provisioning API")

# Master Twilio Credentials
TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")

class ProvisionRequest(BaseModel):
    agent_id: str
    area_code: str

@app.get("/")
def index():
    """Health check endpoint"""
    return "Phone Number Provisioning API is running!"

@app.post("/provision-phone-number")
async def provision_phone_number(request: ProvisionRequest):
    """Provision a phone number with the Twilio account"""
    try:
        db = supabase()
        
        # Check if agent exists and doesn't already have a phone
        agent = db.table('agent').select('*').eq('id', request.agent_id).single().execute()
        if not agent.data:
            raise HTTPException(status_code=404, detail="Agent not found")
        
        existing_phone = db.table('phone_number').select('*').eq('agent_id', request.agent_id).execute()
        if existing_phone.data:
            raise HTTPException(status_code=400, detail="Agent already has a phone number")
        
        # Provision number with Master Twilio
        client = Client(TWILIO_SID, TWILIO_TOKEN)
        
        available = client.available_phone_numbers('US').local.list(
            area_code=request.area_code,
            limit=1
        )
        
        if not available:
            raise HTTPException(status_code=400, detail=f"No numbers available in area code {request.area_code}")
        
        webhook_url = f"https://helloml.app/conversation/{request.agent_id}/voice"
        
        number = client.incoming_phone_numbers.create(
            phone_number=available[0].phone_number,
            voice_url=webhook_url,
            voice_method='POST'
        )
        
        # Save to database
        phone_data = db.table('phone_numbers').insert({
            'agent_id': request.agent_id,
            'phone_number': number.phone_number,
            'area_code': request.area_code,
            'webhook_url': webhook_url,
            'status': 'active'
        }).execute()
        
        return {
            "success": True,
            "phone_number": number.phone_number,
            "agent_id": request.agent_id,
            "webhook_url": webhook_url
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)