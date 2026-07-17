"""The proxy application: the Comfy API v2 surface over a single self-hosted
ComfyUI, wrapping ComfyUI's native HTTP + WebSocket API.

What each v2 operation maps to on ComfyUI:

  * ``POST /api/v2/jobs``        → resolve ``core/ASSET`` refs, then ``POST /prompt``
  * ``GET  /api/v2/jobs/{id}``   → ``/history/{id}`` (+ ``/queue`` for queued/running)
  * ``POST /api/v2/jobs/{id}/cancel`` → ``POST /api/jobs/{id}/cancel`` (atomic)
  * ``GET  /api/v2/jobs/{id}/events``  → live ``/ws`` translated to SSE
  * ``POST /api/v2/assets``      → ``POST /upload/image`` (inputs) or direct
                                   model-dir placement (guarded); blake3 dedup
  * ``POST /api/v2/assets/from-hash`` / ``HEAD .../by-hash/{hash}`` → local index
  * ``GET  /api/v2/assets/{id}`` / ``.../content`` → ``/view`` (Range-capable)

Design invariants preserved from the canonical v2 contract: poll-first
(``GET /jobs/{id}`` is authoritative; SSE is a live enhancement), UUID
identity over content-addressed (blake3) blobs, and "follow links, don't
build URLs" (responses embed follow-up URLs).
"""

from __future__ import annotations

import asyncio
import base64
import collections
import contextlib
import hashlib
import hmac
import json
import mimetypes
import os
import posixpath
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import blake3
from aiohttp import BodyPartReader, ClientError, ClientSession, ClientTimeout, FormData, web

from .assets import AssetRecord, AssetStore
from .realtime import JobEventBridge
from .security import (
    PlacementError,
    atomic_no_clobber_write,
    looks_like_safetensors,
    resolve_placement_path,
    validate_upload_path,
)

# ---- ComfyUI output type -> our normalized output kind ---------------------
_OUTPUT_KIND = {
    "images": "image",
    "gifs": "video",
    "audio": "audio",
    "text": "text",
    "latents": "latent",
}

# Default single-request upload ceiling — matches ComfyUI's own default
# (--max-upload-size, 100 MB). A hard streaming cap so a client can't exhaust
# disk/RAM by never closing a chunked upload.
_DEFAULT_MAX_UPLOAD_BYTES = 100 * 1024 * 1024

# Concurrent SSE streams per proxy — a bounded resource (each holds a ComfyUI
# WS). Beyond this the contract's `too_many_streams` (429) applies; plain
# polling via GET /jobs/{id} is always available regardless.
_MAX_CONCURRENT_STREAMS = 8

_RETENTION = timedelta(hours=24)

# Idempotency-Key bounds. A per-instance self-hosted proxy holds claimed keys
# in memory, so cap both the length of any single key and the total number
# retained — otherwise a caller could grow the set without bound (memory
# exhaustion) by sending unique, arbitrarily long keys. Past the count cap the
# oldest claim is evicted (FIFO); the window is generous enough to cover any
# realistic retry horizon for a single-instance proxy.
_MAX_IDEMPOTENCY_KEY_LEN = 255
_MAX_IDEMPOTENCY_KEYS = 10_000


def _error(status: int, code: str, message: str, **details: Any) -> web.Response:
    """Build the shared error envelope: {"error": {code, message, details}}."""
    return web.json_response(
        {"error": {"code": code, "message": message, "details": details or None}},
        status=status,
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _external_base(request: web.Request) -> str:
    """The absolute base URL clients reached us on, from the request's scheme and
    Host header. Content and job links are built from this so the contract's
    absolute-URI fields (Output.url / Asset.url) are actually absolute — and so a
    client on http://host:port follows links back to the same origin. No
    X-Forwarded-* handling is wired, so those headers are ignored (a client can't
    point the emitted links at another origin); front this with a TLS terminator
    only if you add explicit forwarded-header trust."""
    return f"{request.scheme}://{request.host}"


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _is_ui_format(workflow: dict[str, Any]) -> bool:
    """A UI-export graph has top-level 'nodes'/'links'; the API format is a
    map of node-id -> {class_type, inputs}. This is the single most common
    integrator mistake, so we reject it with a precise code."""
    return isinstance(workflow.get("nodes"), list) and "links" in workflow


def _is_asset_ref(value: Any) -> bool:
    """A node that claims to be a core/ASSET reference by its ``__type`` tag.

    Whether its ``info`` block is well-formed is deliberately NOT checked here:
    a node tagged ``core/ASSET`` is always treated as an (attempted) reference so
    a malformed one is rejected with 422 ``missing_asset`` rather than forwarded
    to ComfyUI verbatim as a literal dict (see ``_rewrite_asset_refs``)."""
    return isinstance(value, dict) and value.get("__type") == "core/ASSET"


class UpstreamUnreachable(Exception):
    """Raised when ComfyUI can't be reached at all (down, restarting, ...), so
    callers can map it to a proper `upstream_unreachable` error envelope
    instead of letting the raw transport error surface as a bare 500."""


def _valid_job_id(job_id: str) -> bool:
    """Job ids are minted server-side as uuid4 strings (see Proxy.submit). A
    non-UUID id is never legitimate and — worse — would be spliced verbatim
    into the upstream ComfyUI request path (see _get_json / cancel_job): a
    dot-segment gets normalized by the HTTP client into an arbitrary ComfyUI
    path, and a literal '?' injects a query string. Reject before any upstream
    call is made rather than trying to sanitize."""
    try:
        uuid.UUID(job_id)
        return True
    except ValueError:
        return False


class Proxy:
    def __init__(
        self,
        comfyui_url: str,
        *,
        comfyui_base_dir: str | None = None,
        max_upload_bytes: int = _DEFAULT_MAX_UPLOAD_BYTES,
    ) -> None:
        self.comfyui = comfyui_url.rstrip("/")
        # Filesystem root of the ComfyUI install, if the proxy is co-located
        # and thus able to place model files directly. None => model-dir
        # placement is disabled and such uploads are rejected with a clear
        # error (input uploads still work, proxied to /upload/image).
        self.base_dir = Path(comfyui_base_dir).resolve() if comfyui_base_dir else None
        self.max_upload_bytes = max_upload_bytes
        self.assets = AssetStore()
        # job_id -> {"workflow": ..., "created_at": datetime, "client_id": str}
        self._jobs: dict[str, dict[str, Any]] = {}
        # Claimed Idempotency-Key values (single-use, reject-on-duplicate — no
        # replay, matching the v2 contract). In-memory for a per-instance
        # self-hosted proxy: keys live for the process lifetime and a restart
        # clears them. Bounded in size (see _MAX_IDEMPOTENCY_KEYS) with FIFO
        # eviction; an OrderedDict is used as an ordered set so the oldest
        # claim can be popped when the cap is reached.
        self._idempotency_keys: collections.OrderedDict[str, None] = collections.OrderedDict()
        self._session: ClientSession | None = None
        self._open_streams = 0
        # Per-process secret used to HMAC-sign stateless output asset ids
        # (see _asset_id / _decode_asset_id) — random per Proxy instance, so
        # a restart invalidates ids from a prior process rather than
        # reusing a fixed key. Never persisted, never logged.
        self._asset_secret = os.urandom(32)

    # -- lifecycle -----------------------------------------------------------
    async def on_startup(self, app: web.Application) -> None:
        self._session = ClientSession(timeout=ClientTimeout(total=None, sock_connect=10))

    async def on_cleanup(self, app: web.Application) -> None:
        if self._session is not None:
            await self._session.close()

    @property
    def session(self) -> ClientSession:
        assert self._session is not None, "session not started"
        return self._session

    # -- stateless output asset ids -------------------------------------------
    def _asset_id(self, job_id: str, filename: str, subfolder: str, type_: str) -> str:
        """Encode a ComfyUI file reference into a stateless, deterministic,
        HMAC-signed asset id, so a job's outputs get stable ids across
        polls without a durable store. Uploaded assets use random UUIDs
        (see assets.new_asset_id); the two are told apart at read time by
        whether this decodes.

        The job id is part of the signed payload so two DIFFERENT jobs that
        happen to produce the same filename/subfolder/type (ComfyUI reuses
        output filenames whenever a workflow uses a fixed prefix, or after a
        restart resets its counter) get DISTINCT asset ids — otherwise a
        client that cached job A's output URL could later be served job B's
        bytes under the same "stable" id.

        The payload is signed with a per-process secret (see
        ``self._asset_secret``) so a client cannot forge an id naming an
        arbitrary filename/subfolder/type and have the proxy trust it —
        without the signature check, a hand-crafted id would let a caller
        read arbitrary files ComfyUI's ``/view`` can reach, or splice an
        attacker-chosen path into a submitted workflow via
        ``_resolve_asset_ref``.
        """
        raw = json.dumps({"j": job_id, "f": filename, "s": subfolder, "t": type_}).encode()
        payload_b64 = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        tag = hmac.new(self._asset_secret, payload_b64.encode(), hashlib.sha256).digest()
        tag_b64 = base64.urlsafe_b64encode(tag).decode().rstrip("=")
        return f"asset_{payload_b64}.{tag_b64}"

    def _decode_asset_id(self, asset_id: str) -> dict[str, str] | None:
        """Decode + verify a stateless asset id minted by ``_asset_id``.

        Returns ``None`` (treated as "unknown/not found" by every caller)
        for anything that isn't a well-formed, correctly-signed id —
        including a syntactically valid but forged one, closing the
        "any well-formed asset_<...> id is trusted" gap.
        """
        if not asset_id.startswith("asset_"):
            return None
        rest = asset_id[len("asset_") :]
        payload_b64, sep, tag_b64 = rest.partition(".")
        if not sep:
            return None
        try:
            given_tag = _b64url_decode(tag_b64)
        except Exception:
            return None
        expected_tag = hmac.new(self._asset_secret, payload_b64.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(expected_tag, given_tag):
            return None
        try:
            ref = json.loads(_b64url_decode(payload_b64))
        except Exception:
            return None
        if isinstance(ref, dict) and {"f", "s", "t"} <= ref.keys():
            # "j" (job id) is present on ids minted after the per-job-scoping
            # change; only f/s/t are read downstream (content fetch / asset-ref
            # resolution), so its presence or absence is harmless here.
            return ref
        return None

    # -- upstream helpers ----------------------------------------------------
    async def _get_json(self, path: str) -> tuple[int, Any]:
        try:
            async with self.session.get(self.comfyui + path) as r:
                body = await r.json() if r.content_type == "application/json" else await r.text()
                return r.status, body
        except (ClientError, asyncio.TimeoutError) as e:
            raise UpstreamUnreachable(str(e)) from e

    # -- job status mapping --------------------------------------------------
    async def _status_of(self, job_id: str, base: str) -> dict[str, Any]:
        # 1) Terminal? history holds completed/failed jobs with their outputs.
        st, hist = await self._get_json(f"/history/{job_id}")
        if st == 200 and isinstance(hist, dict) and job_id in hist:
            entry = hist[job_id]
            status_obj = entry.get("status") or {}
            status_str = status_obj.get("status_str", "success")
            messages = status_obj.get("messages", [])
            interrupted = any(
                ev == "execution_interrupted" for ev, _ in messages if isinstance(ev, str)
            )
            if interrupted:
                return {"status": "canceled", "outputs": self._outputs(job_id, entry, base)}
            if status_str == "success":
                return {"status": "succeeded", "outputs": self._outputs(job_id, entry, base)}
            return {
                "status": "failed",
                "outputs": self._outputs(job_id, entry, base),
                "error": self._error_from(entry),
            }
        # 2) Not terminal: is it running or still queued?
        st, q = await self._get_json("/queue")
        if st == 200 and isinstance(q, dict):
            running = {item[1] for item in q.get("queue_running", []) if len(item) > 1}
            pending = [item[1] for item in q.get("queue_pending", []) if len(item) > 1]
            if job_id in running:
                return {
                    "status": "running",
                    "outputs": [],
                    "progress": self._running_progress(),
                }
            if job_id in pending:
                return {
                    "status": "queued",
                    "outputs": [],
                    "queue_position": pending.index(job_id) + 1,
                }
        # 3) Unknown to ComfyUI (never accepted, or evicted/restarted).
        return {"status": "unknown", "outputs": []}

    def _running_progress(self) -> dict[str, Any]:
        # The poll path has no live step counter; report an indeterminate but
        # schema-valid running snapshot. The SSE stream carries the live one.
        return {"value": 0.0, "nodes_done": 0, "nodes_total": 0}

    def _outputs(self, job_id: str, entry: dict[str, Any], base: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for node_id, node_out in (entry.get("outputs") or {}).items():
            for key, items in node_out.items():
                kind = _OUTPUT_KIND.get(key)
                if not kind or not isinstance(items, list):
                    continue
                for it in items:
                    if not isinstance(it, dict) or "filename" not in it:
                        continue
                    aid = self._asset_id(
                        job_id, it["filename"], it.get("subfolder", ""), it.get("type", "output")
                    )
                    ctype = mimetypes.guess_type(it["filename"])[0] or "application/octet-stream"
                    now = _now()
                    out.append(
                        {
                            "node_id": node_id,
                            "name": it["filename"],
                            "type": kind,
                            "content_type": ctype,
                            "size_bytes": 0,
                            "id": aid,
                            "hash": None,
                            "url": f"{base}/api/v2/assets/{aid}/content",
                            "url_expires_at": _iso(now + _RETENTION),
                        }
                    )
        return out

    def _error_from(self, entry: dict[str, Any]) -> dict[str, Any]:
        for event, data in (entry.get("status") or {}).get("messages", []):
            if event == "execution_error" and isinstance(data, dict):
                return {
                    "code": "node_execution_error",
                    "message": data.get("exception_message", ""),
                    "node_id": data.get("node_id"),
                    "class_type": data.get("node_type"),
                    "traceback": None,
                }
        return {
            "code": "node_execution_error",
            "message": "execution failed",
            "node_id": None,
            "class_type": None,
            "traceback": None,
        }

    def _job(self, job_id: str, state: dict[str, Any], base: str) -> dict[str, Any]:
        meta = self._jobs.get(job_id, {})
        created = meta.get("created_at", _now())
        status = state["status"]
        if status == "unknown":
            status = "expired"  # known-to-proxy but gone upstream => expired
        return {
            "id": job_id,
            "status": status,
            "created_at": _iso(created),
            "started_at": None,
            "completed_at": None,
            "expires_at": _iso(created + _RETENTION),
            "queue_position": state.get("queue_position"),
            "progress": state.get("progress"),
            "outputs": state.get("outputs", []),
            "error": state.get("error"),
            "urls": {
                "self": f"{base}/api/v2/jobs/{job_id}",
                "events": f"{base}/api/v2/jobs/{job_id}/events",
                "cancel": f"{base}/api/v2/jobs/{job_id}/cancel",
            },
        }

    # ==== core/ASSET walker =================================================
    def _resolve_asset_ref(self, info: dict[str, Any]) -> str | None:
        """Resolve one core/ASSET info block to the filename ComfyUI expects
        (the value a LoadImage-style widget would carry), or None if it can't
        be resolved to a ready, owned asset."""
        record: AssetRecord | None = None
        asset_id = info.get("id")
        if isinstance(asset_id, str):
            record = self.assets.get(asset_id)
            if record is None:
                decoded = self._decode_asset_id(asset_id)
                if decoded is not None:
                    # A stateless output id used as an input — resolve to its
                    # filename directly (subfolder-qualified for /view semantics).
                    return (
                        posixpath.join(decoded["s"], decoded["f"]) if decoded["s"] else decoded["f"]
                    )
        if record is None and isinstance(info.get("hash"), str):
            record = self.assets.get_by_hash(info["hash"])
        if record is None:
            return None
        ref = record.comfy_ref
        if ref is not None:
            return (
                posixpath.join(ref["subfolder"], ref["filename"])
                if ref.get("subfolder")
                else ref["filename"]
            )
        # Model file placed on disk: ComfyUI references it by its file_path.
        return record.file_path

    def _rewrite_asset_refs(self, node: Any, missing: list[str]) -> Any:
        """Recursively replace core/ASSET refs in a workflow with the
        filename string ComfyUI expects, collecting unresolvable ids."""
        if _is_asset_ref(node):
            info = node.get("info")
            if not isinstance(info, dict):
                # Tagged core/ASSET but with no (or a non-object) info block —
                # unresolvable. Reject rather than forward the raw dict.
                missing.append("<malformed core/ASSET: missing info>")
                return node
            resolved = self._resolve_asset_ref(info)
            if resolved is None:
                missing.append(str(info.get("id", "<no id>")))
                return node
            return resolved
        if isinstance(node, dict):
            return {k: self._rewrite_asset_refs(v, missing) for k, v in node.items()}
        if isinstance(node, list):
            return [self._rewrite_asset_refs(v, missing) for v in node]
        return node

    # ==== job handlers ======================================================
    async def submit(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return _error(400, "invalid_request", "Body must be JSON.")
        if body.get("webhook_url") is not None or body.get("inputs") is not None:
            return _error(
                422,
                "invalid_workflow",
                "'webhook_url' and 'inputs' are reserved and not accepted.",
            )
        workflow = body.get("workflow")
        if not isinstance(workflow, dict):
            return _error(422, "invalid_workflow", "Missing 'workflow' (API-format graph).")
        if _is_ui_format(workflow):
            return _error(
                422,
                "workflow_format_ui",
                "This is a UI-export graph. Export the workflow in API format instead.",
            )

        missing: list[str] = []
        resolved_workflow = self._rewrite_asset_refs(workflow, missing)
        if missing:
            return _error(
                422,
                "missing_asset",
                f"Unresolvable core/ASSET reference(s): {', '.join(missing)}",
                missing_ids=missing,
            )

        # ComfyUI's POST /prompt requires prompt_id to be a canonical UUID
        # (server.py rejects anything else). We reuse the job id verbatim as
        # prompt_id and client_id, so the job id must itself be a UUID.
        job_id = str(uuid.uuid4())
        # A per-job client_id, not a fixed one shared by every submission.
        # ComfyUI addresses progress/preview/executing/executed/
        # execution_success/error/interrupted WS events at the client_id
        # that SUBMITTED the prompt — it never broadcasts them. A shared
        # fixed client_id means only whichever WS connection currently
        # holds that name (if any) receives events for ANY job, so every
        # other concurrent SSE stream (which connects with its own,
        # different client_id) silently gets none. Using job_id itself as
        # the client_id gives each job's own SSE bridge (see job_events,
        # which connects with this same id) a 1:1 addressable target.
        # Idempotency-Key: single-use, reject-on-duplicate (no replay), matching
        # the v2 contract. Claim it synchronously (no await between the
        # membership test and the add) so two concurrent submits with the same
        # key can't both pass. The claim is released below only if the submit
        # DEFINITELY created no job (ComfyUI rejected the workflow); on an
        # unknown outcome (ComfyUI unreachable — it may already hold the prompt)
        # the key is HELD so a same-key retry can't double-submit.
        key = request.headers.get("Idempotency-Key")
        if key is not None:
            # A present-but-empty header is malformed input, not "no key"; and
            # cap the length so a caller can't grow the in-memory set with an
            # arbitrarily long key.
            if not key or len(key) > _MAX_IDEMPOTENCY_KEY_LEN:
                return _error(
                    400,
                    "invalid_request",
                    f"Idempotency-Key must be 1-{_MAX_IDEMPOTENCY_KEY_LEN} characters.",
                )
            if key in self._idempotency_keys:
                return _error(
                    422,
                    "idempotency_key_reuse",
                    "This Idempotency-Key has already been used. Keys are single-use; "
                    "poll or list your jobs instead of resubmitting with the same key.",
                )
            self._idempotency_keys[key] = None
            if len(self._idempotency_keys) > _MAX_IDEMPOTENCY_KEYS:
                self._idempotency_keys.popitem(last=False)  # evict oldest (FIFO)

        payload = {
            "prompt": resolved_workflow,
            "prompt_id": job_id,
            "client_id": job_id,
        }
        try:
            async with self.session.post(self.comfyui + "/prompt", json=payload) as r:
                if r.status != 200:
                    # A definite non-200 means ComfyUI created no job. Release
                    # the key for a retry based on the status ALONE, before
                    # reading the body — a failed body read must not turn a
                    # definite rejection into a (wrongly) held key.
                    if key:
                        self._idempotency_keys.pop(key, None)
                    try:
                        data = await r.json() if r.content_type == "application/json" else {}
                    except (ClientError, asyncio.TimeoutError, ValueError):
                        data = {}
                    node_errors = data.get("node_errors") or {}
                    msg = (data.get("error") or {}).get("message", "Workflow rejected.")
                    return _error(422, "invalid_workflow", msg, node_errors=node_errors)
        except (ClientError, asyncio.TimeoutError):
            # Unknown outcome — ComfyUI may already hold the prompt. HOLD the
            # key (do not release) so a same-key retry can't double-submit.
            return _error(503, "upstream_unreachable", "Could not reach ComfyUI to submit the job.")
        self._jobs[job_id] = {"workflow": workflow, "created_at": _now(), "client_id": job_id}
        base = _external_base(request)
        return web.json_response(
            self._job(job_id, {"status": "queued", "outputs": []}, base), status=201
        )

    async def get_job(self, request: web.Request) -> web.Response:
        job_id = request.match_info["id"]
        if not _valid_job_id(job_id):
            return _error(404, "not_found", f"No job {job_id}.")
        base = _external_base(request)
        try:
            state = await self._status_of(job_id, base)
        except UpstreamUnreachable:
            return _error(503, "upstream_unreachable", "Could not reach ComfyUI.")
        if state["status"] == "unknown" and job_id not in self._jobs:
            return _error(404, "not_found", f"No job {job_id}.")
        return web.json_response(self._job(job_id, state, base))

    async def cancel_job(self, request: web.Request) -> web.Response:
        job_id = request.match_info["id"]
        if not _valid_job_id(job_id):
            return _error(404, "not_found", f"No job {job_id}.")
        base = _external_base(request)
        if job_id not in self._jobs:
            # Allow cancel of an id ComfyUI still knows even if the proxy
            # restarted; a wholly unknown id is a 404.
            try:
                state = await self._status_of(job_id, base)
            except UpstreamUnreachable:
                return _error(503, "upstream_unreachable", "Could not reach ComfyUI.")
            if state["status"] == "unknown":
                return _error(404, "not_found", f"No job {job_id}.")
        # ComfyUI's atomic per-id cancel (interrupt-if-running or dequeue).
        try:
            async with self.session.post(self.comfyui + f"/api/jobs/{job_id}/cancel") as r:
                await r.read()
        except Exception:
            return _error(503, "upstream_unreachable", "Could not reach ComfyUI to cancel.")
        try:
            state = await self._status_of(job_id, base)
        except UpstreamUnreachable:
            return _error(503, "upstream_unreachable", "Could not reach ComfyUI.")
        # A cancel of a still-running job reports `canceling` until the
        # interrupt lands at the next node boundary.
        if state["status"] == "running":
            state["status"] = "canceling"
        return web.json_response(self._job(job_id, state, base))

    async def job_events(self, request: web.Request) -> web.StreamResponse:
        job_id = request.match_info["id"]
        if not _valid_job_id(job_id):
            return _error(404, "not_found", f"No job {job_id}.")
        base = _external_base(request)
        try:
            state = await self._status_of(job_id, base)
        except UpstreamUnreachable:
            return _error(503, "upstream_unreachable", "Could not reach ComfyUI.")
        if state["status"] == "unknown" and job_id not in self._jobs:
            return _error(404, "not_found", f"No job {job_id}.")
        if self._open_streams >= _MAX_CONCURRENT_STREAMS:
            resp = _error(
                429,
                "too_many_streams",
                "Maximum concurrent event streams reached; poll GET /jobs/{id} instead.",
            )
            resp.headers["Retry-After"] = "5"
            return resp

        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
        await response.prepare(request)

        async def snapshot() -> dict[str, Any]:
            snap = await self._status_of(job_id, base)
            job = self._job(job_id, snap, base)
            return {
                "status": job["status"],
                "queue_position": job["queue_position"],
                "progress": job["progress"],
                "outputs": job["outputs"],
            }

        # Connect the WS bridge with the SAME client_id the job was
        # submitted under (see submit()), so ComfyUI's per-client-addressed
        # events actually reach this connection. Fall back to job_id itself
        # if the proxy has no record of the job (e.g. restarted) — that
        # matches what submit() would have used anyway.
        client_id = self._jobs.get(job_id, {}).get("client_id", job_id)
        bridge = JobEventBridge(
            self.comfyui, job_id, client_id=client_id, snapshot=snapshot, session=self.session
        )
        self._open_streams += 1
        try:
            async for frame in bridge.stream():
                await response.write(frame)
        except (ConnectionResetError, asyncio.CancelledError, UpstreamUnreachable):
            # Headers were already sent (response.prepare above), so a
            # mid-stream ComfyUI outage can't change the status code — end the
            # stream cleanly and let the client fall back to polling (which now
            # returns a proper upstream_unreachable envelope).
            pass
        finally:
            self._open_streams -= 1
            with contextlib.suppress(Exception):
                await response.write_eof()
        return response

    # ==== asset handlers ====================================================
    async def upload_asset(self, request: web.Request) -> web.Response:
        base = _external_base(request)
        if not request.content_type.startswith("multipart/"):
            return _error(422, "invalid_request", "Expected multipart/form-data.")
        try:
            reader = await request.multipart()
        except Exception:
            return _error(422, "invalid_request", "Malformed multipart body.")

        fields: dict[str, str] = {}
        tags: list[str] = []
        tmp_path: str | None = None
        size = 0
        digest = blake3.blake3()
        part_content_type = "application/octet-stream"

        try:
            # mypy/aiohttp-stubs disagree on MultipartReader.__aiter__'s self
            # type across versions; the runtime behavior (iterate parts) is
            # exactly per aiohttp's own docs, so this is a stub-only mismatch.
            async for part in reader:  # type: ignore[misc]
                # Every part of a multipart/form-data body we accept is a
                # plain, named field (BodyPartReader with a name); a nested
                # MultipartReader, or a part with no Content-Disposition
                # name, is not something the v2 upload contract sends.
                # Skip it defensively rather than assume the narrower type.
                if not isinstance(part, BodyPartReader) or part.name is None:
                    continue
                if part.name == "file":
                    part_content_type = part.headers.get("Content-Type", part_content_type)
                    fd, tmp_path = tempfile.mkstemp(prefix="comfy-upload-")
                    with os.fdopen(fd, "wb") as f:
                        while True:
                            chunk = await part.read_chunk(1 << 16)
                            if not chunk:
                                break
                            size += len(chunk)
                            if size > self.max_upload_bytes:
                                return _error(
                                    413,
                                    "payload_too_large",
                                    f"Upload exceeds {self.max_upload_bytes} bytes.",
                                )
                            digest.update(chunk)
                            f.write(chunk)
                elif part.name == "tags":
                    tags.append((await part.text()).strip())
                else:
                    fields[part.name] = (await part.text()).strip()

            if tmp_path is None:
                return _error(422, "invalid_request", "Missing 'file' part.")
            file_path = fields.get("file_path")
            if not file_path:
                return _error(422, "invalid_request", "Missing 'file_path'.")
            # Validate + classify the path ONCE, before deciding which branch
            # handles it (model-directory placement vs. plain input upload).
            # This must happen before that decision, not after: a path like
            # "input/../checkpoints/evil.safetensors" has parts[0] == "input",
            # so a classifier that only looks at the first segment sends it
            # down the (previously unvalidated) input branch, letting the
            # ".." ride along into whatever that branch does with the raw
            # string — completely skipping the model-placement guard. See
            # security.validate_upload_path.
            try:
                is_model, norm_path = validate_upload_path(file_path)
            except PlacementError as e:
                return _error(422, "invalid_request", str(e))
            content_type = fields.get("content_type") or part_content_type
            computed_hash = "blake3:" + digest.hexdigest()

            expected = fields.get("expected_hash")
            if expected and expected.lower() != computed_hash.lower():
                return _error(
                    409,
                    "hash_mismatch",
                    "Client-declared hash does not match the received bytes.",
                )

            # Dedup fast-path: bytes we already have -> return existing asset.
            existing = self.assets.get_by_hash(computed_hash)
            if existing is not None:
                return web.json_response(
                    self._asset_json(existing, created_new=False, base=base), status=200
                )

            with open(tmp_path, "rb") as f:
                data = f.read()

            if is_model:
                record, err = self._place_model_file(
                    norm_path, data, computed_hash, content_type, size, tags
                )
            else:
                record, err = await self._upload_input(
                    norm_path, file_path, data, computed_hash, content_type, size, tags
                )
            if err is not None:
                return err
            assert record is not None
            return web.json_response(
                self._asset_json(record, created_new=True, base=base), status=201
            )
        finally:
            if tmp_path is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path)

    def _place_model_file(
        self,
        norm: str,
        data: bytes,
        hash_: str,
        content_type: str,
        size: int,
        tags: list[str],
    ) -> tuple[AssetRecord | None, web.Response | None]:
        if self.base_dir is None:
            return None, _error(
                422,
                "invalid_request",
                "Model-directory placement requires the proxy to run co-located "
                "with ComfyUI (start it with --comfyui-base-dir).",
            )
        if not looks_like_safetensors(data):
            return None, _error(
                422,
                "invalid_request",
                "Model uploads must be valid safetensors files (header check failed).",
            )
        # `norm` is already validated + "models/"-prefix-stripped by
        # validate_upload_path (called once, before the model/input branch
        # decision) — it is exactly the category-relative path (e.g.
        # "checkpoints/foo.safetensors") resolve_placement_path() expects
        # against a base_dir that IS the ComfyUI models/ directory (its
        # MODEL_ROOTS keys are exactly folder_paths.py's
        # folder_names_and_paths keys, which live directly under models/,
        # not under the install root) — so resolve against
        # `self.base_dir / "models"`, not `self.base_dir` itself.
        try:
            dest = resolve_placement_path(self.base_dir / "models", norm)
        except PlacementError as e:
            return None, _error(422, "invalid_request", str(e))
        try:
            atomic_no_clobber_write(dest, data)
        except PlacementError as e:
            return None, _error(409, "hash_mismatch", str(e))
        # ComfyUI's combo widgets (and its model-loading nodes generally)
        # reference a model by its path RELATIVE TO the model-root
        # directory — folder_paths.get_filename_list()'s values never
        # include the category segment itself. The value substituted for a
        # core/ASSET reference to this file (see _resolve_asset_ref) must
        # therefore be root-relative, not the category-qualified `norm`
        # used for on-disk placement, or ComfyUI's combo validation rejects
        # it as an unknown filename.
        root_relative = posixpath.join(*Path(norm).parts[1:])
        record = self.assets.add(
            hash_=hash_,
            size_bytes=size,
            content_type=content_type,
            file_path=root_relative,
            disk_path=str(dest),
            tags=tags,
        )
        return record, None

    async def _upload_input(
        self,
        norm: str,
        orig_file_path: str,
        data: bytes,
        hash_: str,
        content_type: str,
        size: int,
        tags: list[str],
    ) -> tuple[AssetRecord | None, web.Response | None]:
        # `norm` is already validated + "input/"-prefix-stripped by
        # validate_upload_path (called once, before the model/input branch
        # decision) — derive subfolder/filename from it directly rather
        # than re-parsing the raw client string here.
        subfolder = posixpath.dirname(norm)
        filename = posixpath.basename(norm)
        form = FormData()
        form.add_field("image", data, filename=filename, content_type=content_type)
        form.add_field("type", "input")
        if subfolder:
            form.add_field("subfolder", subfolder)
        try:
            async with self.session.post(self.comfyui + "/upload/image", data=form) as r:
                if r.status != 200:
                    return None, _error(500, "upstream_error", "ComfyUI rejected the upload.")
                resp = await r.json()
        except Exception:
            return None, _error(500, "upstream_error", "Failed to reach ComfyUI for upload.")
        record = self.assets.add(
            hash_=hash_,
            size_bytes=size,
            content_type=content_type,
            file_path=orig_file_path,
            comfy_ref={
                "filename": resp.get("name", filename),
                "subfolder": resp.get("subfolder", subfolder),
                "type": resp.get("type", "input"),
            },
            tags=tags,
        )
        return record, None

    async def asset_from_hash(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return _error(400, "invalid_request", "Body must be JSON.")
        hash_ = body.get("hash")
        if not isinstance(hash_, str):
            return _error(422, "invalid_request", "Missing 'hash'.")
        record = self.assets.get_by_hash(hash_)
        if record is None:
            # A miss and "exists but not yours" are deliberately identical.
            return _error(404, "blob_not_found", "No blob the caller may mint from.")
        # Single-user self-hosted: minting a second reference over the same
        # blob returns the same asset (the reference already exists).
        base = _external_base(request)
        return web.json_response(self._asset_json(record, created_new=False, base=base), status=200)

    async def head_asset_by_hash(self, request: web.Request) -> web.Response:
        hash_ = request.match_info["hash"]
        if self.assets.has_hash(hash_):
            return web.Response(status=200)
        return web.Response(status=404)

    async def get_asset(self, request: web.Request) -> web.Response:
        asset_id = request.match_info["id"]
        base = _external_base(request)
        record = self.assets.get(asset_id)
        if record is not None:
            return web.json_response(self._asset_json(record, created_new=None, base=base))
        decoded = self._decode_asset_id(asset_id)
        if decoded is not None:
            now = _now()
            ctype = mimetypes.guess_type(decoded["f"])[0] or "application/octet-stream"
            return web.json_response(
                {
                    "id": asset_id,
                    "hash": None,
                    "size_bytes": 0,
                    "content_type": ctype,
                    "file_path": decoded["f"],
                    "created_at": _iso(now),
                    "url": f"{base}/api/v2/assets/{asset_id}/content",
                    "url_expires_at": _iso(now + _RETENTION),
                }
            )
        return _error(404, "not_found", "Unknown asset id.")

    async def get_asset_content(self, request: web.Request) -> web.StreamResponse:
        asset_id = request.match_info["id"]
        record = self.assets.get(asset_id)
        if record is not None and record.disk_path:
            # Proxy-placed file on disk: FileResponse handles Range/206 natively.
            return web.FileResponse(record.disk_path)
        if record is not None and record.comfy_ref:
            return await self._stream_view(request, record.comfy_ref)
        decoded = self._decode_asset_id(asset_id)
        if decoded is not None:
            return await self._stream_view(
                request,
                {"filename": decoded["f"], "subfolder": decoded["s"], "type": decoded["t"]},
            )
        return _error(404, "not_found", "Unknown asset id.")

    async def _stream_view(self, request: web.Request, ref: dict[str, str]) -> web.StreamResponse:
        params = {
            "filename": ref["filename"],
            "subfolder": ref.get("subfolder", ""),
            "type": ref.get("type", "output"),
        }
        headers = {}
        if "Range" in request.headers:
            headers["Range"] = request.headers["Range"]
        try:
            upstream = await self.session.get(
                self.comfyui + "/view", params=params, headers=headers
            )
        except Exception:
            return _error(500, "upstream_error", "Failed to reach ComfyUI for content.")
        if upstream.status not in (200, 206):
            upstream.release()
            return _error(404, "not_found", "Output not available upstream.")
        out = web.StreamResponse(status=upstream.status)
        out.content_type = upstream.content_type
        for h in ("Content-Length", "Content-Range", "Accept-Ranges"):
            if h in upstream.headers:
                out.headers[h] = upstream.headers[h]
        await out.prepare(request)
        async for chunk in upstream.content.iter_chunked(1 << 16):
            await out.write(chunk)
        upstream.release()
        await out.write_eof()
        return out

    def _asset_json(
        self, record: AssetRecord, *, created_new: bool | None, base: str
    ) -> dict[str, Any]:
        now = _now()
        body: dict[str, Any] = {
            "id": record.id,
            "hash": record.hash or None,
            "size_bytes": record.size_bytes,
            "content_type": record.content_type,
            "file_path": record.file_path,
            "created_at": record.created_at,
            "url": f"{base}/api/v2/assets/{record.id}/content",
            "url_expires_at": _iso(now + _RETENTION),
        }
        if created_new is not None:
            body["created_new"] = created_new
        return body

    async def health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "healthy", "upstream": self.comfyui})


def make_app(
    comfyui_url: str,
    *,
    comfyui_base_dir: str | None = None,
    max_upload_bytes: int = _DEFAULT_MAX_UPLOAD_BYTES,
    middlewares: list[Any] | None = None,
) -> web.Application:
    proxy = Proxy(
        comfyui_url,
        comfyui_base_dir=comfyui_base_dir,
        max_upload_bytes=max_upload_bytes,
    )
    app = web.Application(
        client_max_size=max_upload_bytes + (1 << 20),
        middlewares=middlewares or [],
    )
    app.on_startup.append(proxy.on_startup)
    app.on_cleanup.append(proxy.on_cleanup)
    app.add_routes(
        [
            web.get("/api/v2/health", proxy.health),
            # jobs
            web.post("/api/v2/jobs", proxy.submit),
            web.get("/api/v2/jobs/{id}", proxy.get_job),
            web.post("/api/v2/jobs/{id}/cancel", proxy.cancel_job),
            web.get("/api/v2/jobs/{id}/events", proxy.job_events),
            # assets
            web.post("/api/v2/assets", proxy.upload_asset),
            web.post("/api/v2/assets/from-hash", proxy.asset_from_hash),
            web.head("/api/v2/assets/by-hash/{hash}", proxy.head_asset_by_hash),
            web.get("/api/v2/assets/{id}", proxy.get_asset),
            web.get("/api/v2/assets/{id}/content", proxy.get_asset_content),
        ]
    )
    return app
