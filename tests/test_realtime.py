"""Direct unit tests for the WS->SSE bridge (realtime.py).

The end-to-end `stack` tests drive the bridge through `demo/fake_comfyui.py`,
which only ever sends one legacy preview frame and one `progress` text frame and
never disconnects mid-job. These tests exercise the branches that harness can't
reach: the poll fallback when the WS is unreachable or drops without a terminal
frame, the metadata-carrying preview format, malformed-payload guards, the
progress throttle, and the `progress_state` arithmetic — all with hand-built
fakes so they are fully deterministic (no real sockets, no sleeps).
"""

from __future__ import annotations

import base64
import json
import struct

import aiohttp

from comfy_api_proxy.realtime import (
    _BIN_PREVIEW_IMAGE,
    _BIN_PREVIEW_IMAGE_WITH_METADATA,
    JobEventBridge,
)

PID = "11111111-1111-1111-1111-111111111111"


class _Msg:
    def __init__(self, type_, data):
        self.type = type_
        self.data = data


class _FakeWS:
    """A stand-in for aiohttp's ClientWebSocketResponse."""

    def __init__(self, messages):
        self._messages = list(messages)
        self.sent = []

    async def send_json(self, obj):
        self.sent.append(obj)

    async def receive(self):
        if self._messages:
            return self._messages.pop(0)
        return _Msg(aiohttp.WSMsgType.CLOSED, None)


class _WSCtx:
    def __init__(self, ws=None, exc=None):
        self._ws = ws
        self._exc = exc

    async def __aenter__(self):
        if self._exc is not None:
            raise self._exc
        return self._ws

    async def __aexit__(self, *a):
        return False


class _FakeSession:
    def __init__(self, ctx):
        self._ctx = ctx
        self.connected = []

    def ws_connect(self, url, **kw):
        self.connected.append(url)
        return self._ctx


def _snapshot_seq(*snaps):
    """A snapshot fn returning each snapshot in turn, holding the last."""
    seq = list(snaps)

    async def _fn():
        return seq.pop(0) if len(seq) > 1 else seq[0]

    return _fn


def _bridge(session, snapshot):
    return JobEventBridge("http://comfy", PID, client_id=PID, snapshot=snapshot, session=session)


async def _drain(agen):
    return [f async for f in agen]


def _frame_data(frame: bytes) -> dict:
    return json.loads(frame.decode().split("data: ", 1)[1])


async def test_ws_unreachable_falls_back_to_poll_terminal():
    # ComfyUI's WS can't be reached: stream() must degrade to poll-driven
    # terminal detection and still deliver status + outputs, never raise.
    session = _FakeSession(_WSCtx(exc=aiohttp.ClientConnectionError("down")))
    snap = _snapshot_seq(
        {"status": "running"},
        {"status": "succeeded", "outputs": [{"id": "o1", "url": "u"}]},
    )
    text = b"".join(await _drain(_bridge(session, snap).stream())).decode()
    assert "event: status" in text
    assert '"status":"succeeded"' in text
    assert '"id":"o1"' in text  # outputs delivered via the poll fallback


async def test_ws_closed_midstream_without_terminal_falls_back_to_poll():
    # A WS that delivers a progress frame then closes (ComfyUI restarting
    # mid-job) with no terminal frame must fall through to the poll backstop.
    progress = _Msg(
        aiohttp.WSMsgType.TEXT,
        json.dumps({"type": "progress", "data": {"value": 1, "max": 4}}),
    )
    closed = _Msg(aiohttp.WSMsgType.CLOSED, None)
    ws = _FakeWS([progress, closed])
    session = _FakeSession(_WSCtx(ws=ws))
    snap = _snapshot_seq(
        {"status": "running"},
        {"status": "succeeded", "outputs": [{"id": "o9"}]},
    )
    text = b"".join(await _drain(_bridge(session, snap).stream())).decode()
    assert ws.sent and ws.sent[0]["type"] == "feature_flags"  # handshake sent first
    assert "event: progress" in text
    assert '"status":"succeeded"' in text
    assert '"id":"o9"' in text


def test_preview_frame_with_metadata_decodes_node_and_mime():
    bridge = _bridge(_FakeSession(_WSCtx()), _snapshot_seq({"status": "running"}))
    img = b"\x89PNG\r\n\x1a\n" + b"body-bytes"
    meta = json.dumps({"image_type": "image/webp", "node_id": "7"}).encode()
    payload = struct.pack(">II", _BIN_PREVIEW_IMAGE_WITH_METADATA, len(meta)) + meta + img
    frame = bridge._on_binary(payload)
    assert frame is not None and b"event: preview" in frame
    data = _frame_data(frame)
    assert data["content_type"] == "image/webp"
    assert data["node_id"] == "7"
    assert base64.b64decode(data["data_base64"]) == img


def test_preview_frame_legacy_format_defaults_node_and_png_mime():
    bridge = _bridge(_FakeSession(_WSCtx()), _snapshot_seq({"status": "running"}))
    img = b"\x89PNG\r\n\x1a\nlegacy"
    payload = struct.pack(">II", _BIN_PREVIEW_IMAGE, 2) + img  # fmt 2 == PNG
    data = _frame_data(bridge._on_binary(payload))
    assert data["content_type"] == "image/png"
    assert data["node_id"] == ""  # legacy frame carries no node attribution
    assert base64.b64decode(data["data_base64"]) == img


def test_preview_frame_malformed_metadata_falls_back_safely():
    bridge = _bridge(_FakeSession(_WSCtx()), _snapshot_seq({"status": "running"}))
    img = b"IMG"
    bad = b"{not-json"
    payload = struct.pack(">II", _BIN_PREVIEW_IMAGE_WITH_METADATA, len(bad)) + bad + img
    data = _frame_data(bridge._on_binary(payload))
    assert data["content_type"] == "image/jpeg"  # default when metadata won't parse
    assert data["node_id"] == ""
    assert base64.b64decode(data["data_base64"]) == img


def test_progress_and_preview_frames_are_throttled_within_window():
    bridge = _bridge(_FakeSession(_WSCtx()), _snapshot_seq({"status": "running"}))
    prog = json.dumps({"type": "progress", "data": {"value": 1, "max": 4}})
    first = bridge._on_text(prog)
    second = bridge._on_text(prog)  # same ~2/s window -> coalesced away
    assert len(first) == 1 and second == []
    img = struct.pack(">II", _BIN_PREVIEW_IMAGE, 2) + b"x"
    assert bridge._on_binary(img) is not None
    assert bridge._on_binary(img) is None  # preview throttled too


def test_translate_progress_state_math():
    tr = _bridge(_FakeSession(_WSCtx()), _snapshot_seq({"status": "running"}))._translate_progress
    empty = tr("progress_state", {"nodes": {}})
    assert empty["value"] == 0.0 and empty["nodes_total"] == 0
    alldone = tr(
        "progress_state",
        {"nodes": {"a": {"state": "finished"}, "b": {"state": "finished"}}},
    )
    assert alldone["value"] == 1.0 and alldone["nodes_done"] == 2
    # 4 nodes, 1 finished, one running at 3/10 -> (1 + 3/10) / 4
    mixed = tr(
        "progress_state",
        {
            "nodes": {
                "a": {"state": "finished"},
                "b": {"state": "running", "value": 3, "max": 10},
                "c": {"state": "pending"},
                "d": {"state": "pending"},
            }
        },
    )
    assert abs(mixed["value"] - (1 + 3 / 10) / 4) < 1e-9
    assert mixed["current_node"] == "b" and mixed["step"] == 3 and mixed["steps"] == 10
    # running node with max == 0 must not divide by zero; frac falls back to done/total
    zero = tr(
        "progress_state",
        {"nodes": {"a": {"state": "finished"}, "b": {"state": "running", "value": 5, "max": 0}}},
    )
    assert zero["value"] == 0.5
