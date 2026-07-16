"""End-to-end smoke test.

This is the one test in this repo that proves the whole path actually works:
start the fake ComfyUI stand-in, start the real proxy in front of it, then
drive the proxy's own HTTP surface directly - submit a workflow, poll job
status, download the output - using nothing but the Python standard library
(``urllib``). Lint and type-checking confirm the code *looks* right; this
confirms it *runs*.

This deliberately does NOT use ``demo/run_demo.py`` or the ``comfy_sdk``
package: that SDK lives in a separate, private repo (Comfy-Org/ComfyPythonSDK),
and this repo's CI must not depend on another repo's credentials to pass.
``demo/run_demo.py`` remains the human-facing demo for people who have that SDK
installed; this test is the self-contained check CI actually runs.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FAKE_COMFYUI_PORT = 8188
PROXY_PORT = 8189
PROXY_BASE = f"http://127.0.0.1:{PROXY_PORT}"
STARTUP_TIMEOUT = 15.0
JOB_TIMEOUT = 15.0
_TERMINAL = {"succeeded", "failed", "expired", "canceled"}

# A minimal API-format graph: a map of node-id -> {class_type, inputs}. NOT a
# UI-export graph (which has top-level "nodes"/"links" and the proxy rejects
# with a precise error). The fake ComfyUI ignores the actual contents and
# always returns one PNG output, so the values here don't matter.
WORKFLOW = {
    "3": {"class_type": "KSampler", "inputs": {"seed": 42, "steps": 1}},
    "9": {"class_type": "SaveImage", "inputs": {"images": ["3", 0]}},
}


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

    for proc, _label, _log_path in procs:
        proc.terminate()
    for proc, _label, _log_path in procs:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def _request(method: str, url: str, body: dict[str, Any] | None = None) -> tuple[int, Any, bytes]:
    """A tiny stdlib-only HTTP client - no SDK, no third-party dependency."""
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            raw = r.read()
            ctype = r.headers.get("Content-Type", "")
            parsed = json.loads(raw) if "application/json" in ctype else None
            return r.status, parsed, raw
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = None
        return e.code, parsed, raw


def test_submit_poll_download_roundtrip(servers):
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

    # 1. Submit the workflow.
    status, job, raw = _request("POST", f"{PROXY_BASE}/api/v2/jobs", {"workflow": WORKFLOW})
    assert status == 201, f"submit failed ({status}): {raw!r}"
    job_id = job["id"]

    # 2. Poll job status until it reaches a terminal state.
    deadline = time.monotonic() + JOB_TIMEOUT
    while job["status"] not in _TERMINAL:
        assert time.monotonic() < deadline, (
            f"job {job_id} did not finish within {JOB_TIMEOUT}s (last status: {job['status']!r})"
        )
        time.sleep(0.2)
        status, job, raw = _request("GET", job["urls"]["self"])
        assert status == 200, f"get_job failed ({status}): {raw!r}"

    assert job["status"] == "succeeded", f"job did not succeed: {job.get('error')}"
    assert job["outputs"], "job succeeded but produced no outputs"

    # 3. Download the output content and assert it is a real, non-empty PNG.
    output = job["outputs"][0]
    status, _parsed, content = _request("GET", output["url"])
    assert status == 200, f"download failed ({status})"
    assert content, "downloaded output is empty"
    assert content.startswith(b"\x89PNG\r\n\x1a\n"), "downloaded output is not a PNG"
