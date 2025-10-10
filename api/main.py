from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse
from pydantic import BaseModel
import os
import sys
from .database import supabase

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

app = FastAPI(
    title="HelloML API",
    description="Provisions phone numbers for agents and handles voice calls",
    version="1.0.0"
)

TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")

class ProvisionRequest(BaseModel):
    agent_id: int
    area_code: str
    
    class Config:
        json_schema_extra = {
            "example": {
                "agent_id": 3,
                "area_code": "415"
            }
        }

@app.get("/", summary="Check API status")
def index():
    """Returns API status"""
    return "Phone Number Provisioning API is running!"

@app.post("/provision-phone-number", summary="Provision phone number")
async def provision_phone_number(request: ProvisionRequest):
    """Buys a phone number and assigns it to an agent"""
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

@app.post('/conversation/{agent_id}/voice', summary="Handle incoming call")
async def handle_incoming_call(agent_id: int, request: Request):
    """Receives incoming call and starts conversation"""
    try:
        db = supabase()
        form_data = await request.form()
        
        caller_phone = form_data.get('From', 'unknown')
        call_sid = form_data.get('CallSid')
        
        # Get agent config from database
        agent_data = db.table('agent').select('*').eq('id', agent_id).single().execute()
        if not agent_data.data:
            raise Exception("Agent not found")
        
        config = agent_data.data
        
        # Create conversation record
        conversation = db.table('conversation').insert({
            'agent_id': agent_id,
            'caller_phone': caller_phone,
            'status': 'in_progress'
        }).execute()
        
        conversation_id = conversation.data[0]['id']
        
        # Build response with agent config
        from voice_agent import VoiceAgent
        agent = VoiceAgent(config)
        
        response = VoiceResponse()
        greeting = agent.get_greeting()
        voice_config = agent.get_voice_config()
        
        response.say(greeting, voice=voice_config['voice'], language=voice_config['language'])
        
        # Gather speech with conversation_id in URL
        response.gather(
            input='speech',
            action=f'/conversation/{agent_id}/process?conversation_id={conversation_id}',
            method='POST',
            speech_timeout='auto',
            timeout=10
        )
        
        response.say("I didn't hear anything. Goodbye!", voice=voice_config['voice'])
        response.hangup()
        
        return Response(content=str(response), media_type="application/xml")
        
    except Exception as e:
        print(f"Error: {e}")
        response = VoiceResponse()
        response.say("Sorry, there was an error.", voice='Polly.Joanna')
        response.hangup()
        return Response(content=str(response), media_type="application/xml")

@app.post('/conversation/{agent_id}/process', summary="Process speech input")
async def process_speech(agent_id: int, request: Request):
    """Converts speech to text and generates AI response"""
    try:
        db = supabase()
        form_data = await request.form()
        
        conversation_id = request.query_params.get('conversation_id')
        user_speech = form_data.get('SpeechResult', '')
        confidence = form_data.get('Confidence', 0)
        
        if not user_speech:
            response = VoiceResponse()
            response.say("I didn't catch that. Could you repeat?", voice='Polly.Joanna')
            response.gather(
                input='speech',
                action=f'/conversation/{agent_id}/process?conversation_id={conversation_id}',
                method='POST',
                speech_timeout='auto',
                timeout=10
            )
            return Response(content=str(response), media_type="application/xml")
        
        # Save user message
        db.table('message').insert({
            'conversation_id': conversation_id,
            'role': 'user',
            'content': user_speech
        }).execute()
        
        # Get conversation history
        history = db.table('message')\
            .select('role, content')\
            .eq('conversation_id', conversation_id)\
            .order('created_at')\
            .limit(10)\
            .execute()
        
        # Get agent config for AI response
        agent_data = db.table('agents').select('*').eq('id', agent_id).single().execute()
        agent_config = agent_data.data
        
        # Generate AI response with agent config
        from voice_agent import VoiceAgent
        agent = VoiceAgent(agent_config)
        ai_response = agent.generate_conversation_response(
            user_speech,
            [f"{m['role']}: {m['content']}" for m in history.data]
        )
        
        # Save AI message
        db.table('message').insert({
            'conversation_id': conversation_id,
            'role': 'assistant',
            'content': ai_response
        }).execute()
        
        # Get voice config from agent
        voice_config = agent.get_voice_config()
        
        # Respond
        response = VoiceResponse()
        response.say(ai_response, voice=voice_config['voice'], language=voice_config['language'])
        response.gather(
            input='speech',
            action=f'/conversation/{agent_id}/process?conversation_id={conversation_id}',
            method='POST',
            speech_timeout='auto',
            timeout=10
        )
        
        return Response(content=str(response), media_type="application/xml")
        
    except Exception as e:
        print(f"Error: {e}")
        response = VoiceResponse()
        response.say("Sorry, error processing request.", voice='Polly.Joanna')
        response.hangup()
        return Response(content=str(response), media_type="application/xml")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

