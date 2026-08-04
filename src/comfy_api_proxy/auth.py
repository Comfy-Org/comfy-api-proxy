"""Optional static bearer-token auth.

Per the canonical spec's ``bearerAuth`` security scheme: "Self-hosted
accepts unauthenticated requests by default and can be configured with a
static bearer token." This module is that opt-in gate — a single shared
secret, not a user/account system, because a self-hosted single-user
ComfyUI has no accounts to check it against.

The proxy's actual safety net is the default bind to ``127.0.0.1`` (see
``cli.py``): a token only starts to matter once someone deliberately widens
the bind address, which is exactly when ``cli.py`` requires one to be set.
"""

from __future__ import annotations

import hmac

from aiohttp import web
from aiohttp.typedefs import Handler, Middleware


def _forbidden(code: str, message: str) -> web.Response:
    return web.json_response(
        {"error": {"code": code, "message": message, "details": None}},
        status=401,
    )


def make_bearer_auth_middleware(token: str) -> Middleware:
    """Build a middleware requiring ``Authorization: Bearer <token>`` on
    every ``/api/v2/*`` request, constant-time compared against ``token``.
    """

    @web.middleware
    async def bearer_auth_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
        if not request.path.startswith("/api/v2/"):
            return await handler(request)
        # Browser CORS preflight never carries Authorization; allow OPTIONS
        # through so an allowlisted origin can negotiate before the real call.
        if request.method == "OPTIONS":
            return await handler(request)
        header = request.headers.get("Authorization", "")
        scheme, _, presented = header.partition(" ")
        if scheme.lower() != "bearer" or not presented:
            return _forbidden("unauthorized", "Missing or malformed Authorization header.")
        if not hmac.compare_digest(presented, token):
            return _forbidden("unauthorized", "Invalid token.")
        return await handler(request)

    return bearer_auth_middleware
