"""Database initialization tests."""

from __future__ import annotations

from sqlalchemy import inspect

from app.db import get_engine, init_db


def test_init_db_creates_expected_tables(client):
    tables = set(inspect(get_engine()).get_table_names())
    assert {"shops", "campaigns", "jobs", "audit_events", "fact_sheets"} <= tables


def test_init_db_is_idempotent(client):
    # Calling init_db again must not raise or drop data.
    init_db()
    init_db()
    assert "campaigns" in inspect(get_engine()).get_table_names()


def test_models_use_postgres_compatible_types(client):
    from app.models import Campaign, Job, Shop

    assert Shop.__tablename__ == "shops"
    assert Campaign.__tablename__ == "campaigns"
    assert Job.__tablename__ == "jobs"
