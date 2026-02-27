"""
OpenAI Realtime API Session Manager.

Manages WebSocket connection to OpenAI Realtime API, handles session configuration,
function calling for RAG, and transcript storage.
"""

import json
import asyncio
import websockets
from typing import Optional, Dict, Any, Callable, List
from api.database import get_service_client
from api.rag import semantic_search
from openai import OpenAI
import os


class RealtimeSession:
    """Manages an OpenAI Realtime API session for a voice agent."""

    def __init__(
        self,
        agent_id: int,
        conversation_id: int,
        agent_config: Dict[str, Any],
        business_info: Optional[Dict[str, Any]] = None,
        on_audio: Optional[Callable] = None,
        on_transcript: Optional[Callable] = None,
        on_error: Optional[Callable] = None,
        on_interrupt: Optional[Callable] = None,
        on_mark: Optional[Callable] = None,
        twilio_ws: Optional[Any] = None,
        call_sid: Optional[str] = None,
        greeting: Optional[str] = None,
        goodbye: Optional[str] = None,
        agent_phone: Optional[str] = None,
        connected_tools: Optional[List[str]] = None,
        tool_settings: Optional[Dict[str, Dict]] = None,
    ):
        """
        Initialize Realtime Session.

        Args:
            agent_id: Database agent ID
            conversation_id: Database conversation ID
            agent_config: Agent configuration from database
            on_audio: Callback for audio output (receives base64 PCM16)
            on_transcript: Callback for transcript updates
            on_error: Callback for error handling
            on_interrupt: Callback to clear Twilio audio buffer on user interruption
            on_mark: Callback to send a mark event to Twilio for audio tracking
            twilio_ws: Twilio Media Stream WebSocket connection
            call_sid: Twilio call SID for call control
            greeting: Initial greeting message to speak when call starts
            goodbye: Farewell message to speak before call ends
            tool_settings: Settings for connected tools, keyed by provider
        """
        self.agent_id = agent_id
        self.conversation_id = conversation_id
        self.agent_config = agent_config
        self.business_info = business_info or {}
        self.on_audio = on_audio
        self.on_transcript = on_transcript
        self.on_error = on_error
        self.on_interrupt = on_interrupt
        self.on_mark = on_mark
        self.twilio_ws = twilio_ws
        self.call_sid = call_sid
        self.greeting = greeting or "Hello! How can I help you today?"
        self.goodbye = goodbye or "Goodbye! Have a great day!"
        self.agent_phone = agent_phone
        self.connected_tools = connected_tools or []
        self.tool_settings = tool_settings or {}

        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.api_key = os.getenv("OPENAI_API_KEY")
        self.running = False

        # For accumulating transcripts
        self.current_user_transcript = ""
        self.current_agent_transcript = ""

        # Audio format: audio/pcmu (μ-law) for Twilio — zero conversion needed.
        # The OpenAI Realtime API natively supports audio/pcmu format,
        # so we pass Twilio's μ-law 8kHz audio straight through without
        # any resampling or PCM conversion. This eliminates quality loss.
        self.audio_format = "pcmu"

        # Track function call state
        self.pending_function_calls: Dict[str, Dict] = {}

        # Track if we're waiting for goodbye to finish
        self.waiting_for_goodbye = False
        self.goodbye_complete = asyncio.Event()

        # Interrupt handling state
        self.last_assistant_item: Optional[str] = None
        self.response_start_timestamp: Optional[int] = None
        self.latest_media_timestamp: int = 0
        self.mark_queue: List[str] = []

    def update_media_timestamp(self, timestamp: int):
        """Update the latest media timestamp from Twilio media events."""
        self.latest_media_timestamp = timestamp

    async def connect(self):
        """Connect to OpenAI Realtime API and configure session."""
        # Get model from agent config, use latest GA model as default
        model = self.agent_config.get('model_type') or 'gpt-realtime-1.5'

        url = f"wss://api.openai.com/v1/realtime?model={model}"

        headers = {
            "Authorization": f"Bearer {self.api_key}"
        }

        try:
            self.ws = await websockets.connect(url, additional_headers=headers)
            self.running = True
            print(f"[RealtimeSession] Connected for conversation {self.conversation_id} with model={model}")

            # Send session configuration
            await self._configure_session()

            # Start listening for events
            asyncio.create_task(self._listen_for_events())

        except Exception as e:
            print(f"[RealtimeSession] Connection error: {e}")
            if self.on_error:
                await self.on_error(str(e))
            raise

    async def _configure_session(self):
        """Configure the Realtime session with agent settings (GA API format)."""
        default_prompt = (
            "You are a helpful AI voice assistant.\n"
            "Answer questions using only the uploaded knowledge base documents.\n"
            "Always be polite, professional, and helpful."
        )

        base_instructions = self.agent_config.get('prompt') or default_prompt

        # Build business context section
        biz = self.business_info
        context_lines = []
        if biz.get('name'):
            context_lines.append(f"- Business name: {biz['name']}")
        if biz.get('address'):
            context_lines.append(f"- Address: {biz['address']}")
        if biz.get('business_email'):
            context_lines.append(f"- Contact email: {biz['business_email']}")
        if biz.get('phone_number'):
            context_lines.append(f"- Business contact phone: {biz['phone_number']}")
        if self.agent_phone:
            context_lines.append(f"- Your phone number (the number callers dialed): {self.agent_phone}")
        business_context = "\n".join(context_lines) if context_lines else "- No business details available."

        # Build tools list and dynamic tool instructions
        tools = [
            self._get_rag_tool_definition(),
            self._get_end_call_tool_definition()
        ]

        # Add calendar tools if Google Calendar is connected
        if 'google-calendar' in self.connected_tools:
            tools.append(self._get_calendar_check_tool())
            tools.append(self._get_calendar_create_tool())

        tool_names = [t["name"] for t in tools]

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
- BEFORE calling, say: "{self.goodbye}" """

        if "check_calendar" in tool_names:
            cal_settings = self.tool_settings.get('google-calendar', {})
            default_duration = cal_settings.get('default_duration', 30)
            allow_conflicts = cal_settings.get('allow_conflicts', False)
            booking_window = cal_settings.get('booking_window_days', 30)
            biz_start = cal_settings.get('business_hours_start', '09:00')
            biz_end = cal_settings.get('business_hours_end', '17:00')

            tool_instructions += f"""

## check_calendar
- Call when the caller asks about availability or wants to know when they're free/busy.
- Returns busy time slots (not event details). Summarize which times are busy vs available.

## create_calendar_event
- Call when the caller wants to schedule, book, or create an appointment.
- Confirm the details (what, when) with the caller BEFORE creating the event.
- Default appointment duration: {default_duration} minutes (use this if caller doesn't specify).
- Business hours: {biz_start} to {biz_end}. Do not book appointments outside these hours.
- Booking window: up to {booking_window} days in advance.
- {"Conflicts are allowed." if allow_conflicts else "Do not book over existing events (check calendar first)."}
- After creating, confirm the event was added."""

        tool_list_str = ", ".join(tool_names)

        instructions = f"""# Role & Objective
You are a voice customer service agent for {biz.get('name') or 'a business'}. Help callers by answering questions using ONLY the uploaded knowledge base documents.

# Context
{business_context}
You represent this business. When asked who you are, what business this is, or for contact details, use the information above.

# Capabilities
You have access to the following tools: {tool_list_str}.
You can ONLY perform actions that your tools allow. If a caller asks you to do something outside your capabilities, let them know what you can help with instead.

# Personality & Tone
## Personality
Professional, friendly, calm, and approachable customer service assistant.

## Tone
Warm, concise, confident, never fawning.

## Length
2-3 sentences per turn.

## Language
- The conversation will be only in English.
- Do not respond in any other language even if the user asks.
- If the user speaks another language, politely explain that support is limited to English.

## Variety
- Do not repeat the same sentence twice. Vary your responses so it doesn't sound robotic.

# Initial Greeting
When you see "[Call connected]", say exactly: "{self.greeting}"
- Say this once, then wait for the caller.
- NEVER repeat the greeting later in the conversation.

# Unclear Audio
- Only respond to clear audio or text.
- If the user's audio is not clear (e.g., ambiguous input, background noise, silent, unintelligible) or if you did not fully hear or understand the user, ask for clarification.
- Do not include any sound effects or onomatopoeic expressions in your responses.

Sample clarification phrases:
- "Sorry, I didn't catch that - could you say it again?"
- "There's some background noise. Please repeat the last part."
- "I only heard part of that. What did you say after...?"

# Tools
{tool_instructions}

# Instructions
- NEVER answer factual questions without calling search_knowledge_base first.
- Keep responses concise - this is a phone call, not an essay.
- If you don't know, say so. Do not make up answers.

{base_instructions}"""

        session_config = {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "instructions": instructions,
                "tools": tools,
                "tool_choice": "auto",
                "output_modalities": ["audio"],
                "voice": self.agent_config.get('voice_model', 'ash'),
                # Use audio/pcmu (μ-law) format — this is what Twilio Media Streams
                # natively sends/receives. No resampling or PCM conversion needed.
                # This matches the official Twilio + OpenAI integration sample:
                # https://github.com/twilio-samples/speech-assistant-openai-realtime-api-python
                "audio": {
                    "input": {
                        "format": {
                            "type": "audio/pcmu"
                        },
                        "transcription": {
                            "model": "gpt-4o-mini-transcribe"
                        },
                        "noise_reduction": {
                            "type": "near_field"
                        },
                        "turn_detection": {
                            # server_vad is more robust for phone audio than semantic_vad.
                            # semantic_vad can phantom-trigger on background noise / silence.
                            "type": "server_vad",
                            "silence_duration_ms": 500,
                            "threshold": 0.6
                        }
                    },
                    "output": {
                        "format": {
                            "type": "audio/pcmu"
                        }
                    }
                }
            }
        }

        await self.send_event(session_config)
        print(f"[RealtimeSession] Session configured - tools: {tool_names}, phone: {self.agent_phone}")

        # Trigger the initial greeting
        await self._trigger_initial_greeting()

    def _get_rag_tool_definition(self) -> Dict[str, Any]:
        """Return the function tool definition for knowledge base semantic search."""
        return {
            "type": "function",
            "name": "search_knowledge_base",
            "description": "Search the business's uploaded knowledge base documents using semantic similarity. Returns matching text chunks ranked by relevance score, or a not-found message if no matches exist.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural language search query to match against document content"
                    }
                },
                "required": ["query"]
            }
        }

    def _get_end_call_tool_definition(self) -> Dict[str, Any]:
        """Return the function tool definition for terminating the active call."""
        return {
            "type": "function",
            "name": "end_call",
            "description": "Terminate the active phone call and disconnect all parties. Returns a success or failure status with a message.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Brief explanation of why the call is ending"
                    }
                },
                "required": ["reason"]
            }
        }

    def _get_calendar_check_tool(self) -> Dict[str, Any]:
        """Return the function tool definition for checking calendar availability."""
        return {
            "type": "function",
            "name": "check_calendar",
            "description": "Check availability on a given date. Returns busy time slots (start/end times when calendar is occupied).",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": "Date to check in YYYY-MM-DD format (e.g. 2026-01-28)"
                    }
                },
                "required": ["date"]
            }
        }

    def _get_calendar_create_tool(self) -> Dict[str, Any]:
        """Return the function tool definition for creating calendar events."""
        return {
            "type": "function",
            "name": "create_calendar_event",
            "description": "Create a new event on the business's Google Calendar. Returns confirmation with event details and a link.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "Title of the event (e.g. 'Meeting with John')"
                    },
                    "date": {
                        "type": "string",
                        "description": "Date of the event in YYYY-MM-DD format"
                    },
                    "start_time": {
                        "type": "string",
                        "description": "Start time in HH:MM format (24-hour, e.g. '14:00')"
                    },
                    "end_time": {
                        "type": "string",
                        "description": "End time in HH:MM format (24-hour, e.g. '15:00')"
                    },
                    "description": {
                        "type": "string",
                        "description": "Optional description or notes for the event"
                    }
                },
                "required": ["summary", "date", "start_time", "end_time"]
            }
        }

    async def _trigger_initial_greeting(self):
        """
        Trigger the initial greeting by sending a call-connected message.

        The model will respond with the greeting defined in instructions.
        We use a user message to trigger the model's response since
        conversation.item.create for assistant messages cannot generate audio.
        """
        try:
            # Create a user message indicating call connection
            greeting_trigger = {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "[Call connected]"
                        }
                    ]
                }
            }
            await self.send_event(greeting_trigger)

            # Trigger response - model will say the greeting from instructions
            await self.send_event({"type": "response.create"})
            print(f"[RealtimeSession] Triggered initial greeting")

        except Exception as e:
            print(f"[RealtimeSession] Failed to trigger greeting: {e}")

    async def _wait_for_audio_completion(self, timeout: float = 5.0):
        """
        Wait for any ongoing audio to finish playing.

        This gives time for the goodbye message to be spoken before disconnecting.
        """
        try:
            # Wait a reasonable time for audio to finish
            # The model should have already said goodbye before calling end_call
            await asyncio.sleep(timeout)
            print(f"[RealtimeSession] Audio completion wait finished")
        except Exception as e:
            print(f"[RealtimeSession] Error waiting for audio completion: {e}")

    async def send_audio(self, audio_base64: str):
        """
        Send audio input to OpenAI.

        Args:
            audio_base64: Base64-encoded PCM16 audio (24kHz)
        """
        if not self.ws or not self.running:
            return

        event = {
            "type": "input_audio_buffer.append",
            "audio": audio_base64
        }

        await self.send_event(event)

    async def send_event(self, event: Dict[str, Any]):
        """Send a client event to OpenAI Realtime API."""
        if not self.ws:
            return

        try:
            await self.ws.send(json.dumps(event))
        except Exception as e:
            print(f"[RealtimeSession] Error sending event: {e}")
            if self.on_error:
                await self.on_error(str(e))

    async def _listen_for_events(self):
        """Listen for server events from OpenAI Realtime API."""
        if not self.ws:
            return

        try:
            async for message in self.ws:
                event = json.loads(message)
                await self._handle_event(event)
        except websockets.exceptions.ConnectionClosed:
            print(f"[RealtimeSession] Connection closed for conversation {self.conversation_id}")
            self.running = False
        except Exception as e:
            print(f"[RealtimeSession] Error in event loop: {e}")
            if self.on_error:
                await self.on_error(str(e))
            self.running = False

    async def _handle_event(self, event: Dict[str, Any]):
        """Handle server events from OpenAI."""
        event_type = event.get("type")

        # Debug: log transcription-related events
        if "transcription" in event_type.lower() if event_type else False:
            print(f"[DEBUG] Transcription event: {event_type} - {event}")

        # Audio output from AI
        if event_type == "response.output_audio.delta":
            audio_base64 = event.get("delta")
            if audio_base64 and self.on_audio:
                await self.on_audio(audio_base64)

            # Track the assistant item for interrupt/truncation
            item_id = event.get("item_id")
            if item_id:
                self.last_assistant_item = item_id
                if self.response_start_timestamp is None:
                    self.response_start_timestamp = self.latest_media_timestamp

                # Send a mark to Twilio so we can track playback position
                if self.on_mark:
                    self.mark_queue.append("responsePart")
                    await self.on_mark()

        # User started speaking - handle interrupt
        elif event_type == "input_audio_buffer.speech_started":
            await self._handle_speech_started()

        # User speech transcript
        elif event_type == "conversation.item.input_audio_transcription.completed":
            transcript = event.get("transcript", "")
            if transcript:
                print(f"[User]: {transcript}")
                await self._save_message('user', transcript)

        # Agent speech transcript (accumulate deltas)
        elif event_type == "response.output_audio_transcript.delta":
            delta = event.get("delta", "")
            self.current_agent_transcript += delta

        # Agent speech transcript completed
        elif event_type == "response.output_audio_transcript.done":
            transcript = event.get("transcript") or self.current_agent_transcript
            if transcript:
                print(f"[Agent]: {transcript}")
                await self._save_message('agent', transcript)
            self.current_agent_transcript = ""

        # Function call requested
        elif event_type == "response.output_item.done":
            item = event.get("item", {})
            if item.get("type") == "function_call":
                await self._handle_function_call(item)

        # Session created confirmation
        elif event_type == "session.created":
            print(f"[RealtimeSession] Session created: {event.get('session', {}).get('id')}")

        # Session updated confirmation
        elif event_type == "session.updated":
            session = event.get("session", {})
            tools = session.get("tools", [])
            audio_cfg = session.get("audio", {})
            turn_detection = audio_cfg.get("input", {}).get("turn_detection", {})
            noise_reduction = audio_cfg.get("input", {}).get("noise_reduction", {})
            print(f"[RealtimeSession] Session updated - tools: {[t.get('name') for t in tools]}, turn_detection: {turn_detection.get('type')}, noise_reduction: {noise_reduction.get('type') if noise_reduction else 'off'}")

        # Error handling
        elif event_type == "error":
            error_obj = event.get("error", {})
            error_msg = error_obj.get("message", "Unknown error")
            error_code = error_obj.get("code", "unknown")
            # Truncation overshoot is expected and harmless — suppress noise
            if "already shorter than" in error_msg:
                print(f"[RealtimeSession] Truncation overshoot (harmless): {error_msg}")
                return

            # (g711_ulaw fallback removed — GA API only supports audio/pcm)

            print(f"[RealtimeSession] ERROR [{error_code}]: {error_msg}")
            print(f"[RealtimeSession] Full error: {error_obj}")
            if self.on_error:
                await self.on_error(error_msg)

    async def _handle_speech_started(self):
        """Handle user speech interruption - truncate assistant audio and clear Twilio buffer."""
        print(f"[RealtimeSession] User speech detected - interrupting")

        if self.last_assistant_item and self.response_start_timestamp is not None:
            elapsed_ms = self.latest_media_timestamp - self.response_start_timestamp
            if elapsed_ms < 0:
                elapsed_ms = 0

            # Use mark_queue length to estimate actual playback position
            # Each mark corresponds to an audio delta chunk; fewer remaining marks
            # means more audio has been played. This provides a safer estimate
            # than raw timestamp math which can overshoot.
            truncate_event = {
                "type": "conversation.item.truncate",
                "item_id": self.last_assistant_item,
                "content_index": 0,
                "audio_end_ms": elapsed_ms
            }
            await self.send_event(truncate_event)
            print(f"[RealtimeSession] Sent truncate for item {self.last_assistant_item} at {elapsed_ms}ms")

        # Clear Twilio's audio buffer so queued audio stops immediately
        if self.on_interrupt:
            await self.on_interrupt()

        # Reset interrupt tracking state
        self.mark_queue.clear()
        self.last_assistant_item = None
        self.response_start_timestamp = None

    async def _handle_function_call(self, item: Dict[str, Any]):
        """Handle function call from OpenAI (RAG search, end_call)."""
        call_id = item.get("call_id")
        function_name = item.get("name")
        arguments_str = item.get("arguments", "{}")

        print(f"[Function Call] {function_name} with args: {arguments_str}")

        try:
            # Parse arguments
            args = json.loads(arguments_str)

            # Route to appropriate function handler
            if function_name == "search_knowledge_base":
                query = args.get("query", "")
                k = args.get("k", 5)
                result = await self._execute_rag_search(query, k)

            elif function_name == "end_call":
                reason = args.get("reason", "Conversation completed")
                result = await self._execute_end_call(reason)

            elif function_name == "check_calendar":
                date_str = args.get("date", "")
                result = await self._execute_check_calendar(date_str)

            elif function_name == "create_calendar_event":
                result = await self._execute_create_calendar_event(args)

            else:
                result = {"error": f"Unknown function: {function_name}"}

            # Send function result back to OpenAI
            response_event = {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(result)
                }
            }

            await self.send_event(response_event)

            # Trigger response generation (unless call ended)
            if function_name != "end_call":
                await self.send_event({"type": "response.create"})

        except Exception as e:
            print(f"[Function Call] Error: {e}")
            # Send error as function output
            error_event = {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps({"error": str(e)})
                }
            }
            await self.send_event(error_event)

    async def _execute_rag_search(self, query: str, k: int = 10) -> Dict[str, Any]:
        """Execute semantic search in RAG knowledge base."""
        try:
            db = get_service_client()
            ai = OpenAI(api_key=self.api_key)

            # Use existing semantic_search function
            matches = semantic_search(
                sb=db,
                ai=ai,
                agent_id=self.agent_id,
                query=query,
                k=k,
                min_similarity=0.3
            )

            if not matches:
                return {
                    "found": False,
                    "message": "No relevant information found in knowledge base."
                }

            # Format results
            results = []
            for match in matches:
                results.append({
                    "text": match.get("chunk_text", ""),
                    "similarity": match.get("score", 0.0),  # 'score' not 'similairty'
                    "filename": match.get("filename", ""),
                    "chunk_id": match.get("chunk_id"),
                })

            return {
                "found": True,
                "results": results,
                "summary": f"Found {len(results)} relevant chunks from knowledge base."
            }

        except Exception as e:
            print(f"[RAG Search] Error: {e}")
            return {
                "found": False,
                "error": str(e)
            }

    async def _execute_end_call(self, reason: str = "Conversation completed") -> Dict[str, Any]:
        """End the current phone call gracefully."""
        try:
            print(f"[EndCall] Ending call. Reason: {reason}")

            # The model should have already said goodbye before calling this function
            # Wait for any audio to finish playing
            await self._wait_for_audio_completion(timeout=4.0)

            # Close Twilio WebSocket connection
            if self.twilio_ws:
                await self.twilio_ws.close(code=1000, reason="Call ended by agent")

            # Update conversation status
            db = get_service_client()
            db.table('conversation').update({
                'status': 'completed',
                'ended_at': 'now()'
            }).eq('id', self.conversation_id).execute()

            # Disconnect OpenAI Realtime session
            await self.disconnect()

            return {
                "success": True,
                "message": f"Call ended successfully. Reason: {reason}"
            }

        except Exception as e:
            print(f"[EndCall] Error: {e}")
            return {
                "success": False,
                "error": str(e)
            }

    async def _execute_check_calendar(self, date_str: str) -> Dict[str, Any]:
        """Check calendar availability for a given date using freebusy API."""
        try:
            from api.crud.integrations import check_availability

            business_id = self.agent_config.get('business_id')
            if not business_id:
                return {"error": "No business associated with this agent"}

            # Build time range for the full day
            time_min = f"{date_str}T00:00:00Z"
            time_max = f"{date_str}T23:59:59Z"

            result = await check_availability(business_id, time_min, time_max)
            print(f"[Calendar] check_calendar for {date_str}: {result.get('count', 0)} busy slots")
            return result

        except Exception as e:
            print(f"[Calendar] Error checking calendar: {e}")
            return {"error": str(e)}

    async def _execute_create_calendar_event(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Create a Google Calendar event with settings enforcement."""
        try:
            from api.crud.integrations import create_calendar_event, check_availability
            from datetime import datetime, timedelta

            business_id = self.agent_config.get('business_id')
            if not business_id:
                return {"error": "No business associated with this agent"}

            # Get calendar settings
            cal_settings = self.tool_settings.get('google-calendar', {})
            default_duration = cal_settings.get('default_duration', 30)
            allow_conflicts = cal_settings.get('allow_conflicts', False)
            booking_window = cal_settings.get('booking_window_days', 30)
            biz_start = cal_settings.get('business_hours_start', '09:00')
            biz_end = cal_settings.get('business_hours_end', '17:00')

            date = args.get("date", "")
            start_time = args.get("start_time", "")
            end_time = args.get("end_time", "")
            summary = args.get("summary", "")
            description = args.get("description", "")

            # Apply default duration if no end_time provided
            if not end_time and start_time:
                start_parts = start_time.split(':')
                if len(start_parts) == 2:
                    start_hour = int(start_parts[0])
                    start_min = int(start_parts[1])
                    end_total_min = start_hour * 60 + start_min + default_duration
                    end_hour = end_total_min // 60
                    end_min = end_total_min % 60
                    end_time = f"{end_hour:02d}:{end_min:02d}"
                    print(f"[Calendar] Applied default duration {default_duration}min: end_time={end_time}")

            # Validate business hours
            if start_time < biz_start or end_time > biz_end:
                return {
                    "error": f"Appointment must be within business hours ({biz_start} to {biz_end}). Please choose a different time."
                }

            # Validate booking window
            try:
                event_date = datetime.strptime(date, "%Y-%m-%d").date()
                today = datetime.now().date()
                days_ahead = (event_date - today).days
                if days_ahead > booking_window:
                    return {
                        "error": f"Cannot book more than {booking_window} days in advance. Please choose an earlier date."
                    }
                if days_ahead < 0:
                    return {"error": "Cannot book appointments in the past."}
            except ValueError:
                pass  # If date parsing fails, let the calendar API handle it

            # Check for conflicts if not allowed
            if not allow_conflicts:
                time_min = f"{date}T{start_time}:00Z"
                time_max = f"{date}T{end_time}:00Z"
                availability = await check_availability(business_id, time_min, time_max)
                if availability.get('busy') and len(availability['busy']) > 0:
                    conflicting = availability['busy'][0]
                    return {
                        "error": f"There's already an appointment at that time (busy from {conflicting.get('start')} to {conflicting.get('end')}). Please choose a different time."
                    }

            start_dt = f"{date}T{start_time}:00"
            end_dt = f"{date}T{end_time}:00"

            result = await create_calendar_event(
                business_id=business_id,
                summary=summary,
                start_datetime=start_dt,
                end_datetime=end_dt,
                description=description,
            )
            print(f"[Calendar] create_calendar_event: {summary} on {date} {start_time}-{end_time}")
            return result

        except Exception as e:
            print(f"[Calendar] Error creating event: {e}")
            return {"error": str(e)}

    async def _save_message(self, role: str, content: str):
        """Save message to database."""
        try:
            db = get_service_client()
            db.table('message').insert({
                'conversation_id': self.conversation_id,
                'role': role,
                'content': content
            }).execute()
        except Exception as e:
            print(f"[RealtimeSession] Error saving message: {e}")

    async def disconnect(self):
        """Disconnect from OpenAI Realtime API."""
        self.running = False
        if self.ws:
            await self.ws.close()
            print(f"[RealtimeSession] Disconnected for conversation {self.conversation_id}")

    async def interrupt(self):
        """Manually interrupt the agent's response."""
        if not self.ws or not self.running:
            return

        event = {
            "type": "response.cancel"
        }
        await self.send_event(event)
        print("[RealtimeSession] Response interrupted")

if __name__ == "__main__":
    print("Success YAYYY! :D")
