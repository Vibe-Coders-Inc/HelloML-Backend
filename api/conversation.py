from fastapi import FastAPI, Request
from fastapi.responses import Response
from twilio.twiml.voice_response import VoiceResponse
import os
import sys
from database import supabase

# Add parent directory to path to import agent
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

app = FastAPI(title="AI Conversation API")

@app.get("/")
def index():
    """Health check endpoint"""
    return "AI Conversation API is running!"

@app.post('/conversation/{agent_id}/voice')
async def handle_incoming_call(agent_id: str, request: Request):
    """Handle incoming voice calls"""
    try:
        db = supabase()
        form_data = await request.form()
        
        caller_phone = form_data.get('From', 'unknown')
        call_sid = form_data.get('CallSid')
        
        # Get agent config from database
        agent_data = db.table('agents').select('*').eq('id', agent_id).single().execute()
        if not agent_data.data:
            raise Exception("Agent not found")
        
        config = agent_data.data
        
        # Create conversation record
        conversation = db.table('conversations').insert({
            'agent_id': agent_id,
            'caller_phone': caller_phone,
            'twilio_call_sid': call_sid,
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

@app.post('/conversation/{agent_id}/process')
async def process_speech(agent_id: str, request: Request):
    """Process speech and generate AI response with RAG"""
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
        db.table('messages').insert({
            'conversation_id': conversation_id,
            'role': 'user',
            'content': user_speech,
            'speech_confidence': float(confidence)
        }).execute()
        
        # Get conversation history
        history = db.table('messages')\
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
        db.table('messages').insert({
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
    uvicorn.run(app, host="0.0.0.0", port=5000)