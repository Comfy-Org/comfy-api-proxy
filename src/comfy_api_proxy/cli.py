"""Command-line entry point: ``comfy-api-proxy --comfyui URL --port N``.

Security posture (the defaults are the safety net):

  * Binds to ``127.0.0.1`` only by default. Widening ``--host`` to a
    non-loopback address is refused unless a ``--token`` is set (or
    ``--allow-insecure-bind`` is passed to override), so the proxy is never
    silently exposed to the network unauthenticated.
  * The default-on origin guard (ported from ComfyUI core) is always wired
    in, matching ComfyUI's own default; a static bearer token gates
    ``/api/v2/*`` when configured.
  * ``--enable-cors-header <origin>`` (repeatable) is the opt-in browser
    escape hatch: allowlisted Origins may call the loopback proxy; ``*`` is
    refused.
"""

from __future__ import annotations

import argparse
import ipaddress
import sys

from aiohttp import web

from .app import _DEFAULT_MAX_UPLOAD_BYTES, make_app
from .auth import make_bearer_auth_middleware
from .middleware import (
    attach_cors_prepare,
    make_cors_middleware,
    make_origin_only_middleware,
    normalize_cors_origin,
)


def _is_loopback_host(host: str) -> bool:
    # NB: an empty host is NOT loopback — asyncio/socket treat "" (like None)
    # as "bind all interfaces" (0.0.0.0/::), so `--host ""` would otherwise
    # sail past the "require a token for any non-loopback bind" check below and
    # expose the API unauthenticated on every interface.
    if not host:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="comfy-api-proxy")
    parser.add_argument(
        "--comfyui",
        default="http://127.0.0.1:8188",
        help="Base URL of the self-hosted ComfyUI (default: %(default)s).",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Address to bind (default: %(default)s, local only).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8189,
        help="Port to serve the v2 API on (default: %(default)s).",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="Require 'Authorization: Bearer <token>' on /api/v2/* requests.",
    )
    parser.add_argument(
        "--comfyui-base-dir",
        default=None,
        help="Filesystem root of the co-located ComfyUI install. Required to "
        "enable direct model-directory placement of model-file uploads; "
        "without it, model uploads are rejected (input uploads still work).",
    )
    parser.add_argument(
        "--max-upload-mb",
        type=int,
        default=_DEFAULT_MAX_UPLOAD_BYTES // (1024 * 1024),
        help="Max single-request upload size in MB (default: %(default)s).",
    )
    parser.add_argument(
        "--allow-insecure-bind",
        action="store_true",
        help="Permit binding a non-loopback --host without a --token. Unsafe: "
        "exposes an unauthenticated proxy to the network.",
    )
    parser.add_argument(
        "--enable-cors-header",
        action="append",
        default=[],
        metavar="ORIGIN",
        dest="cors_origins",
        help="Allow a browser Origin to call this proxy (repeatable). Pass an "
        "explicit origin such as https://app.example.com. Unlike ComfyUI "
        "core, '*' is refused — see docs/browser-access.md.",
    )
    args = parser.parse_args(argv)

    if not _is_loopback_host(args.host) and not args.token and not args.allow_insecure_bind:
        print(
            f"refusing to bind non-loopback host {args.host!r} without --token "
            "(pass --allow-insecure-bind to override).",
            file=sys.stderr,
        )
        return 2

    cors_origins: list[str] = []
    for raw in args.cors_origins:
        try:
            cors_origins.append(normalize_cors_origin(raw))
        except ValueError as exc:
            print(f"invalid --enable-cors-header: {exc}", file=sys.stderr)
            return 2

    # Outermost first: CORS (preflight + headers) → origin guard → optional auth.
    middlewares: list = []
    if cors_origins:
        middlewares.append(make_cors_middleware(cors_origins))
    middlewares.append(make_origin_only_middleware(cors_origins))
    if args.token:
        middlewares.append(make_bearer_auth_middleware(args.token))

    app = make_app(
        args.comfyui,
        comfyui_base_dir=args.comfyui_base_dir,
        max_upload_bytes=args.max_upload_mb * 1024 * 1024,
        middlewares=middlewares,
    )
    if cors_origins:
        attach_cors_prepare(app)
    web.run_app(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
