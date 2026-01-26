# api/crud/billing.py

import os
import stripe
from fastapi import APIRouter, HTTPException, Depends, Request, Header
from pydantic import BaseModel
from typing import Optional
from ..auth import get_current_user, AuthenticatedUser
from ..database import get_service_client

router = APIRouter(prefix="/billing", tags=["Billing"])

# Initialize Stripe
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_SIGNING_SECRET")
STRIPE_PRICE_ID = os.getenv("STRIPE_PRICE_ID")  # $5/mo base price
STRIPE_METERED_PRICE_ID = os.getenv("STRIPE_METERED_PRICE_ID")  # $0.10/min overage
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://helloml.app")


class CheckoutRequest(BaseModel):
    business_id: int


class PortalRequest(BaseModel):
    business_id: int


@router.post("/checkout", summary="Create checkout session")
async def create_checkout_session(
    request: CheckoutRequest,
    current_user: AuthenticatedUser = Depends(get_current_user)
):
    """
    Creates a Stripe Checkout session for subscribing a business.
    Returns the checkout URL to redirect the user to.
    """
    try:
        db = current_user.get_db()

        # Verify user owns the business
        result = db.table('business').select('*').eq('id', request.business_id).single().execute()

        if not result.data:
            raise HTTPException(status_code=404, detail="Business not found")

        business = result.data

        # Check if business already has an active subscription
        sub_result = db.table('subscription').select('*').eq('business_id', request.business_id).eq('status', 'active').execute()

        if sub_result.data:
            raise HTTPException(status_code=400, detail="Business already has an active subscription")

        # Get or create Stripe customer
        customer_id = business.get('stripe_customer_id')

        if not customer_id:
            # Create new Stripe customer
            customer = stripe.Customer.create(
                email=current_user.email,
                metadata={
                    'business_id': str(request.business_id),
                    'user_id': current_user.id
                }
            )
            customer_id = customer.id

            # Save customer ID to business using service client to bypass RLS for update
            service_db = get_service_client()
            service_db.table('business').update({
                'stripe_customer_id': customer_id
            }).eq('id', request.business_id).execute()

        # Build line items - base price only
        # Metered billing (overage) is handled via Stripe Billing Meter events, not checkout
        line_items = [
            {
                'price': STRIPE_PRICE_ID,
                'quantity': 1,
            },
        ]

        # Create checkout session with base subscription
        checkout_session = stripe.checkout.Session.create(
            customer=customer_id,
            payment_method_types=['card'],
            line_items=line_items,
            mode='subscription',
            success_url=f"{FRONTEND_URL}/business/{request.business_id}?checkout=success",
            cancel_url=f"{FRONTEND_URL}/business/{request.business_id}?checkout=canceled",
            metadata={
                'business_id': str(request.business_id),
                'user_id': current_user.id
            },
            subscription_data={
                'metadata': {
                    'business_id': str(request.business_id),
                    'user_id': current_user.id
                }
            }
        )

        return {"checkout_url": checkout_session.url, "session_id": checkout_session.id}

    except stripe.error.StripeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/portal", summary="Create billing portal session")
async def create_portal_session(
    request: PortalRequest,
    current_user: AuthenticatedUser = Depends(get_current_user)
):
    """
    Creates a Stripe Billing Portal session for managing subscription.
    Returns the portal URL to redirect the user to.
    """
    try:
        db = current_user.get_db()

        # Verify user owns the business
        result = db.table('business').select('*').eq('id', request.business_id).single().execute()

        if not result.data:
            raise HTTPException(status_code=404, detail="Business not found")

        business = result.data
        customer_id = business.get('stripe_customer_id')

        if not customer_id:
            raise HTTPException(status_code=400, detail="No billing account found for this business")

        # Create portal session
        portal_session = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=f"{FRONTEND_URL}/business/{request.business_id}"
        )

        return {"portal_url": portal_session.url}

    except stripe.error.StripeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/subscription/{business_id}", summary="Get subscription status")
async def get_subscription(
    business_id: int,
    current_user: AuthenticatedUser = Depends(get_current_user)
):
    """
    Gets the subscription status for a business.
    Always syncs with Stripe to ensure data is current.
    Returns subscription details if active, or null if no subscription.
    """
    try:
        db = current_user.get_db()

        # Verify user owns the business (RLS will handle this)
        biz_result = db.table('business').select('*').eq('id', business_id).single().execute()

        if not biz_result.data:
            raise HTTPException(status_code=404, detail="Business not found")

        # Get subscription from DB
        result = db.table('subscription').select('*').eq('business_id', business_id).order('created_at', desc=True).limit(1).execute()

        if not result.data:
            return {"subscription": None, "has_active_subscription": False}

        subscription = result.data[0]
        stripe_sub_id = subscription.get('stripe_subscription_id')

        # Sync from Stripe to get latest status
        if stripe_sub_id:
            try:
                stripe_sub = stripe.Subscription.retrieve(stripe_sub_id)

                # Update DB with latest from Stripe
                service_db = get_service_client()
                service_db.table('subscription').update({
                    'status': stripe_sub.status,
                    'current_period_start': stripe_sub.current_period_start,
                    'current_period_end': stripe_sub.current_period_end,
                    'cancel_at_period_end': stripe_sub.cancel_at_period_end,
                    'updated_at': 'now()'
                }).eq('stripe_subscription_id', stripe_sub_id).execute()

                # Update local subscription dict with synced values
                subscription['status'] = stripe_sub.status
                subscription['cancel_at_period_end'] = stripe_sub.cancel_at_period_end
                subscription['current_period_start'] = stripe_sub.current_period_start
                subscription['current_period_end'] = stripe_sub.current_period_end

            except stripe.error.StripeError:
                # If Stripe call fails, continue with DB data
                pass

        return {
            "subscription": subscription,
            "has_active_subscription": subscription['status'] == 'active'
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/usage/{business_id}", summary="Get usage stats for billing period")
async def get_usage(
    business_id: int,
    current_user: AuthenticatedUser = Depends(get_current_user)
):
    """
    Gets usage statistics for the current billing period.
    Returns minutes used, included minutes, and overage.
    """
    try:
        db = current_user.get_db()

        # Get subscription to find billing period
        sub_result = db.table('subscription').select('*').eq('business_id', business_id).eq('status', 'active').execute()

        included_minutes = 100  # Base plan includes 100 minutes
        period_start = None
        period_end = None

        if sub_result.data:
            subscription = sub_result.data[0]
            period_start = subscription.get('current_period_start')
            period_end = subscription.get('current_period_end')

        # Get agent for this business
        agent_result = db.table('agent').select('id').eq('business_id', business_id).execute()

        if not agent_result.data:
            return {
                "minutes_used": 0,
                "included_minutes": included_minutes,
                "overage_minutes": 0,
                "period_start": period_start,
                "period_end": period_end
            }

        agent_id = agent_result.data[0]['id']

        # Calculate total minutes from completed conversations in this billing period
        query = db.table('conversation').select('started_at, ended_at').eq('agent_id', agent_id).eq('status', 'completed').not_.is_('ended_at', 'null')

        if period_start:
            query = query.gte('started_at', period_start)

        conversations = query.execute()

        # Calculate total minutes
        total_seconds = 0
        for conv in conversations.data:
            if conv.get('started_at') and conv.get('ended_at'):
                from datetime import datetime
                start = datetime.fromisoformat(conv['started_at'].replace('Z', '+00:00'))
                end = datetime.fromisoformat(conv['ended_at'].replace('Z', '+00:00'))
                duration = (end - start).total_seconds()
                total_seconds += max(0, duration)

        minutes_used = round(total_seconds / 60, 1)
        overage_minutes = max(0, minutes_used - included_minutes)

        return {
            "minutes_used": minutes_used,
            "included_minutes": included_minutes,
            "overage_minutes": overage_minutes,
            "period_start": period_start,
            "period_end": period_end
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/webhook", summary="Stripe webhook handler")
async def stripe_webhook(
    request: Request,
    stripe_signature: str = Header(None, alias="Stripe-Signature")
):
    """
    Handles Stripe webhook events.
    This endpoint is called by Stripe when subscription events occur.
    """
    try:
        payload = await request.body()

        if not STRIPE_WEBHOOK_SECRET:
            raise HTTPException(status_code=500, detail="Webhook secret not configured")

        # Verify webhook signature
        try:
            event = stripe.Webhook.construct_event(
                payload, stripe_signature, STRIPE_WEBHOOK_SECRET
            )
        except stripe.error.SignatureVerificationError:
            raise HTTPException(status_code=400, detail="Invalid signature")

        # Use service client for webhook handling (no user context)
        db = get_service_client()

        # Handle specific event types
        if event['type'] == 'checkout.session.completed':
            session = event['data']['object']
            await handle_checkout_completed(db, session)

        elif event['type'] == 'customer.subscription.created':
            subscription = event['data']['object']
            await handle_subscription_created(db, subscription)

        elif event['type'] == 'customer.subscription.updated':
            subscription = event['data']['object']
            await handle_subscription_updated(db, subscription)

        elif event['type'] == 'customer.subscription.deleted':
            subscription = event['data']['object']
            await handle_subscription_deleted(db, subscription)

        elif event['type'] == 'invoice.payment_failed':
            invoice = event['data']['object']
            await handle_payment_failed(db, invoice)

        elif event['type'] == 'charge.failed':
            charge = event['data']['object']
            await handle_charge_failed(db, charge)

        return {"received": True}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


async def handle_checkout_completed(db, session):
    """Handle successful checkout completion"""
    business_id = session.get('metadata', {}).get('business_id')
    subscription_id = session.get('subscription')
    customer_id = session.get('customer')

    if not business_id or not subscription_id:
        return

    # Fetch full subscription details from Stripe
    subscription = stripe.Subscription.retrieve(subscription_id)

    # Create subscription record
    db.table('subscription').insert({
        'business_id': int(business_id),
        'stripe_subscription_id': subscription_id,
        'stripe_customer_id': customer_id,
        'status': subscription.status,
        'current_period_start': subscription.current_period_start,
        'current_period_end': subscription.current_period_end,
        'cancel_at_period_end': subscription.cancel_at_period_end
    }).execute()


async def handle_subscription_created(db, subscription):
    """Handle subscription creation (may come after checkout.session.completed)"""
    business_id = subscription.get('metadata', {}).get('business_id')

    if not business_id:
        return

    # Check if subscription record already exists
    existing = db.table('subscription').select('id').eq('stripe_subscription_id', subscription.id).execute()

    if existing.data:
        return  # Already created via checkout.session.completed

    # Create subscription record
    db.table('subscription').insert({
        'business_id': int(business_id),
        'stripe_subscription_id': subscription.id,
        'stripe_customer_id': subscription.customer,
        'status': subscription.status,
        'current_period_start': subscription.current_period_start,
        'current_period_end': subscription.current_period_end,
        'cancel_at_period_end': subscription.cancel_at_period_end
    }).execute()


async def handle_subscription_updated(db, subscription):
    """Handle subscription updates (status changes, renewals, etc.)"""
    db.table('subscription').update({
        'status': subscription.status,
        'current_period_start': subscription.current_period_start,
        'current_period_end': subscription.current_period_end,
        'cancel_at_period_end': subscription.cancel_at_period_end,
        'updated_at': 'now()'
    }).eq('stripe_subscription_id', subscription.id).execute()


async def handle_subscription_deleted(db, subscription):
    """Handle subscription cancellation"""
    db.table('subscription').update({
        'status': 'canceled',
        'updated_at': 'now()'
    }).eq('stripe_subscription_id', subscription.id).execute()


async def handle_payment_failed(db, invoice):
    """Handle failed payment from invoice"""
    subscription_id = invoice.get('subscription')

    if subscription_id:
        db.table('subscription').update({
            'status': 'past_due',
            'updated_at': 'now()'
        }).eq('stripe_subscription_id', subscription_id).execute()


async def handle_charge_failed(db, charge):
    """Handle failed charge"""
    customer_id = charge.get('customer')

    if customer_id:
        # Mark any active subscriptions for this customer as past_due
        db.table('subscription').update({
            'status': 'past_due',
            'updated_at': 'now()'
        }).eq('stripe_customer_id', customer_id).eq('status', 'active').execute()


STRIPE_METER_EVENT_NAME = os.getenv("STRIPE_METER_NAME", "call_minutes")


class UsageReportRequest(BaseModel):
    business_id: int
    minutes: float


@router.post("/report-usage", summary="Report call minutes usage")
async def report_usage(
    request: UsageReportRequest,
    current_user: AuthenticatedUser = Depends(get_current_user)
):
    """
    Reports usage for metered billing (call minutes).
    This should be called after each call ends.
    Uses Stripe Billing Meter Events API.
    """
    try:
        db = current_user.get_db()

        # Verify user owns the business and has active subscription
        result = db.table('subscription').select('stripe_customer_id, stripe_subscription_id').eq('business_id', request.business_id).eq('status', 'active').single().execute()

        if not result.data:
            # No active subscription - don't report usage (within free tier or not subscribed)
            return {"reported": False, "reason": "No active subscription found"}

        customer_id = result.data['stripe_customer_id']

        # Report usage via Stripe Billing Meter Events
        # This uses the meter configured on the metered price
        import time
        meter_event = stripe.billing.MeterEvent.create(
            event_name=STRIPE_METER_EVENT_NAME,
            payload={
                "stripe_customer_id": customer_id,
                "value": str(int(request.minutes)),  # Minutes as integer
            },
            timestamp=int(time.time())
        )

        return {"reported": True, "minutes": request.minutes, "event_id": meter_event.identifier}

    except stripe.error.StripeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
