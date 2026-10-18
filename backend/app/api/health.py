"""Health and provider-status endpoints (Source of Truth §46, §38)."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter
from sqlalchemy import text

from ..config import get_settings
from ..db import get_engine
from ..schemas import HealthResponse, ModesResponse, ProviderStatusView
from ..services.extraction import extraction_status
from ..services.gateway import build_gateway
from ..services.stt import stt_status
from ..storage import get_storage

router = APIRouter(tags=["system"])


def _database_status() -> tuple[str, str | None]:
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return "ok", None
    except Exception as exc:  # noqa: BLE001 - surfaced in status payload
        return "error", str(exc)


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    settings = get_settings()
    db_status, _ = _database_status()
    return HealthResponse(
        status="ok" if db_status == "ok" else "degraded",
        version=settings.app_version,
        mode=settings.titan_mode,
        database=db_status,
        assets_dir=str(settings.assets_dir),
        time=datetime.now(timezone.utc),
    )


@router.get("/modes", response_model=ModesResponse)
def modes() -> ModesResponse:
    settings = get_settings()
    gateway = build_gateway(settings)
    providers = [
        ProviderStatusView(**status.as_dict()) for status in gateway.status_report()
    ]
    stt = stt_status(settings)
    extraction = extraction_status(settings)
    db_status, db_error = _database_status()

    storage_stats = get_storage().stats()
    return ModesResponse(
        mode=settings.titan_mode,
        providers=providers,
        stt=ProviderStatusView(**stt.as_dict()),
        extraction=ProviderStatusView(**extraction.as_dict()),
        seal={
            "configured": settings.seal_configured,
            "algorithm": "hmac-sha256",
            "detail": (
                "Fact locking is available."
                if settings.seal_configured
                else "Fact locking is disabled: set TITAN_SEAL_SECRET."
            ),
        },
        auth={
            "configured": settings.auth_configured,
            "production_ready": settings.auth_production_ready,
            "ephemeral_signing_key": settings.auth_secret_ephemeral,
            "demo_login_enabled": settings.demo_login_enabled,
            "phone_login_enabled": settings.phone_login_enabled,
            "google_login_enabled": settings.google_login_enabled,
            "token_ttl_hours": settings.auth_token_ttl_hours,
            "detail": _auth_detail(settings),
        },
        storage=storage_stats,
        database={"status": db_status, "error": db_error, "url": _safe_db_url(settings.database_url)},
    )


def _auth_detail(settings) -> str:
    if not settings.auth_configured:
        return "Authentication is unavailable: set TITAN_AUTH_SECRET."
    if settings.auth_secret_ephemeral:
        return (
            "Signing sessions with a per-process key: tokens are unforgeable but "
            "end at the next backend restart. Set TITAN_AUTH_SECRET to persist them."
        )
    return "Session signing is configured with a durable key."


def _safe_db_url(url: str) -> str:
    """Strip credentials from a database URL for display."""
    if "@" in url and "://" in url:
        scheme, _, rest = url.partition("://")
        _, _, host = rest.rpartition("@")
        return f"{scheme}://***@{host}"
    return url
