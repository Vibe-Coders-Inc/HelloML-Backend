from openai import OpenAI
from dotenv import load_dotenv
import os

load_dotenv()

client = OpenAI()

response = client.responses.create(
    model="gpt-5",
    input="What is the capital of France?",
)

print(response.output_text)