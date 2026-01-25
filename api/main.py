# api/main.py

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from .crud.business import router as business_router
from .crud.agent import router as agent_router
from .crud.phone import router as phone_router
from .crud.conversation import router as conversation_router
from .crud.realtime_voice import router as realtime_router
from .crud.rag_endpoints import router as rag_router
from .crud.phone_maintenance import router as phone_maintenance_router
from api import __version__


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


app = FastAPI(
    title="HelloML API",
    description="API for managing AI voice agents with phone provisioning",
    version=__version__
)

# Add docs access restriction middleware (before CORS)
app.add_middleware(DocsAccessMiddleware)

# Configure CORS for frontend
app.add_middleware(
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
app.include_router(business_router)
app.include_router(agent_router)
app.include_router(phone_router)
app.include_router(conversation_router)
app.include_router(realtime_router)
app.include_router(rag_router)
app.include_router(phone_maintenance_router)


@app.get("/", summary="API status")
def index():
    """Returns API status"""
    return {"status": "running", "message": "HelloML API"}


@app.get("/version", summary="API version")
def version():
    """Returns API version"""
    return {"version": __version__}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
