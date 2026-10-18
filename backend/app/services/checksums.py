"""Single source of truth for asset/FactSheet integrity hashes.

Every asset checksum Titan records is the FULL SHA-256 digest rendered as
``sha256:<64 hex chars>`` — the same value, in the same format, at asset
creation, Guardian verification, certificate issuance and staleness checks.

This module exists because the format had drifted: voice assets were hashed
with a truncated digest while the certificate recomputed the full digest, so
untouched audio produced false ``bytes_unchanged: false`` (tamper) warnings.
Hashes are compared with :func:`matches` and never re-implemented locally.
"""

from __future__ import annotations

import hashlib

ALGORITHM = "sha256"
PREFIX = f"{ALGORITHM}:"


def sha256_hex(data: bytes) -> str:
    """Full 64-character lowercase hex SHA-256 digest, without a prefix."""
    return hashlib.sha256(data).hexdigest()


def sha256_digest(data: bytes) -> str:
    """Canonical binary-asset checksum: ``sha256:<64 hex>``."""
    return PREFIX + sha256_hex(data)


def sha256_text(text: str) -> str:
    """Canonical text-asset checksum: ``sha256:<64 hex>`` of the UTF-8 bytes."""
    return sha256_digest(text.encode("utf-8"))


def matches(data: bytes, expected: str | None) -> bool:
    """True when ``expected`` is unrecorded (nothing to compare) or equal.

    ``None`` means "no checksum was ever recorded", which is not tampering; a
    recorded checksum that differs is.
    """
    if expected is None:
        return True
    return sha256_digest(data) == expected


def current_asset_digest(*, storage_path: str | None, text_content: str | None) -> str | None:
    """Recompute the on-disk/in-DB digest of an asset as it is *now*.

    Binary assets hash their stored bytes; text assets hash their current text
    (so an edit after approval is detectable). Returns ``None`` when neither is
    available (missing file, or a text asset with no text).
    """
    if storage_path:
        from ..storage import get_storage

        storage = get_storage()
        if storage.exists(storage_path):
            return sha256_digest(storage.read_bytes(storage_path))
        return None
    if text_content is not None:
        return sha256_text(text_content)
    return None


__all__ = [
    "ALGORITHM",
    "PREFIX",
    "sha256_hex",
    "sha256_digest",
    "sha256_text",
    "matches",
    "current_asset_digest",
]
