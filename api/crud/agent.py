# api/crud/agent.py

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional
from twilio.rest import Client
import os
from ..database import get_service_client
from ..auth import get_current_user, AuthenticatedUser

router = APIRouter(prefix="/agent", tags=["Agent"])


class AgentCreate(BaseModel):
    business_id: int
    area_code: str
    name: Optional[str] = "Agent"
    model_type: Optional[str] = "gpt-realtime-2025-08-28"
    temperature: Optional[float] = 0.7
    prompt: Optional[str] = None
    greeting: Optional[str] = "Hello There!"
    goodbye: Optional[str] = "Goodbye and take care!"
    voice_model: Optional[str] = "ash"


class AgentUpdate(BaseModel):
    name: Optional[str] = None
    model_type: Optional[str] = None
    temperature: Optional[float] = None
    prompt: Optional[str] = None
    greeting: Optional[str] = None
    goodbye: Optional[str] = None
    status: Optional[str] = None
    voice_model: Optional[str] = None


async def provision_phone_for_agent(agent_id: int, area_code: str):
    """Internal function to provision phone number for agent (uses service client)"""
    db = get_service_client()

    existing_phone = db.table('phone_number').select('*').eq('agent_id', agent_id).execute()
    if existing_phone.data:
        return existing_phone.data[0]

    client = Client(os.getenv("ACCOUNT_SID"), os.getenv("AUTH_TOKEN"))

    available = client.available_phone_numbers('US').local.list(
        area_code=area_code,
        limit=1
    )

    if not available:
        raise Exception(f"No numbers available in area code {area_code}")

    base_url = os.getenv("API_BASE_URL", "https://api.helloml.app")
    webhook_url = f"{base_url}/conversation/{agent_id}/voice"

    number = client.incoming_phone_numbers.create(
        phone_number=available[0].phone_number,
        voice_url=webhook_url,
        voice_method='POST'
    )

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
async def create_agent(
    agent: AgentCreate,
    current_user: AuthenticatedUser = Depends(get_current_user)
):
    """Creates agent and provisions phone number - user must own the business"""
    try:
        db = current_user.get_db()

        business_id = agent.business_id
        area_code = agent.area_code

        # With RLS, this will fail if user doesn't own the business
        business = db.table('business').select('*').eq('id', business_id).single().execute()
        if not business.data:
            raise HTTPException(status_code=404, detail="Business not found or access denied")

        # Check if business already has an agent
        existing_agent = db.table('agent').select('*').eq('business_id', business_id).execute()
        if existing_agent.data:
            raise HTTPException(status_code=400, detail="Business already has an agent")

        # Create agent
        agent_result = db.table('agent').insert({
            'business_id': business_id,
            'name': agent.name,
            'model_type': agent.model_type,
            'temperature': agent.temperature,
            'prompt': agent.prompt,
            'greeting': agent.greeting,
            'goodbye': agent.goodbye,
            'voice_model': agent.voice_model,
            'status': 'active'
        }).execute()

        agent_data = agent_result.data[0]

        # Provision phone number (uses service client internally)
        try:
            phone = await provision_phone_for_agent(agent_data['id'], area_code)
            agent_data['phone_number'] = phone
        except Exception as e:
            agent_data['phone_number'] = None
            agent_data['phone_error'] = str(e)

        return agent_data

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{agent_id}", summary="Get agent")
async def get_agent(
    agent_id: int,
    current_user: AuthenticatedUser = Depends(get_current_user)
):
    """Gets agent by ID with phone number - user must own the agent"""
    try:
        db = current_user.get_db()

        # With RLS, only returns if user owns it
        agent = db.table('agent').select('*').eq('id', agent_id).single().execute()
        if not agent.data:
            raise HTTPException(status_code=404, detail="Agent not found")

        phone = db.table('phone_number').select('*').eq('agent_id', agent_id).execute()

        result = agent.data
        result['phone_number'] = phone.data[0] if phone.data else None

        return result

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/business/{business_id}/agent", summary="Get agent for business")
async def get_agent_by_business(
    business_id: int,
    current_user: AuthenticatedUser = Depends(get_current_user)
):
    """Gets agent for a business - user must own the business"""
    try:
        db = current_user.get_db()

        # With RLS, only returns if user owns it
        agent = db.table('agent').select('*').eq('business_id', business_id).execute()
        if not agent.data:
            raise HTTPException(status_code=404, detail="No agent found for this business")

        agent_data = agent.data[0]

        phone = db.table('phone_number').select('*').eq('agent_id', agent_data['id']).execute()
        agent_data['phone_number'] = phone.data[0] if phone.data else None

        return agent_data

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/{agent_id}", summary="Update agent")
async def update_agent(
    agent_id: int,
    agent: AgentUpdate,
    current_user: AuthenticatedUser = Depends(get_current_user)
):
    """Updates agent configuration - user must own the agent"""
    try:
        db = current_user.get_db()

        update_data = agent.model_dump(exclude_unset=True)

        if not update_data:
            raise HTTPException(status_code=400, detail="No fields to update")

        if 'prompt' in update_data and (update_data['prompt'] is None or update_data['prompt'].strip() == ''):
            print(f"[WARNING] Agent {agent_id}: Prompt set to empty.")

        print(f"[Agent Update] Agent {agent_id}: Updating fields: {list(update_data.keys())}")

        update_data['updated_at'] = 'now()'

        # With RLS, only succeeds if user owns it
        result = db.table('agent').update(update_data).eq('id', agent_id).execute()

        if not result.data:
            raise HTTPException(status_code=404, detail="Agent not found")

        print(f"[Agent Update] Agent {agent_id}: Update successful")

        return result.data[0]

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{agent_id}", summary="Delete agent")
async def delete_agent(
    agent_id: int,
    current_user: AuthenticatedUser = Depends(get_current_user)
):
    """Deletes agent and releases phone number - user must own the agent"""
    try:
        db = current_user.get_db()

        # First verify we can see this agent (RLS check)
        agent_check = db.table('agent').select('id').eq('id', agent_id).execute()
        if not agent_check.data:
            raise HTTPException(status_code=404, detail="Agent not found")

        # Get phone number to release from Twilio (use service client)
        service_db = get_service_client()
        phone = service_db.table('phone_number').select('*').eq('agent_id', agent_id).execute()

        if phone.data:
            try:
                client = Client(os.getenv("ACCOUNT_SID"), os.getenv("AUTH_TOKEN"))
                numbers = client.incoming_phone_numbers.list(phone_number=phone.data[0]['phone_number'])
                if numbers:
                    numbers[0].delete()
            except Exception as e:
                print(f"Failed to release Twilio number: {e}")

        # Delete agent (with RLS)
        db.table('agent').delete().eq('id', agent_id).execute()

        return {"success": True}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
