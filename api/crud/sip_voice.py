"""
OpenAI SIP Integration - Webhook handler for incoming SIP calls.

Replaces the Twilio Media Streams WebSocket relay with direct SIP integration.
Audio flows directly between Twilio and OpenAI — our server only handles
webhook events, call acceptance, and function calls via WebSocket monitoring.
"""

import os
import re
import json
import asyncio
import threading
from datetime import datetime
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse
from api.database import get_service_client
from api.rag import semantic_search
from openai import OpenAI, InvalidWebhookSignatureError
import websockets
import requests as http_requests

FREE_TRIAL_MINUTES = 5

router = APIRouter(prefix="/conversation/sip", tags=["SIP Voice"])

# OpenAI webhook client for signature verification
_openai_client = None

def _get_openai_client():
    global _openai_client
    if _openai_client is None:
        webhook_secret = os.getenv("OPENAI_WEBHOOK_SECRET")
        if not webhook_secret:
            raise ValueError("OPENAI_WEBHOOK_SECRET not set")
        _openai_client = OpenAI(webhook_secret=webhook_secret)
    return _openai_client


def _get_auth_header():
    return {"Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}"}


def _lookup_agent_by_phone(db, to_header: str):
    """
    Look up agent by the called phone number from SIP To header.
    To header format: sip:+18005551212@sip.example.com
    """
    # Extract phone number from SIP URI
    match = re.search(r'sip:(\+?\d+)@', to_header)
    if not match:
        print(f"[SIP] Could not extract phone from To header: {to_header}", flush=True)
        return None, None

    called_number = match.group(1)
    print(f"[SIP] Looking up agent for phone: {called_number}", flush=True)

    # Look up in phone_number table
    phone_data = db.table('phone_number').select('agent_id, phone_number').eq(
        'phone_number', called_number
    ).limit(1).execute()

    if not phone_data.data:
        # Build alternative number formats to try
        alternatives = set()
        raw = called_number.lstrip('+')
        alternatives.add(f'+{raw}')       # +18005551212
        alternatives.add(raw)             # 18005551212
        if raw.startswith('1') and len(raw) == 11:
            alternatives.add(raw[1:])         # 8005551212
            alternatives.add(f'+{raw[1:]}')   # +8005551212
        alternatives.discard(called_number)  # already tried
        for alt_number in alternatives:
            phone_data = db.table('phone_number').select('agent_id, phone_number').eq(
                'phone_number', alt_number
            ).limit(1).execute()
            if phone_data.data:
                break

    if not phone_data.data:
        print(f"[SIP] No agent found for phone: {called_number}", flush=True)
        return None, None

    agent_id = phone_data.data[0]['agent_id']
    agent_phone = phone_data.data[0]['phone_number']

    agent_data = db.table('agent').select('*').eq('id', agent_id).single().execute()
    if not agent_data.data:
        return None, None

    return agent_data.data, agent_phone


def _check_trial_exhausted(db, agent_config):
    """Check if trial is exhausted (no active subscription and >= 5 minutes used)."""
    business_id = agent_config.get('business_id')
    if not business_id:
        return False

    sub_data = db.table('subscription').select('status').eq(
        'business_id', business_id
    ).in_('status', ['active', 'trialing']).limit(1).execute()

    if sub_data.data:
        return False

    agent_id = agent_config['id']
    convos = db.table('conversation').select(
        'started_at, ended_at'
    ).eq('agent_id', agent_id).not_.is_('ended_at', 'null').execute()

    total_minutes = 0.0
    for c in (convos.data or []):
        try:
            start = datetime.fromisoformat(c['started_at'].replace('Z', '+00:00'))
            end = datetime.fromisoformat(c['ended_at'].replace('Z', '+00:00'))
            total_minutes += (end - start).total_seconds() / 60.0
        except (ValueError, TypeError, KeyError):
            continue

    if total_minutes >= FREE_TRIAL_MINUTES:
        print(f"[SIP] Trial exhausted for agent {agent_id}: {total_minutes:.1f} min used", flush=True)
        return True
    return False


def _build_session_config(agent_config, business_info, agent_phone, connected_tools, tool_settings):
    """Build the session config for accepting a SIP call (same logic as realtime_manager)."""
    default_prompt = (
        "You are a helpful AI voice assistant.\n"
        "Answer questions using only the uploaded knowledge base documents.\n"
        "Always be polite, professional, and helpful."
    )
    base_instructions = agent_config.get('prompt') or default_prompt
    greeting = agent_config.get('greeting', 'Hello! How can I help you today?')
    goodbye = agent_config.get('goodbye', 'Goodbye! Have a great day!')

    biz = business_info or {}
    context_lines = []
    if biz.get('name'):
        context_lines.append(f"- Business name: {biz['name']}")
    if biz.get('address'):
        context_lines.append(f"- Address: {biz['address']}")
    if biz.get('business_email'):
        context_lines.append(f"- Contact email: {biz['business_email']}")
    if biz.get('phone_number'):
        context_lines.append(f"- Business contact phone: {biz['phone_number']}")
    if agent_phone:
        context_lines.append(f"- Your phone number (the number callers dialed): {agent_phone}")
    business_context = "\n".join(context_lines) if context_lines else "- No business details available."

    # Build tools
    tools = [
        {
            "type": "function",
            "name": "search_knowledge_base",
            "description": "Search the business's uploaded knowledge base documents using semantic similarity.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Natural language search query"}
                },
                "required": ["query"]
            }
        },
        {
            "type": "function",
            "name": "end_call",
            "description": "Terminate the active phone call.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "Brief explanation"}
                },
                "required": ["reason"]
            }
        }
    ]

    if 'google-calendar' in (connected_tools or []):
        tools.append({
            "type": "function",
            "name": "check_calendar",
            "description": "Check availability on a given date.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "Date in YYYY-MM-DD format"}
                },
                "required": ["date"]
            }
        })
        tools.append({
            "type": "function",
            "name": "create_calendar_event",
            "description": "Create a new calendar event.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "Title of the event"},
                    "date": {"type": "string", "description": "Date in YYYY-MM-DD format"},
                    "start_time": {"type": "string", "description": "Start time HH:MM (24h)"},
                    "end_time": {"type": "string", "description": "End time HH:MM (24h)"},
                    "description": {"type": "string", "description": "Optional notes"}
                },
                "required": ["summary", "date", "start_time", "end_time"]
            }
        })

    tool_names = [t["name"] for t in tools]
    tool_list_str = ", ".join(tool_names)

    tool_instructions = """- Before any tool call, say one short line like "Let me check that for you." Then call the tool immediately."""

    if "search_knowledge_base" in tool_names:
        tool_instructions += """

## search_knowledge_base
- Call BEFORE answering any factual question.
- If no results, retry with different search terms (up to 3 attempts).
- NEVER use your general knowledge or training data - only search results.
- After 3 failed searches, say you don't have that information."""

    if "end_call" in tool_names:
        tool_instructions += f"""

## end_call
- Call when the caller says goodbye or the conversation is complete.
- BEFORE calling, say: "{goodbye}" """

    if "check_calendar" in tool_names:
        cal_settings = (tool_settings or {}).get('google-calendar', {})
        default_duration = cal_settings.get('default_duration', 30)
        allow_conflicts = cal_settings.get('allow_conflicts', False)
        booking_window = cal_settings.get('booking_window_days', 30)
        biz_start = cal_settings.get('business_hours_start', '09:00')
        biz_end = cal_settings.get('business_hours_end', '17:00')

        tool_instructions += f"""

## check_calendar
- Call when the caller asks about availability.

## create_calendar_event
- Confirm details with caller BEFORE creating.
- Default duration: {default_duration} min. Business hours: {biz_start}-{biz_end}.
- Booking window: {booking_window} days. {"Conflicts allowed." if allow_conflicts else "No conflicts allowed."}"""

    instructions = f"""# Role & Objective
You are a voice customer service agent for {biz.get('name') or 'a business'}. Help callers by answering questions using ONLY the uploaded knowledge base documents.

# Context
{business_context}

# Capabilities
You have access to the following tools: {tool_list_str}.
You can ONLY perform actions that your tools allow.

# Personality & Tone
Professional, friendly, calm, and approachable. Warm, concise, confident, never fawning. 2-3 sentences per turn.
English only. Vary your responses.

# Initial Greeting
When you see "[Call connected]", say exactly: "{greeting}"
- Say this once, then wait for the caller.

# Unclear Audio
- Only respond to clear audio or text.
- If unclear, ask for clarification.

# Tools
{tool_instructions}

# Instructions
- NEVER answer factual questions without calling search_knowledge_base first.
- Keep responses concise - this is a phone call.
- If you don't know, say so.

{base_instructions}"""

    voice = agent_config.get('voice_model', 'ash')
    model = agent_config.get('model_type') or 'gpt-realtime-1.5'

    return {
        "type": "realtime",
        "model": model,
        "instructions": instructions,
        "tools": tools,
        "tool_choice": "auto",
        "voice": voice,
        "input_audio_transcription": {
            "model": "gpt-4o-mini-transcribe"
        },
        "input_audio_noise_reduction": {
            "type": "near_field"
        },
        "turn_detection": {
            "type": "semantic_vad",
            "eagerness": "low"
        }
    }


async def _websocket_monitor(call_id, agent_config, conversation_id, agent_phone, connected_tools, tool_settings):
    """
    Connect to the Realtime API WebSocket to monitor events and handle function calls.
    Runs in a background thread via asyncio.
    """
    api_key = os.getenv("OPENAI_API_KEY")
    auth_header = {"Authorization": f"Bearer {api_key}"}
    db = get_service_client()
    agent_id = agent_config['id']
    business_id = agent_config.get('business_id')
    greeting = agent_config.get('greeting', 'Hello! How can I help you today?')

    try:
        async with websockets.connect(
            f"wss://api.openai.com/v1/realtime?call_id={call_id}",
            additional_headers=auth_header,
        ) as ws:
            print(f"[SIP-WS] Connected for call {call_id}, conversation {conversation_id}", flush=True)

            # Trigger initial greeting
            await ws.send(json.dumps({
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "[Call connected]"}]
                }
            }))
            await ws.send(json.dumps({"type": "response.create"}))

            current_agent_transcript = ""
            session_start = asyncio.get_event_loop().time()
            MAX_SESSION_DURATION = 3600  # 1 hour

            async for message in ws:
                # Check max session duration
                elapsed = asyncio.get_event_loop().time() - session_start
                if elapsed >= MAX_SESSION_DURATION:
                    print(f"[SIP-WS] Max session duration reached ({MAX_SESSION_DURATION}s), closing call {call_id}", flush=True)
                    try:
                        await asyncio.to_thread(
                            http_requests.post,
                            f"https://api.openai.com/v1/realtime/calls/{call_id}/hangup",
                            headers=_get_auth_header()
                        )
                    except Exception:
                        pass
                    await ws.close()
                    break

                event = json.loads(message)
                event_type = event.get("type")

                # User transcript
                if event_type == "conversation.item.input_audio_transcription.completed":
                    transcript = event.get("transcript", "")
                    if transcript:
                        print(f"[SIP User]: {transcript}", flush=True)
                        try:
                            db.table('message').insert({
                                'conversation_id': conversation_id,
                                'role': 'user',
                                'content': transcript
                            }).execute()
                        except Exception as e:
                            print(f"[SIP-WS] Error saving user message: {e}", flush=True)

                # Agent transcript delta
                elif event_type == "response.output_audio_transcript.delta":
                    current_agent_transcript += event.get("delta", "")

                # Agent transcript done
                elif event_type == "response.output_audio_transcript.done":
                    transcript = event.get("transcript") or current_agent_transcript
                    if transcript:
                        print(f"[SIP Agent]: {transcript}", flush=True)
                        try:
                            db.table('message').insert({
                                'conversation_id': conversation_id,
                                'role': 'agent',
                                'content': transcript
                            }).execute()
                        except Exception as e:
                            print(f"[SIP-WS] Error saving agent message: {e}", flush=True)
                    current_agent_transcript = ""

                # Function call
                elif event_type == "response.output_item.done":
                    item = event.get("item", {})
                    if item.get("type") == "function_call":
                        await _handle_function_call(
                            ws, item, agent_id, conversation_id,
                            business_id, call_id, connected_tools, tool_settings, db
                        )

                # Session events
                elif event_type == "session.created":
                    print(f"[SIP-WS] Session created", flush=True)
                elif event_type == "session.updated":
                    print(f"[SIP-WS] Session updated", flush=True)
                elif event_type == "error":
                    error_obj = event.get("error", {})
                    msg = error_obj.get("message", "")
                    if "already shorter than" not in msg:
                        print(f"[SIP-WS] Error: {error_obj}", flush=True)

    except websockets.exceptions.ConnectionClosed as e:
        print(f"[SIP-WS] Connection closed for call {call_id}: {e}", flush=True)
    except Exception as e:
        print(f"[SIP-WS] Error: {e}", flush=True)
        import traceback
        print(f"[SIP-WS] Traceback: {traceback.format_exc()}", flush=True)
    finally:
        # Mark conversation completed
        try:
            db.table('conversation').update({
                'status': 'completed',
                'ended_at': 'now()'
            }).eq('id', conversation_id).execute()
            print(f"[SIP-WS] Conversation {conversation_id} marked completed", flush=True)
        except Exception as e:
            print(f"[SIP-WS] Error updating conversation: {e}", flush=True)


async def _handle_function_call(ws, item, agent_id, conversation_id, business_id, call_id, connected_tools, tool_settings, db):
    """Handle function calls from the Realtime API."""
    call_fn_id = item.get("call_id")
    function_name = item.get("name")
    arguments_str = item.get("arguments", "{}")
    print(f"[SIP Function] {function_name}: {arguments_str}", flush=True)

    try:
        args = json.loads(arguments_str)
        result = {}

        if function_name == "search_knowledge_base":
            query = args.get("query", "")
            api_key = os.getenv("OPENAI_API_KEY")
            ai = OpenAI(api_key=api_key)
            matches = semantic_search(sb=db, ai=ai, agent_id=agent_id, query=query, k=10, min_similarity=0.3)
            if matches:
                result = {
                    "found": True,
                    "results": [{"text": m.get("chunk_text", ""), "similarity": m.get("score", 0.0), "filename": m.get("filename", "")} for m in matches],
                    "summary": f"Found {len(matches)} relevant chunks."
                }
            else:
                result = {"found": False, "message": "No relevant information found in knowledge base."}

        elif function_name == "end_call":
            reason = args.get("reason", "Conversation completed")
            print(f"[SIP] Ending call: {reason}", flush=True)
            # Wait for goodbye audio to finish
            await asyncio.sleep(4.0)
            # Hang up via API
            try:
                await asyncio.to_thread(
                    http_requests.post,
                    f"https://api.openai.com/v1/realtime/calls/{call_id}/hangup",
                    headers=_get_auth_header()
                )
            except Exception as e:
                print(f"[SIP] Error hanging up: {e}", flush=True)
            result = {"success": True, "message": f"Call ended: {reason}"}

        elif function_name == "check_calendar":
            from api.crud.integrations import check_availability
            date_str = args.get("date", "")
            time_min = f"{date_str}T00:00:00Z"
            time_max = f"{date_str}T23:59:59Z"
            result = await check_availability(business_id, time_min, time_max)

        elif function_name == "create_calendar_event":
            from api.crud.integrations import create_calendar_event, check_availability
            cal_settings = (tool_settings or {}).get('google-calendar', {})
            default_duration = cal_settings.get('default_duration', 30)
            allow_conflicts = cal_settings.get('allow_conflicts', False)
            biz_start = cal_settings.get('business_hours_start', '09:00')
            biz_end = cal_settings.get('business_hours_end', '17:00')
            booking_window = cal_settings.get('booking_window_days', 30)

            date = args.get("date", "")
            start_time = args.get("start_time", "")
            end_time = args.get("end_time", "")
            summary = args.get("summary", "")
            description = args.get("description", "")

            if not end_time and start_time:
                parts = start_time.split(':')
                if len(parts) == 2:
                    total = int(parts[0]) * 60 + int(parts[1]) + default_duration
                    end_time = f"{total // 60:02d}:{total % 60:02d}"

            if start_time < biz_start or end_time > biz_end:
                result = {"error": f"Must be within business hours ({biz_start}-{biz_end})."}
            elif not allow_conflicts:
                avail = await check_availability(business_id, f"{date}T{start_time}:00Z", f"{date}T{end_time}:00Z")
                if avail.get('busy') and len(avail['busy']) > 0:
                    result = {"error": "Time slot conflict."}
                else:
                    result = await create_calendar_event(business_id=business_id, summary=summary, start_datetime=f"{date}T{start_time}:00", end_datetime=f"{date}T{end_time}:00", description=description)
            else:
                result = await create_calendar_event(business_id=business_id, summary=summary, start_datetime=f"{date}T{start_time}:00", end_datetime=f"{date}T{end_time}:00", description=description)
        else:
            result = {"error": f"Unknown function: {function_name}"}

        # Send function output
        await ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {
                "type": "function_call_output",
                "call_id": call_fn_id,
                "output": json.dumps(result)
            }
        }))

        if function_name != "end_call":
            await ws.send(json.dumps({"type": "response.create"}))

    except Exception as e:
        print(f"[SIP Function] Error: {e}", flush=True)
        await ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {
                "type": "function_call_output",
                "call_id": call_fn_id,
                "output": json.dumps({"error": str(e)})
            }
        }))


@router.post('/webhook', summary="OpenAI SIP incoming call webhook")
async def sip_webhook(request: Request):
    """
    Receives realtime.call.incoming webhook from OpenAI when a SIP call arrives.
    Verifies signature, looks up agent, accepts call, and starts WebSocket monitor.
    """
    body = await request.body()
    headers = dict(request.headers)

    print(f"[SIP] Webhook received", flush=True)

    # Verify webhook signature
    try:
        client = _get_openai_client()
        event = client.webhooks.unwrap(body, headers)
    except InvalidWebhookSignatureError as e:
        print(f"[SIP] Invalid webhook signature: {e}", flush=True)
        raise HTTPException(status_code=400, detail="Invalid signature")
    except Exception as e:
        print(f"[SIP] Webhook verification error: {e}", flush=True)
        raise HTTPException(status_code=400, detail=str(e))

    if event.type != "realtime.call.incoming":
        print(f"[SIP] Unexpected event type: {event.type}", flush=True)
        return JSONResponse({"status": "ignored"})

    call_id = event.data.call_id
    sip_headers = {h.name: h.value for h in (event.data.sip_headers or [])}
    from_header = sip_headers.get("From", "unknown")
    to_header = sip_headers.get("To", "")

    # Log ALL SIP headers for debugging
    print(f"[SIP] All SIP headers: {sip_headers}", flush=True)
    print(f"[SIP] Incoming call {call_id} from={from_header} to={to_header}", flush=True)

    # With SIP trunking, the To header is the SIP URI (project ID), not the phone number.
    # The original dialed number may be in: Diversion header, X-]Original-To, Request-URI,
    # or we need to look it up from Twilio's Call-ID / custom headers.
    # Try multiple sources for the called number:
    # 1. To header (works if it contains a phone number)
    # 2. Diversion header (Twilio often adds this)
    # 3. X-]Twilio-* headers
    # 4. Request-URI header
    # 5. Fall back to trying ALL phone numbers in the DB
    
    lookup_header = to_header
    # Check if To header actually has a phone number
    if not re.search(r'sip:(\+?\d+)@', to_header):
        # To header doesn't have phone - try alternatives
        for alt_header_name in ['Diversion', 'X-Original-To', 'Request-URI', 'P-Asserted-Identity']:
            alt_val = sip_headers.get(alt_header_name, '')
            if alt_val and re.search(r'(\+?\d{10,})', alt_val):
                print(f"[SIP] Found phone in {alt_header_name}: {alt_val}", flush=True)
                lookup_header = alt_val
                break
    
    # Look up agent
    db = get_service_client()
    agent_config, agent_phone = _lookup_agent_by_phone(db, lookup_header)
    
    # If still not found, try looking up by ALL phone numbers (fallback for single-agent setups)
    if not agent_config and not re.search(r'sip:(\+?\d+)@', lookup_header):
        print(f"[SIP] To header has no phone number, trying all agents...", flush=True)
        # Get all phone numbers and find any active agent
        all_phones = db.table('phone_number').select('agent_id, phone_number').execute()
        if all_phones.data:
            # For now, try each phone number's agent
            for phone_entry in all_phones.data:
                agent_data = db.table('agent').select('*').eq('id', phone_entry['agent_id']).single().execute()
                if agent_data.data:
                    agent_config = agent_data.data
                    agent_phone = phone_entry['phone_number']
                    print(f"[SIP] Matched via fallback to agent {agent_config['id']} phone {agent_phone}", flush=True)
                    break

    if not agent_config:
        print(f"[SIP] No agent found, rejecting call", flush=True)
        await asyncio.to_thread(
            lambda: http_requests.post(
                f"https://api.openai.com/v1/realtime/calls/{call_id}/reject",
                headers={**_get_auth_header(), "Content-Type": "application/json"},
                json={"status_code": 404}
            )
        )
        return JSONResponse({"status": "rejected", "reason": "no agent"})

    agent_id = agent_config['id']

    # Check trial
    if _check_trial_exhausted(db, agent_config):
        await asyncio.to_thread(
            lambda: http_requests.post(
                f"https://api.openai.com/v1/realtime/calls/{call_id}/reject",
                headers={**_get_auth_header(), "Content-Type": "application/json"},
                json={"status_code": 486}
            )
        )
        return JSONResponse({"status": "rejected", "reason": "trial exhausted"})

    # Get business info
    business_info = {}
    business_id = agent_config.get('business_id')
    if business_id:
        try:
            biz_data = db.table('business').select('name, address, business_email, phone_number').eq('id', business_id).single().execute()
            if biz_data.data:
                business_info = biz_data.data
        except Exception:
            pass

    # Get connected tools
    connected_tools = []
    tool_settings = {}
    if business_id:
        try:
            tc_data = db.table('tool_connection').select('provider, settings').eq('business_id', business_id).execute()
            for tc in (tc_data.data or []):
                connected_tools.append(tc['provider'])
                tool_settings[tc['provider']] = tc.get('settings') or {}
        except Exception:
            pass

    # Note: idempotency check removed — conversation table has no call_id column

    # Extract caller phone from SIP From header
    caller_match = re.search(r'sip:(\+?\d+)@', from_header)
    caller_phone = caller_match.group(1) if caller_match else 'unknown'

    # Create conversation (matches realtime_voice.py schema — no call_id column)
    conversation = db.table('conversation').insert({
        'agent_id': agent_id,
        'caller_phone': caller_phone,
        'status': 'in_progress'
    }).execute()
    conversation_id = conversation.data[0]['id']
    print(f"[SIP] Created conversation {conversation_id} for agent {agent_id}", flush=True)

    # Build session config and accept call
    session_config = _build_session_config(agent_config, business_info, agent_phone, connected_tools, tool_settings)

    try:
        accept_resp = await asyncio.to_thread(
            lambda: http_requests.post(
                f"https://api.openai.com/v1/realtime/calls/{call_id}/accept",
                headers={**_get_auth_header(), "Content-Type": "application/json"},
                json=session_config
            )
        )
        accept_resp.raise_for_status()
        print(f"[SIP] Call {call_id} accepted (HTTP {accept_resp.status_code})", flush=True)
    except Exception as e:
        print(f"[SIP] Error accepting call: {e}", flush=True)
        db.table('conversation').update({'status': 'failed', 'ended_at': 'now()'}).eq('id', conversation_id).execute()
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)

    # Start WebSocket monitor in background thread
    def _run_ws():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(
            _websocket_monitor(call_id, agent_config, conversation_id, agent_phone, connected_tools, tool_settings)
        )
        loop.close()

    threading.Thread(target=_run_ws, daemon=True).start()
    print(f"[SIP] WebSocket monitor started for call {call_id}", flush=True)

    return JSONResponse({"status": "accepted", "call_id": call_id, "conversation_id": conversation_id})
