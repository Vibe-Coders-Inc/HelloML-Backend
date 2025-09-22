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
        response = self.client.responses.create(
            model="gpt-5-nano",
            input=prompt,
            store=True
        )
        return response.output_text

    def generate_conversation_response(self, user_input, conversation_history=None):
        """Generate a conversational response with context"""
        if conversation_history:
            # Build context from conversation history
            context = "\n".join(conversation_history)
            full_prompt = f"Previous conversation:\n{context}\n\nUser: {user_input}\nAssistant:"
        else:
            full_prompt = f"User: {user_input}\nAssistant:"
        
        response = self.client.responses.create(
            model="gpt-5-nano",
            input=full_prompt,
            store=True
        )
        return response.output_text

    def get_greeting(self):
        """Get a greeting message"""
        return "Hello! I'm your AI assistant. How can I help you today?"

    def get_goodbye(self):
        """Get a goodbye message"""
        return "Thank you for calling. Have a great day! Goodbye!"