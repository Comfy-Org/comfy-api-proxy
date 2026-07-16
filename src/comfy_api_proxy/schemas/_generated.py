"""Generated from spec/openapi.yaml by scripts/generate_models.py.

DO NOT EDIT BY HAND. Re-run `python3 scripts/generate_models.py` after any
change to spec/openapi.yaml. CI's spec-drift check fails the build if this
file and a fresh regeneration disagree.

These models are for validating handler responses in tests (see
tests/test_schema_conformance.py) — deliberately NOT imported on the
request-handling hot path, so a generator quirk or a v2 schema tightening
can never turn into a runtime 500 for a real request.
"""

# ruff: noqa
# fmt: off

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import AnyUrl, AwareDatetime, BaseModel, Field


class Asset(BaseModel):
    """
    A user-owned record identified by a server-assigned UUID, backing an immutable blob whose content carries a server-computed blake3 hash. `hash` may be computed lazily: an asset record (and its retrievable bytes) can exist before its hash is filled in.
    """

    id: str = Field(..., examples=['asset_01JZV8Q3M7K2W9X0Y1Z2A3B4C5'])
    hash: str = Field(
        ...,
        description='`blake3:<hex>`; null while lazily computed.',
        examples=['blake3:9f8a1c0d...'],
    )
    size_bytes: int = Field(..., examples=[4816293])
    content_type: str = Field(..., examples=['image/png'])
    file_path: str | None = Field(None, examples=['photo.png'])
    created_new: bool | None = Field(
        None,
        description='On create responses: distinguishes a brand-new blob (true) from a dedup hit against bytes the platform already had (false).',
    )
    created_at: AwareDatetime
    url: AnyUrl = Field(
        ..., description='Short-lived content URL (signed, or proxy-served).'
    )
    url_expires_at: AwareDatetime


class JobStatus(Enum):
    """
    Lifecycle: queued → running → succeeded | failed | expired;
    a cancel request moves running → canceling → canceled.
    Terminal states: succeeded, canceled, failed, expired.

    """

    queued = 'queued'
    running = 'running'
    succeeded = 'succeeded'
    canceling = 'canceling'
    canceled = 'canceled'
    failed = 'failed'
    expired = 'expired'


class JobUrls(BaseModel):
    """
    Embedded follow-up links — follow these, don't build URLs.
    """

    self: str
    events: str
    cancel: str


class Progress(BaseModel):
    """
    Server-computed progress snapshot (node-count and sampler-step weighted). Complete per snapshot — one fully re-syncs a client.
    """

    value: float = Field(
        ...,
        description='Overall fraction, server-computed.',
        examples=[0.42],
        ge=0.0,
        le=1.0,
    )
    nodes_done: int = Field(..., examples=[11])
    nodes_total: int = Field(..., examples=[31])
    current_node: str | None = Field(None, examples=['12'])
    current_node_class: str | None = Field(None, examples=['KSampler'])
    step: int | None = Field(None, examples=[21])
    steps: int | None = Field(None, examples=[50])
    message: str | None = Field(None, examples=['KSampler 21/50'])


class OutputType(Enum):
    """
    Normalized output kind — nothing silently dropped.
    """

    image = 'image'
    video = 'video'
    audio = 'audio'
    text = 'text'
    file = 'file'
    latent = 'latent'


class JobError(BaseModel):
    """
    Execution failure detail, carried in `job.error` (not an HTTP error).
    """

    code: str = Field(..., examples=['node_execution_error'])
    message: str
    node_id: str | None = None
    class_type: str | None = None
    traceback: str | None = None


class Error(BaseModel):
    code: str = Field(..., examples=['invalid_workflow'])
    message: str = Field(
        ..., examples=["Node 12 (KSampler): required input 'model' is not connected"]
    )
    details: dict[str, Any] | None = Field(
        None,
        examples=[
            {'node_errors': {'12': [{'field': 'model', 'reason': 'missing_input'}]}}
        ],
    )


class ErrorEnvelope(BaseModel):
    """
    Shared error envelope with machine-readable codes. Core codes (v1):
    `invalid_workflow` (422), `workflow_format_ui` (422),
    `missing_asset` (422), `hash_mismatch` (409), `blob_not_found`
    (404), `idempotency_key_reuse` (422), `idempotency_conflict` (409),
    `queue_full` (429 + Retry-After), `insufficient_credits` (402),
    `not_found` (404), `unauthorized` (401), `forbidden` (403).

    """

    error: Error


class StatusEvent(BaseModel):
    """
    SSE `status` event payload.
    """

    status: JobStatus
    queue_position: int | None = None


class PreviewEvent(BaseModel):
    """
    SSE `preview` event payload (JPEG, base64, throttled).
    """

    node_id: str
    content_type: str = Field(..., examples=['image/jpeg'])
    data_base64: str


class LogEvent(BaseModel):
    """
    SSE `log` event payload. Best-effort diagnostics.
    """

    level: str = Field(..., examples=['info'])
    message: str


class FieldType(Enum):
    core_asset = 'core/ASSET'


class Info(BaseModel):
    id: str = Field(..., examples=['asset_01JZV8Q3M7K2W9X0Y1Z2A3B4C5'])
    hash: str | None = Field(None, examples=['blake3:9f8a1c0d...'])
    file_path: str | None = Field(None, examples=['photo.png'])


class AssetReference(BaseModel):
    """
    The typed asset-reference object placed inside workflow JSON where a
    filename would normally go (documented here for tooling; it is not a
    request/response body itself):

        {"__type": "core/ASSET",
         "info": {"id": "asset_...", "hash": "blake3:...",
                  "file_path": "photo.png"}}

    `info.id` (the asset UUID) is required in v1 and authoritative;
    `hash` and `file_path` are optional staging/lookup hints and never
    override a present `id`. A malformed reference or one that is not
    resolvable/ready/owned by the caller fails submission with 422
    `missing_asset`.

    """

    field__type: FieldType = Field(..., alias='__type')
    info: Info


class Output(BaseModel):
    """
    A committed job output. Outputs are assets: `id` is the asset UUID, retrievable via GET /api/v2/assets/{id} for as long as the job is retained. `hash` is lazily computed and may be null on the retrieval hot path.
    """

    node_id: str = Field(..., examples=['9'])
    name: str = Field(..., examples=['ComfyUI_00001_.png'])
    type: OutputType
    content_type: str = Field(..., examples=['image/png'])
    size_bytes: int = Field(..., examples=[1848320])
    id: str = Field(..., description='Asset UUID.', examples=['asset_01JZV9R4N8...'])
    hash: str = Field(..., description='`blake3:<hex>`; null until lazily computed.')
    url: AnyUrl
    url_expires_at: AwareDatetime


class Job(BaseModel):
    """
    One execution of a workflow. Durable from creation until `expires_at`; `outputs` populates incrementally during execution.
    """

    id: str = Field(..., examples=['job_01JZTGXW9Q2M4R8V0B1N3P5D7F'])
    status: JobStatus
    created_at: AwareDatetime
    started_at: AwareDatetime
    completed_at: AwareDatetime
    expires_at: AwareDatetime = Field(
        ...,
        description='Retention deadline — a platform property, not an API constant.',
    )
    queue_position: int
    progress: Progress = Field(
        ...,
        description='The latest progress snapshot; same data the SSE stream pushes.',
    )
    outputs: list[Output]
    error: JobError
    metrics: dict[str, int] | None = Field(
        None, examples=[{'queue_ms': 9000, 'execution_ms': None}]
    )
    urls: JobUrls
