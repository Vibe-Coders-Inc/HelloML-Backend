# api/crud/phone.py

from fastapi import APIRouter, HTTPException
from twilio.rest import Client
import os
from ..database import supabase

router = APIRouter(prefix="/phone", tags=["Phone"])

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
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{phone_id}", summary="Get phone number by ID")
async def get_phone(phone_id: int):
    """Gets phone number details"""
    try:
        db = supabase()
        
        result = db.table('phone_number').select('*').eq('id', phone_id).single().execute()
        
        if not result.data:
            raise HTTPException(status_code=404, detail="Phone number not found")
        
        return result.data
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{phone_id}", summary="Delete and release phone number")
async def delete_phone(phone_id: int):
    """Deletes phone number from database and releases from Twilio"""
    try:
        db = supabase()
        
        # Get phone number details
        phone = db.table('phone_number').select('*').eq('id', phone_id).single().execute()
        
        if not phone.data:
            raise HTTPException(status_code=404, detail="Phone number not found")
        
        phone_data = phone.data
        
        # Release from Twilio
        try:
            client = Client(os.getenv("ACCOUNT_SID"), os.getenv("AUTH_TOKEN"))
            numbers = client.incoming_phone_numbers.list(phone_number=phone_data['phone_number'])
            if numbers:
                numbers[0].delete()
        except Exception as e:
            print(f"Failed to release Twilio number: {e}")
            # Continue with DB deletion even if Twilio fails
        
        # Delete from database
        db.table('phone_number').delete().eq('id', phone_id).execute()
        
        return {"success": True, "message": "Phone number released"}
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/agent/{agent_id}", summary="Delete phone number by agent")
async def delete_phone_by_agent(agent_id: int):
    """Deletes phone number assigned to agent"""
    try:
        db = supabase()
        
        # Get phone number for agent
        phone = db.table('phone_number').select('*').eq('agent_id', agent_id).execute()
        
        if not phone.data:
            raise HTTPException(status_code=404, detail="No phone number found for agent")
        
        phone_data = phone.data[0]
        
        # Release from Twilio
        try:
            client = Client(os.getenv("ACCOUNT_SID"), os.getenv("AUTH_TOKEN"))
            numbers = client.incoming_phone_numbers.list(phone_number=phone_data['phone_number'])
            if numbers:
                numbers[0].delete()
        except Exception as e:
            print(f"Failed to release Twilio number: {e}")
        
        # Delete from database
        db.table('phone_number').delete().eq('agent_id', agent_id).execute()
        
        return {"success": True, "message": "Phone number released"}
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

