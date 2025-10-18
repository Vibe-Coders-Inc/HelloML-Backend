# api/crud/business.py

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from ..database import supabase

router = APIRouter(prefix="/business", tags=["Business"])

class BusinessCreate(BaseModel):
    owner_user_id: str
    name: str
    address: str
    phone_number: Optional[str] = None
    business_email: Optional[str] = None

class BusinessUpdate(BaseModel):
    name: Optional[str] = None
    address: Optional[str] = None
    phone_number: Optional[str] = None
    business_email: Optional[str] = None

@router.post("", summary="Create business")
async def create_business(business: BusinessCreate):
    """Creates a new business"""
    try:
        db = supabase()
        
        # Insert business
        result = db.table('business').insert({
            'owner_user_id': business.owner_user_id,
            'name': business.name,
            'phone_number': business.phone_number,
            'business_email': business.business_email,
            'address': business.address
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
async def update_business(business_id: int, business: BusinessUpdate):
    """Updates business information"""
    try:
        db = supabase()
        
        # Only update fields that are provided
        update_data = business.model_dump(exclude_unset=True)
        
        if not update_data:
            raise HTTPException(status_code=400, detail="No fields to update")
        
        result = db.table('business').update(update_data).eq('id', business_id).execute()
        
        if not result.data:
            raise HTTPException(status_code=404, detail="Business not found")
        
        return result.data[0]
        
    except HTTPException:
        raise
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
