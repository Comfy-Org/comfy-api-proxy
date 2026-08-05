"""Browser-origin → localhost proxy: allowlisted CORS end-to-end.

Simulates a hosted web app (Origin: https://app.example.com) calling a
proxy bound on 127.0.0.1 — the ordinary hosted-web-app → local-proxy shape.
"""

from __future__ import annotations

HOSTED_ORIGIN = "https://app.example.com"
EVIL_ORIGIN = "https://evil.example"


def _assert_cors_headers(headers: dict[str, str], *, origin: str = HOSTED_ORIGIN) -> None:
    assert headers.get("access-control-allow-origin") == origin
    allow = {
        p.strip().lower()
        for p in headers.get("access-control-allow-headers", "").split(",")
        if p.strip()
    }
    assert {"authorization", "content-type", "idempotency-key"} <= allow
    expose = {
        p.strip().lower()
        for p in headers.get("access-control-expose-headers", "").split(",")
        if p.strip()
    }
    assert {"retry-after", "content-range", "accept-ranges"} <= expose
    assert headers.get("access-control-allow-credentials") == "true"
    assert "origin" in {p.strip().lower() for p in headers.get("vary", "").split(",")}


def test_allowlisted_origin_can_preflight_and_call_health(stack_with_cors):
    """Hosted origin: OPTIONS preflight + GET /api/v2/health are readable."""
    status, body, raw, headers = stack_with_cors.request_with_headers(
        "OPTIONS",
        "/api/v2/health",
        headers={
            "Origin": HOSTED_ORIGIN,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization,idempotency-key",
            "Sec-Fetch-Site": "cross-site",
        },
    )
    assert status == 204, raw
    assert body is None
    _assert_cors_headers(headers)

    status, body, raw, headers = stack_with_cors.request_with_headers(
        "GET",
        "/api/v2/health",
        headers={
            "Origin": HOSTED_ORIGIN,
            "Sec-Fetch-Site": "cross-site",
        },
    )
    assert status == 200, raw
    assert body is not None and body.get("status") == "healthy"
    _assert_cors_headers(headers)


def test_allowlisted_origin_can_submit_job_with_idempotency_key(stack_with_cors):
    """Real POST with Authorization-capable preflight headers + Idempotency-Key."""
    status, _body, raw, headers = stack_with_cors.request_with_headers(
        "OPTIONS",
        "/api/v2/jobs",
        headers={
            "Origin": HOSTED_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,authorization,idempotency-key",
            "Sec-Fetch-Site": "cross-site",
        },
    )
    assert status == 204, raw
    _assert_cors_headers(headers)

    status, body, raw, headers = stack_with_cors.request_with_headers(
        "POST",
        "/api/v2/jobs",
        {"workflow": {"1": {}}},
        headers={
            "Origin": HOSTED_ORIGIN,
            "Sec-Fetch-Site": "cross-site",
            "Idempotency-Key": "browser-e2e-1",
        },
    )
    assert status == 201, (status, body, raw)
    _assert_cors_headers(headers)


def test_non_allowlisted_origin_is_rejected(stack_with_cors):
    status, body, _raw, headers = stack_with_cors.request_with_headers(
        "GET",
        "/api/v2/health",
        headers={
            "Origin": EVIL_ORIGIN,
            "Sec-Fetch-Site": "cross-site",
        },
    )
    assert status == 403
    assert body is not None and body["error"]["code"] == "forbidden_origin"
    assert "access-control-allow-origin" not in headers


def test_non_allowlisted_preflight_is_rejected(stack_with_cors):
    status, body, _raw, headers = stack_with_cors.request_with_headers(
        "OPTIONS",
        "/api/v2/health",
        headers={
            "Origin": EVIL_ORIGIN,
            "Access-Control-Request-Method": "GET",
            "Sec-Fetch-Site": "cross-site",
        },
    )
    assert status == 403
    assert body is not None and body["error"]["code"] == "forbidden_origin"
    assert "access-control-allow-origin" not in headers


def test_default_proxy_still_blocks_hosted_origin(stack):
    """Without --enable-cors-header, cross-site hosted origins stay blocked."""
    status, body, _raw, headers = stack.request_with_headers(
        "GET",
        "/api/v2/health",
        headers={
            "Origin": HOSTED_ORIGIN,
            "Sec-Fetch-Site": "cross-site",
        },
    )
    assert status == 403
    assert body is not None and body["error"]["code"] == "forbidden_origin"
    assert "access-control-allow-origin" not in headers


def test_token_gate_allows_preflight_then_requires_bearer(stack_with_cors_and_token):
    """Preflight is unauthenticated; the real call still needs the bearer.

    Probes a token-guarded job path rather than /api/v2/health, which is
    deliberately readable without a credential so schedulers can poll it.
    """
    jid = "11111111-1111-1111-1111-111111111111"
    status, _body, raw, headers = stack_with_cors_and_token.request_with_headers(
        "OPTIONS",
        f"/api/v2/jobs/{jid}",
        headers={
            "Origin": HOSTED_ORIGIN,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
            "Sec-Fetch-Site": "cross-site",
        },
    )
    assert status == 204, raw
    _assert_cors_headers(headers)

    unauth, body, _raw, headers = stack_with_cors_and_token.request_with_headers(
        "GET",
        f"/api/v2/jobs/{jid}",
        headers={
            "Origin": HOSTED_ORIGIN,
            "Sec-Fetch-Site": "cross-site",
        },
    )
    assert unauth == 401
    assert body is not None and body["error"]["code"] == "unauthorized"
    # 401 must still carry CORS headers so the browser can read the error body.
    _assert_cors_headers(headers)

    authed, _body, raw, headers = stack_with_cors_and_token.request_with_headers(
        "GET",
        f"/api/v2/jobs/{jid}",
        headers={
            "Origin": HOSTED_ORIGIN,
            "Sec-Fetch-Site": "cross-site",
            "Authorization": "Bearer secret",
        },
    )
    # Past the gate and into the handler: the job id is unknown, so the
    # handler's own 404 is the proof — a bare "not 401" would also accept a
    # 403 or a 500 that never reached it.
    assert authed == 404, raw
    _assert_cors_headers(headers)
