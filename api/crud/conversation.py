# api/crud/conversation.py

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from ..database import supabase

router = APIRouter(prefix="/conversation", tags=["Conversation"])

class ConversationEnd(BaseModel):
    status: str = "completed"

@router.get("/{conversation_id}", summary="Get conversation details")
async def get_conversation(conversation_id: int, include_messages: bool = False):
    """Gets conversation by ID with optional messages"""
    try:
        db = supabase()
        
        # Get conversation
        conv = db.table('conversation').select('*').eq('id', conversation_id).single().execute()
        
        if not conv.data:
            raise HTTPException(status_code=404, detail="Conversation not found")
        
        result = conv.data
        
        # Include messages if requested
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
    offset: int = 0
):
    """Lists conversations for an agent with filters"""
    try:
        db = supabase()
        
        # Build query
        query = db.table('conversation').select('*').eq('agent_id', agent_id)
        
        # Apply status filter if provided
        if status:
            query = query.eq('status', status)
        
        # Order by started_at descending, apply pagination
        result = query.order('started_at', desc=True).range(offset, offset + limit - 1).execute()
        
        return {
            "conversations": result.data,
            "count": len(result.data),
            "limit": limit,
            "offset": offset
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/business/{business_id}/list", summary="List conversations for business")
async def list_conversations_by_business(
    business_id: int,
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0
):
    """Lists all conversations for a business"""
    try:
        db = supabase()
        
        # Get agent for business
        agent = db.table('agent').select('id').eq('business_id', business_id).execute()
        
        if not agent.data:
            raise HTTPException(status_code=404, detail="No agent found for business")
        
        agent_id = agent.data[0]['id']
        
        # Get conversations
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
async def get_messages(conversation_id: int):
    """Gets all messages for a conversation"""
    try:
        db = supabase()
        
        # Check if conversation exists
        conv = db.table('conversation').select('id').eq('id', conversation_id).execute()
        if not conv.data:
            raise HTTPException(status_code=404, detail="Conversation not found")
        
        # Get messages
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
async def end_conversation(conversation_id: int, data: ConversationEnd):
    """Marks conversation as completed or failed"""
    try:
        db = supabase()
        
        status = data.status
        
        if status not in ['completed', 'failed', 'cancelled']:
            raise HTTPException(status_code=400, detail="Invalid status")
        
        # Update conversation
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
async def delete_conversation(conversation_id: int):
    """Deletes conversation and all messages"""
    try:
        db = supabase()
        
        # Delete (CASCADE will delete messages)
        result = db.table('conversation').delete().eq('id', conversation_id).execute()
        
        return {"success": True, "message": "Conversation deleted"}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/agent/{agent_id}/stats", summary="Get conversation stats for agent")
async def get_agent_stats(agent_id: int):
    """Gets conversation statistics for an agent"""
    try:
        db = supabase()
        
        # Get all conversations
        all_convs = db.table('conversation').select('status').eq('agent_id', agent_id).execute()
        
        # Count by status
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
        
        # Get recent conversations
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
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

