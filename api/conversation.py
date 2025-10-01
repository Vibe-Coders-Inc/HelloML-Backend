from fastapi import FastAPI, Request
from fastapi.responses import Response
from twilio.twiml.voice_response import VoiceResponse
import os
import sys

# Add parent directory to path to import agent
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

app = FastAPI(title="AI Conversation API")

# Store conversation history
conversation_history = {}

@app.get("/")
def index():
    """Health check endpoint"""
    return "AI Conversation API is running!"

@app.post('/conversation/{business_id}/voice')
async def handle_incoming_call(business_id: int, request: Request):
    """Handle incoming voice calls for AI conversation"""
    try:
        # Lazy import to avoid initialization issues
        from agent import Agent
        agent = Agent()
        
        response = VoiceResponse()
        
        # Get form data from request
        form_data = await request.form()
        
        # Get caller's phone number for conversation tracking
        caller_number = form_data.get('From', 'unknown')
        
        # Initialize conversation history for this caller
        if caller_number not in conversation_history:
            conversation_history[caller_number] = []
        
        # Greet the caller using Amazon Polly
        greeting = agent.get_greeting()
        response.say(
            greeting, 
            voice='Polly.Joanna',
            language='en-US'
        )
        
        # Gather speech input
        gather = response.gather(
            input='speech',
            action=f'/conversation/{business_id}/process',
            method='POST',
            speech_timeout='auto',
            timeout=10
        )
        
        # If no speech detected, say goodbye
        response.say(
            "I didn't hear anything. Goodbye!",
            voice='Polly.Joanna-Neural'
        )
        response.hangup()
        
        return Response(content=str(response), media_type="application/xml")
        
    except Exception as e:
        print(f"Error in handle_incoming_call: {e}")
        response = VoiceResponse()
        response.say("Sorry, there was an error. Please try again later.", voice='Polly.Joanna-Neural')
        response.hangup()
        return Response(content=str(response), media_type="application/xml")

@app.post('/conversation/{business_id}/process')
async def process_speech(business_id: int, request: Request):
    """Process speech input and generate AI response"""
    try:
        # Lazy import to avoid initialization issues
        from agent import Agent
        agent = Agent()
        
        response = VoiceResponse()
        
        # Get form data from request
        form_data = await request.form()
        
        # Get caller's phone number
        caller_number = form_data.get('From', 'unknown')
        
        # Get the transcribed text from Twilio
        user_speech = form_data.get('SpeechResult', '')
        
        if user_speech:
            # Add user input to conversation history
            conversation_history[caller_number].append(f"User: {user_speech}")
            
            # Generate AI response with conversation context
            ai_response = agent.generate_conversation_response(
                user_speech, 
                conversation_history[caller_number][-6:]  # Keep last 6 exchanges for context
            )
            
            # Add AI response to conversation history
            conversation_history[caller_number].append(f"Assistant: {ai_response}")
            
            # Use Amazon Polly to speak the response
            response.say(
                ai_response,
                voice='Polly.Joanna',
                language='en-US'
            )
            
            # Continue gathering speech
            gather = response.gather(
                input='speech',
                action=f'/conversation/{business_id}/process',
                method='POST',
                speech_timeout='auto',
                timeout=10
            )
            
            # If no response, end the call
            response.say(
                agent.get_goodbye(),
                voice='Polly.Joanna'
            )
            response.hangup()
            
        else:
            # Didn't catch what they said
            response.say(
                "I didn't catch that. Could you please repeat?",
                voice='Polly.Joanna'
            )
            
            # Try again
            gather = response.gather(
                input='speech',
                action=f'/conversation/{business_id}/process',
                method='POST',
                speech_timeout='auto',
                timeout=10
            )
        
        return Response(content=str(response), media_type="application/xml")
        
    except Exception as e:
        print(f"Error in process_speech: {e}")
        response = VoiceResponse()
        response.say("Sorry, there was an error processing your request.", voice='Polly.Joanna')
        response.hangup()
        return Response(content=str(response), media_type="application/xml")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000)
