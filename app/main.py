# ============================================================
# FILE   : app/main.py
# OWNER  : ROLE 2 — API & Data
# PURPOSE: App entrypoint, mounts routers, health check   [R2]
# ============================================================
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.dependencies import get_llm_provider
from app.routers import audit, decisions, rules

configure_logging()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Resolve the LLM provider at boot so its status is visible immediately.

    Without this the provider is resolved lazily on the first NL request,
    so a misconfiguration (offline stub instead of a real model) only
    surfaces as surprisingly poor AI behaviour mid-demo. Failing to
    resolve is not fatal — get_llm_provider() degrades to the stub.
    """
    get_llm_provider()
    yield


app = FastAPI(title="RuleFlow-AI", lifespan=lifespan)

# CORS: required so the static frontend (frontend/) can call this API from
# the browser. Wide-open is fine for the hackathon demo; restrict in prod.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

register_exception_handlers(app)

app.include_router(decisions.router)
app.include_router(rules.router)
app.include_router(audit.router)


@app.get("/health")
def health() -> dict:
    """Liveness plus the active LLM provider.

    The provider name is included so the frontend (and anyone curling this)
    can tell at a glance whether natural-language features are backed by a
    real model or the offline stub — the single most confusing thing to
    misdiagnose during a demo.
    """
    settings = get_settings()
    provider = get_llm_provider()
    return {
        "status": "ok",
        "llm_provider": settings.llm_provider,
        # For a wrapped provider report what's actually behind it, so this
        # says "GeminiProvider", not the wrapper's own name.
        "llm_active": getattr(provider, "primary_name", type(provider).__name__),
        # True once quota exhaustion has forced offline extraction.
        "llm_degraded": getattr(provider, "degraded", False),
    }
