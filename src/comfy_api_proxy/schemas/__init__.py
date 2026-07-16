"""Typed models generated from the vendored Comfy API v2 spec.

These exist as the **codegen artifact** guarded by CI's spec-drift check:
``scripts/generate_models.py`` regenerates this package from
``spec/openapi.yaml`` and the build fails if the checked-in copy differs,
so the models can never silently drift from the contract.

They are intentionally **not** imported by ``app.py`` or any request
handler — response shape is built as plain ``dict``s in the handlers,
closer to the wire and immune to a generator quirk turning into a spurious
500. They are also **not** the mechanism that validates responses in tests:
the `datamodel-code-generator` version pinned here does not model OpenAPI
3.0 ``nullable: true`` (it emits non-optional fields), so it would reject a
response that legitimately carries a ``null`` where the spec allows one.
Response conformance is therefore checked in
``tests/test_schema_conformance.py`` against ``spec/openapi.yaml`` directly
via ``jsonschema`` (with ``nullable`` converted first). These typed models
remain useful for editor/type hints and as the drift-guarded proof that the
spec still generates cleanly.
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
