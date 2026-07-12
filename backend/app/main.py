"""
app/main.py
-----------
FastAPI application factory.
"""
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import ORJSONResponse

from app.core.config import settings
from app.api.v1 import auth, chat, agents, emails, memory, onboarding, extract, rfq_auto

logging.basicConfig(
    level=logging.DEBUG if not settings.is_production else logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    logger.info("🚀 Orion starting up (env=%s)", settings.app_env)
    yield
    logger.info("🛑 Orion shutting down")


app = FastAPI(
    title="Orion API",
    description="AI Agent Platform for B2B Enterprise",
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
    default_response_class=ORJSONResponse,
    lifespan=lifespan,
)

# ── CORS ─────────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        *settings.allowed_origins,
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Request timing middleware ─────────────────────────────────────────────────
@app.middleware("http")
async def add_process_time_header(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    duration = (time.perf_counter() - start) * 1000
    response.headers["X-Process-Time-Ms"] = f"{duration:.1f}"
    return response


# ── Routes ────────────────────────────────────────────────────────────────────
app.include_router(auth.router, prefix="/api/v1")
app.include_router(chat.router, prefix="/api/v1")
app.include_router(agents.router, prefix="/api/v1")
app.include_router(emails.router, prefix="/api/v1")
app.include_router(memory.router, prefix="/api/v1")
app.include_router(onboarding.router, prefix="/api/v1")
app.include_router(extract.router, prefix="/api/v1")
app.include_router(rfq_auto.router, prefix="/api/v1")


@app.get("/health", tags=["system"])
async def health():
    """Health check — used by load balancers and monitoring."""
    return {
        "status": "ok",
        "app": settings.app_name,
        "env": settings.app_env,
        "version": "0.1.0",
    }


@app.get("/", tags=["system"])
async def root():
    return {"message": f"Welcome to {settings.app_name} API. Docs at /docs"}
