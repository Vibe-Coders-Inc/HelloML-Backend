# api/crud/business.py

from fastapi import APIRouter, HTTPException, Request
from ..database import supabase

router = APIRouter(prefix="/business", tags=["Business"])

@router.post("", summary="Create business")
async def create_business(request: Request):
    """Creates a new business"""
    try:
        db = supabase()
        data = await request.json()
        
        # Insert business
        result = db.table('business').insert({
            'owner_user_id': data['owner_user_id'],
            'name': data['name'],
            'phone_number': data.get('phone_number'),
            'business_email': data.get('business_email'),
            'address': data['address']
        }).execute()
        
        return result.data[0]
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{business_id}", summary="Get business")
async def get_business(business_id: int):
    """Gets business by ID"""
    try:
        db = supabase()
        
        result = db.table('business').select('*').eq('id', business_id).single().execute()
        
        if not result.data:
            raise HTTPException(status_code=404, detail="Business not found")
        
        return result.data
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("", summary="List businesses for owner")
async def list_businesses(owner_user_id: str):
    """Lists all businesses for an owner"""
    try:
        db = supabase()
        
        result = db.table('business').select('*').eq('owner_user_id', owner_user_id).execute()
        
        return result.data
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/{business_id}", summary="Update business")
async def update_business(business_id: int, request: Request):
    """Updates business information"""
    try:
        db = supabase()
        data = await request.json()
        
        result = db.table('business').update(data).eq('id', business_id).execute()
        
        if not result.data:
            raise HTTPException(status_code=404, detail="Business not found")
        
        return result.data[0]
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{business_id}", summary="Delete business")
async def delete_business(business_id: int):
    """Deletes business and all associated data"""
    try:
        db = supabase()
        
        db.table('business').delete().eq('id', business_id).execute()
        
        return {"success": True}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
