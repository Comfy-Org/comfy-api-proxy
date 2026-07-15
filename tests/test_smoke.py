"""End-to-end smoke test.

This is the one test in this repo that proves the whole path actually works:
start the fake ComfyUI stand-in, start the real proxy in front of it, then
drive both with the real demo client (``demo/run_demo.py``, using the
``comfy_sdk`` package) exactly as a user would. Lint and type-checking confirm
the code *looks* right; this confirms it *runs*.

Requires the ``comfy_sdk`` package to be installed (see the CI workflow, which
installs it from the private ``Comfy-Org/ComfyPythonSDK`` repo before running
this test). If it isn't installed, this test fails with a clear error rather
than a confusing import traceback.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FAKE_COMFYUI_PORT = 8188
PROXY_PORT = 8189
STARTUP_TIMEOUT = 15.0


def _require_comfy_sdk() -> None:
    try:
        import comfy_sdk  # noqa: F401
    except ImportError:
        pytest.fail(
            "The 'comfy_sdk' package is not installed. demo/run_demo.py needs it "
            "to talk to the proxy. Install it from Comfy-Org/ComfyPythonSDK "
            "(see the CI workflow's 'Install Comfy Python SDK' step) before "
            "running this test.",
            pytrace=False,
        )


def _wait_for_port(
    host: str, port: int, timeout: float, proc: subprocess.Popen, label: str, log_path: Path
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            _dump_log(label, log_path)
            pytest.fail(
                f"{label} exited early (code {proc.returncode}) before it started listening."
            )
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.25)
            if sock.connect_ex((host, port)) == 0:
                return
        time.sleep(0.2)
    _dump_log(label, log_path)
    pytest.fail(f"{label} did not start listening on {host}:{port} within {timeout}s.")


def _dump_log(label: str, log_path: Path) -> None:
    print(f"\n--- {label} output ({log_path}) ---")
    if log_path.exists():
        print(log_path.read_text())
    else:
        print("(no output captured)")


@pytest.fixture
def servers(tmp_path):
    """Start a background server, waiting until its port answers or it dies."""
    procs: list[tuple[subprocess.Popen, str, Path]] = []

    def _spawn(args: list[str], port: int, label: str) -> subprocess.Popen:
        log_path = tmp_path / f"{label}.log"
        with log_path.open("w") as log_file:
            proc = subprocess.Popen(
                args,
                cwd=REPO_ROOT,
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
        procs.append((proc, label, log_path))
        _wait_for_port("127.0.0.1", port, STARTUP_TIMEOUT, proc, label, log_path)
        return proc

    yield _spawn

    for proc, label, log_path in procs:
        proc.terminate()
    for proc, label, log_path in procs:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        if proc.returncode not in (0, None) and proc.returncode < 0:
            # Negative return code means we had to signal it ourselves (expected
            # on teardown) - not a failure. Only surface genuinely bad exits.
            pass


def test_submit_poll_download_roundtrip(servers, tmp_path):
    _require_comfy_sdk()

    servers(
        [sys.executable, str(REPO_ROOT / "demo" / "fake_comfyui.py")],
        FAKE_COMFYUI_PORT,
        "fake_comfyui",
    )
    servers(
        [
            sys.executable,
            "-m",
            "comfy_api_proxy.cli",
            "--comfyui",
            f"http://127.0.0.1:{FAKE_COMFYUI_PORT}",
            "--port",
            str(PROXY_PORT),
        ],
        PROXY_PORT,
        "proxy",
    )

    out_path = tmp_path / "out.png"
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "demo" / "run_demo.py"), "--out", str(out_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        print("\n--- demo/run_demo.py stdout ---")
        print(result.stdout)
        print("--- demo/run_demo.py stderr ---")
        print(result.stderr)

    assert result.returncode == 0, (
        "demo/run_demo.py did not exit cleanly (see captured output above)"
    )
    assert out_path.exists(), f"expected output PNG at {out_path} was not created"
    assert out_path.stat().st_size > 0, f"output PNG at {out_path} is empty"
