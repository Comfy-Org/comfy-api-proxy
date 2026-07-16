"""Unit tests for the origin-check middleware (ported from ComfyUI core's
create_origin_only_middleware).
"""

from __future__ import annotations

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from comfy_api_proxy.middleware import origin_only_middleware


async def _ok(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


def _make_app() -> web.Application:
    app = web.Application(middlewares=[origin_only_middleware])
    app.router.add_get("/thing", _ok)
    return app


async def _client():
    app = _make_app()
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    return client


class TestOriginOnlyMiddleware:
    async def test_no_origin_header_is_allowed(self, aiohttp_client):
        client = await aiohttp_client(_make_app())
        resp = await client.get("/thing")
        assert resp.status == 200

    async def test_matching_origin_and_host_is_allowed(self, aiohttp_client):
        client = await aiohttp_client(_make_app())
        resp = await client.get(
            "/thing",
            headers={"Host": "127.0.0.1:8189", "Origin": "http://127.0.0.1:8189"},
        )
        assert resp.status == 200

    async def test_mismatched_origin_on_loopback_host_is_rejected(self, aiohttp_client):
        client = await aiohttp_client(_make_app())
        resp = await client.get(
            "/thing",
            headers={"Host": "127.0.0.1:8189", "Origin": "http://evil.example:1234"},
        )
        assert resp.status == 403
        body = await resp.json()
        assert body["error"]["code"] == "forbidden_origin"

    async def test_cross_site_sec_fetch_is_rejected_even_without_origin_mismatch(
        self, aiohttp_client
    ):
        client = await aiohttp_client(_make_app())
        resp = await client.get(
            "/thing",
            headers={
                "Host": "127.0.0.1:8189",
                "Origin": "http://127.0.0.1:8189",
                "Sec-Fetch-Site": "cross-site",
            },
        )
        assert resp.status == 403

    async def test_same_site_sec_fetch_is_allowed(self, aiohttp_client):
        client = await aiohttp_client(_make_app())
        resp = await client.get(
            "/thing",
            headers={"Sec-Fetch-Site": "same-origin"},
        )
        assert resp.status == 200

    async def test_port_omitted_on_one_side_still_matches(self, aiohttp_client):
        client = await aiohttp_client(_make_app())
        resp = await client.get(
            "/thing",
            headers={"Host": "localhost:8189", "Origin": "http://localhost"},
        )
        assert resp.status == 200
