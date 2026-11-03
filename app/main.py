import os
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from app.core.config import settings
from app.core.database import get_db_pool, close_db_pool
from app.services.runbooks import runbook_service
from app.api.chat import router as chat_router
from app.api.alertmanager import router as alertmanager_router
from app.api.remediate import router as remediate_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("kubesentinel.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifecycle: manages database connection and runbooks cache."""
    logger.info("Initializing KubeSentinel...")
    # Initialize runbooks
    loaded = runbook_service.get_all_runbooks()
    logger.info(f"Loaded {len(loaded)} operational runbooks.")

    # Initialize DB pool
    await get_db_pool()

    yield

    logger.info("Shutting down KubeSentinel...")
    await close_db_pool()


app = FastAPI(
    title="KubeSentinel",
    description="Autonomous Kubernetes SRE Copilot & Incident Agent",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register API Routers
app.include_router(chat_router)
app.include_router(alertmanager_router)
app.include_router(remediate_router)


@app.get("/api/health")
async def health_check():
    """Health check endpoint for Kubernetes liveness/readiness probes."""
    return {
        "status": "healthy",
        "service": "kubesentinel",
        "version": "1.0.0",
        "mock_modes": {
            "k8s": settings.K8S_MOCK_MODE,
            "prometheus": settings.PROMETHEUS_MOCK_MODE,
        },
    }


# Frontend Static Files
frontend_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend")
if os.path.exists(frontend_dir):
    app.mount("/static", StaticFiles(directory=frontend_dir), name="static")

    @app.get("/")
    async def serve_index():
        return FileResponse(os.path.join(frontend_dir, "index.html"))
