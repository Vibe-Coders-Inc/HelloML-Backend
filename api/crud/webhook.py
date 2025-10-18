# api/crud/webhook.py

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from twilio.twiml.voice_response import VoiceResponse
from ..database import supabase
from voice_agent import VoiceAgent

router = APIRouter(prefix="/conversation", tags=["Webhook"])

def map_agent_config(db_config):
    """Map database agent config to VoiceAgent format"""
    return {
        'model': db_config.get('model_type', 'gpt-5-nano'),
        'temperature': float(db_config.get('temperature', 0.7)),
        'voice': f"Polly.{db_config.get('voice_model', 'Joanna')}",
        'language': 'en-US',
        'system_prompt': db_config.get('prompt', 'You are a helpful AI assistant.'),
        'greeting': db_config.get('greeting', 'Hello! How can I help you?'),
        'goodbye': db_config.get('goodbye', 'Goodbye!')
    }

@router.post('/{agent_id}/voice', summary="Handle incoming call")
async def handle_incoming_call(agent_id: int, request: Request):
    """Receives incoming call and starts conversation"""
    try:
        db = supabase()
        form_data = await request.form()
        
        caller_phone = form_data.get('From', 'unknown')
        
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
        agent = VoiceAgent(map_agent_config(config))
        
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

@router.post('/{agent_id}/process', summary="Process speech input")
async def process_speech(agent_id: int, request: Request):
    """Converts speech to text and generates AI response"""
    try:
        db = supabase()
        form_data = await request.form()
        
        conversation_id = request.query_params.get('conversation_id')
        user_speech = form_data.get('SpeechResult', '')
        
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
        
        # Get agent config and conversation history
        agent_data = db.table('agent').select('*').eq('id', agent_id).single().execute()
        agent_config = agent_data.data

        history = db.table('message')\
            .select('role, content')\
            .eq('conversation_id', conversation_id)\
            .order('created_at')\
            .limit(5)\
            .execute()
        
        # Generate AI response with agent config
        agent = VoiceAgent(map_agent_config(agent_config))
        
        # Format conversation history for VoiceAgent
        formatted_history = []
        for m in history.data:
            if m['role'] == 'user':
                formatted_history.append(f"User: {m['content']}")
            elif m['role'] == 'agent':
                formatted_history.append(f"Assistant: {m['content']}")
        
        ai_response = agent.generate_conversation_response(
            user_speech,
            formatted_history
        )
        
        # Save both messages (user + agent) after AI generation
        db.table('message').insert([
            {
                'conversation_id': conversation_id,
                'role': 'user',
                'content': user_speech
            },
            {
                'conversation_id': conversation_id,
                'role': 'agent',
                'content': ai_response
            }
        ]).execute()
        
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

