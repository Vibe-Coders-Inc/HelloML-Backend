# api/database.py
from supabase import create_client, Client
from supabase.lib.client_options import ClientOptions
import os
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_PUBLISHED_KEY")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

# Service role client - bypasses RLS, used for Twilio webhooks and background tasks
_service_client: Client = None


def get_service_client() -> Client:
    """
    Returns a Supabase client with service role privileges.
    Bypasses RLS - use only for server-side operations without user context.
    """
    global _service_client
    if _service_client is None:
        if not SUPABASE_SERVICE_KEY:
            raise ValueError("SUPABASE_SERVICE_KEY not found in environment variables")
        _service_client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    return _service_client


def get_user_client(access_token: str) -> Client:
    """
    Returns a Supabase client authenticated as the user.
    RLS policies will be enforced based on auth.uid().
    """
    options = ClientOptions(
        headers={
            "Authorization": f"Bearer {access_token}"
        }
    )
    return create_client(SUPABASE_URL, SUPABASE_ANON_KEY, options)


# Legacy function for backwards compatibility during migration
def supabase() -> Client:
    """
    DEPRECATED: Use get_service_client() or get_user_client() instead.
    Returns service client for now to maintain compatibility.
    """
    return get_service_client()


if __name__ == "__main__":
    try:
        client = get_service_client()
        print("Supabase Service Client Connected Successfully!")
    except Exception as e:
        print(f"An Error has Occurred: {e}")
