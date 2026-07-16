#!/usr/bin/env python3
"""Filter the canonical Comfy API v2 OpenAPI spec into the public,
proxy-safe subset that gets vendored at ``spec/openapi.yaml``.

This is the one place the filtering rule lives. It is designed to be run
from two places (see ``scripts/sync-spec.sh`` and
``docs/sync-workflow.md``):

  1. Locally / manually, against a copy of the cloud repo's
     ``api/v2/openapi.yaml``, via ``scripts/sync-spec.sh``.
  2. From a GitHub Actions job living in the *cloud* repo, which checks out
     this repo and runs this exact script against its own spec file before
     opening a pull request here — the same "push, filter, PR" shape as
     cloud's existing ``push-ingest-types-to-frontend.yml`` for the
     frontend's TypeScript types.

What "filtered" means (see spec/README.md for the full rationale):

  * Drop any operation tagged ``internal`` or marked ``x-internal: true``.
    If a path has no operations left afterwards, drop the whole path.
  * Drop any component (schema / parameter / response / header /
    securityScheme) that is no longer reachable from what's left, so a
    schema that only existed to describe an internal operation does not
    linger in the public copy.
  * Replace the file's leading comment block — which is monorepo-internal
    (issue-tracker ticket numbers, internal service names, internal design
    doc references) — with a public-safe one that just explains provenance.
  * Strip lingering internal ticket-style references (e.g. "(BE-1234)")
    out of description text, wherever they were pasted in verbatim from
    the internal source.

A final self-check greps the *output* for the same internal markers and
raises if any survived — the same "second net" pattern the cloud sync
workflow uses to guard the frontend types package. This keeps a future,
differently-shaped internal marker from silently slipping through if the
structural filtering above has a gap.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import yaml

HTTP_METHODS = {
    "get",
    "put",
    "post",
    "delete",
    "options",
    "head",
    "patch",
    "trace",
}

# Regex sweep for internal references accidentally left in description-style
# free text (ticket IDs, internal doc links, internal service/tool names).
# Intentionally broad: false positives just redact a few extra words from a
# comment/description, which is safe; false negatives are the real risk.
_INTERNAL_TEXT_PATTERNS = [
    re.compile(r"\s*\((?:BE|ENG|INFRA|SEC)-\d+[^)]*\)", re.IGNORECASE),
    # A ticket ref trailing inside a parenthetical after other prose, e.g.
    # "(URL shape not final — BE-3120)": drop from the dash onward, so the
    # paren closes cleanly instead of leaving a dangling "— )".
    re.compile(r"\s*[–—-]+\s*(?:BE|ENG|INFRA|SEC)-\d+(?=\))", re.IGNORECASE),
    re.compile(r"\b(?:BE|ENG|INFRA|SEC)-\d+\b", re.IGNORECASE),
    re.compile(r"https?://(?:www\.)?notion\.so/\S+", re.IGNORECASE),
    re.compile(r"\brunpod\b", re.IGNORECASE),
    re.compile(r"\bservices/[a-z0-9_-]+(?:/[a-z0-9_.-]+)*\b", re.IGNORECASE),
    # Internal design-doc references: "(see the Comfy SDK TDD ...)", "in the
    # TDD", or a bare "Comfy SDK TDD" mention. The design doc is internal; the
    # public contract shouldn't point at it.
    re.compile(r"\s*\(see the Comfy SDK TDD[^)]*\)", re.IGNORECASE),
    re.compile(r"\s+in the TDD\b", re.IGNORECASE),
    re.compile(r"\bComfy SDK TDD\b", re.IGNORECASE),
]

# Markers the post-filter leak guard fails on if found anywhere in the
# rendered output (belt-and-suspenders against a structural-filter gap).
_LEAK_GUARD_PATTERNS = [
    re.compile(r"\bx-internal\s*:\s*true\b"),
    re.compile(r"(?:^|\n)\s*-\s*internal\s*(?:\n|$)"),  # a lingering `tags: [..., internal]`
    re.compile(r"\b(?:BE|ENG|INFRA|SEC)-\d+\b", re.IGNORECASE),
    re.compile(r"notion\.so", re.IGNORECASE),
    re.compile(r"\brunpod\b", re.IGNORECASE),
    re.compile(r"\bComfy SDK TDD\b", re.IGNORECASE),
    re.compile(r"\bthe TDD\b", re.IGNORECASE),
]

PUBLIC_HEADER = """\
# =============================================================================
# Comfy API v2 — public contract (synced, filtered copy)
#
# This file is generated. It is NOT hand-edited here: it is a filtered
# mirror of the canonical spec that lives in the Comfy Org "cloud" monorepo,
# pushed into this repo one-way by an automated sync workflow (see
# spec/README.md and docs/sync-workflow.md). The filter strips any
# operation tagged "internal" (or "x-internal: true"), the components only
# reachable from those operations, and any internal-only references that
# were pasted verbatim into descriptions upstream.
#
# Do not hand-edit this file — the next sync overwrites it. If the public
# contract needs to change, change it upstream and let it flow down.
# =============================================================================
"""


def _is_internal_operation(op: Any) -> bool:
    if not isinstance(op, dict):
        return False
    if op.get("x-internal") is True:
        return True
    tags = op.get("tags") or []
    return isinstance(tags, list) and "internal" in tags


def _strip_internal_operations(spec: dict[str, Any]) -> int:
    """Remove internal operations (and now-empty paths) in place.

    Returns the number of operations removed.
    """
    removed = 0
    paths = spec.get("paths") or {}
    for path_key in list(paths.keys()):
        path_item = paths[path_key]
        if not isinstance(path_item, dict):
            continue
        for method in list(path_item.keys()):
            if method not in HTTP_METHODS:
                continue
            if _is_internal_operation(path_item[method]):
                del path_item[method]
                removed += 1
        remaining_methods = [m for m in path_item if m in HTTP_METHODS]
        if not remaining_methods:
            del paths[path_key]
    return removed


def _collect_refs(node: Any, out: set[str]) -> None:
    """Collect every ``#/components/...`` pointer reachable from ``node``.

    Deliberately not limited to values under a literal ``$ref`` key: this
    spec also points at components from vendor extensions using a plain
    string, e.g. ``x-sse-events.status.schema: '#/components/schemas/...'``
    (not a real ``$ref``, since ``x-sse-events`` isn't a place the OpenAPI
    spec allows one). Treating any ``#/components/...``-shaped string as a
    pointer catches those too, so this doesn't misclassify a
    vendor-extension-only schema as unreachable.
    """
    if isinstance(node, dict):
        for value in node.values():
            _collect_refs(value, out)
    elif isinstance(node, list):
        for item in node:
            _collect_refs(item, out)
    elif isinstance(node, str) and node.startswith("#/components/"):
        out.add(node)


def _ref_to_location(ref: str) -> tuple[str, str]:
    # "#/components/schemas/Asset" -> ("schemas", "Asset")
    parts = ref.removeprefix("#/components/").split("/", 1)
    return parts[0], parts[1]


def _reachable_components(spec: dict[str, Any]) -> set[str]:
    """BFS every ``#/components/...`` pointer transitively reachable from
    ``spec['paths']`` (including pointers nested inside other components,
    e.g. ``Job`` -> ``Progress``)."""
    components = spec.get("components") or {}
    reachable: set[str] = set()
    frontier: set[str] = set()
    _collect_refs(spec.get("paths"), frontier)

    while frontier:
        new_frontier: set[str] = set()
        for ref in frontier:
            if ref in reachable:
                continue
            reachable.add(ref)
            group, name = _ref_to_location(ref)
            node = (components.get(group) or {}).get(name)
            _collect_refs(node, new_frontier)
        frontier = new_frontier - reachable
    return reachable


def _prune_components_orphaned_by_stripping(
    spec: dict[str, Any], reachable_before: set[str]
) -> int:
    """Drop only the components that were reachable *before* stripping
    internal operations but are no longer reachable *after* — i.e. ones
    that existed solely to support the internal surface that just got
    removed.

    This deliberately does NOT do a blanket "drop anything not directly
    referenced" sweep: this spec intentionally documents a few
    tooling-only schemas (``AssetReference``, and the SSE payload schemas
    under ``x-sse-events``) that are never the literal body of a request
    or response. Those were already "unreachable by direct $ref" before
    any filtering and must survive it — see spec/README.md and the
    upstream api/v2/README.md ("documented here for tooling; it is not a
    request/response body itself").
    """
    components = spec.get("components") or {}
    if not components:
        return 0

    reachable_after = _reachable_components(spec)
    orphaned = reachable_before - reachable_after

    removed = 0
    for ref in orphaned:
        group_name, entry_name = _ref_to_location(ref)
        group = components.get(group_name)
        if isinstance(group, dict) and entry_name in group:
            del group[entry_name]
            removed += 1
    for group_name in list(components.keys()):
        if not components[group_name]:
            del components[group_name]
    return removed


def _redact_internal_text(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _redact_internal_text(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_redact_internal_text(v) for v in node]
    if isinstance(node, str):
        redacted = node
        for pattern in _INTERNAL_TEXT_PATTERNS:
            redacted = pattern.sub("", redacted)
        return redacted
    return node


def filter_spec(source_text: str) -> str:
    """Filter a canonical OpenAPI YAML document down to the public subset.

    Returns the rendered YAML text (including the public-safe header
    comment), ready to write to ``spec/openapi.yaml``.
    """
    spec = yaml.safe_load(source_text)
    if not isinstance(spec, dict):
        raise ValueError("Source OpenAPI document did not parse to a mapping.")

    reachable_before = _reachable_components(spec)
    ops_removed = _strip_internal_operations(spec)
    components_removed = _prune_components_orphaned_by_stripping(spec, reachable_before)
    spec = _redact_internal_text(spec)

    body = yaml.safe_dump(spec, sort_keys=False, allow_unicode=True, width=88)

    # Leak-check only the filtered *data* (the YAML body), not our own
    # PUBLIC_HEADER prose above — the header legitimately explains what
    # "x-internal: true" and ticket-style references mean, which would
    # otherwise trip its own guard.
    _leak_check(body)

    print(
        f"filter_openapi: removed {ops_removed} internal operation(s), "
        f"{components_removed} unreachable component(s).",
        file=sys.stderr,
    )
    return PUBLIC_HEADER + body


def _leak_check(rendered: str) -> None:
    leaks = [p.pattern for p in _LEAK_GUARD_PATTERNS if p.search(rendered)]
    if leaks:
        raise SystemExit(
            "filter_openapi: internal marker(s) survived filtering — refusing to "
            f"write a leaking spec. Patterns matched: {leaks}"
        )


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(
            f"usage: {argv[0]} <source-openapi.yaml> <output-openapi.yaml>",
            file=sys.stderr,
        )
        return 2
    source_path, output_path = Path(argv[1]), Path(argv[2])
    rendered = filter_spec(source_path.read_text())
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(rendered)
    print(f"filter_openapi: wrote {output_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
