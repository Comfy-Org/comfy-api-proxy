"""Local proxy exposing the Comfy API v2 in front of a self-hosted ComfyUI.

Demo scope (first iteration slice): submit a workflow, poll job status, and
download outputs. No file upload, no live-progress stream, no idempotency yet —
those follow per docs/sdk/plan.md.
"""

__version__ = "0.0.1"
