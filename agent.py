import os
from openai import OpenAI

class Agent:
    def __init__(self):
        """Initialize the Agent"""
        # Initialize OpenAI client with API key from environment
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY not found in environment variables")
        self.client = OpenAI(api_key=api_key)

    def generate_response(self, prompt):
        """Generate a response from the OpenAI client"""
        response = self.client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "You are a helpful AI assistant on a phone call. Keep responses concise and natural."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=150
        )
        return response.choices[0].message.content

    def generate_conversation_response(self, user_input, conversation_history=None):
        """Generate a conversational response with context"""
        messages = [
            {"role": "system", "content": "You are a helpful AI assistant in a phone conversation. Keep responses concise and natural."}
        ]
        
        if conversation_history:
            # Add conversation history to messages
            for exchange in conversation_history[-6:]:  # Last 6 exchanges
                if exchange.startswith("User: "):
                    messages.append({"role": "user", "content": exchange[6:]})
                elif exchange.startswith("Assistant: "):
                    messages.append({"role": "assistant", "content": exchange[11:]})
        
        # Add current user input
        messages.append({"role": "user", "content": user_input})
        
        response = self.client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=messages,
            max_tokens=150
        )
        return response.choices[0].message.content

    def get_greeting(self):
        """Get a greeting message"""
        return "Hello! I'm your AI assistant. How can I help you today?"

    def get_goodbye(self):
        """Get a goodbye message"""
        return "Thank you for calling. Have a great day! Goodbye!"