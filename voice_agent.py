# voice_agent.py

import os
from typing import List, Optional, Dict, Any
from openai import OpenAI

class VoiceAgent:    
    def __init__(self, agent_config: Optional[Dict[str, Any]] = None):
        """Initialize the VoiceAgent"""
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY not found in environment variables")
        
        self.client = OpenAI(api_key=api_key)
        self.config = agent_config or {}
        
        # Configuration
        self.model = self.config.get('model', 'gpt-5-nano')
        self.system_prompt = self.config.get('system_prompt', 'You are a helpful AI assistant.')
        self.voice = self.config.get('voice', 'Polly.Joanna')
        self.language = self.config.get('language', 'en-US')

    def generate_conversation_response(self, user_input: str, conversation_history: Optional[List[str]] = None) -> str:
        """Generate AI response with conversation context and system prompt"""
        try:
            # Build conversation context
            context = (self.system_prompt or "You are a helpful AI assistant.") + "\n\n"
            
            if conversation_history:
                context += "\n".join(conversation_history) + "\n\n"
            
            context += f"User: {user_input}\nAssistant:"
            
            # Generate response using responses API
            response = self.client.responses.create(
                model=self.model,
                input=context,
                store=True
            )
            
            return response.output_text.strip()
            
        except Exception as e:
            print(f"Error generating response: {str(e)}")
            import traceback
            traceback.print_exc()
            return "I apologize, but I'm having trouble processing your request right now. Please try again."

    def get_greeting(self) -> str:
        """Get greeting from agent config"""
        return self.config.get('greeting', "Hello! How can I help you today?")

    def get_voice_config(self) -> Dict[str, str]:
        """Get voice configuration for Twilio"""
        return {
            'voice': self.voice,
            'language': self.language
        }
