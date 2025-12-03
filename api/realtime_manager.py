"""
OpenAI Realtime API Session Manager.

Manages WebSocket connection to OpenAI Realtime API, handles session configuration,
function calling for RAG, and transcript storage.
"""

import json
import asyncio
import websockets
from typing import Optional, Dict, Any, Callable
from api.database import supabase
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
        on_audio: Optional[Callable] = None,
        on_transcript: Optional[Callable] = None,
        on_error: Optional[Callable] = None,
        twilio_ws: Optional[Any] = None,
        call_sid: Optional[str] = None,
        greeting: Optional[str] = None,
        goodbye: Optional[str] = None,
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
            twilio_ws: Twilio Media Stream WebSocket connection
            call_sid: Twilio call SID for call control
            greeting: Initial greeting message to speak when call starts
            goodbye: Farewell message to speak before call ends
        """
        self.agent_id = agent_id
        self.conversation_id = conversation_id
        self.agent_config = agent_config
        self.on_audio = on_audio
        self.on_transcript = on_transcript
        self.on_error = on_error
        self.twilio_ws = twilio_ws
        self.call_sid = call_sid
        self.greeting = greeting or "Hello! How can I help you today?"
        self.goodbye = goodbye or "Goodbye! Have a great day!"

        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.api_key = os.getenv("OPENAI_API_KEY")
        self.running = False

        # For accumulating transcripts
        self.current_user_transcript = ""
        self.current_agent_transcript = ""

        # Track function call state
        self.pending_function_calls: Dict[str, Dict] = {}

    async def connect(self):
        """Connect to OpenAI Realtime API and configure session."""
        # Get model from agent config, use latest GA model as default
        model = self.agent_config.get('model_type') or 'gpt-realtime-2025-08-28'

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
        """Configure the Realtime session with agent settings."""
        # Extract agent configuration
        default_prompt = """You are a helpful AI voice assistant.

                            AVAILABLE TOOLS:
                            1. search_knowledge_base - Search uploaded documents to find accurate information
                            2. end_call - End the phone call

                            TOOL USAGE GUIDELINES:
                            - Use search_knowledge_base to find information from uploaded documents before answering questions
                            - Use end_call when: the customer asks to hang up, the conversation is complete, or the issue is fully resolved
                            - Always be polite, professional, and helpful"""

        # Get base instructions from agent config
        # Use 'or' to handle empty strings, not just None
        base_instructions = self.agent_config.get('prompt') or default_prompt

        # Build complete instructions with directives at the top
        instructions = f"""*** CRITICAL INSTRUCTIONS - FOLLOW EXACTLY ***

                                1. LANGUAGE: You MUST speak ONLY in English. NEVER use Spanish, French, or any other language under any circumstances UNLESS YOU ARE GETTING RESPONSES IN THAT LANGUAGE.

                                2. FIRST RESPONSE / GREETING: Your very first words when this call starts MUST be EXACTLY: "{self.greeting}"
                                - Say this greeting immediately and exactly as written
                                - Do NOT add any introduction, do NOT say "hello" or "hi" first
                                - After the greeting, wait for the user to respond

                                3. FUNCTION CALLING - YOU HAVE ACCESS TO THREE TOOLS THAT YOU MUST USE:

                                A. search_knowledge_base - *** ABSOLUTE REQUIREMENT: SEARCH BEFORE EVERY ANSWER ***

                                    *** YOU ARE STRICTLY PROHIBITED FROM: ***
                                    - Using your general knowledge or training data
                                    - Making assumptions based on common sense
                                    - Answering ANY question without searching first
                                    - Giving up after one failed search

                                    *** MANDATORY SEARCH PROTOCOL: ***

                                    RULE 1: SEARCH FIRST, ALWAYS
                                    Before answering ANY customer question (except greetings/small talk), you MUST:
                                    1. Call search_knowledge_base with the customer's key terms
                                    2. If no results: Try AGAIN with DIFFERENT search terms (synonyms, broader terms)
                                    3. If still no results: Try a THIRD time with simplified keywords

                                    RULE 2: UPLOADED DOCUMENTS = ONLY SOURCE OF TRUTH
                                    - Search results are the HOLY GRAIL - the ONLY source you can use
                                    - NEVER use your general knowledge about businesses, menus, or products
                                    - If it's not in the search results, you DON'T know it
                                    - Even if you "know" something from training, IGNORE IT - only use search results

                                    RULE 3: SEARCH STRATEGY (try IN ORDER)
                                    - Attempt 1: Use exact words from customer's question
                                    - Attempt 2: Use synonyms (e.g., "iced coffee" → "cold coffee", "coffee beverage")
                                    - Attempt 3: Use category terms (e.g., "drinks", "beverages", "menu items")

                                B. end_call - End the conversation gracefully
                                    WHEN TO USE:
                                    - Customer says goodbye, "that's all", "thank you bye", etc.
                                    - Issue is fully resolved and customer seems satisfied
                                    - Customer explicitly says they want to hang up

                                    HOW TO USE:
                                    - Call end_call(reason="brief explanation")
                                    - Example: end_call(reason="Customer inquiry resolved")
                                    - The goodbye message will be said automatically

                                4. GOODBYE: When ending the call, say EXACTLY: "{self.goodbye}"

                                *** YOUR ROLE ***

                                {base_instructions}

                                *** REMEMBER ***
                                - SEARCH 3 TIMES MINIMUM before saying "I don't have that information"
                                - TRY DIFFERENT KEYWORDS: exact terms → synonyms → categories
                                - UPLOADED DOCUMENTS = ONLY SOURCE - never use general knowledge or training data
                                - Every factual answer MUST come from search_knowledge_base results
                                - Be conversational and natural while following these strict search requirements"""

        # Get configuration from agent settings
        model = self.agent_config.get('model_type') or 'gpt-realtime-2025-08-28'

        session_config = {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "instructions": instructions,
                "tools": [
                    self._get_rag_tool_definition(),
                    self._get_end_call_tool_definition()
                ],
                "tool_choice": "auto"
            }
        }

        await self.send_event(session_config)
        print(f"[RealtimeSession] Session configured with model={model}")

        # Trigger the initial greeting by sending a silent user message
        await self._trigger_initial_greeting()

    def _get_rag_tool_definition(self) -> Dict[str, Any]:
        """Get the RAG semantic search function tool definition."""
        return {
            "type": "function",
            "name": "search_knowledge_base",
            "description": "MANDATORY: Search uploaded documents - the ONLY source of truth. MUST be called before answering ANY factual question about the business. Call multiple times with different queries if first search fails. NEVER answer without searching first.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query to find relevant information in the knowledge base"
                    },
                    "k": {
                        "type": "integer",
                        "description": "Number of relevant chunks to return (default: 10)",
                        "default": 10
                    }
                },
                "required": ["query"]
            }
        }

    async def _trigger_initial_greeting(self):
        """
        Trigger the agent to speak the greeting by sending a fake user input.

        The OpenAI Realtime API with server VAD waits for user input before responding.
        We send a minimal user message to trigger the agent's first response (the greeting).
        """
        try:
            # Send a conversation item as if the user connected/said hello
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

            # Trigger the assistant to respond with the greeting
            response_event = {
                "type": "response.create"
            }
            await self.send_event(response_event)
            print(f"[RealtimeSession] Triggered initial greeting")

        except Exception as e:
            print(f"[RealtimeSession] Failed to trigger initial greeting: {e}")

    async def _send_goodbye_message(self):
        """
        Trigger the agent to say goodbye before ending.

        Since the goodbye directive is in the system instructions, we add a system
        message to remind the agent to say goodbye, then trigger a response.
        """
        try:
            # Add a system reminder to say goodbye
            reminder_event = {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "system",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "The call is ending. Say your goodbye message to the caller now."
                        }
                    ]
                }
            }
            await self.send_event(reminder_event)

            # Trigger response to make the agent say goodbye
            response_event = {
                "type": "response.create"
            }
            await self.send_event(response_event)
            print(f"[RealtimeSession] Triggered goodbye message (will say: {self.goodbye})")

        except Exception as e:
            print(f"[RealtimeSession] Failed to trigger goodbye message: {e}")

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

        # Audio output from AI
        if event_type == "response.output_audio.delta":
            audio_base64 = event.get("delta")
            if audio_base64 and self.on_audio:
                await self.on_audio(audio_base64)

        # User speech transcript
        elif event_type == "input_audio_transcription.completed":
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

        # Error handling
        elif event_type == "error":
            error_msg = event.get("error", {}).get("message", "Unknown error")
            print(f"[RealtimeSession] Error: {error_msg}")
            if self.on_error:
                await self.on_error(error_msg)

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
            db = supabase()
            ai = OpenAI(api_key=self.api_key)

            # Use existing semantic_search function
            matches = semantic_search(
                sb=db,
                ai=ai,
                agent_id=self.agent_id,
                query=query,
                k=k,
                min_similarity=0.5
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
                    "similarity": match.get("similarity", 0.0),
                    "document_id": match.get("document_id")
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

            # Send goodbye message before ending call
            await self._send_goodbye_message()

            # Wait briefly for goodbye audio to play (approximately 3 seconds for typical goodbye message)
            await asyncio.sleep(3)

            # Close Twilio WebSocket connection
            if self.twilio_ws:
                await self.twilio_ws.close(code=1000, reason="Call ended by agent")

            # Update conversation status
            db = supabase()
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

    async def _save_message(self, role: str, content: str):
        """Save message to database."""
        try:
            db = supabase()
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
