"""Shared test harness: spawn the fake ComfyUI + the real proxy as
subprocesses and drive them over HTTP with the standard library only.

Deliberately no SDK and no third-party HTTP client — the same self-contained
approach the original smoke test used, so CI never depends on another repo's
credentials or a network resource beyond localhost.
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
STARTUP_TIMEOUT = 15.0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(port: int, proc: subprocess.Popen, label: str, log_path: Path) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            _dump_log(label, log_path)
            pytest.fail(f"{label} exited early (code {proc.returncode}).")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.25)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.2)
    _dump_log(label, log_path)
    pytest.fail(f"{label} did not listen on 127.0.0.1:{port} within {STARTUP_TIMEOUT}s.")


def _dump_log(label: str, log_path: Path) -> None:
    print(f"\n--- {label} output ({log_path}) ---")
    print(log_path.read_text() if log_path.exists() else "(no output captured)")


class Stack:
    def __init__(self, base: str, comfyui_port: int, proxy_port: int) -> None:
        self.base = base
        self.comfyui_port = comfyui_port
        self.proxy_port = proxy_port

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, Any, bytes]:
        status, parsed, raw, _resp_headers = self.request_with_headers(
            method, path, body, headers=headers
        )
        return status, parsed, raw

    def request_with_headers(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, Any, bytes, dict[str, str]]:
        """Like :meth:`request`, but also returns response headers (lower-cased keys)."""
        url = self.base + path if path.startswith("/") else path
        data = json.dumps(body).encode() if body is not None else None
        hdrs = dict(headers or {})
        if data is not None:
            hdrs.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                raw = r.read()
                ctype = r.headers.get("Content-Type", "")
                parsed = json.loads(raw) if "application/json" in ctype else None
                return r.status, parsed, raw, {k.lower(): v for k, v in r.headers.items()}
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = None
            return e.code, parsed, raw, {k.lower(): v for k, v in e.headers.items()}

    def upload(
        self, file_path: str, content: bytes, content_type: str, **fields: str
    ) -> tuple[int, Any, bytes]:
        """multipart/form-data POST to /api/v2/assets, stdlib-only."""
        boundary = "----comfyproxytest0xBOUNDARY"
        parts: list[bytes] = []

        def field(name: str, value: str) -> None:
            parts.append(f"--{boundary}\r\n".encode())
            parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
            parts.append(value.encode() + b"\r\n")

        parts.append(f"--{boundary}\r\n".encode())
        parts.append(
            b'Content-Disposition: form-data; name="file"; filename="'
            + file_path.split("/")[-1].encode()
            + b'"\r\n'
        )
        parts.append(f"Content-Type: {content_type}\r\n\r\n".encode())
        parts.append(content + b"\r\n")
        field("file_path", file_path)
        field("content_type", content_type)
        for k, v in fields.items():
            field(k, v)
        parts.append(f"--{boundary}--\r\n".encode())
        payload = b"".join(parts)

        req = urllib.request.Request(
            self.base + "/api/v2/assets",
            data=payload,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                raw = r.read()
                return r.status, json.loads(raw), raw
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = None
            return e.code, parsed, raw

    def read_sse(self, path: str, timeout: float = 20.0) -> list[tuple[str, Any]]:
        """Read an SSE stream to completion, returning [(event, data), ...].
        The server closes the stream at the terminal status, ending the read."""
        # Follow the server-provided link as-is when it is absolute (the API
        # now returns absolute urls.* per the contract); only join a bare path.
        url = self.base + path if path.startswith("/") else path
        req = urllib.request.Request(url, method="GET")
        events: list[tuple[str, Any]] = []
        with urllib.request.urlopen(req, timeout=timeout) as r:
            event_name = "message"
            for line_bytes in r:
                line = line_bytes.decode().rstrip("\n")
                if line.startswith("event:"):
                    event_name = line[len("event:") :].strip()
                elif line.startswith("data:"):
                    payload = line[len("data:") :].strip()
                    try:
                        events.append((event_name, json.loads(payload)))
                    except json.JSONDecodeError:
                        events.append((event_name, payload))
                elif line == "":
                    event_name = "message"
        return events


def _make_stack(
    tmp_path: Path,
    *,
    comfyui_base_dir: str | None = None,
    token: str | None = None,
    max_upload_mb: int | None = None,
    state_dir: str | None = None,
    comfyui_port: int | None = None,
    cors_origins: list[str] | None = None,
):
    """Spawn fake ComfyUI + the real proxy on free ports; yield a Stack driver.

    Pass ``comfyui_port`` to reuse an already-running fake (the ``fake_comfyui``
    fixture) so a test can restart the proxy alone, leaving upstream history
    intact — the state a proxy restart in production actually meets.
    """
    procs: list[tuple[subprocess.Popen, str, Path]] = []
    proxy_port = _free_port()

    def _spawn(args: list[str], port: int, label: str) -> None:
        log_path = tmp_path / f"{label}.log"
        with log_path.open("w") as log_file:
            proc = subprocess.Popen(args, cwd=REPO_ROOT, stdout=log_file, stderr=subprocess.STDOUT)
        procs.append((proc, label, log_path))
        _wait_for_port(port, proc, label, log_path)

    if comfyui_port is None:
        comfyui_port = _free_port()
        _spawn(
            [
                sys.executable,
                str(REPO_ROOT / "demo" / "fake_comfyui.py"),
                "--port",
                str(comfyui_port),
            ],
            comfyui_port,
            "fake_comfyui",
        )
    proxy_args = [
        sys.executable,
        "-m",
        "comfy_api_proxy.cli",
        "--comfyui",
        f"http://127.0.0.1:{comfyui_port}",
        "--port",
        str(proxy_port),
    ]
    if comfyui_base_dir is not None:
        proxy_args += ["--comfyui-base-dir", comfyui_base_dir]
    if token is not None:
        proxy_args += ["--token", token]
    if max_upload_mb is not None:
        proxy_args += ["--max-upload-mb", str(max_upload_mb)]
    if state_dir is not None:
        proxy_args += ["--state-dir", state_dir]
    for origin in cors_origins or []:
        proxy_args += ["--enable-cors-header", origin]
    _spawn(proxy_args, proxy_port, "proxy")

    stack = Stack(f"http://127.0.0.1:{proxy_port}", comfyui_port, proxy_port)

    def _cleanup() -> None:
        for proc, _label, _log in procs:
            proc.terminate()
        for proc, _label, _log in procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)

    return stack, _cleanup


@pytest.fixture
def make_stack():
    """Factory for Stack drivers with custom kwargs (e.g. state_dir restarts)."""
    return _make_stack


@pytest.fixture
def fake_comfyui(tmp_path) -> Any:
    """A fake ComfyUI whose lifetime is independent of any proxy; yields its
    port for `make_stack(..., comfyui_port=...)`."""
    port = _free_port()
    log_path = tmp_path / "fake_comfyui_shared.log"
    with log_path.open("w") as log_file:
        proc = subprocess.Popen(
            [sys.executable, str(REPO_ROOT / "demo" / "fake_comfyui.py"), "--port", str(port)],
            cwd=REPO_ROOT,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
    _wait_for_port(port, proc, "fake_comfyui_shared", log_path)
    try:
        yield port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


@pytest.fixture
def stack(tmp_path) -> Any:
    """Spawn fake ComfyUI + proxy on free ports; yield a Stack driver."""
    s, cleanup = _make_stack(tmp_path)
    try:
        yield s
    finally:
        cleanup()


@pytest.fixture
def stack_with_token(tmp_path) -> Any:
    """Same as `stack`, but the proxy requires `Authorization: Bearer secret`
    on every `/api/v2/*` request (the opt-in static-token gate)."""
    s, cleanup = _make_stack(tmp_path, token="secret")
    try:
        yield s
    finally:
        cleanup()


@pytest.fixture
def stack_small_upload(tmp_path) -> Any:
    """Same as `stack`, but the proxy is started with a 1 MB single-request
    upload ceiling so the streaming size-cap enforcement can be exercised."""
    s, cleanup = _make_stack(tmp_path, max_upload_mb=1)
    try:
        yield s
    finally:
        cleanup()


@pytest.fixture
def stack_with_models_dir(tmp_path) -> Any:
    """Same as `stack`, but the proxy is started with --comfyui-base-dir
    pointing at a fresh temp directory, so model-file placement is enabled.
    Yields (Stack, base_dir: Path)."""
    base_dir = tmp_path / "comfyui_base"
    (base_dir / "models").mkdir(parents=True)
    s, cleanup = _make_stack(tmp_path, comfyui_base_dir=str(base_dir))
    try:
        yield s, base_dir
    finally:
        cleanup()


@pytest.fixture
def stack_with_cors(tmp_path) -> Any:
    """Proxy with an explicit browser-origin allowlist (hosted app → localhost)."""
    s, cleanup = _make_stack(tmp_path, cors_origins=["https://app.example.com"])
    try:
        yield s
    finally:
        cleanup()


@pytest.fixture
def stack_with_cors_and_token(tmp_path) -> Any:
    """CORS allowlist plus bearer token — mirrors a hardened local browser setup."""
    s, cleanup = _make_stack(
        tmp_path,
        token="secret",
        cors_origins=["https://app.example.com"],
    )
    try:
        yield s
    finally:
        cleanup()
