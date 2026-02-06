"""
Twilio Media Streams WebSocket handler for OpenAI Realtime API integration.

Handles incoming calls from Twilio and bridges audio between Twilio Media Streams
and OpenAI Realtime API.

Supports horizontal scaling via Fly.io session affinity - the WebSocket URL
includes the machine ID so calls are routed to the same instance that
handled the initial webhook.
"""

import os
import json
import asyncio
from datetime import datetime
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import Response
from api.database import get_service_client
from api.realtime_manager import RealtimeSession
from api.audio_utils import twilio_to_openai, openai_to_twilio

FREE_TRIAL_MINUTES = 5


router = APIRouter(prefix="/conversation", tags=["Voice"])


@router.post('/{agent_id}/voice', summary="Handle incoming call (Twilio webhook)")
async def handle_incoming_call(agent_id: int, request: Request):
    """
    Receives incoming call from Twilio and returns TwiML to initiate Media Stream.

    This replaces the old TwiML webhook that used <Gather> for speech.
    Now we use <Connect><Stream> to establish a WebSocket connection for bidirectional audio.
    """
    try:
        db = get_service_client()
        form_data = await request.form()

        caller_phone = form_data.get('From', 'unknown')

        # Get agent config from database
        agent_data = db.table('agent').select('*').eq('id', agent_id).single().execute()
        if not agent_data.data:
            return Response(
                content='<Response><Say>Agent not found.</Say><Hangup/></Response>',
                media_type="application/xml"
            )

        agent_config = agent_data.data

        # Check if trial is exhausted (no active subscription and >= 5 minutes used)
        business_id = agent_config.get('business_id')
        if business_id:
            sub_data = db.table('subscription').select('status').eq(
                'business_id', business_id
            ).in_('status', ['active', 'trialing']).limit(1).execute()

            has_active_sub = bool(sub_data.data)

            if not has_active_sub:
                # Calculate used minutes from completed conversations
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
                    print(f"[TwilioWebhook] Trial exhausted for agent {agent_id}: {total_minutes:.1f} min used", flush=True)
                    return Response(
                        content='<Response><Say>Your free trial has ended. Please subscribe to continue using this service. Goodbye.</Say><Hangup/></Response>',
                        media_type="application/xml"
                    )

        # Create conversation record
        conversation = db.table('conversation').insert({
            'agent_id': agent_id,
            'caller_phone': caller_phone,
            'status': 'in_progress'
        }).execute()

        conversation_id = conversation.data[0]['id']

        # Get greeting from agent config
        greeting = agent_config.get('greeting', 'Hello! How can I help you today?')

        # Build TwiML response with Media Stream
        # The WebSocket URL is where Twilio will connect for bidirectional audio
        # Note: Query parameters are stripped by Twilio, use <Parameter> tags instead
        # Include Fly.io machine ID for session affinity (horizontal scaling)
        machine_id = os.getenv("FLY_MACHINE_ID", "local")
        ws_url = f"wss://{request.url.hostname}/conversation/{agent_id}/media-stream/{machine_id}"

        print(f"[TwilioWebhook] Incoming call for agent {agent_id}, conversation {conversation_id}", flush=True)
        print(f"[TwilioWebhook] Machine ID: {machine_id}", flush=True)
        print(f"[TwilioWebhook] Generated WebSocket URL: {ws_url}", flush=True)

        twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
                    <Response>
                        <Connect>
                            <Stream url="{ws_url}">
                                <Parameter name="agent_id" value="{agent_id}" />
                                <Parameter name="conversation_id" value="{conversation_id}" />
                            </Stream>
                        </Connect>
                    </Response>'''

        return Response(content=twiml, media_type="application/xml")

    except Exception as e:
        print(f"[TwilioWebhook] Error: {e}")
        return Response(
            content='<Response><Say>Sorry, there was an error.</Say><Hangup/></Response>',
            media_type="application/xml"
        )


@router.websocket('/{agent_id}/media-stream/{target_machine_id}')
async def media_stream_handler(websocket: WebSocket, agent_id: int, target_machine_id: str):
    """
    Handle bidirectional audio streaming between Twilio and OpenAI Realtime API.

    Twilio sends/receives: μ-law 8kHz (base64)
    OpenAI sends/receives: PCM16 24kHz (base64)

    The target_machine_id parameter enables Fly.io session affinity for horizontal scaling.
    The FlyReplayMiddleware intercepts requests to wrong machines before they reach here.
    """
    current_machine_id = os.getenv("FLY_MACHINE_ID", "local")
    print(f"[MediaStream] WebSocket connection for agent {agent_id} on machine {current_machine_id}", flush=True)

    try:
        await websocket.accept()
        print(f"[MediaStream] WebSocket accepted", flush=True)
    except Exception as e:
        print(f"[MediaStream] Error accepting WebSocket: {e}", flush=True)
        return

    # Wait for the "start" event from Twilio to get conversation_id from customParameters
    # Twilio sends "connected" event first, then "start" event
    conversation_id = None
    stream_sid = None
    call_sid = None

    try:
        # Loop through initial messages to find the "start" event
        start_event_found = False
        max_attempts = 5
        attempt = 0

        while not start_event_found and attempt < max_attempts:
            attempt += 1
            message = await websocket.receive_text()
            print(f"[MediaStream] Received message {attempt}: {message[:200]}", flush=True)

            event = json.loads(message)
            event_type = event.get("event")

            if event_type == "connected":
                print(f"[MediaStream] Received 'connected' event, waiting for 'start'", flush=True)
                continue
            elif event_type == "start":
                stream_sid = event.get("start", {}).get("streamSid")
                call_sid = event.get("start", {}).get("callSid")
                custom_params = event.get("start", {}).get("customParameters", {})
                conversation_id = custom_params.get("conversation_id")

                print(f"[MediaStream] Stream started: {stream_sid}", flush=True)
                print(f"[MediaStream] Call SID: {call_sid}", flush=True)
                print(f"[MediaStream] Extracted conversation_id from customParameters: {conversation_id}", flush=True)
                print(f"[MediaStream] Custom parameters: {custom_params}", flush=True)
                start_event_found = True
            else:
                print(f"[MediaStream] Unexpected event type: {event_type}", flush=True)

        if not start_event_found:
            print(f"[MediaStream] Error: Did not receive 'start' event after {attempt} messages", flush=True)
            await websocket.close(code=1008, reason="Start event not received")
            return
    except Exception as e:
        print(f"[MediaStream] Error reading start event: {e}", flush=True)
        import traceback
        print(f"[MediaStream] Traceback: {traceback.format_exc()}", flush=True)
        await websocket.close(code=1008, reason="Failed to read start event")
        return

    if not conversation_id:
        print("[MediaStream] Error: conversation_id not in customParameters", flush=True)
        await websocket.close(code=1008, reason="Missing conversation_id")
        return

    try:
        conversation_id = int(conversation_id)
        print(f"[MediaStream] Parsed conversation_id: {conversation_id}", flush=True)
    except ValueError:
        print("[MediaStream] Error: Invalid conversation_id format", flush=True)
        await websocket.close(code=1008, reason="Invalid conversation_id")
        return

    print(f"[MediaStream] WebSocket connected for conversation {conversation_id}", flush=True)

    db = None
    realtime_session: RealtimeSession = None

    # Audio buffering for Twilio
    twilio_audio_queue = asyncio.Queue()

    try:
        print(f"[MediaStream] Getting database connection", flush=True)
        db = get_service_client()
        print(f"[MediaStream] Database connection established", flush=True)
        # Get agent config
        print(f"[MediaStream] Fetching agent config for agent_id={agent_id}", flush=True)
        agent_data = db.table('agent').select('*').eq('id', agent_id).single().execute()
        if not agent_data.data:
            print(f"[MediaStream] Error: Agent {agent_id} not found", flush=True)
            await websocket.close(code=1008, reason="Agent not found")
            return

        agent_config = agent_data.data
        print(f"[MediaStream] Agent config loaded: {agent_config.get('name', 'unknown')}", flush=True)

        # Fetch business info for context
        business_info = {}
        business_id = agent_config.get('business_id')
        if business_id:
            try:
                biz_data = db.table('business').select('name, address, business_email, phone_number').eq('id', business_id).single().execute()
                if biz_data.data:
                    business_info = biz_data.data
                    print(f"[MediaStream] Business info loaded: {business_info.get('name', 'unknown')}", flush=True)
            except Exception as e:
                print(f"[MediaStream] Warning: Could not fetch business info: {e}", flush=True)

        # Fetch the agent's Twilio phone number
        agent_phone = None
        try:
            phone_data = db.table('phone_number').select('phone_number').eq('agent_id', agent_id).limit(1).execute()
            if phone_data.data:
                agent_phone = phone_data.data[0].get('phone_number')
                print(f"[MediaStream] Agent phone number loaded: {agent_phone}", flush=True)
        except Exception as e:
            print(f"[MediaStream] Warning: Could not fetch agent phone: {e}", flush=True)

        # Fetch connected tool providers and their settings for this business
        connected_tools = []
        tool_settings = {}
        if business_id:
            try:
                tc_data = db.table('tool_connection').select('provider, settings').eq('business_id', business_id).execute()
                for tc in (tc_data.data or []):
                    provider = tc['provider']
                    connected_tools.append(provider)
                    tool_settings[provider] = tc.get('settings') or {}
                if connected_tools:
                    print(f"[MediaStream] Connected tools: {connected_tools}", flush=True)
                    print(f"[MediaStream] Tool settings: {tool_settings}", flush=True)
            except Exception as e:
                print(f"[MediaStream] Warning: Could not fetch tool connections: {e}", flush=True)

        # Callback to send audio to Twilio
        async def send_audio_to_twilio(openai_audio_base64: str):
            """Convert OpenAI PCM16 24kHz audio to Twilio μ-law 8kHz and send."""
            try:
                if not stream_sid:
                    print("[MediaStream] Warning: stream_sid not set, skipping audio send")
                    return

                # Convert PCM16 24kHz → μ-law 8kHz
                twilio_audio = openai_to_twilio(openai_audio_base64, source_rate=24000)

                media_event = {
                    "event": "media",
                    "streamSid": stream_sid,
                    "media": {
                        "payload": twilio_audio
                    }
                }
                await websocket.send_json(media_event)
            except Exception as e:
                print(f"[MediaStream] Error sending audio to Twilio: {e}")

        # Callback to clear Twilio audio buffer on user interruption
        async def handle_interrupt():
            """Send clear event to Twilio to stop queued audio immediately."""
            try:
                if stream_sid:
                    await websocket.send_json({
                        "event": "clear",
                        "streamSid": stream_sid
                    })
                    print(f"[MediaStream] Sent clear event to Twilio")
            except Exception as e:
                print(f"[MediaStream] Error sending clear to Twilio: {e}")

        # Callback to send mark event to Twilio for audio playback tracking
        async def handle_mark():
            """Send mark event to Twilio to track audio playback position."""
            try:
                if stream_sid:
                    await websocket.send_json({
                        "event": "mark",
                        "streamSid": stream_sid,
                        "mark": {"name": "responsePart"}
                    })
            except Exception as e:
                print(f"[MediaStream] Error sending mark to Twilio: {e}")

        # Callback for error handling
        async def handle_error(error_msg: str):
            print(f"[MediaStream] Realtime API error: {error_msg}", flush=True)

        # Extract greeting and goodbye from agent config
        greeting = agent_config.get('greeting', 'Hello! How can I help you today?')
        goodbye = agent_config.get('goodbye', 'Goodbye! Have a great day!')

        # Create OpenAI Realtime session
        print(f"[MediaStream] Creating OpenAI Realtime session", flush=True)
        try:
            realtime_session = RealtimeSession(
                agent_id=agent_id,
                conversation_id=conversation_id,
                agent_config=agent_config,
                business_info=business_info,
                on_audio=send_audio_to_twilio,
                on_error=handle_error,
                on_interrupt=handle_interrupt,
                on_mark=handle_mark,
                twilio_ws=websocket,
                call_sid=call_sid,
                greeting=greeting,
                goodbye=goodbye,
                agent_phone=agent_phone,
                connected_tools=connected_tools,
                tool_settings=tool_settings
            )
            print(f"[MediaStream] Realtime session created successfully", flush=True)
        except Exception as e:
            print(f"[MediaStream] Error creating Realtime session: {e}", flush=True)
            raise

        # Connect to OpenAI Realtime API
        print(f"[MediaStream] Connecting to OpenAI Realtime API", flush=True)
        try:
            await realtime_session.connect()
            print(f"[MediaStream] Successfully connected to OpenAI Realtime API", flush=True)
        except Exception as e:
            print(f"[MediaStream] Error connecting to OpenAI Realtime API: {e}", flush=True)
            raise

        # Main event loop: receive events from Twilio
        async for message in websocket.iter_text():
            try:
                event = json.loads(message)
                event_type = event.get("event")

                # Connection started
                if event_type == "start":
                    stream_sid = event.get("start", {}).get("streamSid")
                    print(f"[MediaStream] Stream started: {stream_sid}")

                # Audio from caller (Twilio → OpenAI)
                elif event_type == "media":
                    media = event.get("media", {})
                    twilio_audio_base64 = media.get("payload")

                    # Update media timestamp for interrupt tracking
                    timestamp = int(media.get("timestamp", 0))
                    if realtime_session:
                        realtime_session.update_media_timestamp(timestamp)

                    if twilio_audio_base64 and realtime_session:
                        # Convert μ-law 8kHz → PCM16 24kHz
                        openai_audio = twilio_to_openai(twilio_audio_base64, target_rate=24000)
                        await realtime_session.send_audio(openai_audio)

                # Mark packets as received - pop from session mark queue
                elif event_type == "mark":
                    if realtime_session and realtime_session.mark_queue:
                        realtime_session.mark_queue.pop(0)

                # Stream stopped
                elif event_type == "stop":
                    print(f"[MediaStream] Stream stopped")
                    break

            except json.JSONDecodeError:
                print(f"[MediaStream] Invalid JSON received")
            except Exception as e:
                print(f"[MediaStream] Error processing event: {e}")

    except WebSocketDisconnect:
        print(f"[MediaStream] WebSocket disconnected for conversation {conversation_id}", flush=True)
    except Exception as e:
        print(f"[MediaStream] Error in media stream handler: {e}", flush=True)
        import traceback
        print(f"[MediaStream] Traceback: {traceback.format_exc()}", flush=True)
    finally:
        print(f"[MediaStream] Entering cleanup for conversation {conversation_id}", flush=True)
        # Clean up
        if realtime_session:
            await realtime_session.disconnect()

        # Update conversation status
        try:
            db.table('conversation').update({
                'status': 'completed',
                'ended_at': 'now()'
            }).eq('id', conversation_id).execute()
            print(f"[MediaStream] Conversation {conversation_id} marked as completed")
        except Exception as e:
            print(f"[MediaStream] Error updating conversation: {e}")

        # Close WebSocket if still open
        try:
            await websocket.close()
        except:
            pass
