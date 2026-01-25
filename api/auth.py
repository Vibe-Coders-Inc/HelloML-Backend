# api/auth.py

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import Optional
from .database import supabase

security = HTTPBearer()


class AuthenticatedUser:
    """Represents an authenticated user from Supabase JWT"""
    def __init__(self, id: str, email: Optional[str] = None):
        self.id = id
        self.email = email


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security)
) -> AuthenticatedUser:
    """
    Validates the JWT token from Authorization header and returns the user.
    Raises 401 if token is invalid or expired.
    """
    token = credentials.credentials

    try:
        db = supabase()
        # Verify token with Supabase - this checks signature, expiration, etc.
        response = db.auth.get_user(token)

        if not response or not response.user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired token",
                headers={"WWW-Authenticate": "Bearer"}
            )

        user = response.user
        return AuthenticatedUser(
            id=user.id,
            email=user.email
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"}
        )


async def get_optional_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(HTTPBearer(auto_error=False))
) -> Optional[AuthenticatedUser]:
    """
    Optional authentication - returns user if valid token provided, None otherwise.
    Useful for endpoints that work differently for authenticated vs anonymous users.
    """
    if not credentials:
        return None

    try:
        return await get_current_user(credentials)
    except HTTPException:
        return None


def verify_business_ownership(db, business_id: int, user_id: str) -> dict:
    """
    Verifies user owns the business. Returns business data if owned.
    Raises 403 if user doesn't own the business, 404 if not found.
    """
    result = db.table('business').select('*').eq('id', business_id).single().execute()

    if not result.data:
        raise HTTPException(status_code=404, detail="Business not found")

    if result.data.get('owner_user_id') != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have permission to access this business"
        )

    return result.data


def verify_agent_ownership(db, agent_id: int, user_id: str) -> dict:
    """
    Verifies user owns the agent (via business). Returns agent data if owned.
    Raises 403 if user doesn't own the agent, 404 if not found.
    """
    # Get agent with business info
    result = db.table('agent').select('*, business:business_id(owner_user_id)').eq('id', agent_id).single().execute()

    if not result.data:
        raise HTTPException(status_code=404, detail="Agent not found")

    business = result.data.get('business')
    if not business or business.get('owner_user_id') != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have permission to access this agent"
        )

    return result.data
