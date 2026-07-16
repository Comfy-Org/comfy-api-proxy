#!/usr/bin/env python3
"""Regenerate pydantic models from spec/openapi.yaml.

Run this after ``scripts/sync-spec.sh`` whenever the vendored spec
changes. The output is checked in at
``src/comfy_api_proxy/schemas/_generated.py`` — CI's ``spec-drift`` job
re-runs this script into a temp file and diffs it against the checked-in
copy, so a spec sync without a matching model regeneration fails the
build instead of silently drifting.

Why generated models exist at all, and why they are *not* on the request
handling hot path: see src/comfy_api_proxy/schemas/__init__.py.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = REPO_ROOT / "spec" / "openapi.yaml"
OUTPUT_PATH = REPO_ROOT / "src" / "comfy_api_proxy" / "schemas" / "_generated.py"

HEADER = '''\
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

'''


def main() -> int:
    if not SPEC_PATH.exists():
        print(
            f"generate_models: {SPEC_PATH} not found — run scripts/sync-spec.sh first.",
            file=sys.stderr,
        )
        return 1

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "-m",
        "datamodel_code_generator",
        "--input",
        str(SPEC_PATH),
        "--input-file-type",
        "openapi",
        "--output-model-type",
        "pydantic_v2.BaseModel",
        "--target-python-version",
        "3.10",
        "--snake-case-field",
        "--use-schema-description",
        "--use-standard-collections",
        "--field-constraints",
        "--output",
        str(OUTPUT_PATH),
    ]
    print(f"generate_models: {' '.join(cmd)}", file=sys.stderr)
    subprocess.run(cmd, check=True, cwd=REPO_ROOT)

    body = OUTPUT_PATH.read_text()
    # datamodel-code-generator emits its own header docstring/comment; drop
    # everything before the first real import so ours is the only header.
    marker = "from __future__ import annotations"
    idx = body.find(marker)
    if idx != -1:
        body = body[idx:]
    OUTPUT_PATH.write_text(HEADER + body)
    print(f"generate_models: wrote {OUTPUT_PATH}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
