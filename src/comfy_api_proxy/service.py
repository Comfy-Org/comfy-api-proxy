"""Background start/stop for the proxy — a tiny, dependency-free supervisor.

`comfy-api-proxy start` launches the server as a detached child process and
records its PID and address in a single JSON file under a per-user state
directory; `stop`/`status` read that file. There's no daemon manager here — the
child is just the ordinary foreground `run` server, detached from the terminal.

The state directory can be overridden with `COMFY_API_PROXY_STATE_DIR` (used by
the tests so they never touch the real user directory).
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def _state_dir() -> Path:
    override = os.environ.get("COMFY_API_PROXY_STATE_DIR")
    if override:
        directory = Path(override)
    else:
        base = os.environ.get("XDG_STATE_HOME")
        root = Path(base) if base else Path.home() / ".local" / "state"
        directory = root / "comfy-api-proxy"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _state_file() -> Path:
    return _state_dir() / "proxy.json"


def _log_file() -> Path:
    return _state_dir() / "proxy.log"


def read_state() -> dict[str, Any] | None:
    path = _state_file()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def _clear_state() -> None:
    _state_file().unlink(missing_ok=True)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # On Windows os.kill(pid, 0) would call TerminateProcess (killing it),
        # so probe with tasklist instead.
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return str(pid) in out.stdout
        except (OSError, subprocess.SubprocessError):
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user
    except OSError:
        return False
    return True


def _terminate(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=10)
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    for _ in range(50):  # up to ~5s for a graceful stop
        if not _pid_alive(pid):
            return
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _connect_host(host: str) -> str:
    # A bind address of "", 0.0.0.0 or :: isn't connectable; probe loopback.
    return "127.0.0.1" if host in ("", "0.0.0.0", "::") else host


def _wait_port(host: str, port: int, proc: subprocess.Popen[bytes], timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    target = _connect_host(host)
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False  # the child exited before binding
        try:
            with socket.create_connection((target, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def start(run_argv: list[str], host: str, port: int) -> int:
    """Launch the server detached; record its PID/address; wait for it to bind."""
    existing = read_state()
    if existing and _pid_alive(int(existing.get("pid", -1))):
        print(
            f"comfy-api-proxy is already running at {existing.get('url')} "
            f"(pid {existing.get('pid')}).",
            file=sys.stderr,
        )
        return 1

    cmd = [sys.executable, "-m", "comfy_api_proxy.cli", "run", *run_argv]
    popen_kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL}
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        popen_kwargs["creationflags"] = 0x00000008 | 0x00000200
    else:
        popen_kwargs["start_new_session"] = True

    log_path = _log_file()
    with open(log_path, "ab") as log:
        proc = subprocess.Popen(cmd, stdout=log, stderr=log, **popen_kwargs)

    url = f"http://{_connect_host(host)}:{port}"
    if not _wait_port(host, port, proc):
        _terminate(proc.pid)
        print(f"comfy-api-proxy failed to start; see {log_path}", file=sys.stderr)
        return 1

    _state_file().write_text(
        json.dumps({"pid": proc.pid, "host": host, "port": port, "url": url}),
        encoding="utf-8",
    )
    print(f"comfy-api-proxy started at {url} (pid {proc.pid}). Stop it with: comfy-api-proxy stop")
    return 0


def stop() -> int:
    state = read_state()
    if not state or not _pid_alive(int(state.get("pid", -1))):
        _clear_state()
        print("comfy-api-proxy is not running.")
        return 0
    _terminate(int(state["pid"]))
    _clear_state()
    print(f"comfy-api-proxy stopped (pid {state['pid']}).")
    return 0


def status() -> int:
    state = read_state()
    if state and _pid_alive(int(state.get("pid", -1))):
        print(f"comfy-api-proxy is running at {state.get('url')} (pid {state.get('pid')}).")
        return 0
    print("comfy-api-proxy is not running.")
    return 1
