"""Translate ComfyUI's WebSocket into v2 Server-Sent Events.

ComfyUI's ``/ws`` is the only *live* signal it exposes — history/queue polling
tells you where a job is, but not the moment-to-moment progress and previews.
This module bridges one SSE client to ComfyUI's WS for a single job:

  * connect to ComfyUI ``/ws?clientId=…`` and perform the ``feature_flags``
    handshake (client sends first; server replies) — declaring
    ``supports_preview_metadata`` so previews arrive with their node id;
  * emit a snapshot on connect (a ``status`` event, plus the latest
    ``progress`` and most-recent ``preview`` if the job is already running),
    reconciled from ``GET /history`` / ``/queue`` so a client that connects
    late still gets current state;
  * forward subsequent ``progress``/``progress_state`` and preview frames,
    throttled to ~2/s, as v2 ``progress`` / ``preview`` events;
  * emit an ``output`` event as each output asset is committed, and a final
    ``status`` event at the terminal transition, after which the stream ends;
  * send an SSE heartbeat comment periodically so idle connections and dead
    peers are detected.

The bridge is intentionally per-connection and best-effort: the authoritative
state is always ``GET /api/v2/jobs/{id}`` (poll-first), and every ``progress``
event is a complete snapshot, so a dropped frame or a reconnect costs nothing.
If ComfyUI's WS can't be reached, the caller falls back to a poll-driven
stream so the endpoint still yields terminal state rather than 500-ing.
"""

from __future__ import annotations

import asyncio
import json
import struct
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import aiohttp

# ComfyUI binary WS framing: a 4-byte big-endian event type, then the payload.
# (protocol.BinaryEventTypes in ComfyUI core.)
_BIN_PREVIEW_IMAGE = 1
_BIN_PREVIEW_IMAGE_WITH_METADATA = 4
# Within a PREVIEW_IMAGE payload: a 4-byte big-endian image-format enum, then
# the encoded image bytes. 1 = JPEG, 2 = PNG (server.py send_image()).
_IMG_FORMAT_MIME = {1: "image/jpeg", 2: "image/png"}

_THROTTLE_SECONDS = 0.5  # ~2 progress/preview events per second
_HEARTBEAT_SECONDS = 15.0


def sse_frame(event: str, data: dict[str, Any]) -> bytes:
    """Encode one SSE frame. No ``id:`` — the v2 stream is live-push, not a
    replayable log (there is deliberately no Last-Event-ID resume)."""
    payload = json.dumps(data, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n".encode()


def sse_comment(text: str) -> bytes:
    """An SSE comment line — used as a heartbeat; ignored by clients."""
    return f": {text}\n\n".encode()


StatusSnapshotFn = Callable[[], Awaitable[dict[str, Any]]]


class JobEventBridge:
    """Bridges ComfyUI's WS to a v2 SSE stream for one ``prompt_id``."""

    def __init__(
        self,
        comfyui_url: str,
        prompt_id: str,
        *,
        client_id: str,
        snapshot: StatusSnapshotFn,
        session: aiohttp.ClientSession,
    ) -> None:
        self._comfyui = comfyui_url.rstrip("/")
        self._prompt_id = prompt_id
        # Must match the client_id the prompt was SUBMITTED under — ComfyUI
        # addresses progress/preview/executing/executed/execution_success/
        # error/interrupted WS events at that specific client_id, never
        # broadcasting them. A bridge connecting under a different (e.g.
        # freshly-random) client_id would never see this job's events.
        self._client_id = client_id
        self._snapshot = snapshot
        self._session = session
        self._last_progress_emit = 0.0
        self._last_preview_emit = 0.0
        self._last_output_emit = 0.0
        self._terminal = {"succeeded", "failed", "canceled", "expired"}

    def _ws_url(self, client_id: str) -> str:
        base = self._comfyui.replace("http://", "ws://").replace("https://", "wss://")
        return f"{base}/ws?clientId={client_id}"

    async def stream(self) -> AsyncIterator[bytes]:
        """Yield SSE frames until the job reaches a terminal status.

        Always yields at least an initial ``status`` snapshot. If the job is
        already terminal on connect, yields that and returns immediately —
        no WS connection is opened.
        """
        snap = await self._snapshot()
        yield sse_frame("status", self._status_event(snap))
        if snap.get("status") in self._terminal:
            # Already terminal on connect (late/reconnecting client): still
            # deliver the job's outputs, matching every other terminal path in
            # this file. Without this, a client connecting after completion got
            # only `status` and had to fall back to GET /jobs/{id} for outputs.
            for frame in self._final_outputs(snap, set()):
                yield frame
            return
        if snap.get("progress"):
            yield sse_frame("progress", snap["progress"])

        try:
            async for frame in self._pump_ws(self._client_id):
                yield frame
        except (aiohttp.ClientError, asyncio.TimeoutError):
            # WS unreachable/broke: fall back to poll-driven terminal detection
            # so the stream still completes with an authoritative status.
            async for frame in self._poll_until_terminal():
                yield frame

    def _status_event(self, snap: dict[str, Any]) -> dict[str, Any]:
        event: dict[str, Any] = {"status": snap.get("status", "queued")}
        if snap.get("queue_position") is not None:
            event["queue_position"] = snap["queue_position"]
        return event

    async def _pump_ws(self, client_id: str) -> AsyncIterator[bytes]:
        seen_outputs: set[str] = set()
        async with self._session.ws_connect(self._ws_url(client_id), heartbeat=30.0) as ws:
            # feature_flags handshake — client sends first (server.py).
            await ws.send_json(
                {"type": "feature_flags", "data": {"supports_preview_metadata": True}}
            )
            last_beat = time.monotonic()
            while True:
                try:
                    msg = await asyncio.wait_for(ws.receive(), timeout=_HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    yield sse_comment("keepalive")
                    last_beat = time.monotonic()
                    # Reconcile in case a terminal transition was missed.
                    snap = await self._snapshot()
                    if snap.get("status") in self._terminal:
                        yield sse_frame("status", self._status_event(snap))
                        for frame in self._final_outputs(snap, seen_outputs):
                            yield frame
                        return
                    continue

                if msg.type == aiohttp.WSMsgType.TEXT:
                    for frame in self._on_text(msg.data):
                        yield frame
                    # An `executed` frame means a node's outputs have just been
                    # committed — surface them live (deduped via seen_outputs)
                    # rather than only at the terminal transition. Best-effort:
                    # if an output's durable asset isn't resolvable in the
                    # snapshot yet, it is simply picked up on a later `executed`
                    # or the terminal reconcile below. The snapshot stays
                    # authoritative and seen_outputs guarantees no output is
                    # ever emitted twice. Coalesced to ~2/s so a workflow with
                    # many output nodes can't fire one /history re-fetch per
                    # node (a skipped fetch loses nothing — the next allowed
                    # `executed` or the terminal reconcile still delivers them).
                    if self._is_executed_text(msg.data):
                        for frame in await self._reconcile_outputs(seen_outputs):
                            yield frame
                    # A terminal text event ends the stream after reconciling.
                    if self._is_terminal_text(msg.data):
                        snap = await self._snapshot()
                        yield sse_frame("status", self._status_event(snap))
                        for frame in self._final_outputs(snap, seen_outputs):
                            yield frame
                        return
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    preview_frame = self._on_binary(msg.data)
                    if preview_frame is not None:
                        yield preview_frame
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.ERROR,
                ):
                    break

                if time.monotonic() - last_beat > _HEARTBEAT_SECONDS:
                    yield sse_comment("keepalive")
                    last_beat = time.monotonic()

        # WS closed without a terminal text event; reconcile via poll.
        async for frame in self._poll_until_terminal():
            yield frame

    def _on_text(self, raw: str) -> list[bytes]:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if not isinstance(msg, dict):
            return []
        mtype = msg.get("type")
        data = msg.get("data") or {}
        # ComfyUI targets prompt-scoped messages with a prompt_id; ignore
        # anything for a different job sharing this ComfyUI.
        pid = data.get("prompt_id")
        if pid is not None and pid != self._prompt_id:
            return []

        if mtype in ("progress", "progress_state"):
            now = time.monotonic()
            if now - self._last_progress_emit < _THROTTLE_SECONDS:
                return []
            self._last_progress_emit = now
            return [sse_frame("progress", self._translate_progress(mtype, data))]
        # `execution_error` is surfaced purely as a terminal transition — the
        # final `status` event carries the failure. No separate `log` event is
        # emitted: the v2 contract reserves `log` as not-yet-emitted, matching
        # the Comfy Cloud surface, so the two stay at feature parity.
        return []

    def _is_terminal_text(self, raw: str) -> bool:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return False
        if not isinstance(msg, dict):
            return False
        data = msg.get("data") or {}
        pid = data.get("prompt_id")
        if pid is not None and pid != self._prompt_id:
            return False
        # execution_success / execution_error / execution_interrupted are the
        # terminal WS signals for a prompt.
        return msg.get("type") in (
            "execution_success",
            "execution_error",
            "execution_interrupted",
        )

    def _is_executed_text(self, raw: str) -> bool:
        """True for an ``executed`` frame addressed to this prompt — the signal
        that a node's outputs have just been committed, so they can be surfaced
        as a live ``output`` event instead of only at the terminal transition."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return False
        if not isinstance(msg, dict):
            return False
        data = msg.get("data") or {}
        pid = data.get("prompt_id")
        if pid is not None and pid != self._prompt_id:
            return False
        return msg.get("type") == "executed"

    def _translate_progress(self, mtype: str, data: dict[str, Any]) -> dict[str, Any]:
        if mtype == "progress":
            value = float(data.get("value", 0))
            maxv = float(data.get("max", 1)) or 1.0
            return {
                "value": max(0.0, min(1.0, value / maxv)),
                "nodes_done": 0,
                "nodes_total": 0,
                "current_node": str(data["node"]) if data.get("node") is not None else None,
                "step": int(value),
                "steps": int(maxv),
            }
        # progress_state: {nodes: {id: {value, max, state, ...}}}
        nodes = data.get("nodes") or {}
        total = len(nodes)
        done = sum(1 for n in nodes.values() if n.get("state") == "finished")
        running = next((nid for nid, n in nodes.items() if n.get("state") == "running"), None)
        frac = (done / total) if total else 0.0
        cur = nodes.get(running) if running else None
        if cur and cur.get("max"):
            frac = (done + (cur["value"] / cur["max"])) / total if total else 0.0
        return {
            "value": max(0.0, min(1.0, frac)),
            "nodes_done": done,
            "nodes_total": total,
            "current_node": str(running) if running is not None else None,
            "step": int(cur["value"]) if cur and cur.get("value") is not None else None,
            "steps": int(cur["max"]) if cur and cur.get("max") is not None else None,
        }

    def _on_binary(self, data: bytes) -> bytes | None:
        if len(data) < 8:
            return None
        now = time.monotonic()
        if now - self._last_preview_emit < _THROTTLE_SECONDS:
            return None
        (event_type,) = struct.unpack(">I", data[:4])
        node_id = ""
        if event_type == _BIN_PREVIEW_IMAGE:
            (fmt,) = struct.unpack(">I", data[4:8])
            mime = _IMG_FORMAT_MIME.get(fmt, "image/jpeg")
            image_bytes = data[8:]
        elif event_type == _BIN_PREVIEW_IMAGE_WITH_METADATA:
            (meta_len,) = struct.unpack(">I", data[4:8])
            try:
                meta = json.loads(data[8 : 8 + meta_len].decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                meta = {}
            mime = meta.get("image_type", "image/jpeg")
            node_id = str(meta.get("node_id", ""))
            image_bytes = data[8 + meta_len :]
        else:
            return None
        self._last_preview_emit = now
        import base64

        return sse_frame(
            "preview",
            {
                "node_id": node_id,
                "content_type": mime,
                "data_base64": base64.b64encode(image_bytes).decode("ascii"),
            },
        )

    async def _reconcile_outputs(self, seen: set[str]) -> list[bytes]:
        """Re-snapshot and return SSE frames for any newly-committed outputs
        (deduped via ``seen``), throttled to ~2/s so a burst of ``executed``
        frames can't fan out into a burst of /history re-fetches. A skipped
        window loses nothing — the next allowed ``executed`` or the terminal
        reconcile still delivers those outputs."""
        now = time.monotonic()
        if now - self._last_output_emit < _THROTTLE_SECONDS:
            return []
        self._last_output_emit = now
        snap = await self._snapshot()
        return self._final_outputs(snap, seen)

    def _final_outputs(self, snap: dict[str, Any], seen: set[str]) -> list[bytes]:
        frames = []
        for out in snap.get("outputs", []):
            key = out.get("id", "")
            if key in seen:
                continue
            seen.add(key)
            frames.append(sse_frame("output", out))
        return frames

    async def _poll_until_terminal(self) -> AsyncIterator[bytes]:
        seen: set[str] = set()
        deadline = time.monotonic() + 600  # generous hard cap
        while time.monotonic() < deadline:
            snap = await self._snapshot()
            if snap.get("status") in self._terminal:
                yield sse_frame("status", self._status_event(snap))
                for frame in self._final_outputs(snap, seen):
                    yield frame
                return
            await asyncio.sleep(1.0)
            yield sse_comment("keepalive")
