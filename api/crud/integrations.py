"""
Third-party tool integration endpoints.

Handles OAuth flows and API interactions for external services
like Google Calendar.
"""

import os
import httpx
from datetime import datetime, timezone
from urllib.parse import urlencode
from fastapi import APIRouter, Depends, HTTPException, status, Query
from fastapi.responses import RedirectResponse
from api.database import get_service_client
from api.auth import get_current_user, AuthenticatedUser, verify_business_ownership


router = APIRouter(prefix="/integrations", tags=["Integrations"])

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "")
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://helloml.app")

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/userinfo.email",
]


# ── OAuth Endpoints ──────────────────────────────────────────────


@router.get("/google/auth", summary="Get Google OAuth URL")
async def google_auth_url(
    business_id: int = Query(...),
    current_user: AuthenticatedUser = Depends(get_current_user),
):
    """Generate Google OAuth authorization URL."""
    db = current_user.get_db()
    verify_business_ownership(db, business_id, current_user.id)

    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(GOOGLE_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": str(business_id),
    }
    auth_url = f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"
    return {"auth_url": auth_url}


@router.get("/google/callback", summary="Google OAuth callback")
async def google_callback(
    code: str = Query(...),
    state: str = Query(...),
):
    """
    Exchange Google auth code for tokens and store them.
    This endpoint is called by Google's redirect — no auth header available.
    """
    business_id = int(state)
    db = get_service_client()

    # Exchange code for tokens
    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uri": GOOGLE_REDIRECT_URI,
                "grant_type": "authorization_code",
            },
        )

    if token_resp.status_code != 200:
        print(f"[Integrations] Token exchange failed: {token_resp.text}")
        return RedirectResponse(
            url=f"{FRONTEND_URL}/business/{business_id}#agent?error=token_exchange_failed"
        )

    tokens = token_resp.json()
    access_token = tokens["access_token"]
    refresh_token = tokens.get("refresh_token", "")
    expires_in = tokens.get("expires_in", 3600)
    token_expiry = datetime.now(timezone.utc).isoformat()

    # Calculate actual expiry
    from datetime import timedelta
    expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    token_expiry = expiry.isoformat()

    # Fetch user email
    account_email = None
    async with httpx.AsyncClient() as client:
        user_resp = await client.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if user_resp.status_code == 200:
            account_email = user_resp.json().get("email")

    # Upsert into tool_connection
    db.table("tool_connection").upsert(
        {
            "business_id": business_id,
            "provider": "google-calendar",
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_expiry": token_expiry,
            "account_email": account_email,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
        on_conflict="business_id,provider",
    ).execute()

    print(f"[Integrations] Google Calendar connected for business {business_id} ({account_email})")

    return RedirectResponse(url=f"{FRONTEND_URL}/business/{business_id}#agent")


@router.get("/{business_id}/connections", summary="List tool connections")
async def list_connections(
    business_id: int,
    current_user: AuthenticatedUser = Depends(get_current_user),
):
    """List all tool connections for a business (no tokens exposed)."""
    db = current_user.get_db()
    verify_business_ownership(db, business_id, current_user.id)

    result = db.table("tool_connection").select(
        "id, provider, account_email, settings, created_at"
    ).eq("business_id", business_id).execute()

    return {"connections": result.data or []}


@router.delete("/{business_id}/connections/{provider}", summary="Disconnect tool")
async def disconnect_tool(
    business_id: int,
    provider: str,
    current_user: AuthenticatedUser = Depends(get_current_user),
):
    """Remove a tool connection."""
    db = current_user.get_db()
    verify_business_ownership(db, business_id, current_user.id)

    db.table("tool_connection").delete().eq(
        "business_id", business_id
    ).eq("provider", provider).execute()

    print(f"[Integrations] Disconnected {provider} for business {business_id}")
    return {"status": "disconnected"}


@router.patch("/{business_id}/connections/{provider}/settings", summary="Update tool settings")
async def update_tool_settings(
    business_id: int,
    provider: str,
    body: dict,
    current_user: AuthenticatedUser = Depends(get_current_user),
):
    """Update settings for a tool connection."""
    db = current_user.get_db()
    verify_business_ownership(db, business_id, current_user.id)

    settings = body.get("settings", {})

    result = db.table("tool_connection").update({
        "settings": settings,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("business_id", business_id).eq("provider", provider).execute()

    if not result.data:
        raise HTTPException(status_code=404, detail="Connection not found")

    print(f"[Integrations] Updated settings for {provider} (business {business_id}): {settings}")
    return {"status": "updated", "settings": settings}


# ── Tool Settings Helpers (used by voice agent) ──────────────────


def get_tool_settings(business_id: int, provider: str) -> dict:
    """Get settings for a specific tool connection."""
    db = get_service_client()
    result = db.table("tool_connection").select("settings").eq(
        "business_id", business_id
    ).eq("provider", provider).single().execute()

    if not result.data:
        return {}

    return result.data.get("settings") or {}


def get_all_tool_settings(business_id: int) -> dict:
    """Get settings for all tool connections for a business, keyed by provider."""
    db = get_service_client()
    result = db.table("tool_connection").select(
        "provider, settings"
    ).eq("business_id", business_id).execute()

    settings_by_provider = {}
    for conn in (result.data or []):
        settings_by_provider[conn["provider"]] = conn.get("settings") or {}

    return settings_by_provider


# ── Google Calendar Helpers (used by voice agent) ────────────────


async def get_google_access_token(business_id: int) -> str:
    """Load Google tokens from DB, refresh if expired, return valid access token."""
    db = get_service_client()
    result = db.table("tool_connection").select("*").eq(
        "business_id", business_id
    ).eq("provider", "google-calendar").single().execute()

    if not result.data:
        raise ValueError("Google Calendar not connected")

    conn = result.data
    access_token = conn["access_token"]
    refresh_token = conn["refresh_token"]
    token_expiry = conn.get("token_expiry")

    # Check if token is expired
    needs_refresh = True
    if token_expiry:
        try:
            expiry = datetime.fromisoformat(token_expiry.replace("Z", "+00:00"))
            needs_refresh = datetime.now(timezone.utc) >= expiry
        except (ValueError, TypeError):
            needs_refresh = True

    if needs_refresh and refresh_token:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": GOOGLE_CLIENT_ID,
                    "client_secret": GOOGLE_CLIENT_SECRET,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
            )

        if resp.status_code == 200:
            new_tokens = resp.json()
            access_token = new_tokens["access_token"]
            expires_in = new_tokens.get("expires_in", 3600)
            from datetime import timedelta
            new_expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

            db.table("tool_connection").update({
                "access_token": access_token,
                "token_expiry": new_expiry.isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("business_id", business_id).eq("provider", "google-calendar").execute()

            print(f"[Integrations] Refreshed Google token for business {business_id}")
        else:
            print(f"[Integrations] Token refresh failed: {resp.text}")
            raise ValueError("Failed to refresh Google token")

    return access_token


async def list_calendar_events(business_id: int, time_min: str, time_max: str) -> dict:
    """List events from the user's primary Google Calendar."""
    access_token = await get_google_access_token(business_id)

    params = {
        "timeMin": time_min,
        "timeMax": time_max,
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": "20",
    }

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://www.googleapis.com/calendar/v3/calendars/primary/events",
            headers={"Authorization": f"Bearer {access_token}"},
            params=params,
        )

    if resp.status_code != 200:
        return {"error": f"Google Calendar API error: {resp.status_code}", "detail": resp.text}

    data = resp.json()
    events = []
    for item in data.get("items", []):
        events.append({
            "summary": item.get("summary", "(No title)"),
            "start": item.get("start", {}).get("dateTime") or item.get("start", {}).get("date"),
            "end": item.get("end", {}).get("dateTime") or item.get("end", {}).get("date"),
            "location": item.get("location"),
            "description": item.get("description"),
        })

    return {"events": events, "count": len(events)}


async def create_calendar_event(
    business_id: int,
    summary: str,
    start_datetime: str,
    end_datetime: str,
    description: str = "",
    timezone_str: str = "America/Chicago",
) -> dict:
    """Create an event on the user's primary Google Calendar."""
    access_token = await get_google_access_token(business_id)

    event_body = {
        "summary": summary,
        "start": {
            "dateTime": start_datetime,
            "timeZone": timezone_str,
        },
        "end": {
            "dateTime": end_datetime,
            "timeZone": timezone_str,
        },
    }
    if description:
        event_body["description"] = description

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://www.googleapis.com/calendar/v3/calendars/primary/events",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json=event_body,
        )

    if resp.status_code not in (200, 201):
        return {"error": f"Failed to create event: {resp.status_code}", "detail": resp.text}

    created = resp.json()
    return {
        "status": "created",
        "summary": created.get("summary"),
        "start": created.get("start", {}).get("dateTime"),
        "end": created.get("end", {}).get("dateTime"),
        "link": created.get("htmlLink"),
    }
