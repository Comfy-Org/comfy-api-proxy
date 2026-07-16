"""Typed models generated from the vendored Comfy API v2 spec.

These are used to validate handler *responses* in tests
(``tests/test_schema_conformance.py``), catching drift between what a
handler actually returns and what ``spec/openapi.yaml`` promises.

They are intentionally **not** imported by ``app.py`` or any request
handler: pydantic v2's OpenAPI-generated models are strict about things
like enum membership and required fields, and a false-positive validation
error on the hot path (e.g. because the generator inferred a slightly
tighter type than intended) would turn into a spurious 500 for a real
client request. Response shape is instead built up as plain ``dict``s in
the handlers — closer to the wire, and the thing that actually ships. The
generated models are the independent check that those dicts match the
contract, run in tests, not in production.
"""

from __future__ import annotations

from comfy_api_proxy.schemas._generated import (
    Asset,
    AssetReference,
    ErrorEnvelope,
    Job,
    JobError,
    JobStatus,
    JobUrls,
    LogEvent,
    Output,
    OutputType,
    PreviewEvent,
    Progress,
    StatusEvent,
)

__all__ = [
    "Asset",
    "AssetReference",
    "ErrorEnvelope",
    "Job",
    "JobError",
    "JobStatus",
    "JobUrls",
    "LogEvent",
    "Output",
    "OutputType",
    "PreviewEvent",
    "Progress",
    "StatusEvent",
]
