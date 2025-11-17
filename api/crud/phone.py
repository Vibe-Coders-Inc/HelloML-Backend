# api/crud/phone.py

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from twilio.rest import Client
import os
import logging
from typing import Optional
from ..database import supabase

# Configure logging
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/phone", tags=["Phone"])

class ProvisionPhoneRequest(BaseModel):
    agent_id: int
    area_code: str
    force: Optional[bool] = False  # Allow forcing re-provision by cleaning up existing phone


@router.post("/provision", summary="Provision phone number for agent")
async def provision_phone(request: ProvisionPhoneRequest):
    """
    Provisions a new phone number for an existing agent.

    If force=True, will automatically release any existing phone number before provisioning.
    This is useful for recovering from inconsistent states or re-provisioning after deletion.
    """
    try:
        db = supabase()

        # Check if agent exists and get agent data
        agent = db.table('agent').select('*').eq('id', request.agent_id).execute()
        if not agent.data:
            raise HTTPException(status_code=404, detail="Agent not found")

        agent_data = agent.data[0]

        # Validate agent status - only provision for agents that aren't deleted/invalid
        if agent_data.get('status') not in ['active', 'inactive', 'paused']:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot provision phone for agent with status: {agent_data.get('status')}"
            )

        # Check if agent already has a phone
        existing_phone = db.table('phone_number').select('*').eq('agent_id', request.agent_id).execute()

        if existing_phone.data:
            if not request.force:
                # Return clear error message with the existing phone info
                phone = existing_phone.data[0]
                raise HTTPException(
                    status_code=400,
                    detail=f"Agent already has phone number: {phone['phone_number']}. Use force=true to replace it."
                )
            else:
                # Force mode: Clean up existing phone before provisioning new one
                logger.info(f"Force mode: Releasing existing phone for agent {request.agent_id}")
                phone_id = existing_phone.data[0]['id']
                try:
                    # Use the internal cleanup logic
                    await _cleanup_phone_internal(db, phone_id)
                    logger.info(f"Successfully cleaned up phone {phone_id} for agent {request.agent_id}")
                except Exception as cleanup_error:
                    logger.error(f"Failed to cleanup existing phone: {cleanup_error}")
                    raise HTTPException(
                        status_code=500,
                        detail=f"Failed to release existing phone: {str(cleanup_error)}"
                    )

        # Initialize Twilio client
        client = Client(os.getenv("ACCOUNT_SID"), os.getenv("AUTH_TOKEN"))

        # Search for available numbers in the requested area code
        try:
            available = client.available_phone_numbers('US').local.list(
                area_code=request.area_code,
                limit=1
            )
        except Exception as twilio_error:
            logger.error(f"Twilio search failed: {twilio_error}")
            raise HTTPException(
                status_code=502,
                detail=f"Failed to search Twilio for numbers: {str(twilio_error)}"
            )

        if not available:
            raise HTTPException(
                status_code=404,
                detail=f"No phone numbers available in area code {request.area_code}"
            )

        # Set up webhook URL
        base_url = os.getenv("API_BASE_URL", "https://api.helloml.app")
        webhook_url = f"{base_url}/conversation/{request.agent_id}/voice"

        # Purchase number from Twilio
        try:
            number = client.incoming_phone_numbers.create(
                phone_number=available[0].phone_number,
                voice_url=webhook_url,
                voice_method='POST'
            )
            logger.info(f"Provisioned Twilio number {number.phone_number} for agent {request.agent_id}")
        except Exception as twilio_error:
            logger.error(f"Twilio provisioning failed: {twilio_error}")
            raise HTTPException(
                status_code=502,
                detail=f"Failed to purchase number from Twilio: {str(twilio_error)}"
            )

        # Save to database
        try:
            result = db.table('phone_number').insert({
                'agent_id': request.agent_id,
                'phone_number': number.phone_number,
                'country': 'US',
                'area_code': request.area_code,
                'webhook_url': webhook_url,
                'status': 'active'
            }).execute()

            if not result.data:
                raise Exception("Database insert returned no data")

            logger.info(f"Saved phone number {number.phone_number} to database for agent {request.agent_id}")
            return result.data[0]

        except Exception as db_error:
            # Rollback: Release the Twilio number if database save fails
            logger.error(f"Database save failed, rolling back Twilio purchase: {db_error}")
            try:
                number.delete()
                logger.info(f"Successfully rolled back Twilio number {number.phone_number}")
            except Exception as rollback_error:
                logger.error(f"CRITICAL: Failed to rollback Twilio number: {rollback_error}")

            raise HTTPException(
                status_code=500,
                detail=f"Failed to save phone number to database: {str(db_error)}"
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error in provision_phone: {e}")
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


async def _cleanup_phone_internal(db, phone_id: int):
    """
    Internal helper function to cleanup/release a phone number.
    Handles both Twilio release and database deletion with proper error handling.
    """
    # Get phone number details
    phone = db.table('phone_number').select('*').eq('id', phone_id).single().execute()

    if not phone.data:
        raise Exception(f"Phone number with ID {phone_id} not found")

    phone_data = phone.data

    # Try to release from Twilio first
    try:
        client = Client(os.getenv("ACCOUNT_SID"), os.getenv("AUTH_TOKEN"))
        numbers = client.incoming_phone_numbers.list(phone_number=phone_data['phone_number'])
        if numbers:
            numbers[0].delete()
            logger.info(f"Released Twilio number: {phone_data['phone_number']}")
        else:
            logger.warning(f"Twilio number not found: {phone_data['phone_number']} (may have been already released)")
    except Exception as twilio_error:
        logger.error(f"Failed to release Twilio number {phone_data['phone_number']}: {twilio_error}")
        # Don't raise - continue with DB cleanup even if Twilio fails
        # The number might have been manually deleted or already released

    # Delete from database
    try:
        db.table('phone_number').delete().eq('id', phone_id).execute()
        logger.info(f"Deleted phone number {phone_id} from database")
    except Exception as db_error:
        logger.error(f"Failed to delete phone from database: {db_error}")
        raise Exception(f"Database deletion failed: {str(db_error)}")


@router.get("/agent/{agent_id}", summary="Get phone number for agent")
async def get_phone_by_agent(agent_id: int):
    """Gets phone number assigned to agent"""
    try:
        db = supabase()

        result = db.table('phone_number').select('*').eq('agent_id', agent_id).execute()

        if not result.data:
            raise HTTPException(status_code=404, detail="No phone number found for agent")

        return result.data[0]

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting phone by agent: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{phone_id}", summary="Get phone number by ID")
async def get_phone(phone_id: int):
    """Gets phone number details by ID"""
    try:
        db = supabase()

        result = db.table('phone_number').select('*').eq('id', phone_id).single().execute()

        if not result.data:
            raise HTTPException(status_code=404, detail="Phone number not found")

        return result.data

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting phone by ID: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{phone_id}", summary="Delete and release phone number")
async def delete_phone(phone_id: int):
    """
    Deletes phone number from database and releases from Twilio.

    This operation will attempt to release the number from Twilio first,
    then remove it from the database. If Twilio release fails (e.g., number
    already released), the database deletion will still proceed.
    """
    try:
        db = supabase()

        # Verify phone exists before attempting cleanup
        phone = db.table('phone_number').select('*').eq('id', phone_id).single().execute()

        if not phone.data:
            raise HTTPException(status_code=404, detail="Phone number not found")

        # Use internal cleanup function for consistency
        try:
            await _cleanup_phone_internal(db, phone_id)
            return {
                "success": True,
                "message": f"Phone number {phone.data['phone_number']} released and deleted"
            }
        except Exception as cleanup_error:
            logger.error(f"Failed to cleanup phone {phone_id}: {cleanup_error}")
            raise HTTPException(
                status_code=500,
                detail=f"Failed to delete phone number: {str(cleanup_error)}"
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in delete_phone: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/agent/{agent_id}", summary="Delete phone number by agent")
async def delete_phone_by_agent(agent_id: int):
    """
    Deletes phone number assigned to a specific agent.

    This is a convenience endpoint that finds the phone number for an agent
    and deletes it. Equivalent to looking up the phone_id first and calling
    DELETE /phone/{phone_id}.
    """
    try:
        db = supabase()

        # Get phone number for agent
        phone = db.table('phone_number').select('*').eq('agent_id', agent_id).execute()

        if not phone.data:
            raise HTTPException(status_code=404, detail="No phone number found for agent")

        phone_data = phone.data[0]
        phone_id = phone_data['id']

        # Use internal cleanup function for consistency
        try:
            await _cleanup_phone_internal(db, phone_id)
            return {
                "success": True,
                "message": f"Phone number {phone_data['phone_number']} released and deleted for agent {agent_id}"
            }
        except Exception as cleanup_error:
            logger.error(f"Failed to cleanup phone for agent {agent_id}: {cleanup_error}")
            raise HTTPException(
                status_code=500,
                detail=f"Failed to delete phone number: {str(cleanup_error)}"
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in delete_phone_by_agent: {e}")
        raise HTTPException(status_code=500, detail=str(e))

