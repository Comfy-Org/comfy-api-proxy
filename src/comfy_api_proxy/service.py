"""Background start/stop for the proxy — a tiny, dependency-free supervisor.

`comfy-api-proxy start` launches the server as a detached child process and
records its PID and address in a single JSON file under a per-user state
directory; `stop`/`status` read that file. There's no daemon manager here — the
child is just the ordinary foreground `run` server, detached from the terminal.

Identity: the state carries a PID, but a PID alone can be recycled by an
unrelated process. `stop`/`status` therefore also confirm the process's command
line looks like this proxy before acting on it (best-effort; falls back to a
plain liveness check where the command line can't be read). And `start` refuses
to launch onto an already-occupied port, so a bound port reported by another
process is never mistaken for a successful start.

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

_MARKER = "comfy_api_proxy"  # substring expected in our child's command line


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
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    # A corrupt or unexpected shape (e.g. `[]` or `{"pid": "abc"}`) must not
    # crash callers — treat anything that isn't a well-formed record as "no
    # state".
    if not isinstance(data, dict) or _state_pid(data) <= 0:
        return None
    return data


def _state_pid(state: dict[str, Any]) -> int:
    try:
        return int(state.get("pid", -1))
    except (TypeError, ValueError):
        return -1


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


def _pid_cmdline(pid: int) -> str | None:
    """Best-effort command line of a PID, or None if it can't be read here."""
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["wmic", "process", "where", f"ProcessId={pid}", "get", "CommandLine"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        else:
            out = subprocess.run(
                ["ps", "-p", str(pid), "-o", "args="],
                capture_output=True,
                text=True,
                timeout=5,
            )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def _pid_is_ours(pid: int) -> bool:
    """Is `pid` alive AND (best-effort) actually this proxy — not a recycled PID?

    Falls back to a plain liveness check when the command line can't be
    inspected, so behavior is never worse than tracking the PID alone.
    """
    if not _pid_alive(pid):
        return False
    cmdline = _pid_cmdline(pid)
    if cmdline is None:
        return True  # can't tell; assume it's ours rather than refuse to manage it
    return _MARKER in cmdline


def _connect_host(host: str) -> str:
    # A bind address of "" or 0.0.0.0 isn't connectable; probe IPv4 loopback.
    # A bind of :: (all IPv6) is probed via the IPv6 loopback.
    if host in ("", "0.0.0.0"):
        return "127.0.0.1"
    if host == "::":
        return "::1"
    return host


def _url(host: str, port: int) -> str:
    target = _connect_host(host)
    if ":" in target:  # IPv6 literal must be bracketed in a URL
        return f"http://[{target}]:{port}"
    return f"http://{target}:{port}"


def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((_connect_host(host), port), timeout=timeout):
            return True
    except OSError:
        return False


def _wait_port(host: str, port: int, proc: subprocess.Popen[bytes], timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False  # the child exited before binding
        if _port_open(host, port, timeout=1):
            return True
        time.sleep(0.2)
    return False


def start(run_argv: list[str], host: str, port: int) -> int:
    """Launch the server detached; record its PID/address; wait for it to bind."""
    existing = read_state()
    if existing and _pid_is_ours(_state_pid(existing)):
        print(
            f"comfy-api-proxy is already running at {existing.get('url')} "
            f"(pid {existing.get('pid')}).",
            file=sys.stderr,
        )
        return 1

    # Refuse to start onto an occupied port: otherwise a port already held by
    # another process would make the readiness check below pass while our child
    # fails to bind and exits.
    if _port_open(host, port):
        print(
            f"comfy-api-proxy cannot start: {_url(host, port)} is already in use. "
            "Stop the other process or choose a different --port.",
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

    if not _wait_port(host, port, proc):
        _terminate(proc.pid)
        print(f"comfy-api-proxy failed to start; see {log_path}", file=sys.stderr)
        return 1

    url = _url(host, port)
    _state_file().write_text(
        json.dumps({"pid": proc.pid, "host": host, "port": port, "url": url}),
        encoding="utf-8",
    )
    print(f"comfy-api-proxy started at {url} (pid {proc.pid}). Stop it with: comfy-api-proxy stop")
    return 0


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


def stop() -> int:
    state = read_state()
    pid = _state_pid(state) if state else -1
    # Only terminate a process we can still identify as ours — never a PID that
    # has since been recycled by something unrelated.
    if not state or not _pid_is_ours(pid):
        _clear_state()
        print("comfy-api-proxy is not running.")
        return 0
    _terminate(pid)
    _clear_state()
    print(f"comfy-api-proxy stopped (pid {pid}).")
    return 0


def status() -> int:
    state = read_state()
    if state and _pid_is_ours(_state_pid(state)):
        print(f"comfy-api-proxy is running at {state.get('url')} (pid {state.get('pid')}).")
        return 0
    print("comfy-api-proxy is not running.")
    return 1
