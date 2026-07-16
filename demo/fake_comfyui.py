"""A stand-in for ComfyUI, just enough to exercise the proxy end to end
without a GPU. Implements the native endpoints the proxy calls:

    POST /prompt                 queue a job (client-supplied prompt_id)
    GET  /queue                  running/pending snapshot
    GET  /history/{id}           terminal state + outputs
    GET  /view                   serve output/input bytes (Range-capable)
    POST /upload/image           accept an input upload -> {name, subfolder, type}
    POST /api/jobs/{id}/cancel   atomic per-id cancel -> {"cancelled": bool}
    GET  /ws                     feature_flags handshake + progress/preview/terminal

State machine (deliberately tiny but realistic):

  * ``POST /prompt`` puts the job in ``running`` and schedules it to move to
    ``history`` (success) after a short delay — so a plain poll sees
    running then succeeded.
  * A workflow whose graph contains an input ``{"hang": true}`` never
    auto-completes, so cancellation has a deterministic in-flight target.
  * ``/ws`` drives one connecting client: it performs the handshake, emits a
    ``progress`` text frame and a binary preview frame, then waits for the
    job to reach a terminal state (via the timer or a cancel) and emits the
    matching ``execution_success`` / ``execution_interrupted`` text frame.
"""

from __future__ import annotations

import asyncio
import json
import struct
import uuid

from aiohttp import WSMsgType, web

# 1x1 red PNG, reused for both outputs and preview frames.
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
    "53de0000000c49444154789c6360f8cf00000301010018dd8db10000000049454e44ae426082"
)

# prompt_id -> {"state": "running"|"success"|"interrupted", "outputs": {...},
#               "hang": bool}
_jobs: dict[str, dict] = {}
# Files accepted via /upload/image, keyed by (type, subfolder, name).
_uploads: set[tuple[str, str, str]] = set()

_COMPLETE_DELAY = 0.2


def _default_outputs() -> dict:
    return {"9": {"images": [{"filename": "out.png", "subfolder": "", "type": "output"}]}}


async def _auto_complete(prompt_id: str, delay: float = _COMPLETE_DELAY) -> None:
    await asyncio.sleep(delay)
    job = _jobs.get(prompt_id)
    if job and job["state"] == "running" and not job["hang"]:
        job["state"] = "success"


async def prompt(request: web.Request) -> web.Response:
    body = await request.json()
    prompt_id = body["prompt_id"]
    # Real ComfyUI records which client_id a prompt was submitted under and
    # addresses that prompt's WS events (progress/preview/executing/
    # execution_success/error/interrupted) at that client_id specifically —
    # it never broadcasts to every open WS. Track it here too so `websocket`
    # below can honor the same scoped-delivery contract instead of just
    # blasting events at whichever job happens to be running.
    client_id = body.get("client_id")
    # Real ComfyUI rejects a non-UUID prompt_id (server.py validates it), so a
    # proxy that mints its own id must mint a canonical UUID. Enforce it here
    # too, otherwise this class of bug stays invisible against the fake.
    try:
        uuid.UUID(str(prompt_id))
    except (ValueError, TypeError):
        return web.json_response(
            {
                "error": {"type": "invalid_prompt", "message": "prompt_id must be a valid UUID"},
                "node_errors": {},
            },
            status=400,
        )
    graph = body.get("prompt", {})
    if not graph:
        return web.json_response(
            {"error": {"type": "invalid", "message": "empty graph"}, "node_errors": {}},
            status=400,
        )
    hang = any(
        isinstance(node, dict)
        and isinstance(node.get("inputs"), dict)
        and node["inputs"].get("hang") is True
        for node in graph.values()
    )
    # A workflow may set `complete_after_seconds` on any node to control how
    # long the fake stays running — lets a test connect its SSE stream before
    # the job completes without racing the default 0.2s timer.
    complete_after = _COMPLETE_DELAY
    for node in graph.values():
        if isinstance(node, dict) and isinstance(node.get("inputs"), dict):
            v = node["inputs"].get("complete_after_seconds")
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                complete_after = float(v)
    _jobs[prompt_id] = {
        "state": "running",
        "outputs": _default_outputs(),
        "hang": hang,
        "client_id": client_id,
    }
    if not hang:
        asyncio.create_task(_auto_complete(prompt_id, complete_after))
    return web.json_response({"prompt_id": prompt_id, "number": 1, "node_errors": {}})


def _history_entry(job: dict) -> dict:
    if job["state"] == "interrupted":
        return {
            "outputs": {},
            "status": {
                "status_str": "error",
                "completed": False,
                "messages": [["execution_interrupted", {}]],
            },
        }
    return {
        "outputs": job["outputs"],
        "status": {"status_str": "success", "completed": True, "messages": []},
    }


async def history(request: web.Request) -> web.Response:
    pid = request.match_info["id"]
    job = _jobs.get(pid)
    if job and job["state"] in ("success", "interrupted"):
        return web.json_response({pid: _history_entry(job)})
    return web.json_response({})


async def queue(request: web.Request) -> web.Response:
    running = [[0, pid] for pid, j in _jobs.items() if j["state"] == "running"]
    return web.json_response({"queue_running": running, "queue_pending": []})


async def view(request: web.Request) -> web.StreamResponse:
    # Byte body with Range support, for parity with ComfyUI's FileResponse.
    range_header = request.headers.get("Range")
    if range_header and range_header.startswith("bytes="):
        start_s, _, end_s = range_header[len("bytes=") :].partition("-")
        start = int(start_s) if start_s else 0
        end = int(end_s) if end_s else len(_PNG) - 1
        chunk = _PNG[start : end + 1]
        resp = web.Response(body=chunk, status=206, content_type="image/png")
        resp.headers["Content-Range"] = f"bytes {start}-{end}/{len(_PNG)}"
        resp.headers["Accept-Ranges"] = "bytes"
        return resp
    return web.Response(body=_PNG, content_type="image/png")


async def upload_image(request: web.Request) -> web.Response:
    post = await request.post()
    image = post.get("image")
    name = getattr(image, "filename", None) or "upload.png"
    subfolder = str(post.get("subfolder", "") or "")
    type_ = str(post.get("type", "input") or "input")
    _uploads.add((type_, subfolder, name))
    return web.json_response({"name": name, "subfolder": subfolder, "type": type_})


async def cancel_job(request: web.Request) -> web.Response:
    pid = request.match_info["job_id"]
    job = _jobs.get(pid)
    if job and job["state"] == "running":
        job["state"] = "interrupted"
        return web.json_response({"cancelled": True})
    return web.json_response({"cancelled": False})


async def websocket(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    # The connecting client's clientId — real ComfyUI's /ws takes this as a
    # query param and only ever delivers a given prompt's events to the
    # connection whose clientId matches the one that prompt was submitted
    # under (see `prompt()` above). Honoring that scoping here (instead of
    # just picking "the" running job) is what makes this fake actually
    # exercise per-client addressing rather than happening to work only
    # because tests run one job at a time.
    client_id = request.query.get("clientId")
    # feature_flags handshake: client sends first, server replies.
    async for msg in ws:
        if msg.type == WSMsgType.TEXT:
            data = json.loads(msg.data)
            if data.get("type") == "feature_flags":
                await ws.send_json(
                    {"type": "feature_flags", "data": {"supports_preview_metadata": True}}
                )
                break
        elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.ERROR):
            return ws

    # Drive only the running job that was submitted under THIS connection's
    # client_id — never a job belonging to some other client.
    pid = next(
        (
            p
            for p, j in _jobs.items()
            if j["state"] == "running" and j.get("client_id") == client_id
        ),
        None,
    )
    if pid is not None:
        await ws.send_json(
            {"type": "progress", "data": {"value": 1, "max": 2, "node": "3", "prompt_id": pid}}
        )
        # Binary preview: >I event type (1 = PREVIEW_IMAGE), >I format (2 = PNG).
        frame = struct.pack(">I", 1) + struct.pack(">I", 2) + _PNG
        await ws.send_bytes(frame)
        # Wait for the job to reach a terminal state (timer or cancel).
        for _ in range(200):
            state = _jobs.get(pid, {}).get("state")
            if state == "success":
                # Real ComfyUI fires `executed` (a node's committed outputs)
                # before the terminal `execution_success`; by now /history
                # already carries this job's outputs, so the proxy can surface
                # them as a live `output` event ahead of the terminal status.
                await ws.send_json(
                    {
                        "type": "executed",
                        "data": {
                            "node": "9",
                            "output": _default_outputs()["9"],
                            "prompt_id": pid,
                        },
                    }
                )
                await ws.send_json({"type": "execution_success", "data": {"prompt_id": pid}})
                break
            if state == "interrupted":
                await ws.send_json({"type": "execution_interrupted", "data": {"prompt_id": pid}})
                break
            await asyncio.sleep(0.05)
    await ws.close()
    return ws


def make_fake() -> web.Application:
    app = web.Application()
    app.add_routes(
        [
            web.post("/prompt", prompt),
            web.get("/history/{id}", history),
            web.get("/queue", queue),
            web.get("/view", view),
            web.post("/upload/image", upload_image),
            web.post("/api/jobs/{job_id}/cancel", cancel_job),
            web.get("/ws", websocket),
        ]
    )
    return app


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(prog="fake_comfyui")
    parser.add_argument("--port", type=int, default=8188)
    args = parser.parse_args()
    web.run_app(make_fake(), host="127.0.0.1", port=args.port)
