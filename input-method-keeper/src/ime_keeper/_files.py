from __future__ import annotations

import fcntl
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def atomic_write_json(path: Path, data: Mapping[str, Any]) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def backup_path(path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return path.with_name(f"{path.name}.broken.{stamp}")


def timestamp_for_filename() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")



class contextlib_suppress_file_not_found:
    def __enter__(self):
        return None

    def __exit__(self, exc_type, exc, tb):
        return exc_type is FileNotFoundError


class FileLock:
    def __init__(self, path: Path, blocking: bool = True):
        self.path = Path(path)
        self.blocking = blocking
        self.handle = None
        self.acquired = False

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+")
        flags = fcntl.LOCK_EX
        if not self.blocking:
            flags |= fcntl.LOCK_NB
        try:
            fcntl.flock(self.handle.fileno(), flags)
            self.acquired = True
        except BlockingIOError:
            self.acquired = False
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.handle is not None:
            if self.acquired:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
        return False


def run_lock_path(state_dir: Path) -> Path:
    return Path(state_dir) / "run.lock"
