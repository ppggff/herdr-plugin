from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .input_source import CommandResult


def pane_parts(pane_id: Optional[str]) -> Tuple[str, str]:
    if not pane_id:
        return "-", "-"
    value = str(pane_id)
    if ":" in value:
        workspace_id, local_pane_id = value.split(":", 1)
        return local_pane_id or "-", workspace_id or "-"
    return value, "-"

class HerdrClient:
    def __init__(self, env: Mapping[str, str]):
        self.env = dict(env)

    def current_pane(self) -> Optional[Dict[str, Any]]:
        socket_path_value = self.env.get("HERDR_SOCKET_PATH", "")
        if socket_path_value:
            pane = self._current_pane_socket(socket_path_value)
            if pane:
                return pane
        return self._current_pane_cli()

    def _current_pane_socket(self, socket_path_value: str) -> Optional[Dict[str, Any]]:
        response = self._socket_request(
            "pane.current", {}, timeout=1.0, socket_path=socket_path_value
        )
        if not isinstance(response, dict):
            return None
        result = response.get("result", {})
        if isinstance(result, dict):
            pane = result.get("pane")
            if isinstance(pane, dict):
                return pane
        return None

    def _socket_request(
        self,
        method: str,
        params: Mapping[str, Any],
        timeout: float,
        socket_path: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        socket_path_value = socket_path or self.env.get("HERDR_SOCKET_PATH", "")
        if not socket_path_value or timeout <= 0:
            return None
        request = {
            "id": f"ime-keeper-{os.getpid()}-{time.monotonic_ns()}",
            "method": method,
            "params": dict(params),
        }
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(timeout)
                sock.connect(socket_path_value)
                sock.sendall((json.dumps(request) + "\n").encode("utf-8"))
                chunks = []
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    if b"\n" in chunk:
                        break
            line = b"".join(chunks).splitlines()[0]
            response = json.loads(line.decode("utf-8"))
        except Exception:
            return None
        return response if isinstance(response, dict) else None

    def list_panes_socket(self, timeout: float) -> Optional[List[Dict[str, Any]]]:
        response = self._socket_request("pane.list", {}, timeout=timeout)
        if not isinstance(response, dict) or response.get("error"):
            return None
        result = response.get("result")
        panes = result.get("panes") if isinstance(result, dict) else None
        if not isinstance(panes, list):
            return None
        validated: List[Dict[str, Any]] = []
        for pane in panes:
            if (
                not isinstance(pane, dict)
                or not isinstance(pane.get("pane_id"), str)
                or not pane["pane_id"]
            ):
                return None
            validated.append(pane)
        return validated

    def pane_presence_socket(self, pane_id: str, timeout: float) -> str:
        response = self._socket_request("pane.get", {"pane_id": pane_id}, timeout=timeout)
        if not isinstance(response, dict):
            return "unknown"
        error = response.get("error")
        if isinstance(error, dict):
            return "absent" if error.get("code") == "pane_not_found" else "unknown"
        result = response.get("result")
        pane = result.get("pane") if isinstance(result, dict) else None
        if isinstance(pane, dict) and pane.get("pane_id") == pane_id:
            return "present"
        return "unknown"

    def _current_pane_cli(self) -> Optional[Dict[str, Any]]:
        herdr_bin = self._herdr_bin()
        if not herdr_bin:
            return None
        child_env = dict(os.environ)
        child_env.update(self.env)
        child_env.pop("HERDR_PANE_ID", None)
        try:
            completed = subprocess.run(
                [herdr_bin, "pane", "current"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=1.0,
                check=False,
                env=child_env,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if completed.returncode != 0:
            return None
        try:
            response = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return None
        result = response.get("result", {})
        pane = result.get("pane") if isinstance(result, dict) else None
        return pane if isinstance(pane, dict) else None

    def _herdr_bin(self) -> Optional[str]:
        return self.env.get("HERDR_BIN_PATH") or shutil.which("herdr")

    def _run_herdr(self, args: List[str], timeout: float = 1.0) -> CommandResult:
        herdr_bin = self._herdr_bin()
        if not herdr_bin:
            return CommandResult(False, "", "herdr executable not found", None)
        child_env = dict(os.environ)
        child_env.update(self.env)
        try:
            completed = subprocess.run(
                [herdr_bin] + args,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
                env=child_env,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return CommandResult(False, "", str(exc), None)
        return CommandResult(
            completed.returncode == 0,
            completed.stdout.strip(),
            completed.stderr.strip(),
            completed.returncode,
        )

    def _run_herdr_json(self, args: List[str], timeout: float = 2.0) -> Dict[str, Any]:
        result = self._run_herdr(args, timeout=timeout)
        if not result.ok:
            raise RuntimeError(result.stderr or result.stdout or "herdr command failed")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"herdr returned invalid json: {exc}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("herdr returned non-object json")
        error = payload.get("error")
        if error:
            raise RuntimeError(str(error))
        return payload

    def list_workspaces(self) -> List[Dict[str, Any]]:
        payload = self._run_herdr_json(["workspace", "list"], timeout=2.0)
        result = payload.get("result")
        if not isinstance(result, dict):
            return []
        workspaces = result.get("workspaces", [])
        return [item for item in workspaces if isinstance(item, dict)]

    def list_tabs(self, workspace_id: Optional[str] = None) -> List[Dict[str, Any]]:
        args = ["tab", "list"]
        if workspace_id:
            args += ["--workspace", workspace_id]
        payload = self._run_herdr_json(args, timeout=2.0)
        result = payload.get("result")
        if not isinstance(result, dict):
            return []
        tabs = result.get("tabs", [])
        return [item for item in tabs if isinstance(item, dict)]

    def list_panes(self, workspace_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if workspace_id is None and self.env.get("HERDR_SOCKET_PATH"):
            socket_panes = self.list_panes_socket(1.0)
            if socket_panes is not None:
                return socket_panes
        args = ["pane", "list"]
        if workspace_id:
            args += ["--workspace", workspace_id]
        payload = self._run_herdr_json(args, timeout=2.0)
        result = payload.get("result")
        if not isinstance(result, dict):
            return []
        panes = result.get("panes", [])
        return [item for item in panes if isinstance(item, dict)]

    def show_notification(self, title: str, body: str) -> CommandResult:
        return self._socket_command(
            "notification.show",
            {"title": title, "body": body, "position": "top-right", "sound": "none"},
            timeout=1.5,
        )

    def report_pane_status(self, pane_id: str, status: str, ttl_ms: int) -> CommandResult:
        return self._socket_command(
            "pane.report_metadata",
            {
                "pane_id": pane_id,
                "source": "ppggff.input-method-keeper",
                "tokens": {"ime": status},
                "ttl_ms": ttl_ms,
            },
            timeout=1.5,
        )

    def _socket_command(
        self, method: str, params: Mapping[str, Any], timeout: float
    ) -> CommandResult:
        response = self._socket_request(method, params, timeout=timeout)
        if not isinstance(response, dict):
            return CommandResult(False, "", "herdr socket unavailable", None)
        error = response.get("error")
        if isinstance(error, dict):
            return CommandResult(False, "", str(error.get("message") or error), None)
        if not isinstance(response.get("result"), dict):
            return CommandResult(False, "", "invalid herdr socket response", None)
        return CommandResult(True, "", "", 0)

    def doctor(self) -> CommandResult:
        return CommandResult(True, "", "")
