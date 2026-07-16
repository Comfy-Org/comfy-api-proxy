"""Default-on request-origin guard, ported from ComfyUI core's
``create_origin_only_middleware`` (``server.py``, verified against that
file directly).

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
"""

from __future__ import annotations

import ipaddress
import socket
import urllib.parse

from aiohttp import web
from aiohttp.typedefs import Handler


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


@web.middleware
async def origin_only_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
    if request.headers.get("Sec-Fetch-Site") == "cross-site":
        return _forbidden("Cross-site request blocked (Sec-Fetch-Site: cross-site).")

    host_header = request.headers.get("Host")
    origin_header = request.headers.get("Origin")
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
