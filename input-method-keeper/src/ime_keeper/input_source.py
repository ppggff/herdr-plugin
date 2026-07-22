from __future__ import annotations

import dataclasses
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional


def short_input_source(input_source_id: Optional[str]) -> str:
    if not input_source_id:
        return "unknown"
    return str(input_source_id).rsplit(".", 1)[-1]

@dataclasses.dataclass(frozen=True)
class CommandResult:
    ok: bool
    stdout: str
    stderr: str
    exit_code: Optional[int] = None



class BackendExecutor:
    def __init__(self, config: Mapping[str, Any]):
        backend = config.get("backend", {})
        if not isinstance(backend, dict):
            backend = {}
        self.executable = self._resolve_executable(
            backend.get("executable_candidates", ["macism"])
        )
        self.current_args = self._coerce_args(backend.get("current_args", []), [])
        self.select_args = self._coerce_args(backend.get("select_args", ["{id}"]), ["{id}"])

    def _coerce_args(self, value: Any, default: List[str]) -> List[str]:
        if value is None:
            return list(default)
        if isinstance(value, str):
            return [value]
        if isinstance(value, (list, tuple)):
            return [str(item) for item in value]
        return list(default)

    def _resolve_executable(self, candidates: Any) -> str:
        if isinstance(candidates, str):
            candidates = [candidates]
        for candidate in candidates or ["macism"]:
            candidate = str(candidate)
            if "/" in candidate:
                if Path(candidate).exists():
                    return candidate
            else:
                resolved = shutil.which(candidate)
                if resolved:
                    return resolved
        return str((candidates or ["macism"])[0])

    def _run(self, args: List[str], timeout: float = 2.0) -> CommandResult:
        try:
            completed = subprocess.run(
                [self.executable] + [str(arg) for arg in args],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
            )
            return CommandResult(
                completed.returncode == 0,
                completed.stdout.strip(),
                completed.stderr.strip(),
                completed.returncode,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return CommandResult(False, "", str(exc), None)

    def current(self) -> str:
        result = self._run(self.current_args)
        if not result.ok:
            raise RuntimeError(result.stderr or "backend current failed")
        return result.stdout.strip()

    def select(self, input_source_id: str) -> CommandResult:
        args = [str(arg).replace("{id}", input_source_id) for arg in self.select_args]
        return self._run(args)

    def doctor(self) -> CommandResult:
        return self._run(self.current_args)


_UNKNOWN_CURRENT = object()


def ensure_input_source_details(
    backend: Any, target: str, known_current: Any = _UNKNOWN_CURRENT
) -> Dict[str, Any]:
    details: Dict[str, Any] = {
        "target": target,
        "current": None,
        "action": "no-target",
    }
    if not target:
        details["current_ms"] = 0.0
        details["select_ms"] = 0.0
        return details
    current_started = time.monotonic()
    current = backend.current() if known_current is _UNKNOWN_CURRENT else str(known_current)
    details["current_ms"] = (time.monotonic() - current_started) * 1000
    details["select_ms"] = 0.0
    details["current"] = current
    if current == target:
        details["action"] = "already-current"
        return details
    select_started = time.monotonic()
    result = backend.select(target)
    details["select_ms"] = (time.monotonic() - select_started) * 1000
    if isinstance(result, CommandResult) and not result.ok:
        raise RuntimeError(result.stderr or "backend select failed")
    details["action"] = "selected"
    if isinstance(result, CommandResult):
        details["select_exit_code"] = result.exit_code
        details["select_stdout"] = result.stdout
        details["select_stderr"] = result.stderr
    return details


def ensure_input_source(backend: Any, target: str) -> str:
    return str(ensure_input_source_details(backend, target)["action"])
