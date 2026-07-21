"""Local proxy exposing the Comfy API v2 in front of a self-hosted ComfyUI.

Demo scope (first iteration slice): submit a workflow, poll job status, and
download outputs. No file upload, no live-progress stream, no idempotency yet —
those follow in later iterations.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    # Single source of truth: the installed package metadata (which the
    # tag-driven release injects into pyproject.toml at build time). Avoids a
    # hardcoded value drifting from the published version.
    __version__ = _pkg_version("comfy-api-proxy")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.0.0+unknown"
