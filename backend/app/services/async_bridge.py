"""The one async/event-loop bridge for calling coroutines from sync code.

Titan's service layer is synchronous (SQLAlchemy sessions, FastAPI dependencies)
while providers (LLM/image/voice/STT clients) expose async methods. This module
is the single audited implementation used by every such call site:

- no running loop (the normal sync service path) -> ``asyncio.run``;
- a running loop (async middleware / upcoming async endpoints) -> offload to a
  worker thread, so an event loop is never nested.

Duplicate private ``_run``/``_run_coro`` helpers were removed in the integrity
repair; one of them had already drifted into a broken import that silently
disabled the ASR round-trip.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from typing import Any, TypeVar

T = TypeVar("T")


def run_sync(awaitable: Any) -> Any:
    """Run ``awaitable`` from synchronous code and return its result."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, awaitable).result()


__all__ = ["run_sync"]
