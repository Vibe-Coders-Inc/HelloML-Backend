from flask import Flask, request
from twilio.twiml.voice_response import VoiceResponse
import os
from dotenv import load_dotenv
from agent import Agent

load_dotenv()

agent = Agent()
app = Flask(__name__)

# Store conversation history
conversation_history = {}

@app.route("/voice", methods=['POST'])
def handle_incoming_call():
    """Handle incoming calls"""
    response = VoiceResponse()
    
    # Get caller's phone number for conversation tracking
    caller_number = request.form.get('From', 'unknown')
    
    # Initialize conversation history for this caller
    if caller_number not in conversation_history:
        conversation_history[caller_number] = []
    
    # Greet the caller using Amazon Polly
    greeting = agent.get_greeting()
    response.say(
        greeting, 
        voice='Polly.Joanna',  # Amazon Polly voice
        language='en-US'
    )
    
    # Gather speech input
    gather = response.gather(
        input='speech',
        action='/process_speech',
        method='POST',
        speech_timeout='auto',
        timeout=10
    )
    
    # If no speech detected, say goodbye
    response.say(
        "I didn't hear anything. Goodbye!",
        voice='Polly.Joanna'
    )
    response.hangup()
    
    return str(response)

@app.route('/process_speech', methods=['POST'])
def process_speech():
    """Process the speech input from the user"""
    response = VoiceResponse()
    
    # Get caller's phone number
    caller_number = request.form.get('From', 'unknown')
    
    # Get the transcribed text from Twilio
    user_speech = request.form.get('SpeechResult', '')
    
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
            action='/process_speech',
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
            action='/process_speech',
            method='POST',
            speech_timeout='auto',
            timeout=10
        )
    
    return str(response)

if __name__ == "__main__":
    app.run(debug=True, port=5000)
else:
    app = app # For Vercel Deployment! :D