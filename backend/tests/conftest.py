"""Shared pytest fixtures.

Environment variables are set *before* the app package is imported so the
cached settings/engine bind to a throwaway SQLite DB and asset directory.

Authentication note: every owned route requires a signed bearer token, so the
`client` fixture is pre-authenticated as a single primary account. Tests that
need to prove *cross-account* isolation use the `other_account` fixture, which
yields a second real account rather than a client-supplied owner string.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="titan-tests-"))

os.environ["TITAN_MODE"] = "mock"
os.environ["TITAN_STT_PROVIDER"] = "auto"
os.environ["TITAN_EXTRACTION_PROVIDER"] = "auto"
os.environ["TITAN_SEAL_SECRET"] = "test-seal-secret-do-not-use-in-prod"
# Set explicitly so tokens stay valid across `reset_settings_cache()` calls;
# without it mock mode would mint a fresh per-process key each rebuild.
os.environ["TITAN_AUTH_SECRET"] = "test-auth-secret-do-not-use-in-prod"
os.environ["TITAN_DATABASE_URL"] = f"sqlite:///{(_TMP / 'test.db').as_posix()}"
os.environ["TITAN_ASSETS_DIR"] = str(_TMP / "assets")
for _provider_key in (
    "TITAN_AGNES_API_BASE",
    "TITAN_AGNES_API_KEY",
    "TITAN_SARVAM_API_KEY",
    "TITAN_SARVAM_API_KEY_2",
    "TITAN_SARVAM_API_KEY_3",
    "TITAN_SARVAM_API_KEY_4",
    "TITAN_WINDSOR_API_KEY",
    "TITAN_UPLOADPOST_API_KEY",
    "TITAN_AGNES_INTERFACE_VERIFIED",
    "TITAN_SARVAM_INTERFACE_VERIFIED",
):
    os.environ.pop(_provider_key, None)

# Tests must be hermetic. `_build_settings` re-reads the repository-root `.env`
# on every build, and that file may hold live provider credentials and
# interface-verified flags — which would silently change what these tests
# assert. Disable the loader outright: everything a test needs is set in
# `os.environ` above, or by the `env_override` fixture.
from app import config as _config  # noqa: E402  (the env above must be set first)

_config._load_dotenv = lambda path: None

PRIMARY_EMAIL = "primary@titan.test"
OTHER_EMAIL = "other@titan.test"


@pytest.fixture(scope="session")
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


def _create_account(email: str, display_name: str) -> str:
    """Insert an account directly and return its bearer token.

    Going through the model rather than `POST /auth/register` keeps the fixture
    usable for tests that are not about registration, and keeps the owner UID
    deterministic (it is derived from the email, not the row id).
    """
    from app.config import get_settings
    from app.db import get_session_factory
    from app.models import User
    from app.security import mint_token, owner_uid_for_email

    uid = owner_uid_for_email(email)
    with get_session_factory()() as session:
        existing = session.query(User).filter(User.owner_uid == uid).one_or_none()
        if existing is None:
            session.add(
                User(
                    owner_uid=uid,
                    email=email,
                    display_name=display_name,
                    password_hash=None,
                    is_demo=True,
                )
            )
            session.commit()
    token, _ = mint_token(uid=uid, email=email, settings=get_settings())
    return token


@pytest.fixture(autouse=True)
def _clean_db(client):
    """Truncate all tables before each test, then re-seed the primary account."""
    from app.db import Base, get_session_factory

    with get_session_factory()() as session:
        for table in reversed(Base.metadata.sorted_tables):
            session.execute(table.delete())
        session.commit()

    token = _create_account(PRIMARY_EMAIL, "Primary Shop")
    client.headers["Authorization"] = f"Bearer {token}"
    yield


@pytest.fixture
def other_account(client):
    """Headers for a second, unrelated account (for isolation tests)."""
    token = _create_account(OTHER_EMAIL, "Other Shop")
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def anonymous(client):
    """Headers with no credential at all."""
    return {"Authorization": ""}


@pytest.fixture
def env_override(monkeypatch):
    """Change env vars + reset cached settings/engine between tests."""
    from app import config, db
    from app.storage import reset_storage_cache

    def _apply(**values: str):
        for key, value in values.items():
            monkeypatch.setenv(key, value)
        config.reset_settings_cache()
        db.reset_engine()
        reset_storage_cache()

    yield _apply

    from app import config as _config, db as _db
    from app.storage import reset_storage_cache as _reset_storage

    _reset_storage()
    _db.reset_engine()
    _config.reset_settings_cache()


@pytest.fixture
def no_seal_secret(monkeypatch):
    """Temporarily remove the HMAC seal secret from EVERY source.

    ``app.config._build_settings`` re-reads the repo-root ``.env`` on every
    build, so deleting the environment variable alone is not enough on a
    machine that has a local ``.env``: the file would silently restore the
    secret and the "no secret" test would not test anything. Disable the .env
    loader for the duration so the test exercises the unconfigured path.
    """
    from app import config

    monkeypatch.delenv("TITAN_SEAL_SECRET", raising=False)
    monkeypatch.setattr(config, "_load_dotenv", lambda path: None)
    config.reset_settings_cache()
    yield
    config.reset_settings_cache()
