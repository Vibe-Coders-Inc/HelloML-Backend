# api/crud/agent.py

from fastapi import APIRouter, HTTPException, Request
from twilio.rest import Client
import os
from ..database import supabase

router = APIRouter(prefix="/agent", tags=["Agent"])

async def provision_phone_for_agent(agent_id: int, area_code: str):
    """Internal function to provision phone number for agent"""
    db = supabase()
    
    # Check if agent already has a phone
    existing_phone = db.table('phone_number').select('*').eq('agent_id', agent_id).execute()
    if existing_phone.data:
        return existing_phone.data[0]
    
    # Provision number with Twilio
    client = Client(os.getenv("ACCOUNT_SID"), os.getenv("AUTH_TOKEN"))
    
    available = client.available_phone_numbers('US').local.list(
        area_code=area_code,
        limit=1
    )
    
    if not available:
        raise Exception(f"No numbers available in area code {area_code}")
    
    webhook_url = f"https://www.helloml.app/conversation/{agent_id}/voice"
    
    number = client.incoming_phone_numbers.create(
        phone_number=available[0].phone_number,
        voice_url=webhook_url,
        voice_method='POST'
    )
    
    # Save to database
    result = db.table('phone_number').insert({
        'agent_id': agent_id,
        'phone_number': number.phone_number,
        'country': 'US',
        'area_code': area_code,
        'webhook_url': webhook_url,
        'status': 'active'
    }).execute()
    
    return result.data[0]


@router.post("", summary="Create agent")
async def create_agent(request: Request):
    """Creates agent and provisions phone number"""
    try:
        db = supabase()
        data = await request.json()
        
        business_id = data['business_id']
        area_code = data['area_code']
        
        # Check if business exists
        business = db.table('business').select('*').eq('id', business_id).single().execute()
        if not business.data:
            raise HTTPException(status_code=404, detail="Business not found")
        
        # Check if business already has an agent (one per business)
        existing_agent = db.table('agent').select('*').eq('business_id', business_id).execute()
        if existing_agent.data:
            raise HTTPException(status_code=400, detail="Business already has an agent")
        
        # Create agent
        agent_result = db.table('agent').insert({
            'business_id': business_id,
            'name': data.get('name', 'Agent'),
            'model_type': data.get('model_type', 'gpt-5-nano'),
            'temperature': data.get('temperature', 0.7),
            'voice_model': data.get('voice_model', 'Joanna'),
            'prompt': data.get('prompt'),
            'greeting': data.get('greeting', 'Hello There!'),
            'goodbye': data.get('goodbye', 'Goodbye and take care!'),
            'status': 'active'
        }).execute()
        
        agent = agent_result.data[0]
        
        # Provision phone number
        try:
            phone = await provision_phone_for_agent(agent['id'], area_code)
            agent['phone_number'] = phone
        except Exception as e:
            # If phone provisioning fails, still return agent but with error
            agent['phone_number'] = None
            agent['phone_error'] = str(e)
        
        return agent
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{agent_id}", summary="Get agent")
async def get_agent(agent_id: int):
    """Gets agent by ID with phone number"""
    try:
        db = supabase()
        
        # Get agent
        agent = db.table('agent').select('*').eq('id', agent_id).single().execute()
        if not agent.data:
            raise HTTPException(status_code=404, detail="Agent not found")
        
        # Get phone number
        phone = db.table('phone_number').select('*').eq('agent_id', agent_id).execute()
        
        result = agent.data
        result['phone_number'] = phone.data[0] if phone.data else None
        
        return result
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/business/{business_id}/agent", summary="Get agent for business")
async def get_agent_by_business(business_id: int):
    """Gets agent for a business"""
    try:
        db = supabase()
        
        # Get agent
        agent = db.table('agent').select('*').eq('business_id', business_id).execute()
        if not agent.data:
            raise HTTPException(status_code=404, detail="No agent found for this business")
        
        agent_data = agent.data[0]
        
        # Get phone number
        phone = db.table('phone_number').select('*').eq('agent_id', agent_data['id']).execute()
        agent_data['phone_number'] = phone.data[0] if phone.data else None
        
        return agent_data
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/{agent_id}", summary="Update agent")
async def update_agent(agent_id: int, request: Request):
    """Updates agent configuration"""
    try:
        db = supabase()
        data = await request.json()
        
        # Build update dict
        update_data = {}
        allowed_fields = ['name', 'model_type', 'temperature', 'voice_model', 'prompt', 'greeting', 'goodbye', 'status']
        
        for field in allowed_fields:
            if field in data:
                update_data[field] = data[field]
        
        if not update_data:
            raise HTTPException(status_code=400, detail="No fields to update")
        
        update_data['updated_at'] = 'now()'
        
        result = db.table('agent').update(update_data).eq('id', agent_id).execute()
        
        if not result.data:
            raise HTTPException(status_code=404, detail="Agent not found")
        
        return result.data[0]
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{agent_id}", summary="Delete agent")
async def delete_agent(agent_id: int):
    """Deletes agent and releases phone number"""
    try:
        db = supabase()
        
        # Get phone number to release from Twilio
        phone = db.table('phone_number').select('*').eq('agent_id', agent_id).execute()
        
        if phone.data:
            try:
                client = Client(os.getenv("ACCOUNT_SID"), os.getenv("AUTH_TOKEN"))
                # Find and release the Twilio number
                numbers = client.incoming_phone_numbers.list(phone_number=phone.data[0]['phone_number'])
                if numbers:
                    numbers[0].delete()
            except Exception as e:
                print(f"Failed to release Twilio number: {e}")
        
        # Delete agent (CASCADE will delete phone_number, conversations, etc.)
        db.table('agent').delete().eq('id', agent_id).execute()
        
        return {"success": True}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

