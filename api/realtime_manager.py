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
        """
        self.agent_id = agent_id
        self.conversation_id = conversation_id
        self.agent_config = agent_config
        self.on_audio = on_audio
        self.on_transcript = on_transcript
        self.on_error = on_error

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
        url = "wss://api.openai.com/v1/realtime?model=gpt-realtime"

        headers = {
            "Authorization": f"Bearer {self.api_key}"
        }

        try:
            self.ws = await websockets.connect(url, additional_headers=headers)
            self.running = True
            print(f"[RealtimeSession] Connected for conversation {self.conversation_id}")

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
        instructions = self.agent_config.get('prompt', 'You are a helpful assistant.')
        voice = self.agent_config.get('voice_model', 'shimmer')
        temperature = self.agent_config.get('temperature', 0.7)

        session_config = {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "model": "gpt-realtime",
                "instructions": instructions,
                "audio": {
                    "input": {
                        "format": "pcm16"
                    },
                    "output": {
                        "voice": voice,
                        "format": "pcm16"
                    }
                },
                "input_audio_transcription": {
                    "model": "whisper-1"
                },
                "temperature": temperature,
                "turn_detection": {
                    "type": "semantic_vad",
                    "eagerness": "medium",
                    "interrupt_response": True
                },
                "tools": [self._get_rag_tool_definition()]
            }
        }

        await self.send_event(session_config)
        print(f"[RealtimeSession] Session configured with voice={voice}, temp={temperature}")

    def _get_rag_tool_definition(self) -> Dict[str, Any]:
        """Get the RAG semantic search function tool definition."""
        return {
            "type": "function",
            "name": "search_knowledge_base",
            "description": "Search the business's knowledge base (documents, FAQs, policies) for relevant information to answer customer questions accurately.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query to find relevant information in the knowledge base"
                    },
                    "k": {
                        "type": "integer",
                        "description": "Number of relevant chunks to return (default: 5)",
                        "default": 5
                    }
                },
                "required": ["query"]
            }
        }

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
        elif event_type == "conversation.item.created":
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
        """Handle function call from OpenAI (RAG search)."""
        call_id = item.get("call_id")
        function_name = item.get("name")
        arguments_str = item.get("arguments", "{}")

        print(f"[Function Call] {function_name} with args: {arguments_str}")

        if function_name != "search_knowledge_base":
            return

        try:
            # Parse arguments
            args = json.loads(arguments_str)
            query = args.get("query", "")
            k = args.get("k", 5)

            # Execute RAG search
            result = await self._execute_rag_search(query, k)

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

            # Trigger response generation
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

    async def _execute_rag_search(self, query: str, k: int = 5) -> Dict[str, Any]:
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
                min_similarity=0.6
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
