# api/database.py
from supabase import create_client, Client
import os
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase_client: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

def supabase() -> Client: 
    return supabase_client

if __name__ == "__main__":
    try:
        supabase()
        print("Supabase Connected Successfully!")
    except Exception as e:
        print(f"An Error has Occured: {e}")
