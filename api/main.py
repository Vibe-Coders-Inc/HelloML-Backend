# api/main.py

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp, Receive, Send, Scope
import sys
import os
import re

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from .crud.business import router as business_router
from .crud.agent import router as agent_router
from .crud.phone import router as phone_router
from .crud.conversation import router as conversation_router
from .crud.realtime_voice import router as realtime_router
from .crud.rag_endpoints import router as rag_router
from .crud.phone_maintenance import router as phone_maintenance_router
from .crud.billing import router as billing_router
from .crud.integrations import router as integrations_router
from .crud.demo import router as demo_router
from api import __version__


class FlyReplayMiddleware:
    """
    ASGI middleware for Fly.io session affinity.

    Intercepts WebSocket upgrade requests to media-stream endpoints and checks
    if the request is on the correct machine. If not, returns an HTTP response
    with fly-replay header to redirect to the correct instance.

    This enables horizontal scaling by ensuring voice call WebSocket connections
    stay on the same machine that handled the initial Twilio webhook.
    """

    # Pattern to match media-stream WebSocket paths with machine ID
    MEDIA_STREAM_PATTERN = re.compile(r'^/conversation/\d+/media-stream/([a-zA-Z0-9]+)$')

    def __init__(self, app: ASGIApp):
        self.app = app
        self.current_machine_id = os.getenv("FLY_MACHINE_ID", "local")

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] == "websocket":
            path = scope.get("path", "")
            match = self.MEDIA_STREAM_PATTERN.match(path)

            if match:
                target_machine_id = match.group(1)

                # Check if we need to replay to a different machine
                if target_machine_id != "local" and target_machine_id != self.current_machine_id:
                    print(f"[FlyReplay] Replaying WebSocket from {self.current_machine_id} to {target_machine_id}", flush=True)

                    # Send HTTP 307 response with fly-replay header before WebSocket upgrade
                    await send({
                        "type": "websocket.http.response.start",
                        "status": 307,
                        "headers": [
                            (b"fly-replay", f"instance={target_machine_id}".encode()),
                            (b"content-type", b"text/plain"),
                        ],
                    })
                    await send({
                        "type": "websocket.http.response.body",
                        "body": b"Replaying to correct instance",
                    })
                    return

        # Not a replay case, continue normally
        await self.app(scope, receive, send)


class DocsAccessMiddleware(BaseHTTPMiddleware):
    """Restrict /docs and /redoc access to dev environments only."""

    # Hosts where docs should be accessible
    ALLOWED_HOSTS = [
        "api.dev.helloml.app",
        "fly.dev",  # Fly.io direct URL for dev access
        "localhost",
        "127.0.0.1",
    ]

    # Hosts where docs should be blocked (production)
    BLOCKED_HOSTS = [
        "api.helloml.app",
    ]

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Check if accessing docs endpoints
        if path in ["/docs", "/redoc", "/openapi.json"]:
            host = request.headers.get("host", "").split(":")[0]  # Remove port if present

            # Block if host is production
            if any(blocked in host for blocked in self.BLOCKED_HOSTS):
                return JSONResponse(
                    status_code=404,
                    content={"detail": "Not found"}
                )

            # Allow if host is in allowed list (dev environments)
            is_allowed = any(allowed in host for allowed in self.ALLOWED_HOSTS)

            if not is_allowed:
                return JSONResponse(
                    status_code=404,
                    content={"detail": "Not found"}
                )

        return await call_next(request)


_app = FastAPI(
    title="HelloML API",
    description="API for managing AI voice agents with phone provisioning",
    version=__version__
)

# Add docs access restriction middleware (before CORS)
_app.add_middleware(DocsAccessMiddleware)

# Configure CORS for frontend
_app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://helloml.app",          # Production domain
        "https://www.helloml.app",      # Production with www
        "http://localhost:3000",        # Local development
        "http://localhost:5173",        # Vite dev server
        "https://dev.helloml.app"       # Dev Environment
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register all routers
_app.include_router(business_router)
_app.include_router(agent_router)
_app.include_router(phone_router)
_app.include_router(conversation_router)
_app.include_router(realtime_router)
_app.include_router(rag_router)
_app.include_router(phone_maintenance_router)
_app.include_router(billing_router)
_app.include_router(integrations_router)
_app.include_router(demo_router)


@_app.get("/", summary="API status")
def index():
    """Returns API status"""
    return {"status": "running", "message": "HelloML API"}


@_app.get("/version", summary="API version")
def version():
    """Returns API version"""
    return {"version": __version__}


# Wrap with Fly.io session affinity middleware for WebSocket routing
# This must be the outermost middleware to intercept WebSocket upgrades
app = FlyReplayMiddleware(_app)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(_app, host="0.0.0.0", port=8000)
