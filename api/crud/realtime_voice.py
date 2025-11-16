"""
Twilio Media Streams WebSocket handler for OpenAI Realtime API integration.

Handles incoming calls from Twilio and bridges audio between Twilio Media Streams
and OpenAI Realtime API.
"""

import json
import asyncio
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import Response
from api.database import supabase
from api.realtime_manager import RealtimeSession
from api.audio_utils import twilio_to_openai, openai_to_twilio


router = APIRouter()


@router.post('/{agent_id}/realtime-voice', summary="Handle incoming call (Twilio webhook)")
async def handle_incoming_call(agent_id: int, request: Request):
    """
    Receives incoming call from Twilio and returns TwiML to initiate Media Stream.

    This replaces the old TwiML webhook that used <Gather> for speech.
    Now we use <Connect><Stream> to establish a WebSocket connection for bidirectional audio.
    """
    try:
        db = supabase()
        form_data = await request.form()

        caller_phone = form_data.get('From', 'unknown')

        # Get agent config from database
        agent_data = db.table('agent').select('*').eq('id', agent_id).single().execute()
        if not agent_data.data:
            return Response(
                content='<Response><Say voice="Polly.Joanna">Agent not found.</Say><Hangup/></Response>',
                media_type="application/xml"
            )

        agent_config = agent_data.data

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
        ws_url = f"wss://{request.url.hostname}/conversation/{agent_id}/media-stream?conversation_id={conversation_id}"

        twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
                    <Response>
                        <Say voice="Polly.Joanna">{greeting}</Say>
                        <Connect>
                            <Stream url="{ws_url}">
                                <Parameter name="agent_id" value="{agent_id}" />
                                <Parameter name="conversation_id" value="{conversation_id}" />
                            </Stream>
                        </Connect>
                    </Response>'''

        print(f"[TwilioWebhook] Incoming call for agent {agent_id}, conversation {conversation_id}")

        return Response(content=twiml, media_type="application/xml")

    except Exception as e:
        print(f"[TwilioWebhook] Error: {e}")
        return Response(
            content='<Response><Say voice="Polly.Joanna">Sorry, there was an error.</Say><Hangup/></Response>',
            media_type="application/xml"
        )


@router.websocket('/{agent_id}/media-stream')
async def media_stream_handler(websocket: WebSocket, agent_id: int, conversation_id: int):
    """
    Handle bidirectional audio streaming between Twilio and OpenAI Realtime API.

    Twilio sends/receives: μ-law 8kHz (base64)
    OpenAI sends/receives: PCM16 24kHz (base64)
    """
    await websocket.accept()
    print(f"[MediaStream] WebSocket connected for conversation {conversation_id}")

    db = supabase()
    realtime_session: RealtimeSession = None
    stream_sid = None

    # Audio buffering for Twilio
    twilio_audio_queue = asyncio.Queue()

    try:
        # Get agent config
        agent_data = db.table('agent').select('*').eq('id', agent_id).single().execute()
        if not agent_data.data:
            await websocket.close(code=1008, reason="Agent not found")
            return

        agent_config = agent_data.data

        # Callback to send audio to Twilio
        async def send_audio_to_twilio(openai_audio_base64: str):
            """Convert OpenAI audio and send to Twilio."""
            try:
                # Validate stream_sid is set
                if not stream_sid:
                    print("[MediaStream] Warning: stream_sid not set, skipping audio send")
                    return

                # Convert PCM16 24kHz → μ-law 8kHz
                twilio_audio = openai_to_twilio(openai_audio_base64, source_rate=24000)

                # Send to Twilio via WebSocket
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

        # Callback for error handling
        async def handle_error(error_msg: str):
            print(f"[MediaStream] Realtime API error: {error_msg}")

        # Create OpenAI Realtime session
        realtime_session = RealtimeSession(
            agent_id=agent_id,
            conversation_id=conversation_id,
            agent_config=agent_config,
            on_audio=send_audio_to_twilio,
            on_error=handle_error
        )

        # Connect to OpenAI Realtime API
        await realtime_session.connect()

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

                    if twilio_audio_base64 and realtime_session:
                        # Convert μ-law 8kHz → PCM16 24kHz
                        openai_audio = twilio_to_openai(twilio_audio_base64, target_rate=24000)

                        # Send to OpenAI Realtime API
                        await realtime_session.send_audio(openai_audio)

                # Mark packets as received (required by Twilio)
                elif event_type == "mark":
                    mark_name = event.get("mark", {}).get("name")
                    # print(f"[MediaStream] Mark received: {mark_name}")

                # Stream stopped
                elif event_type == "stop":
                    print(f"[MediaStream] Stream stopped")
                    break

            except json.JSONDecodeError:
                print(f"[MediaStream] Invalid JSON received")
            except Exception as e:
                print(f"[MediaStream] Error processing event: {e}")

    except WebSocketDisconnect:
        print(f"[MediaStream] WebSocket disconnected for conversation {conversation_id}")
    except Exception as e:
        print(f"[MediaStream] Error in media stream handler: {e}")
    finally:
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
