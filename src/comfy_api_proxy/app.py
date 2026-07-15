"""The proxy application: Comfy API v2 (demo subset) over a single ComfyUI.

Design notes (verified against the ComfyUI HTTP/WebSocket API):
  * The proxy mints each job's id and passes it to ComfyUI as ``prompt_id``.
    ComfyUI accepts a client-supplied canonical UUID, so history lookups are 1:1.
  * ComfyUI's history and its ``/api/jobs`` endpoint are the SAME in-memory
    store (cap 10,000, lost on restart); we poll ``/history/{id}`` for terminal
    state and outputs, and ``/prompt`` + ``/queue`` to tell queued from running.
  * Job status is served by plain polling — the first-iteration "poll-first"
    path. The live-progress stream is a later milestone.
"""

from __future__ import annotations

import base64
import json
import uuid
from typing import Any

from aiohttp import ClientSession, web

# ---- ComfyUI output type -> our normalized output kind ---------------------
_OUTPUT_KIND = {
    "images": "image",
    "gifs": "video",
    "audio": "audio",
    "text": "text",
    "latents": "latent",
}


def _error(status: int, code: str, message: str, **details: Any) -> web.Response:
    """Build the shared error envelope: {"error": {code, message, details}}."""
    return web.json_response(
        {"error": {"code": code, "message": message, "details": details}},
        status=status,
    )


def _asset_id(filename: str, subfolder: str, type_: str) -> str:
    """Encode a ComfyUI file reference into an opaque, stateless asset id.

    Lets GET /assets/{id}/content reconstruct the upstream /view call without a
    database — sufficient for the demo; the durable store lands in a later PR.
    """
    raw = json.dumps({"f": filename, "s": subfolder, "t": type_}).encode()
    return "asset_" + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_asset_id(asset_id: str) -> dict[str, str] | None:
    if not asset_id.startswith("asset_"):
        return None
    b64 = asset_id[len("asset_") :]
    pad = "=" * (-len(b64) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(b64 + pad))
    except Exception:
        return None


def _is_ui_format(workflow: dict[str, Any]) -> bool:
    """A UI-export graph has top-level 'nodes'/'links'; the API format is a
    map of node-id -> {class_type, inputs}. This is the single most common
    integrator mistake, so we reject it with a precise code."""
    return isinstance(workflow.get("nodes"), list) and "links" in workflow


class Proxy:
    def __init__(self, comfyui_url: str) -> None:
        self.comfyui = comfyui_url.rstrip("/")
        # job_id -> the original submitted workflow, so GET returns it back.
        self._submitted: dict[str, dict[str, Any]] = {}

    # -- upstream helpers ----------------------------------------------------
    async def _get(self, session: ClientSession, path: str) -> tuple[int, Any]:
        async with session.get(self.comfyui + path) as r:
            body = await r.json() if r.content_type == "application/json" else await r.text()
            return r.status, body

    # -- job status mapping --------------------------------------------------
    async def _status_of(self, session: ClientSession, job_id: str) -> dict[str, Any]:
        # 1) Terminal? history holds completed/failed jobs with their outputs.
        st, hist = await self._get(session, f"/history/{job_id}")
        if st == 200 and isinstance(hist, dict) and job_id in hist:
            entry = hist[job_id]
            status_str = (entry.get("status") or {}).get("status_str", "success")
            if status_str == "success":
                return {"status": "succeeded", "outputs": self._outputs(entry)}
            return {
                "status": "failed",
                "outputs": self._outputs(entry),
                "error": self._error_from(entry),
            }
        # 2) Not terminal: is it running or still queued?
        st, q = await self._get(session, "/queue")
        if st == 200 and isinstance(q, dict):
            running = {item[1] for item in q.get("queue_running", []) if len(item) > 1}
            pending = {item[1] for item in q.get("queue_pending", []) if len(item) > 1}
            if job_id in running:
                return {"status": "running", "outputs": []}
            if job_id in pending:
                return {"status": "queued", "outputs": []}
        # 3) Unknown to ComfyUI (never accepted, or evicted/restarted).
        return {"status": "unknown", "outputs": []}

    def _outputs(self, entry: dict[str, Any]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for node_id, node_out in (entry.get("outputs") or {}).items():
            for key, items in node_out.items():
                kind = _OUTPUT_KIND.get(key)
                if not kind or not isinstance(items, list):
                    continue
                for it in items:
                    if not isinstance(it, dict) or "filename" not in it:
                        continue
                    aid = _asset_id(
                        it["filename"], it.get("subfolder", ""), it.get("type", "output")
                    )
                    out.append(
                        {
                            "node_id": node_id,
                            "name": it["filename"],
                            "type": kind,
                            "id": aid,
                            "url": f"/api/v2/assets/{aid}/content",
                        }
                    )
        return out

    def _error_from(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        for event, data in (entry.get("status") or {}).get("messages", []):
            if event == "execution_error" and isinstance(data, dict):
                return {
                    "code": "node_execution_error",
                    "message": data.get("exception_message", ""),
                    "node_id": data.get("node_id"),
                    "class_type": data.get("node_type"),
                }
        return {"code": "node_execution_error", "message": "execution failed"}

    def _job(self, job_id: str, state: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": job_id,
            "status": state["status"],
            "outputs": state.get("outputs", []),
            "error": state.get("error"),
            "urls": {
                "self": f"/api/v2/jobs/{job_id}",
                "events": f"/api/v2/jobs/{job_id}/events",
                "cancel": f"/api/v2/jobs/{job_id}/cancel",
            },
        }

    # -- handlers ------------------------------------------------------------
    async def submit(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return _error(400, "invalid_request", "Body must be JSON.")
        workflow = body.get("workflow")
        if not isinstance(workflow, dict):
            return _error(422, "invalid_workflow", "Missing 'workflow' (API-format graph).")
        if _is_ui_format(workflow):
            return _error(
                422,
                "workflow_format_ui",
                "This is a UI-export graph. Export the workflow in API format instead.",
            )
        job_id = str(uuid.uuid4())
        payload = {"prompt": workflow, "prompt_id": job_id, "client_id": "comfy-api-proxy"}
        async with ClientSession() as session:
            async with session.post(self.comfyui + "/prompt", json=payload) as r:
                data = await r.json() if r.content_type == "application/json" else {}
                if r.status != 200:
                    node_errors = data.get("node_errors") or {}
                    msg = (data.get("error") or {}).get("message", "Workflow rejected.")
                    return _error(422, "invalid_workflow", msg, node_errors=node_errors)
        self._submitted[job_id] = workflow
        job = self._job(job_id, {"status": "queued", "outputs": []})
        return web.json_response(job, status=201)

    async def get_job(self, request: web.Request) -> web.Response:
        job_id = request.match_info["id"]
        async with ClientSession() as session:
            state = await self._status_of(session, job_id)
        if state["status"] == "unknown" and job_id not in self._submitted:
            return _error(404, "not_found", f"No job {job_id}.")
        return web.json_response(self._job(job_id, state))

    async def get_content(self, request: web.Request) -> web.Response:
        ref = _decode_asset_id(request.match_info["id"])
        if ref is None:
            return _error(404, "not_found", "Unknown asset id.")
        params = {"filename": ref["f"], "subfolder": ref["s"], "type": ref["t"]}
        async with ClientSession() as session:
            async with session.get(self.comfyui + "/view", params=params) as r:
                if r.status != 200:
                    return _error(404, "not_found", "Output not available upstream.")
                data = await r.read()
                return web.Response(body=data, content_type=r.content_type)

    async def health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "healthy", "upstream": self.comfyui})


def make_app(comfyui_url: str) -> web.Application:
    proxy = Proxy(comfyui_url)
    app = web.Application()
    app.add_routes(
        [
            web.get("/api/v2/health", proxy.health),
            web.post("/api/v2/jobs", proxy.submit),
            web.get("/api/v2/jobs/{id}", proxy.get_job),
            web.get("/api/v2/assets/{id}/content", proxy.get_content),
        ]
    )
    return app
