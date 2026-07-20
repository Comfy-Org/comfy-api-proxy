"""Tests for the optional static bearer-token gate (auth.py).

The module has no coverage today: no other test sends a token, so its accept
and reject paths are only incidentally exercised. These test the middleware
directly (fast, no server) for every accept/reject branch, plus one end-to-end
check that the CLI actually installs the gate on the live proxy.
"""

from __future__ import annotations

from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from comfy_api_proxy.auth import make_bearer_auth_middleware


async def _invoke(mw, headers, path="/api/v2/jobs/x"):
    """Run the middleware with a stub handler; report (response, handler_ran)."""
    called = {"v": False}

    async def handler(_req):
        called["v"] = True
        return web.json_response({"ok": True})

    resp = await mw(make_mocked_request("GET", path, headers=headers), handler)
    return resp, called["v"]


async def test_missing_or_malformed_authorization_rejected():
    mw = make_bearer_auth_middleware("secret")
    for headers in (
        {},  # no header at all
        {"Authorization": "secret"},  # no scheme
        {"Authorization": "Basic secret"},  # wrong scheme
        {"Authorization": "Bearer"},  # scheme, no token
        {"Authorization": "Bearer "},  # scheme, empty token
    ):
        resp, ran = await _invoke(mw, headers)
        assert resp.status == 401, headers
        assert not ran, f"handler must not run for {headers!r}"


async def test_wrong_token_rejected_and_handler_never_runs():
    resp, ran = await _invoke(
        make_bearer_auth_middleware("secret"), {"Authorization": "Bearer nope"}
    )
    assert resp.status == 401
    assert not ran


async def test_correct_token_allows_request():
    resp, ran = await _invoke(
        make_bearer_auth_middleware("secret"), {"Authorization": "Bearer secret"}
    )
    assert resp.status == 200
    assert ran


async def test_non_v2_path_bypasses_the_gate():
    # The gate only guards /api/v2/*; a health/other path passes through with
    # no Authorization header.
    _resp, ran = await _invoke(make_bearer_auth_middleware("secret"), {}, path="/health")
    assert ran


def test_token_gate_enforced_end_to_end(stack_with_token):
    jid = "11111111-1111-1111-1111-111111111111"
    unauth, body, _ = stack_with_token.request("GET", f"/api/v2/jobs/{jid}")
    assert unauth == 401, body
    assert body["error"]["code"] == "unauthorized"
    authed, _, _ = stack_with_token.request(
        "GET", f"/api/v2/jobs/{jid}", headers={"Authorization": "Bearer secret"}
    )
    assert authed != 401  # past the gate (404 for the unknown job id is fine)


def test_unauthenticated_by_default_when_no_token_configured(stack):
    # Regression guard for the documented default: with no token set, a normal
    # request with no Authorization header succeeds rather than 401-ing.
    status, _, raw = stack.request("POST", "/api/v2/jobs", {"workflow": {"1": {}}})
    assert status == 201, raw
