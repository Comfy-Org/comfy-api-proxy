"""A tiny stand-in for ComfyUI, just enough to exercise the proxy end to end
without a GPU. Implements the handful of endpoints the proxy calls:
/prompt, /history/{id}, /queue, /view. A submitted job "completes" instantly
with one PNG output.
"""
from __future__ import annotations

from aiohttp import web

# 1x1 red PNG.
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
    "53de0000000c49444154789c6360f8cf00000301010018dd8db10000000049454e44ae426082"
)

_history: dict[str, dict] = {}


async def prompt(request: web.Request) -> web.Response:
    body = await request.json()
    prompt_id = body["prompt_id"]
    graph = body.get("prompt", {})
    # Reject an obviously invalid graph (no nodes) to exercise the 422 path.
    if not graph:
        return web.json_response(
            {"error": {"type": "invalid", "message": "empty graph"}, "node_errors": {}},
            status=400,
        )
    _history[prompt_id] = {
        "outputs": {"9": {"images": [{"filename": "out.png", "subfolder": "", "type": "output"}]}},
        "status": {"status_str": "success", "completed": True, "messages": []},
    }
    return web.json_response({"prompt_id": prompt_id, "number": 1, "node_errors": {}})


async def history(request: web.Request) -> web.Response:
    pid = request.match_info["id"]
    return web.json_response({pid: _history[pid]} if pid in _history else {})


async def queue(request: web.Request) -> web.Response:
    return web.json_response({"queue_running": [], "queue_pending": []})


async def view(request: web.Request) -> web.Response:
    return web.Response(body=_PNG, content_type="image/png")


def make_fake() -> web.Application:
    app = web.Application()
    app.add_routes(
        [
            web.post("/prompt", prompt),
            web.get("/history/{id}", history),
            web.get("/queue", queue),
            web.get("/view", view),
        ]
    )
    return app


if __name__ == "__main__":
    web.run_app(make_fake(), host="127.0.0.1", port=8188)
