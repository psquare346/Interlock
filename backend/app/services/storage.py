"""Object storage seam (SCALE.md D6).

Uploaded spreadsheets and generated reports go through put()/get() — never raw
filesystem paths in callers. Today the backend is a local directory
(settings.STORAGE_DIR); the S3 backend is a drop-in replacement of these three
functions, keeping the app tier stateless.

Keys are namespaced by the caller, e.g. "demo/audits/<job_id>.csv". Path
traversal is rejected here so callers can pass user-influenced key parts.
"""

from __future__ import annotations

from pathlib import Path

from ..config import get_settings


def _root() -> Path:
    root = Path(get_settings().STORAGE_DIR).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _path(key: str) -> Path:
    root = _root()
    path = (root / key).resolve()
    if root not in path.parents:
        raise ValueError(f"Illegal storage key: {key!r}")
    return path


def put(key: str, data: bytes) -> str:
    """Store bytes under key; returns the key for persistence on DB rows."""
    path = _path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return key


def get(key: str) -> bytes:
    return _path(key).read_bytes()


def exists(key: str) -> bool:
    return _path(key).is_file()
