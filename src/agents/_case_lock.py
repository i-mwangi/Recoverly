from __future__ import annotations

import contextlib
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Final

log = logging.getLogger("recoverly.agents.case_lock")

DEFAULT_TTL_SECONDS: Final = 900
FIELD_SEPARATOR: Final = "\t"

_SAFE_CHARS: Final = frozenset("-_")


def lock_dir() -> Path:
    configured = os.getenv("CASE_LOCK_DIR", "").strip()
    if configured:
        return Path(configured)
    return Path(tempfile.gettempdir()) / "recoverly_locks"


def ttl_seconds() -> int:
    try:
        return int(os.getenv("CASE_LOCK_TTL_SEC", "").strip())
    except ValueError:
        return DEFAULT_TTL_SECONDS


def _sanitize(case_id: str) -> str:
    safe = "".join(char for char in case_id if char.isalnum() or char in _SAFE_CHARS)
    if not safe:
        raise ValueError(f"case_id has no filesystem-safe characters: {case_id!r}")
    return safe


def _lock_path(case_id: str) -> Path:
    return lock_dir() / f"{_sanitize(case_id)}.lock"


def _age_seconds(path: Path) -> float | None:
    try:
        return time.time() - path.stat().st_mtime
    except OSError:
        return None


def _is_stale(path: Path) -> bool:
    age = _age_seconds(path)
    return age is None or age > ttl_seconds()


def try_acquire(case_id: str, owner_agent: str) -> bool:
    path = _lock_path(case_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = f"{owner_agent}{FIELD_SEPARATOR}{time.time()}\n".encode()

    for _ in range(2):
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            if not _is_stale(path):
                return False
            log.info("reclaiming stale case lock for %s", case_id)
            with contextlib.suppress(OSError):
                path.unlink()
            continue

        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
        return True

    return False


def release(case_id: str) -> None:
    with contextlib.suppress(OSError):
        _lock_path(case_id).unlink()


def held_by(case_id: str) -> str | None:
    path = _lock_path(case_id)
    try:
        first_line = path.read_text(encoding="utf-8").splitlines()[0]
    except (OSError, IndexError):
        return None
    return first_line.split(FIELD_SEPARATOR, 1)[0] or None


@contextlib.contextmanager
def case_lock(case_id: str, owner_agent: str):
    acquired = try_acquire(case_id, owner_agent)
    try:
        yield acquired
    finally:
        if acquired:
            release(case_id)
