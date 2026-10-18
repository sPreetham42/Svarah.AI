"""Filesystem asset storage abstraction.

All generated or uploaded media lives under one configurable root. Relative
POSIX-style paths are stored in the database; absolute paths are derived at
read time.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from .config import get_settings

_SAFE_SUFFIX = re.compile(r"^[a-zA-Z0-9.]{1,10}$")


class StorageError(Exception):
    pass


class AssetStorage:
    """Rooted, traversal-safe file store."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, relative_path: str) -> Path:
        candidate = (self.root / relative_path).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise StorageError(f"Path escapes asset root: {relative_path!r}")
        return candidate

    def save_bytes(self, data: bytes, relative_path: str) -> str:
        target = self._resolve(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return relative_path.replace("\\", "/")

    def read_bytes(self, relative_path: str) -> bytes:
        target = self._resolve(relative_path)
        if not target.is_file():
            raise StorageError(f"Asset not found: {relative_path!r}")
        return target.read_bytes()

    def exists(self, relative_path: str) -> bool:
        return self._resolve(relative_path).is_file()

    def delete(self, relative_path: str) -> bool:
        target = self._resolve(relative_path)
        if target.is_file():
            target.unlink()
            return True
        return False

    def absolute_path(self, relative_path: str) -> Path:
        return self._resolve(relative_path)

    def list_files(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(
            p.relative_to(self.root).as_posix()
            for p in self.root.rglob("*")
            if p.is_file()
        )

    def stats(self) -> dict[str, object]:
        files = self.list_files()
        total = sum((self.root / f).stat().st_size for f in files)
        return {"root": str(self.root), "files": len(files), "bytes": total}


def safe_suffix(mime: str | None, fallback: str = ".webm") -> str:
    """Map a few known audio MIME types to file extensions."""
    mapping = {
        "audio/webm": ".webm",
        "audio/ogg": ".ogg",
        "audio/mpeg": ".mp3",
        "audio/mp4": ".m4a",
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
        "audio/flac": ".flac",
    }
    if mime:
        base = mime.split(";", 1)[0].strip().lower()
        if base in mapping:
            return mapping[base]
    return fallback


def validate_suffix(suffix: str) -> str:
    suffix = suffix if suffix.startswith(".") else f".{suffix}"
    if not _SAFE_SUFFIX.match(suffix.lstrip(".")):
        raise StorageError(f"Unsafe file suffix: {suffix!r}")
    return suffix.lower()


@lru_cache(maxsize=1)
def get_storage() -> AssetStorage:
    return AssetStorage(get_settings().assets_dir)


def reset_storage_cache() -> None:
    """Test hook."""
    get_storage.cache_clear()
