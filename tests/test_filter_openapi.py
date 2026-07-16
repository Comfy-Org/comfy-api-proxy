"""Unit tests for scripts/filter_openapi.py — the spec-sync filter.

Uses small synthetic fixtures (not the real cloud spec, which this repo
does not have access to at test time) to pin down the two behaviors that
matter most:

  1. An operation tagged ``internal`` or ``x-internal: true`` is removed,
     and so is any component that only existed to support it.
  2. A component that is legitimately "undocumented by direct $ref" —
     because it's referenced only from a vendor extension like
     ``x-sse-events`` — is NOT mistaken for an orphan and must survive.
     (This is the bug the naive "prune anything not $ref'd" approach would
     have: the real spec's ``AssetReference``, ``StatusEvent``,
     ``PreviewEvent``, and ``LogEvent`` schemas are all in this shape.)
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from filter_openapi import filter_spec  # noqa: E402

FIXTURE = """
openapi: 3.0.3
info: {title: t, version: 1.0.0}
paths:
  /public:
    get:
      operationId: getPublic
      tags: [assets]
      responses:
        "200":
          content:
            application/json:
              schema: {$ref: "#/components/schemas/Public"}
  /secret:
    post:
      operationId: postSecret
      tags: [internal]
      responses:
        "200":
          content:
            application/json:
              schema: {$ref: "#/components/schemas/Secret"}
    get:
      operationId: getSecretStatus
      x-internal: true
      responses:
        "200":
          content:
            application/json:
              schema: {$ref: "#/components/schemas/SecretStatus"}
components:
  schemas:
    Public: {type: object}
    Secret: {type: object}
    SecretStatus: {type: object}
    OrphanDocOnly:
      type: object
      description: >-
        Only referenced from a vendor extension, like AssetReference /
        StatusEvent in the real spec. Never internal; must survive.
x-doc-only-refs:
  note: {schema: "#/components/schemas/OrphanDocOnly"}
"""


def test_internal_operation_and_its_path_are_removed():
    out = yaml.safe_load(filter_spec(FIXTURE))
    assert "/secret" not in out["paths"]
    assert "/public" in out["paths"]


def test_components_orphaned_by_stripping_are_removed():
    out = yaml.safe_load(filter_spec(FIXTURE))
    schemas = out["components"]["schemas"]
    assert "Secret" not in schemas
    assert "SecretStatus" not in schemas


def test_vendor_extension_only_schema_survives():
    out = yaml.safe_load(filter_spec(FIXTURE))
    schemas = out["components"]["schemas"]
    assert "OrphanDocOnly" in schemas, (
        "a schema referenced only via a vendor extension (not a real $ref) "
        "must not be mistaken for an orphan and pruned"
    )


def test_directly_referenced_schema_survives():
    out = yaml.safe_load(filter_spec(FIXTURE))
    assert "Public" in out["components"]["schemas"]


def test_internal_ticket_references_are_redacted_from_text():
    fixture_with_ticket = FIXTURE.replace(
        "description: >-",
        "description: >-\n        (BE-1234) internal note, then:",
    )
    out = filter_spec(fixture_with_ticket)
    assert "BE-1234" not in out


def test_no_internal_operations_leaves_everything_untouched():
    fixture = """
openapi: 3.0.3
info: {title: t, version: 1.0.0}
paths:
  /a:
    get:
      operationId: getA
      responses:
        "200":
          content:
            application/json:
              schema: {$ref: "#/components/schemas/A"}
components:
  schemas:
    A: {type: object}
    DocOnly: {type: object}
x-doc-only-refs:
  note: {schema: "#/components/schemas/DocOnly"}
"""
    out = yaml.safe_load(filter_spec(fixture))
    assert set(out["components"]["schemas"]) == {"A", "DocOnly"}
    assert "/a" in out["paths"]
