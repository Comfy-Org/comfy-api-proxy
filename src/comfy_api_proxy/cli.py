"""Command-line entry point.

Usage:

  * ``comfy-api-proxy`` / ``comfy-api-proxy run`` — run in the foreground
    (Ctrl+C to stop). A bare invocation with flags is treated as ``run`` for
    backward compatibility.
  * ``comfy-api-proxy start`` — start in the background (detached); prints the
    URL and returns.
  * ``comfy-api-proxy stop`` — stop the background proxy.
  * ``comfy-api-proxy status`` — report whether the background proxy is running.

Security posture (the defaults are the safety net):

  * Binds to ``127.0.0.1`` only by default. Widening ``--host`` to a
    non-loopback address is refused unless a ``--token`` is set (or
    ``--allow-insecure-bind`` is passed to override), so the proxy is never
    silently exposed to the network unauthenticated.
  * The default-on origin guard (ported from ComfyUI core) is always wired
    in, matching ComfyUI's own default; a static bearer token gates
    ``/api/v2/*`` when configured.
"""

from __future__ import annotations

import argparse
import ipaddress
import sys

from aiohttp import web

from . import service
from .app import _DEFAULT_MAX_UPLOAD_BYTES, make_app
from .auth import make_bearer_auth_middleware
from .middleware import origin_only_middleware

_COMMANDS = {"run", "start", "stop", "status"}


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


def _add_server_args(parser: argparse.ArgumentParser) -> None:
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


def _bind_refused(args: argparse.Namespace) -> bool:
    if not _is_loopback_host(args.host) and not args.token and not args.allow_insecure_bind:
        print(
            f"refusing to bind non-loopback host {args.host!r} without --token "
            "(pass --allow-insecure-bind to override).",
            file=sys.stderr,
        )
        return True
    return False


def _run_foreground(args: argparse.Namespace) -> int:
    if _bind_refused(args):
        return 2
    middlewares: list = [origin_only_middleware]
    if args.token:
        middlewares.append(make_bearer_auth_middleware(args.token))
    app = make_app(
        args.comfyui,
        comfyui_base_dir=args.comfyui_base_dir,
        max_upload_bytes=args.max_upload_mb * 1024 * 1024,
        middlewares=middlewares,
    )
    web.run_app(app, host=args.host, port=args.port)
    return 0


def _server_argv(args: argparse.Namespace) -> list[str]:
    """Reconstruct the run flags to hand to a detached child process."""
    argv = [
        "--comfyui",
        args.comfyui,
        "--host",
        args.host,
        "--port",
        str(args.port),
        "--max-upload-mb",
        str(args.max_upload_mb),
    ]
    if args.token:
        argv += ["--token", args.token]
    if args.comfyui_base_dir:
        argv += ["--comfyui-base-dir", args.comfyui_base_dir]
    if args.allow_insecure_bind:
        argv += ["--allow-insecure-bind"]
    return argv


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    # Backward compatibility: a bare invocation (no subcommand, or leading flags)
    # runs in the foreground, same as the original single-command CLI.
    if not raw or (raw[0] not in _COMMANDS and raw[0] not in ("-h", "--help")):
        raw = ["run", *raw]

    parser = argparse.ArgumentParser(prog="comfy-api-proxy")
    sub = parser.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run", help="Run in the foreground (Ctrl+C to stop).")
    _add_server_args(run_p)
    start_p = sub.add_parser("start", help="Start the proxy in the background.")
    _add_server_args(start_p)
    sub.add_parser("stop", help="Stop the background proxy.")
    sub.add_parser("status", help="Show whether the background proxy is running.")

    args = parser.parse_args(raw)

    if args.command == "run":
        return _run_foreground(args)
    if args.command == "start":
        if _bind_refused(args):
            return 2
        return service.start(_server_argv(args), args.host, args.port)
    if args.command == "stop":
        return service.stop()
    if args.command == "status":
        return service.status()
    return 2  # unreachable (subparser is required)


if __name__ == "__main__":
    raise SystemExit(main())
