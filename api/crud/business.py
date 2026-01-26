# api/crud/business.py

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional
import stripe
import os
from ..auth import get_current_user, AuthenticatedUser, verify_business_ownership

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

router = APIRouter(prefix="/business", tags=["Business"])


class BusinessCreate(BaseModel):
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
async def create_business(
    business: BusinessCreate,
    current_user: AuthenticatedUser = Depends(get_current_user)
):
    """Creates a new business for the authenticated user"""
    try:
        db = current_user.get_db()

        result = db.table('business').insert({
            'owner_user_id': current_user.id,
            'name': business.name,
            'phone_number': business.phone_number,
            'business_email': business.business_email,
            'address': business.address
        }).execute()

        return result.data[0]

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{business_id}", summary="Get business")
async def get_business(
    business_id: int,
    current_user: AuthenticatedUser = Depends(get_current_user)
):
    """Gets business by ID - user must own the business"""
    try:
        db = current_user.get_db()

        # With RLS enabled, this will only return if user owns it
        result = db.table('business').select('*').eq('id', business_id).single().execute()

        if not result.data:
            raise HTTPException(status_code=404, detail="Business not found")

        return result.data

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("", summary="List businesses for owner")
async def list_businesses(
    current_user: AuthenticatedUser = Depends(get_current_user)
):
    """Lists all businesses for the authenticated user"""
    try:
        db = current_user.get_db()

        # With RLS enabled, this automatically filters to user's businesses
        result = db.table('business').select('*').execute()

        return result.data

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/{business_id}", summary="Update business")
async def update_business(
    business_id: int,
    business: BusinessUpdate,
    current_user: AuthenticatedUser = Depends(get_current_user)
):
    """Updates business information - user must own the business"""
    try:
        db = current_user.get_db()

        update_data = business.model_dump(exclude_unset=True)

        if not update_data:
            raise HTTPException(status_code=400, detail="No fields to update")

        # With RLS enabled, this will only succeed if user owns the business
        result = db.table('business').update(update_data).eq('id', business_id).execute()

        if not result.data:
            raise HTTPException(status_code=404, detail="Business not found")

        return result.data[0]

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{business_id}", summary="Delete business")
async def delete_business(
    business_id: int,
    current_user: AuthenticatedUser = Depends(get_current_user)
):
    """Deletes business and all associated data - user must own the business"""
    try:
        db = current_user.get_db()

        # First, get the business to check for stripe_customer_id
        business_result = db.table('business').select('*').eq('id', business_id).single().execute()
        if not business_result.data:
            raise HTTPException(status_code=404, detail="Business not found")

        business = business_result.data
        stripe_customer_id = business.get('stripe_customer_id')

        # Check Stripe directly for active subscriptions
        if stripe_customer_id:
            try:
                stripe_subs = stripe.Subscription.list(
                    customer=stripe_customer_id,
                    status='active',
                    limit=1
                )
                if stripe_subs.data:
                    raise HTTPException(
                        status_code=400,
                        detail="Cannot delete business with active subscription. Please cancel your subscription first."
                    )
            except stripe.error.StripeError as e:
                print(f"[BUSINESS] Stripe check failed during delete: {str(e)}")
                # Continue with delete if Stripe check fails - don't block on Stripe errors

        # With RLS enabled, this will only succeed if user owns the business
        db.table('business').delete().eq('id', business_id).execute()

        return {"success": True}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
