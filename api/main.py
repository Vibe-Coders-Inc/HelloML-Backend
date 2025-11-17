# api/main.py

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from .crud.business import router as business_router
from .crud.agent import router as agent_router
from .crud.phone import router as phone_router
from .crud.conversation import router as conversation_router
from .crud.realtime_voice import router as realtime_router
from .crud.rag_endpoints import router as rag_router
from api import __version__

app = FastAPI(
    title="HelloML API",
    description="API for managing AI voice agents with phone provisioning",
    version=__version__
)

# Configure CORS for frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://helloml.app",
        "https://www.helloml.app/dashboard",
        "https://helloml.app/dashboard",
        "http://localhost:3001",  # For local development
        "http://localhost:5173",  # Vite default port
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
