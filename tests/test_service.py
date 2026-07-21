"""start / status / stop lifecycle for the background service.

Drives the real CLI as a subprocess (with an isolated state dir) so the
detached-child spawn, PID tracking, and teardown are all exercised end to end.
ComfyUI doesn't need to be reachable — the proxy binds its own port regardless.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _cli(args: list[str], env: dict[str, str], timeout: float = 30.0):
    return subprocess.run(
        [sys.executable, "-m", "comfy_api_proxy.cli", *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def test_start_status_stop_cycle(tmp_path):
    port = _free_port()
    env = {**os.environ, "COMFY_API_PROXY_STATE_DIR": str(tmp_path)}
    run = ["start", "--port", str(port), "--comfyui", "http://127.0.0.1:1"]
    try:
        # Nothing running yet.
        assert _cli(["status"], env).returncode == 1

        started = _cli(run, env)
        assert started.returncode == 0, started.stderr
        assert "started" in started.stdout
        assert _port_open(port)

        running = _cli(["status"], env)
        assert running.returncode == 0
        assert "running" in running.stdout

        # A second start is refused rather than double-binding.
        again = _cli(run, env)
        assert again.returncode == 1
        assert "already running" in again.stderr
    finally:
        stopped = _cli(["stop"], env)
        assert stopped.returncode == 0, stopped.stderr
        assert "stopped" in stopped.stdout or "not running" in stopped.stdout

    # Port released and status reflects stopped.
    for _ in range(50):
        if not _port_open(port):
            break
        time.sleep(0.1)
    assert not _port_open(port)
    assert _cli(["status"], env).returncode == 1


def test_stop_when_not_running_is_a_clean_no_op(tmp_path):
    env = {**os.environ, "COMFY_API_PROXY_STATE_DIR": str(tmp_path)}
    result = _cli(["stop"], env)
    assert result.returncode == 0
    assert "not running" in result.stdout


def test_corrupt_state_file_is_ignored_not_a_crash(tmp_path):
    # A malformed state file (bad JSON, wrong shape, non-int pid) must degrade
    # to "not running" rather than tracebacking in status/stop.
    env = {**os.environ, "COMFY_API_PROXY_STATE_DIR": str(tmp_path)}
    state = tmp_path / "proxy.json"
    for bad in ("not json at all", "[]", '{"pid": "abc"}', '{"nope": 1}'):
        state.write_text(bad, encoding="utf-8")
        st = _cli(["status"], env)
        assert st.returncode == 1, (bad, st.stderr)
        assert "not running" in st.stdout
        state.write_text(bad, encoding="utf-8")
        stop = _cli(["stop"], env)
        assert stop.returncode == 0, (bad, stop.stderr)
