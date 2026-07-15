"""Local proxy exposing the Comfy API v2 in front of a self-hosted ComfyUI.

Demo scope (first iteration slice): submit a workflow, poll job status, and
download outputs. No file upload, no live-progress stream, no idempotency yet —
those follow in later iterations.
"""

__version__ = "0.0.1"
