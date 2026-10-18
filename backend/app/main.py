"""Titan backend application factory (Source of Truth §4, §47)."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from .api import (
    assets,
    auth,
    campaigns,
    factsheets,
    guardian_api,
    health,
    plans,
    publisher,
    social,
    stt_stream,
    videos,
    voice_turn,
    voice_profiles,
)
from .config import get_settings
from .db import init_db
from .errors import error_body, register_exception_handlers
from .storage import get_storage

logger = logging.getLogger("titan")


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    init_db()
    storage = get_storage()  # creates the asset root if missing
    logger.info(
        "Titan started mode=%s db=%s assets=%s",
        settings.titan_mode,
        settings.database_url,
        storage.root,
    )
    # Configuration problems are surfaced at startup rather than at the first
    # failing request. Neither is fatal in mock mode; both are fatal in live.
    if not settings.seal_configured:
        logger.warning(
            "TITAN_SEAL_SECRET is not set: fact locking will be refused "
            "(no unsigned 'locked' facts are ever produced)."
        )
    if settings.auth_secret_ephemeral:
        logger.warning(
            "TITAN_AUTH_SECRET is not set: signing sessions with a random "
            "per-process key. Tokens stay unforgeable but every restart signs "
            "users out. Set TITAN_AUTH_SECRET for a stable local setup."
        )
    if not settings.is_mock and not settings.auth_production_ready:
        logger.error(
            "TITAN_MODE=live without a durable TITAN_AUTH_SECRET: "
            "authentication endpoints will refuse to issue sessions."
        )
    if not settings.is_mock:
        # Live mode never substitutes a mock for a missing provider, so say
        # at startup which features will fail (key values are never logged).
        if not settings.agnes_configured:
            logger.error(
                "TITAN_MODE=live but Agnes is not configured (TITAN_AGNES_API_BASE / "
                "TITAN_AGNES_API_KEY): extraction, validation, planning, image and "
                "video generation will return errors."
            )
        if not settings.sarvam_configured:
            logger.error(
                "TITAN_MODE=live but TITAN_SARVAM_API_KEY is not set: speech-to-text "
                "and text-to-speech will return errors."
            )
        if settings.stt_provider == "faster-whisper":
            logger.warning(
                "TITAN_STT_PROVIDER=faster-whisper is a legacy option; Sarvam is the "
                "supported speech provider (TITAN_STT_PROVIDER=sarvam)."
            )
    elif settings.agnes_configured or settings.sarvam_configured:
        logger.warning(
            "Provider keys are set but TITAN_MODE=mock: all providers are mocked. "
            "Set TITAN_MODE=live to use Agnes and Sarvam."
        )
    yield


class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject oversized bodies before a route (or a parser) sees them.

    Audio and reference-voice uploads arrive as base64 inside JSON, so the
    per-field limits in `Settings` are not enough on their own: a single huge
    request would still be buffered and decoded first.
    """

    def __init__(self, app, max_bytes: int) -> None:
        super().__init__(app)
        self._max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next):
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > self._max_bytes:
                    return JSONResponse(
                        status_code=413,
                        content=error_body(
                            "request_too_large",
                            f"The request body exceeds the {self._max_bytes} byte limit.",
                            {"limit_bytes": self._max_bytes},
                        ),
                    )
            except ValueError:
                return JSONResponse(
                    status_code=400,
                    content=error_body("bad_content_length", "Content-Length is not a number."),
                )
        return await call_next(request)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Svarah.ai — Marketing OS",
        version=settings.app_version,
        description="Voice-first marketing campaign system with a Fact Integrity core.",
        lifespan=lifespan,
    )

    app.add_middleware(RequestSizeLimitMiddleware, max_bytes=settings.max_request_bytes)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    register_exception_handlers(app)

    app.include_router(health.router, prefix="/api")
    app.include_router(auth.router, prefix="/api")
    app.include_router(stt_stream.router, prefix="/api")
    app.include_router(campaigns.router, prefix="/api")
    app.include_router(factsheets.router, prefix="/api")
    app.include_router(plans.router, prefix="/api")
    app.include_router(assets.router, prefix="/api")
    app.include_router(voice_profiles.router, prefix="/api")
    app.include_router(videos.router, prefix="/api")
    app.include_router(voice_turn.router, prefix="/api")
    app.include_router(guardian_api.router, prefix="/api")
    app.include_router(publisher.router, prefix="/api")
    app.include_router(social.router, prefix="/api")

    @app.get("/", include_in_schema=False)
    def root() -> dict[str, str]:
        return {"name": "Svarah API", "docs": "/docs", "health": "/api/health"}

    @app.get("/health", include_in_schema=False)
    def health_alias():
        """Root-level liveness alias.

        Monitoring agents and load balancers routinely probe `/health`; the
        canonical endpoint lives under `/api`. This returns the same payload so
        those probes succeed instead of filling the log with 404s.
        """
        return health.health()

    return app


app = create_app()
