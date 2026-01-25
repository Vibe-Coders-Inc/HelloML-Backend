# api/crud/conversation.py

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional
from ..auth import get_current_user, AuthenticatedUser

router = APIRouter(prefix="/conversation", tags=["Conversation"])


class ConversationEnd(BaseModel):
    status: str = "completed"


@router.get("/{conversation_id}", summary="Get conversation details")
async def get_conversation(
    conversation_id: int,
    include_messages: bool = False,
    current_user: AuthenticatedUser = Depends(get_current_user)
):
    """Gets conversation by ID with optional messages - user must own the conversation"""
    try:
        db = current_user.get_db()

        # RLS will filter to owned conversations
        conv = db.table('conversation').select('*').eq('id', conversation_id).single().execute()

        if not conv.data:
            raise HTTPException(status_code=404, detail="Conversation not found")

        result = conv.data

        if include_messages:
            messages = db.table('message')\
                .select('*')\
                .eq('conversation_id', conversation_id)\
                .order('created_at')\
                .execute()
            result['messages'] = messages.data

        return result

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/agent/{agent_id}/list", summary="List conversations for agent")
async def list_conversations(
    agent_id: int,
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    current_user: AuthenticatedUser = Depends(get_current_user)
):
    """Lists conversations for an agent - user must own the agent"""
    try:
        db = current_user.get_db()

        # RLS will filter to owned agents/conversations
        query = db.table('conversation').select('*').eq('agent_id', agent_id)

        if status:
            query = query.eq('status', status)

        result = query.order('started_at', desc=True).range(offset, offset + limit - 1).execute()

        return {
            "conversations": result.data,
            "count": len(result.data),
            "limit": limit,
            "offset": offset
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/business/{business_id}/list", summary="List conversations for business")
async def list_conversations_by_business(
    business_id: int,
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    current_user: AuthenticatedUser = Depends(get_current_user)
):
    """Lists all conversations for a business - user must own the business"""
    try:
        db = current_user.get_db()

        # Get agent for business (RLS will filter)
        agent = db.table('agent').select('id').eq('business_id', business_id).execute()

        if not agent.data:
            raise HTTPException(status_code=404, detail="No agent found for business")

        agent_id = agent.data[0]['id']

        query = db.table('conversation').select('*').eq('agent_id', agent_id)

        if status:
            query = query.eq('status', status)

        result = query.order('started_at', desc=True).range(offset, offset + limit - 1).execute()

        return {
            "conversations": result.data,
            "count": len(result.data),
            "limit": limit,
            "offset": offset
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{conversation_id}/messages", summary="Get messages for conversation")
async def get_messages(
    conversation_id: int,
    current_user: AuthenticatedUser = Depends(get_current_user)
):
    """Gets all messages for a conversation - user must own the conversation"""
    try:
        db = current_user.get_db()

        # Verify conversation access (RLS)
        conv = db.table('conversation').select('id').eq('id', conversation_id).execute()
        if not conv.data:
            raise HTTPException(status_code=404, detail="Conversation not found")

        messages = db.table('message')\
            .select('*')\
            .eq('conversation_id', conversation_id)\
            .order('created_at')\
            .execute()

        return {
            "conversation_id": conversation_id,
            "messages": messages.data,
            "count": len(messages.data)
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/{conversation_id}/end", summary="End conversation")
async def end_conversation(
    conversation_id: int,
    data: ConversationEnd,
    current_user: AuthenticatedUser = Depends(get_current_user)
):
    """Marks conversation as completed or failed - user must own the conversation"""
    try:
        db = current_user.get_db()

        status = data.status

        if status not in ['completed', 'failed', 'cancelled']:
            raise HTTPException(status_code=400, detail="Invalid status")

        # RLS will filter
        result = db.table('conversation').update({
            'status': status,
            'ended_at': 'now()'
        }).eq('id', conversation_id).execute()

        if not result.data:
            raise HTTPException(status_code=404, detail="Conversation not found")

        return result.data[0]

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{conversation_id}", summary="Delete conversation")
async def delete_conversation(
    conversation_id: int,
    current_user: AuthenticatedUser = Depends(get_current_user)
):
    """Deletes conversation and all messages - user must own the conversation"""
    try:
        db = current_user.get_db()

        # RLS will filter
        db.table('conversation').delete().eq('id', conversation_id).execute()

        return {"success": True, "message": "Conversation deleted"}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/agent/{agent_id}/stats", summary="Get conversation stats for agent")
async def get_agent_stats(
    agent_id: int,
    current_user: AuthenticatedUser = Depends(get_current_user)
):
    """Gets conversation statistics for an agent - user must own the agent"""
    try:
        db = current_user.get_db()

        # RLS will filter
        all_convs = db.table('conversation').select('status').eq('agent_id', agent_id).execute()

        stats = {
            'total': len(all_convs.data),
            'in_progress': 0,
            'completed': 0,
            'failed': 0,
            'cancelled': 0
        }

        for conv in all_convs.data:
            status = conv['status']
            if status in stats:
                stats[status] += 1

        recent = db.table('conversation')\
            .select('*')\
            .eq('agent_id', agent_id)\
            .order('started_at', desc=True)\
            .limit(10)\
            .execute()

        return {
            "agent_id": agent_id,
            "stats": stats,
            "recent_conversations": recent.data
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
