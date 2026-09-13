from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import IO, Iterator

try:
    import fcntl

    _HAVE_FCNTL = True
except ImportError:
    fcntl = None
    _HAVE_FCNTL = False

try:
    import msvcrt

    _HAVE_MSVCRT = True
except ImportError:
    msvcrt = None
    _HAVE_MSVCRT = False

_LOCK_REGION_BYTES = 1


def _lock(handle: IO) -> None:
    if _HAVE_FCNTL:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    elif _HAVE_MSVCRT:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, _LOCK_REGION_BYTES)


def _unlock(handle: IO) -> None:
    if _HAVE_FCNTL:
        with contextlib.suppress(OSError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    elif _HAVE_MSVCRT:
        with contextlib.suppress(OSError):
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, _LOCK_REGION_BYTES)


@contextlib.contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        _lock(handle)
        try:
            yield
        finally:
            _unlock(handle)


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, default=str, ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        _lock(handle)
        try:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            _unlock(handle)


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(text, encoding="utf-8")
    os.replace(tmp_path, path)
