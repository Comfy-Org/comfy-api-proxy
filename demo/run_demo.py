"""End-to-end demo: the SAME script runs a workflow against a Comfy API v2
surface — here the local proxy — and downloads the result.

Usage:
    python demo/run_demo.py                 # against the local proxy (default)
    python demo/run_demo.py --base URL --key KEY   # against any v2 surface

Only --base (and --key for cloud) change between surfaces; nothing else does.
That is the whole point of the one-contract design.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make the demo self-contained: import the SDK from the sibling repo checkout.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ComfyPythonSDK"))
from comfy_sdk import Comfy  # noqa: E402

# A minimal API-format workflow. The fake ComfyUI ignores the content and
# returns one image; a real ComfyUI would execute these nodes.
WORKFLOW = {
    "3": {"class_type": "KSampler", "inputs": {"seed": 42, "steps": 1}},
    "9": {"class_type": "SaveImage", "inputs": {"images": ["3", 0]}},
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8189")
    ap.add_argument("--key", default=None)
    ap.add_argument("--out", default="demo_output.png")
    args = ap.parse_args()

    client = Comfy(args.base, api_key=args.key)
    print(f"→ running a workflow against {args.base}")
    wf = client.workflows.from_json(WORKFLOW)
    job = client.run(wf)
    print(f"  job {job.id} → {job.status}, {len(job.outputs)} output(s)")
    for out in job.outputs:
        path = out.to_file(args.out)
        size = Path(path).stat().st_size
        print(f"  downloaded {out.type} '{out.name}' → {path} ({size} bytes)")
    print("✓ done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
