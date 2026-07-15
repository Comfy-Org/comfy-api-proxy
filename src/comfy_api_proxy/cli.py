"""Command-line entry point: ``comfy-api-proxy --comfyui URL --port N``.

Binds to 127.0.0.1 by default (the safe default; widening the bind address and
requiring a token are follow-up work).
"""

from __future__ import annotations

import argparse

from aiohttp import web

from .app import make_app


def main() -> None:
    parser = argparse.ArgumentParser(prog="comfy-api-proxy")
    parser.add_argument(
        "--comfyui",
        default="http://127.0.0.1:8188",
        help="Base URL of the self-hosted ComfyUI (default: %(default)s).",
    )
    parser.add_argument(
        "--host", default="127.0.0.1", help="Address to bind (default: %(default)s, local only)."
    )
    parser.add_argument(
        "--port", type=int, default=8189, help="Port to serve the v2 API on (default: %(default)s)."
    )
    args = parser.parse_args()
    web.run_app(make_app(args.comfyui), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
