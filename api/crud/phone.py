# api/crud/phone.py

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from twilio.rest import Client
import os
import logging
from typing import Optional
from ..database import get_service_client
from ..auth import get_current_user, AuthenticatedUser

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/phone", tags=["Phone"])


class ProvisionPhoneRequest(BaseModel):
    agent_id: int
    area_code: str
    force: Optional[bool] = False


@router.post("/provision", summary="Provision phone number for agent")
async def provision_phone(
    request: ProvisionPhoneRequest,
    current_user: AuthenticatedUser = Depends(get_current_user)
):
    """Provisions a new phone number for an existing agent. User must own the agent."""
    try:
        db = current_user.get_db()
        service_db = get_service_client()

        # Verify user owns the agent (RLS will filter)
        agent = db.table('agent').select('*').eq('id', request.agent_id).execute()
        if not agent.data:
            raise HTTPException(status_code=404, detail="Agent not found or access denied")

        agent_data = agent.data[0]

        if agent_data.get('status') not in ['active', 'inactive', 'paused']:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot provision phone for agent with status: {agent_data.get('status')}"
            )

        # Check existing phone (use service client to ensure we see it)
        existing_phone = service_db.table('phone_number').select('*').eq('agent_id', request.agent_id).execute()

        if existing_phone.data:
            if not request.force:
                phone = existing_phone.data[0]
                raise HTTPException(
                    status_code=400,
                    detail=f"Agent already has phone number: {phone['phone_number']}. Use force=true to replace it."
                )
            else:
                logger.info(f"Force mode: Releasing existing phone for agent {request.agent_id}")
                phone_id = existing_phone.data[0]['id']
                try:
                    await _cleanup_phone_internal(service_db, phone_id)
                except Exception as cleanup_error:
                    raise HTTPException(status_code=500, detail=f"Failed to release existing phone: {str(cleanup_error)}")

        # Provision with Twilio
        client = Client(os.getenv("ACCOUNT_SID"), os.getenv("AUTH_TOKEN"))

        try:
            available = client.available_phone_numbers('US').local.list(area_code=request.area_code, limit=1)
        except Exception as twilio_error:
            raise HTTPException(status_code=502, detail=f"Failed to search Twilio: {str(twilio_error)}")

        if not available:
            raise HTTPException(status_code=404, detail=f"No phone numbers available in area code {request.area_code}")

        base_url = os.getenv("API_BASE_URL", "https://api.helloml.app")
        webhook_url = f"{base_url}/conversation/{request.agent_id}/voice"

        try:
            number = client.incoming_phone_numbers.create(
                phone_number=available[0].phone_number,
                voice_url=webhook_url,
                voice_method='POST'
            )
        except Exception as twilio_error:
            raise HTTPException(status_code=502, detail=f"Failed to purchase number: {str(twilio_error)}")

        # Save to database (use service client for insert)
        try:
            result = service_db.table('phone_number').insert({
                'agent_id': request.agent_id,
                'phone_number': number.phone_number,
                'country': 'US',
                'area_code': request.area_code,
                'webhook_url': webhook_url,
                'status': 'active'
            }).execute()

            return result.data[0]

        except Exception as db_error:
            # Rollback Twilio purchase
            try:
                number.delete()
            except:
                pass
            raise HTTPException(status_code=500, detail=f"Failed to save phone number: {str(db_error)}")

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


async def _cleanup_phone_internal(db, phone_id: int):
    """Internal helper to cleanup/release a phone number."""
    phone = db.table('phone_number').select('*').eq('id', phone_id).single().execute()

    if not phone.data:
        raise Exception(f"Phone number with ID {phone_id} not found")

    phone_data = phone.data

    try:
        client = Client(os.getenv("ACCOUNT_SID"), os.getenv("AUTH_TOKEN"))
        numbers = client.incoming_phone_numbers.list(phone_number=phone_data['phone_number'])
        if numbers:
            numbers[0].delete()
    except Exception as e:
        logger.error(f"Failed to release Twilio number: {e}")

    db.table('phone_number').delete().eq('id', phone_id).execute()


@router.get("/agent/{agent_id}", summary="Get phone number for agent")
async def get_phone_by_agent(
    agent_id: int,
    current_user: AuthenticatedUser = Depends(get_current_user)
):
    """Gets phone number assigned to agent - user must own the agent"""
    try:
        db = current_user.get_db()

        # RLS will filter to owned agents only
        result = db.table('phone_number').select('*').eq('agent_id', agent_id).execute()

        if not result.data:
            raise HTTPException(status_code=404, detail="No phone number found for agent")

        return result.data[0]

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{phone_id}", summary="Get phone number by ID")
async def get_phone(
    phone_id: int,
    current_user: AuthenticatedUser = Depends(get_current_user)
):
    """Gets phone number details by ID - user must own the associated agent"""
    try:
        db = current_user.get_db()

        result = db.table('phone_number').select('*').eq('id', phone_id).single().execute()

        if not result.data:
            raise HTTPException(status_code=404, detail="Phone number not found")

        return result.data

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{phone_id}", summary="Delete and release phone number")
async def delete_phone(
    phone_id: int,
    current_user: AuthenticatedUser = Depends(get_current_user)
):
    """Deletes phone number - user must own the agent"""
    try:
        db = current_user.get_db()
        service_db = get_service_client()

        # Check ownership via RLS
        phone = db.table('phone_number').select('*').eq('id', phone_id).single().execute()

        if not phone.data:
            raise HTTPException(status_code=404, detail="Phone number not found")

        await _cleanup_phone_internal(service_db, phone_id)

        return {"success": True, "message": f"Phone number {phone.data['phone_number']} released"}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/agent/{agent_id}", summary="Delete phone number by agent")
async def delete_phone_by_agent(
    agent_id: int,
    current_user: AuthenticatedUser = Depends(get_current_user)
):
    """Deletes phone number for agent - user must own the agent"""
    try:
        db = current_user.get_db()
        service_db = get_service_client()

        # Check ownership via RLS
        phone = db.table('phone_number').select('*').eq('agent_id', agent_id).execute()

        if not phone.data:
            raise HTTPException(status_code=404, detail="No phone number found for agent")

        phone_data = phone.data[0]

        await _cleanup_phone_internal(service_db, phone_data['id'])

        return {"success": True, "message": f"Phone number {phone_data['phone_number']} released"}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{phone_id}/reactivate", summary="Reactivate a paused phone number")
async def reactivate_phone(
    phone_id: int,
    current_user: AuthenticatedUser = Depends(get_current_user)
):
    """
    Reactivates a paused phone number.
    Phone numbers are automatically paused after 14 days of inactivity.
    Reactivating resets the inactivity timer.
    """
    try:
        db = current_user.get_db()

        # Check ownership via RLS
        phone = db.table('phone_number').select('*').eq('id', phone_id).single().execute()

        if not phone.data:
            raise HTTPException(status_code=404, detail="Phone number not found")

        phone_data = phone.data

        if phone_data['status'] != 'paused':
            raise HTTPException(
                status_code=400,
                detail=f"Phone number is not paused (current status: {phone_data['status']})"
            )

        # Reactivate the phone number
        result = db.table('phone_number').update({
            'status': 'active',
            'paused_at': None,
            'last_call_at': 'now()'  # Reset the inactivity timer
        }).eq('id', phone_id).execute()

        if not result.data:
            raise HTTPException(status_code=500, detail="Failed to reactivate phone number")

        return {
            "success": True,
            "message": f"Phone number {phone_data['phone_number']} reactivated",
            "phone": result.data[0]
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/agent/{agent_id}/reactivate", summary="Reactivate phone by agent")
async def reactivate_phone_by_agent(
    agent_id: int,
    current_user: AuthenticatedUser = Depends(get_current_user)
):
    """Reactivates a paused phone number for the given agent."""
    try:
        db = current_user.get_db()

        # Check ownership via RLS
        phone = db.table('phone_number').select('*').eq('agent_id', agent_id).execute()

        if not phone.data:
            raise HTTPException(status_code=404, detail="No phone number found for agent")

        phone_data = phone.data[0]

        if phone_data['status'] != 'paused':
            raise HTTPException(
                status_code=400,
                detail=f"Phone number is not paused (current status: {phone_data['status']})"
            )

        # Reactivate the phone number
        result = db.table('phone_number').update({
            'status': 'active',
            'paused_at': None,
            'last_call_at': 'now()'
        }).eq('id', phone_data['id']).execute()

        if not result.data:
            raise HTTPException(status_code=500, detail="Failed to reactivate phone number")

        return {
            "success": True,
            "message": f"Phone number {phone_data['phone_number']} reactivated",
            "phone": result.data[0]
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
