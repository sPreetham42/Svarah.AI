"""Application configuration.

Settings are read from environment variables (optionally loaded from a `.env`
file at the repository root). Keep this module dependency-light: a plain
dataclass avoids pulling in `pydantic-settings` for a handful of values.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent

VALID_MODES = ("live", "mock")
VALID_STT_PROVIDERS = ("auto", "mock", "sarvam", "faster-whisper", "none")
VALID_EXTRACTION_PROVIDERS = ("auto", "mock", "agnes", "none")
VALID_VOICE_PROVIDERS = ("auto", "mock", "sarvam", "none")
VALID_TASKS_MODES = ("local", "cloud_tasks")
VALID_VIDEO_PROVIDERS = ("auto", "agnes", "magichour")
APP_VERSION = "0.3.0"


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader. Existing environment variables win."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value != "" else default


_PLACEHOLDER_PREFIXES = ("your_", "paste_", "replace_", "changeme", "<")


def _env_secret(name: str) -> str | None:
    """A provider key, with `.env.example` placeholders treated as unset.

    Copying the template without filling it in must read as "not configured"
    (a clear message) rather than be sent to the provider as a bogus key.
    """
    value = _env(name)
    if value and value.lower().startswith(_PLACEHOLDER_PREFIXES):
        return None
    return value


def _env_secret_list(name: str) -> tuple[str, ...]:
    """Comma-separated provider keys (blank entries and placeholders dropped)."""
    raw = _env(name) or ""
    keys = [item.strip() for item in raw.split(",")]
    return tuple(k for k in keys if k and not k.lower().startswith(_PLACEHOLDER_PREFIXES))


def _env_secret_series(name: str, count: int) -> tuple[str, ...]:
    """Numbered provider keys: `NAME`, `NAME_2` … `NAME_<count>`.

    Unset entries and placeholders are skipped and duplicates dropped, so the
    keys can be filled in any order.
    """
    names = [name] + [f"{name}_{n}" for n in range(2, count + 1)]
    keys: list[str] = []
    for env_name in names:
        value = _env_secret(env_name)
        if value and value not in keys:
            keys.append(value)
    return tuple(keys)


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _resolve_choice(env_name: str, default: str, valid: tuple[str, ...]) -> str:
    """Read and validate a finite choice env var (empty/missing → default)."""
    raw = (_env(env_name, default) or default).strip().lower()
    if raw not in valid:
        raise ValueError(f"Invalid {env_name}={raw!r}. Expected one of {valid}.")
    return raw


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    titan_mode: str
    database_url: str
    assets_dir: Path
    cors_origins: tuple[str, ...]
    stt_provider: str
    whisper_model: str
    whisper_compute_type: str
    extraction_provider: str
    provider_timeout: float
    provider_max_retries: int
    provider_backoff_base: float
    agnes_api_base: str | None
    agnes_api_key: str | None
    agnes_text_model: str
    agnes_image_model: str
    agnes_video_model: str
    poster_art_fallback_dir: Path
    sarvam_api_base: str | None
    #: Up to four keys, tried in order; the next one is used when a key is
    #: rejected, out of credits or rate limited.
    sarvam_api_keys: tuple[str, ...]
    sarvam_stt_model: str
    voice_provider: str
    max_voice_sample_mb: int
    #: Text model the Brag Director subagent runs on.
    brag_agent_model: str
    enable_agnes_video: bool
    #: auto | agnes | magichour. `auto` uses Magic Hour when keys are set.
    video_provider: str
    magichour_api_base: str
    #: One or more keys; the client moves to the next when one is out of credits.
    magichour_api_keys: tuple[str, ...]
    magichour_model: str
    magichour_resolution: str | None
    max_video_jobs: int
    max_posters: int
    max_video_variants: int
    max_voice_variants: int
    max_copy_variants: int
    tasks_mode: str
    cloud_project: str | None
    cloud_tasks_location: str | None
    cloud_tasks_queue: str | None
    cloud_run_worker_url: str | None
    windsor_mcp_url: str
    windsor_auth_mode: str
    windsor_api_key: str | None
    windsor_cache_seconds: int
    uploadpost_api_base: str
    uploadpost_api_key: str | None
    enable_demo_sabotage: bool
    #: Dedicated HMAC key for the fact-lock seal. Never logged or returned.
    seal_secret: str | None
    #: Dedicated HMAC key for session tokens. Never logged or returned.
    #: Kept separate from `seal_secret` so rotating one never weakens the other.
    auth_secret: str | None
    #: True when `auth_secret` was generated for this process only (mock mode
    #: convenience). Tokens stay unforgeable but do not survive a restart.
    auth_secret_ephemeral: bool
    auth_token_ttl_hours: int
    allow_demo_login: bool
    #: Firebase Web API key of the project whose phone and Google sign-ins are accepted.
    #: Identifies the project to Google; it is not a credential by itself.
    firebase_api_key: str | None
    github_client_id: str | None
    github_client_secret: str | None
    max_request_bytes: int
    app_version: str = APP_VERSION

    @property
    def is_mock(self) -> bool:
        return self.titan_mode == "mock"

    @property
    def seal_configured(self) -> bool:
        return bool(self.seal_secret and self.seal_secret.strip())

    @property
    def auth_configured(self) -> bool:
        """True when session tokens can be signed at all."""
        return bool(self.auth_secret and self.auth_secret.strip())

    @property
    def auth_production_ready(self) -> bool:
        """True only with a durable, operator-supplied auth secret.

        Live mode refuses to serve authentication without one: an ephemeral
        per-process key would silently log every user out on each restart and
        cannot be shared across replicas.
        """
        return self.auth_configured and not self.auth_secret_ephemeral

    @property
    def demo_login_enabled(self) -> bool:
        """Password-less demo sign-in — mock mode only, never in live mode."""
        return self.is_mock and self.allow_demo_login

    @property
    def phone_login_enabled(self) -> bool:
        """Phone sign-in needs a Firebase project to verify against and a key to sign sessions."""
        return bool(self.firebase_api_key) and self.auth_configured

    @property
    def github_login_enabled(self) -> bool:
        return bool(self.github_client_id and self.github_client_secret) and self.auth_configured

    @property
    def google_login_enabled(self) -> bool:
        """Google sign-in is verified through the same Firebase project as phone sign-in."""
        return self.phone_login_enabled

    @property
    def max_audio_bytes(self) -> int:
        return 25 * 1024 * 1024  # 25 MB per upload

    @property
    def max_voice_sample_bytes(self) -> int:
        return self.max_voice_sample_mb * 1024 * 1024

    @property
    def agnes_configured(self) -> bool:
        return bool(self.agnes_api_base and self.agnes_api_key)

    @property
    def sarvam_api_key(self) -> str | None:
        """The primary Sarvam key (the first configured one)."""
        return self.sarvam_api_keys[0] if self.sarvam_api_keys else None

    @property
    def sarvam_configured(self) -> bool:
        return bool(self.sarvam_api_keys)

    @property
    def magichour_configured(self) -> bool:
        return bool(self.magichour_api_keys)

    @property
    def video_engine(self) -> str:
        """The AI video provider live mode will actually call."""
        if self.video_provider == "auto":
            return "magichour" if self.magichour_configured else "agnes"
        return self.video_provider

    @property
    def agnes_video_enabled(self) -> bool:
        return self.enable_agnes_video and self.agnes_configured

    @property
    def windsor_configured(self) -> bool:
        return bool((self.windsor_auth_mode == "oauth") or self.windsor_api_key)

    @property
    def uploadpost_configured(self) -> bool:
        """Whether posting to the owner's own social accounts can really run."""
        return bool(self.uploadpost_api_key)


def _build_settings() -> Settings:
    _load_dotenv(REPO_ROOT / ".env")
    _load_dotenv(BACKEND_DIR / ".env")

    mode = (_env("TITAN_MODE", "mock") or "mock").lower()
    if mode not in VALID_MODES:
        raise ValueError(
            f"Invalid TITAN_MODE={mode!r}. Expected one of {VALID_MODES}."
        )

    stt_provider = (_env("TITAN_STT_PROVIDER", "auto") or "auto").lower()
    if stt_provider not in VALID_STT_PROVIDERS:
        raise ValueError(
            f"Invalid TITAN_STT_PROVIDER={stt_provider!r}. "
            f"Expected one of {VALID_STT_PROVIDERS}."
        )

    extraction_provider = (_env("TITAN_EXTRACTION_PROVIDER", "auto") or "auto").lower()
    if extraction_provider not in VALID_EXTRACTION_PROVIDERS:
        raise ValueError(
            f"Invalid TITAN_EXTRACTION_PROVIDER={extraction_provider!r}. "
            f"Expected one of {VALID_EXTRACTION_PROVIDERS}."
        )

    tasks_mode = (_env("TITAN_TASKS_MODE", "local") or "local").lower()
    if tasks_mode not in VALID_TASKS_MODES:
        raise ValueError(
            f"Invalid TITAN_TASKS_MODE={tasks_mode!r}. Expected one of {VALID_TASKS_MODES}."
        )

    database_url = _env("TITAN_DATABASE_URL") or f"sqlite:///{(BACKEND_DIR / 'titan.db').as_posix()}"
    assets_dir = Path(_env("TITAN_ASSETS_DIR") or (BACKEND_DIR / "assets_store"))
    if not assets_dir.is_absolute():
        assets_dir = (REPO_ROOT / assets_dir).resolve()

    origins = _env("TITAN_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")
    cors_origins = tuple(o.strip() for o in (origins or "").split(",") if o.strip())

    # Session-token key. A missing key is NOT treated as "no auth needed":
    # mock mode mints a random per-process key (tokens remain unforgeable but
    # do not survive a restart) and live mode refuses to serve auth at all.
    auth_secret = _env("TITAN_AUTH_SECRET")
    auth_secret_ephemeral = False
    if not auth_secret:
        if mode == "mock":
            auth_secret = secrets.token_urlsafe(48)
            auth_secret_ephemeral = True
        else:
            auth_secret = None

    return Settings(
        titan_mode=mode,
        database_url=database_url,
        assets_dir=assets_dir,
        cors_origins=cors_origins,
        stt_provider=stt_provider,
        whisper_model=_env("TITAN_WHISPER_MODEL", "base") or "base",
        whisper_compute_type=_env("TITAN_WHISPER_COMPUTE_TYPE", "int8") or "int8",
        extraction_provider=extraction_provider,
        provider_timeout=_env_float("TITAN_PROVIDER_TIMEOUT", 30.0),
        provider_max_retries=_env_int("TITAN_PROVIDER_MAX_RETRIES", 2),
        provider_backoff_base=_env_float("TITAN_PROVIDER_BACKOFF_BASE", 0.5),
        agnes_api_base=_env("TITAN_AGNES_API_BASE"),
        agnes_api_key=_env_secret("TITAN_AGNES_API_KEY"),
        agnes_text_model=_env("TITAN_AGNES_TEXT_MODEL", "agnes-3.0-flash") or "agnes-3.0-flash",
        agnes_image_model=_env("TITAN_AGNES_IMAGE_MODEL", "agnes-image-2.5-flash") or "agnes-image-2.5-flash",
        agnes_video_model=_env("TITAN_AGNES_VIDEO_MODEL", "agnes-video-2.5") or "agnes-video-2.5",
        poster_art_fallback_dir=(REPO_ROOT / (_env("TITAN_POSTER_ART_FALLBACK_DIR") or "backend/assets/prebaked_art")).resolve(),
        sarvam_api_base=_env("TITAN_SARVAM_API_BASE", "https://api.sarvam.ai"),
        sarvam_api_keys=_env_secret_series("TITAN_SARVAM_API_KEY", 4),
        sarvam_stt_model=_env("TITAN_SARVAM_STT_MODEL", "saaras:v4") or "saaras:v4",
        voice_provider=_resolve_choice("TITAN_VOICE_PROVIDER", "auto", VALID_VOICE_PROVIDERS),
        max_voice_sample_mb=_env_int("TITAN_MAX_VOICE_SAMPLE_MB", 50),
        brag_agent_model=_env("TITAN_BRAG_AGENT_MODEL", "agnes-3.0-flash") or "agnes-3.0-flash",
        enable_agnes_video=(_env("TITAN_ENABLE_AGNES_VIDEO", "true") or "true").lower() != "false",
        video_provider=_resolve_choice("TITAN_VIDEO_PROVIDER", "auto", VALID_VIDEO_PROVIDERS),
        magichour_api_base=_env("TITAN_MAGICHOUR_API_BASE", "https://api.magichour.ai") or "https://api.magichour.ai",
        magichour_api_keys=_env_secret_list("TITAN_MAGICHOUR_API_KEYS"),
        magichour_model=_env("TITAN_MAGICHOUR_MODEL", "default") or "default",
        magichour_resolution=_env("TITAN_MAGICHOUR_RESOLUTION"),
        max_video_jobs=_env_int("TITAN_MAX_VIDEO_JOBS_PER_CAMPAIGN", 2),
        max_posters=_env_int("TITAN_MAX_POSTERS", 3),
        max_video_variants=_env_int("TITAN_MAX_VIDEO_VARIANTS", 2),
        max_voice_variants=_env_int("TITAN_MAX_VOICE_VARIANTS", 5),
        max_copy_variants=_env_int("TITAN_MAX_COPY_VARIANTS", 3),
        tasks_mode=_resolve_choice("TITAN_TASKS_MODE", "local", VALID_TASKS_MODES),
        cloud_project=_env("TITAN_CLOUD_PROJECT"),
        cloud_tasks_location=_env("TITAN_CLOUD_TASKS_LOCATION"),
        cloud_tasks_queue=_env("TITAN_CLOUD_TASKS_QUEUE"),
        cloud_run_worker_url=_env("TITAN_CLOUD_RUN_WORKER_URL"),
        windsor_mcp_url=_env("TITAN_WINDSOR_MCP_URL", "https://mcp.windsor.ai/") or "https://mcp.windsor.ai/",
        windsor_auth_mode=(_env("TITAN_WINDSOR_AUTH_MODE", "oauth") or "oauth").lower(),
        windsor_api_key=_env_secret("TITAN_WINDSOR_API_KEY"),
        windsor_cache_seconds=_env_int("TITAN_WINDSOR_CONNECTOR_CACHE_SECONDS", 300),
        uploadpost_api_base=_env("TITAN_UPLOADPOST_API_BASE", "https://api.upload-post.com") or "https://api.upload-post.com",
        uploadpost_api_key=_env_secret("TITAN_UPLOADPOST_API_KEY"),
        enable_demo_sabotage=(_env("TITAN_ENABLE_DEMO_SABOTAGE", "false") or "false").lower() == "true",
        seal_secret=_env("TITAN_SEAL_SECRET"),
        auth_secret=auth_secret,
        auth_secret_ephemeral=auth_secret_ephemeral,
        auth_token_ttl_hours=_env_int("TITAN_AUTH_TOKEN_TTL_HOURS", 8760),
        allow_demo_login=(_env("TITAN_ALLOW_DEMO_LOGIN", "true") or "true").lower() != "false",
        firebase_api_key=_env_secret("TITAN_FIREBASE_API_KEY"),
        github_client_id=_env("TITAN_GITHUB_CLIENT_ID"),
        github_client_secret=_env_secret("TITAN_GITHUB_CLIENT_SECRET"),
        max_request_bytes=_env_int("TITAN_MAX_REQUEST_BYTES", 32 * 1024 * 1024),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return _build_settings()


def reset_settings_cache() -> None:
    """Test hook: re-read environment variables."""
    get_settings.cache_clear()
