"""Unit tests for the origin-check and CORS middleware."""

from __future__ import annotations

import pytest
from aiohttp import web

from comfy_api_proxy.middleware import (
    attach_cors_prepare,
    make_cors_middleware,
    make_origin_only_middleware,
    normalize_cors_origin,
    origin_only_middleware,
)


async def _ok(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


def _make_app(*, cors_origins: list[str] | None = None) -> web.Application:
    origins = cors_origins or []
    middlewares = []
    if origins:
        middlewares.append(make_cors_middleware(origins))
    middlewares.append(make_origin_only_middleware(origins) if origins else origin_only_middleware)
    app = web.Application(middlewares=middlewares)
    if origins:
        attach_cors_prepare(app)
    app.router.add_get("/thing", _ok)
    app.router.add_route("OPTIONS", "/thing", _ok)
    return app


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


class TestCorsAllowlist:
    async def test_allowlisted_cross_site_origin_is_allowed(self, aiohttp_client):
        origin = "https://app.example.com"
        client = await aiohttp_client(_make_app(cors_origins=[origin]))
        resp = await client.get(
            "/thing",
            headers={
                "Host": "127.0.0.1:8189",
                "Origin": origin,
                "Sec-Fetch-Site": "cross-site",
            },
        )
        assert resp.status == 200
        assert resp.headers["Access-Control-Allow-Origin"] == origin
        allow = {p.strip().lower() for p in resp.headers["Access-Control-Allow-Headers"].split(",")}
        assert {"authorization", "content-type", "idempotency-key"} <= allow
        expose = {
            p.strip().lower() for p in resp.headers["Access-Control-Expose-Headers"].split(",")
        }
        assert {"retry-after", "content-range", "accept-ranges"} <= expose

    async def test_preflight_returns_204_with_cors_headers(self, aiohttp_client):
        origin = "https://app.example.com"
        client = await aiohttp_client(_make_app(cors_origins=[origin]))
        resp = await client.options(
            "/thing",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,idempotency-key",
                "Sec-Fetch-Site": "cross-site",
            },
        )
        assert resp.status == 204
        assert resp.headers["Access-Control-Allow-Origin"] == origin
        allow = {p.strip().lower() for p in resp.headers["Access-Control-Allow-Headers"].split(",")}
        assert {"authorization", "content-type", "idempotency-key"} <= allow
        methods = {
            p.strip().upper() for p in resp.headers["Access-Control-Allow-Methods"].split(",")
        }
        assert "POST" in methods

    async def test_non_allowlisted_origin_still_rejected(self, aiohttp_client):
        client = await aiohttp_client(_make_app(cors_origins=["https://app.example.com"]))
        resp = await client.get(
            "/thing",
            headers={
                "Host": "127.0.0.1:8189",
                "Origin": "https://evil.example",
                "Sec-Fetch-Site": "cross-site",
            },
        )
        assert resp.status == 403
        assert "Access-Control-Allow-Origin" not in resp.headers


class TestNormalizeCorsOrigin:
    def test_strips_trailing_slash(self):
        assert normalize_cors_origin("https://app.example.com/") == "https://app.example.com"

    def test_omits_default_https_port(self):
        assert normalize_cors_origin("https://app.example.com:443") == "https://app.example.com"

    def test_omits_default_http_port(self):
        assert normalize_cors_origin("http://app.example.com:80") == "http://app.example.com"

    def test_keeps_non_default_port(self):
        assert (
            normalize_cors_origin("https://app.example.com:8443") == "https://app.example.com:8443"
        )

    def test_rejects_wildcard(self):
        with pytest.raises(ValueError, match="not allowed"):
            normalize_cors_origin("*")

    def test_rejects_path(self):
        with pytest.raises(ValueError, match="path"):
            normalize_cors_origin("https://app.example.com/api")

    def test_rejects_userinfo(self):
        with pytest.raises(ValueError, match="userinfo"):
            normalize_cors_origin("https://user:pass@app.example.com")

    def test_rejects_invalid_port(self):
        with pytest.raises(ValueError, match="invalid port"):
            normalize_cors_origin("https://app.example.com:bad")

    def test_rejects_empty_port(self):
        with pytest.raises(ValueError, match="empty port"):
            normalize_cors_origin("http://host:")
