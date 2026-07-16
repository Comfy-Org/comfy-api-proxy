"""Validate real handler responses against the vendored OpenAPI contract.

The generated pydantic models (src/comfy_api_proxy/schemas/_generated.py) are
the typed codegen artifact guarded by CI's spec-drift check — but this
generator version does not model OpenAPI 3.0 ``nullable: true`` (it emits
non-optional fields), so it cannot validate a response that legitimately
carries a ``null`` where the spec allows one. This test therefore validates
against ``spec/openapi.yaml`` directly with ``jsonschema``, converting the
OpenAPI-3.0 ``nullable`` keyword into a JSON-Schema-compatible nullable type
first. This runs in tests only — never on the request hot path.
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any

import jsonschema
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SPEC = yaml.safe_load((REPO_ROOT / "spec" / "openapi.yaml").read_text())

_DROP_KEYS = {"example", "examples", "discriminator", "xml", "externalDocs", "nullable"}


def _convert(node: Any) -> Any:
    """Recursively convert an OpenAPI 3.0 schema to a JSON-Schema-compatible
    one: honor ``nullable: true`` and drop OpenAPI-only annotation keys."""
    if isinstance(node, list):
        return [_convert(v) for v in node]
    if not isinstance(node, dict):
        return node

    nullable = node.get("nullable") is True
    converted = {k: _convert(v) for k, v in node.items() if k not in _DROP_KEYS}

    if nullable:
        if "type" in converted and isinstance(converted["type"], str):
            converted["type"] = [converted["type"], "null"]
        else:
            # $ref / allOf / oneOf / anyOf: wrap to also permit null.
            converted = {"anyOf": [converted, {"type": "null"}]}
    return converted


# Convert the whole document once so `$ref: '#/components/schemas/X'` pointers
# stay resolvable against the converted tree.
CONVERTED_SPEC = _convert(SPEC)


def _validate(instance: Any, schema_name: str) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # RefResolver deprecation
        resolver = jsonschema.validators.RefResolver.from_schema(CONVERTED_SPEC)
        validator = jsonschema.Draft202012Validator(
            {"$ref": f"#/components/schemas/{schema_name}"}, resolver=resolver
        )
        validator.validate(instance)


_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
    "53de0000000c49444154789c6360f8cf00000301010018dd8db10000000049454e44ae426082"
)
_TERMINAL = {"succeeded", "failed", "expired", "canceled"}


def test_asset_response_conforms(stack):
    status, asset, raw = stack.upload("cat.png", _PNG, "image/png")
    assert status == 201, raw
    _validate(asset, "Asset")


def test_job_response_conforms_queued_and_terminal(stack):
    _, asset, _ = stack.upload("cat.png", _PNG, "image/png")
    workflow = {
        "1": {
            "class_type": "LoadImage",
            "inputs": {"image": {"__type": "core/ASSET", "info": {"id": asset["id"]}}},
        },
        "9": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}},
    }
    status, job, raw = stack.request("POST", "/api/v2/jobs", {"workflow": workflow})
    assert status == 201, raw
    _validate(job, "Job")  # freshly-queued job (nulls where allowed)

    import time

    deadline = time.monotonic() + 15
    while job["status"] not in _TERMINAL:
        assert time.monotonic() < deadline
        time.sleep(0.1)
        _, job, _ = stack.request("GET", job["urls"]["self"])
    _validate(job, "Job")  # terminal job with outputs
    for output in job["outputs"]:
        _validate(output, "Output")


def test_error_envelope_conforms(stack):
    status, body, _ = stack.request("GET", "/api/v2/jobs/job_ghost")
    assert status == 404
    _validate(body, "ErrorEnvelope")


def test_converter_handles_nullable_scalar_and_ref():
    # Guard the converter itself: a nullable scalar accepts null, and a
    # nullable $ref/allOf wraps into anyOf-null.
    src = {
        "type": "object",
        "properties": {
            "a": {"type": "string", "nullable": True},
            "b": {"allOf": [{"type": "object"}], "nullable": True},
        },
    }
    out = _convert(src)
    assert out["properties"]["a"]["type"] == ["string", "null"]
    assert {"type": "null"} in out["properties"]["b"]["anyOf"]
    jsonschema.Draft202012Validator(out).validate({"a": None, "b": None})
