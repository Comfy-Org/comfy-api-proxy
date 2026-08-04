"""Request-origin guard and opt-in CORS for browser clients.

Default-on origin guard
-----------------------
Ported from ComfyUI core's ``create_origin_only_middleware`` (``server.py``,
verified against that file directly).

ComfyUI's own server applies this by default (it can be turned off with
``--enable-cors-header``) because a local, unauthenticated-by-default
server is a DNS-rebinding / drive-by-CSRF target: any web page a user has
open in their browser can otherwise script requests straight at
``127.0.0.1:8188`` (browsers don't block that). This proxy is exactly the
same shape of target, so it carries the same default-on guard rather than
relying on the operator to remember to add one.

Ported algorithm (matches ComfyUI's two checks, in the same order):

1. If the request carries ``Sec-Fetch-Site: cross-site``, reject
   immediately — this header is set by the browser itself and can't be
   spoofed by page script, so it's the strongest signal when present.
2. Otherwise, if both ``Host`` and ``Origin`` headers are present AND the
   ``Host`` header names a loopback address (127.0.0.1/::1/localhost) —
   deliberately scoped to loopback only, matching upstream's comment that
   this keeps the check from misfiring on a real, non-loopback deployment
   fronted by a reverse proxy that legitimately rewrites Host — reject if
   the Origin's hostname doesn't match the Host's hostname (port ignored
   whenever either side omits one, so `Origin: https://localhost` still
   matches `Host: localhost:8189`).

A request with **no** ``Origin`` header at all (curl, the SDK, server-to-
server calls) is let through unchanged by check 2 — browsers always send
``Origin`` for the cross-site requests this guard exists to stop, so
requiring one would only break legitimate non-browser clients.

Opt-in CORS allowlist
---------------------
``--enable-cors-header <origin>`` (repeatable) is the escape hatch for a
hosted web app that calls a user's local proxy. Unlike ComfyUI core, this
proxy **refuses** ``*`` — only explicit origins are accepted — so
authenticated / credentialed browser requests never get an unrestricted
``Access-Control-Allow-Origin``. An allowlisted Origin bypasses the
origin-only checks above and receives CORS response headers (including on
preflight ``OPTIONS`` and on ``GET /api/v2/health``).
"""

from __future__ import annotations

import ipaddress
import socket
import urllib.parse
from collections.abc import Collection

from aiohttp import web
from aiohttp.typedefs import Handler, Middleware

# Stashed on the request by CORS middleware; read by on_response_prepare.
_CORS_ORIGIN_KEY = "cors_allowed_origin"

CORS_ALLOW_METHODS = "GET, HEAD, POST, PUT, PATCH, DELETE, OPTIONS"
CORS_ALLOW_HEADERS = "Authorization, Content-Type, Idempotency-Key"
# Response headers a browser client may need to read after a cross-origin call.
CORS_EXPOSE_HEADERS = "Retry-After, Content-Range, Accept-Ranges"
CORS_MAX_AGE = "600"


def _forbidden(message: str) -> web.Response:
    # Local import avoided on purpose: this tiny error envelope is
    # duplicated from app.py's `_error` helper rather than imported, since
    # importing app.py here would create app.py -> middleware -> app.py
    # circular import (app.py wires this middleware into the Application).
    return web.json_response(
        {"error": {"code": "forbidden_origin", "message": message, "details": None}},
        status=403,
    )


def _is_loopback(host: str | None) -> bool:
    if not host:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        pass
    # Not a literal IP — resolve it and check every returned address.
    # Mirrors upstream: if ALL resolved addresses are loopback, treat the
    # hostname (e.g. "localhost") as loopback too.
    loopback = False
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            for _family, _type, _proto, _canon, sockaddr in socket.getaddrinfo(
                host, None, family, socket.SOCK_STREAM
            ):
                if not ipaddress.ip_address(sockaddr[0]).is_loopback:
                    return loopback
                loopback = True
        except socket.gaierror:
            pass
    return loopback


def normalize_cors_origin(value: str) -> str:
    """Validate and normalize a single ``--enable-cors-header`` value.

    Accepts ``http(s)://host[:port]`` only. Trailing slashes are stripped.
    Userinfo and malformed/empty ports are rejected. Default ports (80 for
    http, 443 for https) are omitted so allowlist matching matches browser
    ``Origin`` headers. ``*`` is refused — authenticated browser traffic
    must never get an unrestricted wildcard ACAO.
    """
    raw = value.strip()
    if not raw:
        raise ValueError("CORS origin must be a non-empty http(s) URL.")
    if raw == "*":
        raise ValueError(
            "CORS origin '*' is not allowed; pass explicit origins "
            "(e.g. https://app.example.com). Unrestricted wildcards are "
            "unsafe for authenticated or credentialed browser requests."
        )
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"CORS origin must be an absolute http(s) URL with a host, got {value!r}.")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise ValueError(f"CORS origin must not include userinfo, got {value!r}.")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError(f"CORS origin must be an absolute http(s) URL with a host, got {value!r}.")
    # Empty port (``http://host:``) leaves ``port`` as None but keeps the colon.
    authority = parsed.netloc.rsplit("@", 1)[-1]
    if authority.endswith(":"):
        raise ValueError(f"CORS origin has an empty port, got {value!r}.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"CORS origin has an invalid port, got {value!r}.") from exc
    if parsed.path not in ("", "/") or parsed.params or parsed.query or parsed.fragment:
        raise ValueError(f"CORS origin must not include a path, query, or fragment, got {value!r}.")
    # Reconstruct from validated host/port (not raw netloc) for a stable allowlist key.
    host = f"[{hostname}]" if ":" in hostname else hostname
    if (
        port is None
        or (parsed.scheme == "http" and port == 80)
        or (parsed.scheme == "https" and port == 443)
    ):
        return f"{parsed.scheme}://{host}"
    return f"{parsed.scheme}://{host}:{port}"


def apply_cors_headers(response: web.StreamResponse, origin: str) -> None:
    """Attach CORS headers for an exact allowlisted origin (never ``*``)."""
    response.headers["Access-Control-Allow-Origin"] = origin
    existing_vary = response.headers.get("Vary")
    if existing_vary is None:
        response.headers["Vary"] = "Origin"
    elif "Origin" not in {part.strip() for part in existing_vary.split(",")}:
        response.headers["Vary"] = f"{existing_vary}, Origin"
    response.headers["Access-Control-Allow-Methods"] = CORS_ALLOW_METHODS
    response.headers["Access-Control-Allow-Headers"] = CORS_ALLOW_HEADERS
    response.headers["Access-Control-Expose-Headers"] = CORS_EXPOSE_HEADERS
    response.headers["Access-Control-Allow-Credentials"] = "true"
    response.headers["Access-Control-Max-Age"] = CORS_MAX_AGE


def make_cors_middleware(allowed_origins: Collection[str]) -> Middleware:
    """Emit CORS headers for allowlisted Origins; answer OPTIONS preflight.

    Non-allowlisted Origins are untouched (and still face the origin-only
    guard). Register :func:`attach_cors_prepare` on the app so streaming
    responses (SSE) also get headers before they are prepared.
    """
    allowed = frozenset(normalize_cors_origin(o) for o in allowed_origins)
    if not allowed:
        raise ValueError("make_cors_middleware requires at least one origin.")

    @web.middleware
    async def cors_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
        origin = request.headers.get("Origin")
        if origin and origin in allowed:
            request[_CORS_ORIGIN_KEY] = origin
            if request.method == "OPTIONS":
                # Preflight — no body, no auth. Headers land via on_response_prepare.
                return web.Response(status=204)
        return await handler(request)

    return cors_middleware


def attach_cors_prepare(app: web.Application) -> None:
    """Apply stashed CORS headers before any response (including streams) is sent."""

    async def _on_prepare(request: web.Request, response: web.StreamResponse) -> None:
        origin = request.get(_CORS_ORIGIN_KEY)
        if origin:
            apply_cors_headers(response, origin)

    app.on_response_prepare.append(_on_prepare)


def make_origin_only_middleware(allowed_origins: Collection[str] = ()) -> Middleware:
    """Build the default-on origin guard, with optional allowlist bypass.

    Origins in ``allowed_origins`` skip both the ``Sec-Fetch-Site`` and the
    Host/Origin mismatch checks so an operator-configured hosted app can
    reach a loopback proxy. Everything else keeps the ComfyUI-core behaviour.
    """
    allowed = frozenset(normalize_cors_origin(o) for o in allowed_origins)

    @web.middleware
    async def origin_only_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
        origin_header = request.headers.get("Origin")
        if origin_header and origin_header in allowed:
            if request.method == "OPTIONS":
                return web.Response(status=204)
            return await handler(request)

        if request.headers.get("Sec-Fetch-Site") == "cross-site":
            return _forbidden("Cross-site request blocked (Sec-Fetch-Site: cross-site).")

        host_header = request.headers.get("Host")
        if host_header and origin_header:
            parsed_origin = urllib.parse.urlparse(origin_header)
            origin_domain = parsed_origin.netloc.lower()
            host_domain = host_header.lower()
            host_domain_parsed = urllib.parse.urlsplit("//" + host_domain)

            # Scoped to loopback hosts only (matches upstream): a real,
            # non-loopback deployment may sit behind a reverse proxy that
            # legitimately rewrites Host, which this check must not break.
            if _is_loopback(host_domain_parsed.hostname):
                if parsed_origin.port is None:
                    host_domain = host_domain_parsed.hostname or host_domain
                if host_domain_parsed.port is None:
                    origin_domain = parsed_origin.hostname or origin_domain

                if host_domain and origin_domain and host_domain != origin_domain:
                    return _forbidden(
                        f"Origin '{origin_header}' does not match request host '{host_header}'."
                    )

        if request.method == "OPTIONS":
            return web.Response()
        return await handler(request)

    return origin_only_middleware


# Default instance: no allowlist (preserves import sites / tests that use the
# module-level name directly).
origin_only_middleware = make_origin_only_middleware()
