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
GOOGLE_DRIVE_REDIRECT_URI = os.getenv("GOOGLE_DRIVE_REDIRECT_URI", "")
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://helloml.app")

# Microsoft / Outlook
MS_CLIENT_ID = os.getenv("MS_CLIENT_ID", "")
MS_CLIENT_SECRET = os.getenv("MS_CLIENT_SECRET", "")
MS_REDIRECT_URI = os.getenv("MS_REDIRECT_URI", "")
MS_TENANT = "common"  # Multi-tenant: personal + work accounts

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/calendar.app.created",
    "https://www.googleapis.com/auth/calendar.freebusy",
]

GOOGLE_DRIVE_SCOPES = [
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/drive.file",
]

MS_SCOPES = [
    "openid",
    "email",
    "offline_access",
    "Calendars.ReadWrite",
    "User.Read",
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

    return RedirectResponse(url=f"{FRONTEND_URL}/business/{business_id}?connected=google-calendar#agent")


# ── Google Drive OAuth ───────────────────────────────────────────


@router.get("/google-drive/auth", summary="Get Google Drive OAuth URL")
async def google_drive_auth_url(
    business_id: int = Query(...),
    current_user: AuthenticatedUser = Depends(get_current_user),
):
    """Generate Google Drive OAuth authorization URL."""
    db = current_user.get_db()
    verify_business_ownership(db, business_id, current_user.id)

    redirect_uri = GOOGLE_DRIVE_REDIRECT_URI or GOOGLE_REDIRECT_URI.replace(
        "/google/callback", "/google-drive/callback"
    )
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(GOOGLE_DRIVE_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": str(business_id),
    }
    auth_url = f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"
    return {"auth_url": auth_url}


@router.get("/google-drive/callback", summary="Google Drive OAuth callback")
async def google_drive_callback(
    code: str = Query(...),
    state: str = Query(...),
):
    """Exchange Google Drive auth code for tokens and store them."""
    business_id = int(state)
    db = get_service_client()

    redirect_uri = GOOGLE_DRIVE_REDIRECT_URI or GOOGLE_REDIRECT_URI.replace(
        "/google/callback", "/google-drive/callback"
    )

    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )

    if token_resp.status_code != 200:
        print(f"[Integrations] Google Drive token exchange failed: {token_resp.text}")
        return RedirectResponse(
            url=f"{FRONTEND_URL}/business/{business_id}#agent?error=token_exchange_failed"
        )

    tokens = token_resp.json()
    access_token = tokens["access_token"]
    refresh_token = tokens.get("refresh_token", "")
    expires_in = tokens.get("expires_in", 3600)

    from datetime import timedelta
    expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

    # Fetch user email
    account_email = None
    async with httpx.AsyncClient() as client:
        user_resp = await client.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if user_resp.status_code == 200:
            account_email = user_resp.json().get("email")

    db.table("tool_connection").upsert(
        {
            "business_id": business_id,
            "provider": "google-drive",
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_expiry": expiry.isoformat(),
            "account_email": account_email,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
        on_conflict="business_id,provider",
    ).execute()

    print(f"[Integrations] Google Drive connected for business {business_id} ({account_email})")

    # Auto-index Drive docs in background
    import asyncio
    asyncio.create_task(_index_drive_docs_background(business_id))

    return RedirectResponse(url=f"{FRONTEND_URL}/business/{business_id}?connected=google-drive#agent")


# ── Outlook / Microsoft OAuth ────────────────────────────────────


@router.get("/outlook/auth", summary="Get Outlook OAuth URL")
async def outlook_auth_url(
    business_id: int = Query(...),
    current_user: AuthenticatedUser = Depends(get_current_user),
):
    """Generate Microsoft OAuth authorization URL for Outlook Calendar."""
    db = current_user.get_db()
    verify_business_ownership(db, business_id, current_user.id)

    params = {
        "client_id": MS_CLIENT_ID,
        "redirect_uri": MS_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(MS_SCOPES),
        "response_mode": "query",
        "state": str(business_id),
    }
    auth_url = f"https://login.microsoftonline.com/{MS_TENANT}/oauth2/v2.0/authorize?{urlencode(params)}"
    return {"auth_url": auth_url}


@router.get("/outlook/callback", summary="Outlook OAuth callback")
async def outlook_callback(
    code: str = Query(...),
    state: str = Query(...),
):
    """Exchange Microsoft auth code for tokens and store them."""
    business_id = int(state)
    db = get_service_client()

    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            f"https://login.microsoftonline.com/{MS_TENANT}/oauth2/v2.0/token",
            data={
                "code": code,
                "client_id": MS_CLIENT_ID,
                "client_secret": MS_CLIENT_SECRET,
                "redirect_uri": MS_REDIRECT_URI,
                "grant_type": "authorization_code",
                "scope": " ".join(MS_SCOPES),
            },
        )

    if token_resp.status_code != 200:
        print(f"[Integrations] Outlook token exchange failed: {token_resp.text}")
        return RedirectResponse(
            url=f"{FRONTEND_URL}/business/{business_id}#agent?error=token_exchange_failed"
        )

    tokens = token_resp.json()
    access_token = tokens["access_token"]
    refresh_token = tokens.get("refresh_token", "")
    expires_in = tokens.get("expires_in", 3600)

    from datetime import timedelta
    expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

    # Fetch user email from Microsoft Graph
    account_email = None
    async with httpx.AsyncClient() as client:
        user_resp = await client.get(
            "https://graph.microsoft.com/v1.0/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if user_resp.status_code == 200:
            user_data = user_resp.json()
            account_email = user_data.get("mail") or user_data.get("userPrincipalName")

    db.table("tool_connection").upsert(
        {
            "business_id": business_id,
            "provider": "outlook-calendar",
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_expiry": expiry.isoformat(),
            "account_email": account_email,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
        on_conflict="business_id,provider",
    ).execute()

    print(f"[Integrations] Outlook connected for business {business_id} ({account_email})")

    return RedirectResponse(url=f"{FRONTEND_URL}/business/{business_id}?connected=outlook-calendar#agent")


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

HELLOML_CALENDAR_NAME = "HelloML Appointments"


async def get_google_access_token(business_id: int) -> tuple[str, dict]:
    """
    Load Google tokens from DB, refresh if expired.
    Returns (access_token, connection_record).
    """
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

            conn["access_token"] = access_token
            print(f"[Integrations] Refreshed Google token for business {business_id}")
        else:
            print(f"[Integrations] Token refresh failed: {resp.text}")
            raise ValueError("Failed to refresh Google token")

    return access_token, conn


async def get_or_create_helloml_calendar(business_id: int) -> str:
    """
    Get the HelloML calendar ID for a business, creating it if needed.
    The calendar ID is stored in the connection's settings.
    """
    access_token, conn = await get_google_access_token(business_id)
    settings = conn.get("settings") or {}

    # Return cached calendar ID if we have it
    if settings.get("calendar_id"):
        return settings["calendar_id"]

    # Create a new secondary calendar
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://www.googleapis.com/calendar/v3/calendars",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json={
                "summary": HELLOML_CALENDAR_NAME,
                "description": "Appointments scheduled via HelloML voice agent",
                "timeZone": "America/Chicago",
            },
        )

    if resp.status_code not in (200, 201):
        raise ValueError(f"Failed to create calendar: {resp.status_code} - {resp.text}")

    calendar_id = resp.json()["id"]

    # Store calendar ID in settings
    settings["calendar_id"] = calendar_id
    db = get_service_client()
    db.table("tool_connection").update({
        "settings": settings,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("business_id", business_id).eq("provider", "google-calendar").execute()

    print(f"[Integrations] Created HelloML calendar for business {business_id}: {calendar_id}")
    return calendar_id


async def check_availability(
    business_id: int,
    time_min: str,
    time_max: str,
    timezone_str: str = "America/Chicago",
) -> dict:
    """
    Check availability using the freebusy API.
    Returns busy time slots within the given range.
    """
    access_token, conn = await get_google_access_token(business_id)
    account_email = conn.get("account_email")

    if not account_email:
        return {"error": "No account email found for this connection"}

    # Query freebusy for the user's primary calendar
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://www.googleapis.com/calendar/v3/freeBusy",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json={
                "timeMin": time_min,
                "timeMax": time_max,
                "timeZone": timezone_str,
                "items": [{"id": account_email}],
            },
        )

    if resp.status_code != 200:
        return {"error": f"Freebusy API error: {resp.status_code}", "detail": resp.text}

    data = resp.json()
    calendars = data.get("calendars", {})
    calendar_data = calendars.get(account_email, {})
    busy_slots = calendar_data.get("busy", [])

    return {
        "busy": busy_slots,
        "count": len(busy_slots),
        "time_min": time_min,
        "time_max": time_max,
    }


async def list_calendar_events(business_id: int, time_min: str, time_max: str) -> dict:
    """List events from the HelloML calendar (appointments we created)."""
    access_token, _ = await get_google_access_token(business_id)
    calendar_id = await get_or_create_helloml_calendar(business_id)

    params = {
        "timeMin": time_min,
        "timeMax": time_max,
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": "50",
    }

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events",
            headers={"Authorization": f"Bearer {access_token}"},
            params=params,
        )

    if resp.status_code != 200:
        return {"error": f"Google Calendar API error: {resp.status_code}", "detail": resp.text}

    data = resp.json()
    events = []
    for item in data.get("items", []):
        events.append({
            "id": item.get("id"),
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
    """Create an event on the HelloML calendar."""
    access_token, _ = await get_google_access_token(business_id)
    calendar_id = await get_or_create_helloml_calendar(business_id)

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
            f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events",
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
        "id": created.get("id"),
        "summary": created.get("summary"),
        "start": created.get("start", {}).get("dateTime"),
        "end": created.get("end", {}).get("dateTime"),
        "link": created.get("htmlLink"),
    }


async def update_calendar_event(
    business_id: int,
    event_id: str,
    summary: str = None,
    start_datetime: str = None,
    end_datetime: str = None,
    description: str = None,
    timezone_str: str = "America/Chicago",
) -> dict:
    """Update an event on the HelloML calendar."""
    access_token, _ = await get_google_access_token(business_id)
    calendar_id = await get_or_create_helloml_calendar(business_id)

    # Build patch body with only provided fields
    event_body = {}
    if summary is not None:
        event_body["summary"] = summary
    if start_datetime is not None:
        event_body["start"] = {"dateTime": start_datetime, "timeZone": timezone_str}
    if end_datetime is not None:
        event_body["end"] = {"dateTime": end_datetime, "timeZone": timezone_str}
    if description is not None:
        event_body["description"] = description

    if not event_body:
        return {"error": "No fields to update"}

    async with httpx.AsyncClient() as client:
        resp = await client.patch(
            f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events/{event_id}",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json=event_body,
        )

    if resp.status_code != 200:
        return {"error": f"Failed to update event: {resp.status_code}", "detail": resp.text}

    updated = resp.json()
    return {
        "status": "updated",
        "id": updated.get("id"),
        "summary": updated.get("summary"),
        "start": updated.get("start", {}).get("dateTime"),
        "end": updated.get("end", {}).get("dateTime"),
        "link": updated.get("htmlLink"),
    }


async def delete_calendar_event(business_id: int, event_id: str) -> dict:
    """Delete an event from the HelloML calendar."""
    access_token, _ = await get_google_access_token(business_id)
    calendar_id = await get_or_create_helloml_calendar(business_id)

    async with httpx.AsyncClient() as client:
        resp = await client.delete(
            f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events/{event_id}",
            headers={"Authorization": f"Bearer {access_token}"},
        )

    if resp.status_code not in (200, 204):
        return {"error": f"Failed to delete event: {resp.status_code}", "detail": resp.text}

    return {"status": "deleted", "id": event_id}


# ── Outlook Calendar Helpers ─────────────────────────────────────


async def get_outlook_access_token(business_id: int) -> tuple[str, dict]:
    """
    Load Outlook tokens from DB, refresh if expired.
    Returns (access_token, connection_record).
    """
    db = get_service_client()
    result = db.table("tool_connection").select("*").eq(
        "business_id", business_id
    ).eq("provider", "outlook-calendar").single().execute()

    if not result.data:
        raise ValueError("Outlook Calendar not connected")

    conn = result.data
    access_token = conn["access_token"]
    refresh_token = conn["refresh_token"]
    token_expiry = conn.get("token_expiry")

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
                f"https://login.microsoftonline.com/{MS_TENANT}/oauth2/v2.0/token",
                data={
                    "client_id": MS_CLIENT_ID,
                    "client_secret": MS_CLIENT_SECRET,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                    "scope": " ".join(MS_SCOPES),
                },
            )

        if resp.status_code == 200:
            new_tokens = resp.json()
            access_token = new_tokens["access_token"]
            new_refresh = new_tokens.get("refresh_token", refresh_token)
            expires_in = new_tokens.get("expires_in", 3600)
            from datetime import timedelta
            new_expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

            db.table("tool_connection").update({
                "access_token": access_token,
                "refresh_token": new_refresh,
                "token_expiry": new_expiry.isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("business_id", business_id).eq("provider", "outlook-calendar").execute()

            conn["access_token"] = access_token
            print(f"[Integrations] Refreshed Outlook token for business {business_id}")
        else:
            print(f"[Integrations] Outlook token refresh failed: {resp.text}")
            raise ValueError("Failed to refresh Outlook token")

    return access_token, conn


async def outlook_check_availability(
    business_id: int,
    time_min: str,
    time_max: str,
    timezone_str: str = "America/Chicago",
) -> dict:
    """Check Outlook calendar availability using Microsoft Graph."""
    access_token, conn = await get_outlook_access_token(business_id)
    account_email = conn.get("account_email")

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://graph.microsoft.com/v1.0/me/calendar/getSchedule",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json={
                "schedules": [account_email],
                "startTime": {"dateTime": time_min, "timeZone": timezone_str},
                "endTime": {"dateTime": time_max, "timeZone": timezone_str},
                "availabilityViewInterval": 30,
            },
        )

    if resp.status_code != 200:
        return {"error": f"Outlook API error: {resp.status_code}", "detail": resp.text}

    data = resp.json()
    schedules = data.get("value", [])
    busy_slots = []
    for schedule in schedules:
        for item in schedule.get("scheduleItems", []):
            busy_slots.append({
                "start": item.get("start", {}).get("dateTime"),
                "end": item.get("end", {}).get("dateTime"),
                "subject": item.get("subject", "Busy"),
                "status": item.get("status", "busy"),
            })

    return {
        "busy": busy_slots,
        "count": len(busy_slots),
        "time_min": time_min,
        "time_max": time_max,
    }


async def outlook_create_event(
    business_id: int,
    summary: str,
    start_datetime: str,
    end_datetime: str,
    description: str = "",
    timezone_str: str = "America/Chicago",
) -> dict:
    """Create an event on Outlook calendar via Microsoft Graph."""
    access_token, _ = await get_outlook_access_token(business_id)

    event_body = {
        "subject": summary,
        "start": {"dateTime": start_datetime, "timeZone": timezone_str},
        "end": {"dateTime": end_datetime, "timeZone": timezone_str},
    }
    if description:
        event_body["body"] = {"contentType": "text", "content": description}

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://graph.microsoft.com/v1.0/me/events",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json=event_body,
        )

    if resp.status_code not in (200, 201):
        return {"error": f"Failed to create Outlook event: {resp.status_code}", "detail": resp.text}

    created = resp.json()
    return {
        "status": "created",
        "id": created.get("id"),
        "summary": created.get("subject"),
        "start": created.get("start", {}).get("dateTime"),
        "end": created.get("end", {}).get("dateTime"),
        "link": created.get("webLink"),
    }


# ── Google Drive Helpers ─────────────────────────────────────────


async def get_google_drive_access_token(business_id: int) -> str:
    """Load Google Drive tokens from DB, refresh if expired. Returns access_token."""
    db = get_service_client()
    result = db.table("tool_connection").select("*").eq(
        "business_id", business_id
    ).eq("provider", "google-drive").single().execute()

    if not result.data:
        raise ValueError("Google Drive not connected")

    conn = result.data
    access_token = conn["access_token"]
    refresh_token = conn["refresh_token"]
    token_expiry = conn.get("token_expiry")

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
            }).eq("business_id", business_id).eq("provider", "google-drive").execute()

            print(f"[Integrations] Refreshed Google Drive token for business {business_id}")
        else:
            print(f"[Integrations] Google Drive token refresh failed: {resp.text}")
            raise ValueError("Failed to refresh Google Drive token")

    return access_token


async def list_drive_docs(business_id: int, max_results: int = 50) -> list:
    """
    List text-based documents from Google Drive.
    Returns docs, spreadsheets, PDFs, and text files.
    """
    access_token = await get_google_drive_access_token(business_id)

    # Search for readable document types
    query = (
        "mimeType='application/vnd.google-apps.document' or "
        "mimeType='application/vnd.google-apps.spreadsheet' or "
        "mimeType='application/pdf' or "
        "mimeType='text/plain' or "
        "mimeType='text/csv'"
    )

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://www.googleapis.com/drive/v3/files",
            headers={"Authorization": f"Bearer {access_token}"},
            params={
                "q": f"({query}) and trashed=false",
                "fields": "files(id,name,mimeType,modifiedTime,size)",
                "pageSize": max_results,
                "orderBy": "modifiedTime desc",
            },
        )

    if resp.status_code != 200:
        print(f"[Drive] List files error: {resp.status_code} - {resp.text}")
        return []

    return resp.json().get("files", [])


async def export_drive_doc_text(business_id: int, file_id: str, mime_type: str) -> str:
    """Export a Drive document as plain text."""
    access_token = await get_google_drive_access_token(business_id)

    async with httpx.AsyncClient(timeout=30.0) as client:
        if mime_type.startswith("application/vnd.google-apps."):
            # Google Docs/Sheets — use export
            resp = await client.get(
                f"https://www.googleapis.com/drive/v3/files/{file_id}/export",
                headers={"Authorization": f"Bearer {access_token}"},
                params={"mimeType": "text/plain"},
            )
        else:
            # Regular files — download directly
            resp = await client.get(
                f"https://www.googleapis.com/drive/v3/files/{file_id}",
                headers={"Authorization": f"Bearer {access_token}"},
                params={"alt": "media"},
            )

    if resp.status_code != 200:
        print(f"[Drive] Export error for {file_id}: {resp.status_code}")
        return ""

    return resp.text[:50000]  # Cap at 50k chars per doc


async def index_drive_docs(business_id: int) -> dict:
    """
    Index all readable Google Drive documents into the RAG pipeline.
    Uses the existing upsert_document_text for chunking + embedding.
    """
    from api.rag import upsert_document_text
    from openai import OpenAI

    db = get_service_client()
    ai = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    # Get agent_id for this business
    agent_data = db.table("agent").select("id").eq("business_id", business_id).limit(1).execute()
    if not agent_data.data:
        return {"error": "No agent found for this business"}
    agent_id = agent_data.data[0]["id"]

    docs = await list_drive_docs(business_id)
    if not docs:
        return {"indexed": 0, "message": "No documents found in Google Drive"}

    indexed = 0
    errors = 0
    for doc in docs:
        try:
            text = await export_drive_doc_text(business_id, doc["id"], doc["mimeType"])
            if not text or len(text.strip()) < 20:
                continue

            # Use filename as document identifier
            filename = f"drive:{doc['name']}"
            upsert_document_text(
                sb=db,
                ai=ai,
                agent_id=agent_id,
                filename=filename,
                text=text,
            )
            indexed += 1
            print(f"[Drive] Indexed: {doc['name']} ({len(text)} chars)")
        except Exception as e:
            errors += 1
            print(f"[Drive] Error indexing {doc.get('name', 'unknown')}: {e}")

    return {"indexed": indexed, "errors": errors, "total_found": len(docs)}


async def _index_drive_docs_background(business_id: int):
    """Background task to index Drive docs after OAuth connection."""
    try:
        result = await index_drive_docs(business_id)
        print(f"[Drive] Background indexing complete for business {business_id}: {result}")
    except Exception as e:
        print(f"[Drive] Background indexing failed for business {business_id}: {e}")


# ── Drive indexing endpoint ──────────────────────────────────────


@router.post("/{business_id}/google-drive/index", summary="Index Google Drive documents")
async def trigger_drive_index(
    business_id: int,
    current_user: AuthenticatedUser = Depends(get_current_user),
):
    """Manually trigger re-indexing of Google Drive documents into RAG."""
    db = current_user.get_db()
    verify_business_ownership(db, business_id, current_user.id)

    result = await index_drive_docs(business_id)
    return result


@router.post("/{business_id}/google-drive/index-files", summary="Index specific Google Drive files by ID")
async def index_drive_files_by_id(
    business_id: int,
    body: dict,
    current_user: AuthenticatedUser = Depends(get_current_user),
):
    """Index specific files selected via Google Picker. Body: { file_ids: [{ id, name, mimeType }] }"""
    db = current_user.get_db()
    verify_business_ownership(db, business_id, current_user.id)

    file_ids = body.get("file_ids", [])
    if not file_ids:
        raise HTTPException(status_code=400, detail="No file_ids provided")

    from api.rag import upsert_document_text
    from openai import OpenAI

    sdb = get_service_client()
    ai = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    agent_data = sdb.table("agent").select("id").eq("business_id", business_id).limit(1).execute()
    if not agent_data.data:
        raise HTTPException(status_code=404, detail="No agent found for this business")
    agent_id = agent_data.data[0]["id"]

    indexed = 0
    errors = 0
    for file_info in file_ids:
        try:
            file_id = file_info["id"]
            name = file_info.get("name", "unknown")
            mime_type = file_info.get("mimeType", "")
            text = await export_drive_doc_text(business_id, file_id, mime_type)
            if not text or len(text.strip()) < 20:
                continue
            filename = f"drive:{name}"
            upsert_document_text(sb=sdb, ai=ai, agent_id=agent_id, filename=filename, text=text)
            indexed += 1
            print(f"[Drive] Indexed via Picker: {name} ({len(text)} chars)")
        except Exception as e:
            errors += 1
            print(f"[Drive] Error indexing {file_info.get('name', 'unknown')}: {e}")

    return {"indexed": indexed, "errors": errors, "total": len(file_ids)}


@router.get("/{business_id}/google-drive/picker-token", summary="Get Drive access token for Google Picker")
async def get_drive_picker_token(
    business_id: int,
    current_user: AuthenticatedUser = Depends(get_current_user),
):
    """Return a fresh access token for use with Google Picker on the frontend."""
    db = current_user.get_db()
    verify_business_ownership(db, business_id, current_user.id)

    access_token = await get_google_drive_access_token(business_id)
    return {"access_token": access_token}


@router.get("/{business_id}/google-drive/folders", summary="List Google Drive folders")
async def list_drive_folders(
    business_id: int,
    current_user: AuthenticatedUser = Depends(get_current_user),
):
    """List folders from Google Drive for folder selection UI."""
    db = current_user.get_db()
    verify_business_ownership(db, business_id, current_user.id)

    access_token = await get_google_drive_access_token(business_id)

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://www.googleapis.com/drive/v3/files",
            headers={"Authorization": f"Bearer {access_token}"},
            params={
                "q": "mimeType='application/vnd.google-apps.folder' and trashed=false",
                "fields": "files(id,name,parents)",
                "pageSize": 100,
                "orderBy": "name",
            },
        )

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Failed to list Drive folders")

    folders = resp.json().get("files", [])
    # Add a "My Drive (All)" option at the top
    return {"folders": [{"id": "root", "name": "My Drive (All files)"}] + folders}


@router.get("/{business_id}/google-drive/files", summary="List indexed Google Drive files")
async def list_drive_files_endpoint(
    business_id: int,
    current_user: AuthenticatedUser = Depends(get_current_user),
):
    """List files from Google Drive that have been or can be indexed."""
    db = current_user.get_db()
    verify_business_ownership(db, business_id, current_user.id)

    docs = await list_drive_docs(business_id)
    return {"files": docs}


@router.get("/{business_id}/calendars", summary="List available calendars")
async def list_calendars(
    business_id: int,
    current_user: AuthenticatedUser = Depends(get_current_user),
):
    """List calendars from connected Google or Outlook account."""
    db = current_user.get_db()
    verify_business_ownership(db, business_id, current_user.id)

    # Check which calendar provider is connected
    result = db.table("tool_connection").select("provider, access_token, refresh_token").eq(
        "business_id", business_id
    ).in_("provider", ["google-calendar", "outlook-calendar"]).execute()

    if not result.data:
        raise HTTPException(status_code=404, detail="No calendar connected")

    conn = result.data[0]
    provider = conn["provider"]

    if provider == "google-calendar":
        access_token, _ = await get_google_access_token(business_id)
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://www.googleapis.com/calendar/v3/users/me/calendarList",
                headers={"Authorization": f"Bearer {access_token}"},
                params={"minAccessRole": "writer"},
            )
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail="Failed to list Google calendars")
        items = resp.json().get("items", [])
        return {"calendars": [{"id": c["id"], "name": c.get("summary", c["id"]), "primary": c.get("primary", False)} for c in items]}

    elif provider == "outlook-calendar":
        access_token, _ = await get_outlook_access_token(business_id)
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://graph.microsoft.com/v1.0/me/calendars",
                headers={"Authorization": f"Bearer {access_token}"},
            )
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail="Failed to list Outlook calendars")
        items = resp.json().get("value", [])
        return {"calendars": [{"id": c["id"], "name": c.get("name", "Calendar"), "primary": c.get("isDefaultCalendar", False)} for c in items]}

    raise HTTPException(status_code=400, detail=f"Unsupported provider: {provider}")
