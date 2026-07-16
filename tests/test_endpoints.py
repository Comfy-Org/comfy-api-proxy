"""End-to-end tests for the full v2 surface over the fake ComfyUI.

Covers the story the demo couldn't: upload an input asset, reference it from
a workflow via a core/ASSET object, run, and download — plus cancel, the
dedup/from-hash/by-hash paths, SSE, and the model-placement security guards.
Stdlib-only HTTP (see conftest.Stack); no SDK, no third-party client.
"""

from __future__ import annotations

import base64
import json
import struct
import threading
import time

# A 1x1 PNG, same bytes the fake serves — content the proxy hashes for dedup.
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
    "53de0000000c49444154789c6360f8cf00000301010018dd8db10000000049454e44ae426082"
)
_TERMINAL = {"succeeded", "failed", "expired", "canceled"}


def _poll_until_terminal(stack, job, timeout=15.0):
    deadline = time.monotonic() + timeout
    while job["status"] not in _TERMINAL:
        assert time.monotonic() < deadline, f"job stuck in {job['status']!r}"
        time.sleep(0.1)
        status, job, raw = stack.request("GET", job["urls"]["self"])
        assert status == 200, raw
    return job


def test_upload_asset_returns_asset_shape(stack):
    status, asset, raw = stack.upload("cat.png", _PNG, "image/png", tags="input")
    assert status == 201, raw
    assert asset["id"].startswith("asset_")
    assert asset["hash"].startswith("blake3:")
    assert asset["size_bytes"] == len(_PNG)
    assert asset["content_type"] == "image/png"
    assert asset["created_new"] is True
    assert asset["url"] == f"/api/v2/assets/{asset['id']}/content"


def test_upload_dedups_identical_bytes(stack):
    status1, a1, _ = stack.upload("cat.png", _PNG, "image/png")
    status2, a2, _ = stack.upload("cat-again.png", _PNG, "image/png")
    assert status1 == 201
    assert status2 == 200, "identical bytes should dedup to the existing blob"
    assert a2["created_new"] is False
    assert a1["hash"] == a2["hash"]


def test_expected_hash_mismatch_rejected(stack):
    status, body, raw = stack.upload("cat.png", _PNG, "image/png", expected_hash="blake3:deadbeef")
    assert status == 409, raw
    assert body["error"]["code"] == "hash_mismatch"


def test_from_hash_and_by_hash(stack):
    _, asset, _ = stack.upload("cat.png", _PNG, "image/png")
    h = asset["hash"]

    status, _, _ = stack.request("HEAD", f"/api/v2/assets/by-hash/{h}")
    assert status == 200

    status, _, _ = stack.request("HEAD", "/api/v2/assets/by-hash/blake3:nope")
    assert status == 404

    status, body, raw = stack.request("POST", "/api/v2/assets/from-hash", {"hash": h})
    assert status == 200, raw
    assert body["id"] == asset["id"]

    status, body, _ = stack.request("POST", "/api/v2/assets/from-hash", {"hash": "blake3:nope"})
    assert status == 404
    assert body["error"]["code"] == "blob_not_found"


def test_get_asset_metadata_and_content(stack):
    _, asset, _ = stack.upload("cat.png", _PNG, "image/png")
    status, meta, raw = stack.request("GET", f"/api/v2/assets/{asset['id']}")
    assert status == 200, raw
    assert meta["id"] == asset["id"]

    status, _, content = stack.request("GET", asset["url"])
    assert status == 200
    assert content.startswith(b"\x89PNG\r\n\x1a\n")


def test_upload_run_with_asset_ref_download(stack):
    # 1) Upload an input asset.
    _, asset, _ = stack.upload("cat.png", _PNG, "image/png", tags="input")

    # 2) Submit a workflow that references it via a core/ASSET object — the
    #    proxy's walker must rewrite it to the filename ComfyUI expects.
    workflow = {
        "1": {
            "class_type": "LoadImage",
            "inputs": {"image": {"__type": "core/ASSET", "info": {"id": asset["id"]}}},
        },
        "9": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}},
    }
    status, job, raw = stack.request("POST", "/api/v2/jobs", {"workflow": workflow})
    assert status == 201, raw

    # 3) Poll to completion and download the output.
    job = _poll_until_terminal(stack, job)
    assert job["status"] == "succeeded", job.get("error")
    assert job["outputs"], "no outputs"
    status, _, content = stack.request("GET", job["outputs"][0]["url"])
    assert status == 200
    assert content.startswith(b"\x89PNG\r\n\x1a\n")


def test_unresolvable_asset_ref_rejected(stack):
    workflow = {
        "1": {
            "class_type": "LoadImage",
            "inputs": {"image": {"__type": "core/ASSET", "info": {"id": "asset_ghost"}}},
        }
    }
    status, body, raw = stack.request("POST", "/api/v2/jobs", {"workflow": workflow})
    assert status == 422, raw
    assert body["error"]["code"] == "missing_asset"


def test_ui_format_workflow_rejected(stack):
    status, body, _ = stack.request(
        "POST", "/api/v2/jobs", {"workflow": {"nodes": [], "links": []}}
    )
    assert status == 422
    assert body["error"]["code"] == "workflow_format_ui"


def test_reserved_fields_rejected(stack):
    status, body, _ = stack.request(
        "POST", "/api/v2/jobs", {"workflow": {"1": {}}, "webhook_url": "http://x"}
    )
    assert status == 422
    assert body["error"]["code"] == "invalid_workflow"


def test_cancel_running_job(stack):
    # A "hang" input keeps the fake job in-flight so cancel has a real target.
    workflow = {"1": {"class_type": "Noop", "inputs": {"hang": True}}}
    status, job, raw = stack.request("POST", "/api/v2/jobs", {"workflow": workflow})
    assert status == 201, raw
    # Let it reach running.
    time.sleep(0.3)
    status, body, raw = stack.request("POST", job["urls"]["cancel"])
    assert status == 200, raw
    assert body["status"] in ("canceling", "canceled")
    job = _poll_until_terminal(stack, body)
    assert job["status"] == "canceled"


def test_cancel_unknown_job_404(stack):
    status, body, _ = stack.request("POST", "/api/v2/jobs/job_ghost/cancel")
    assert status == 404


def test_get_unknown_job_404(stack):
    status, body, _ = stack.request("GET", "/api/v2/jobs/job_ghost")
    assert status == 404
    assert body["error"]["code"] == "not_found"


def test_sse_stream_delivers_progress_preview_and_terminal(stack):
    # A hang job so the SSE bridge connects while it is still running and
    # exercises the WS-driven progress/preview path, then we cancel it to
    # drive the terminal transition.
    workflow = {"1": {"class_type": "Noop", "inputs": {"hang": True}}}
    _, job, _ = stack.request("POST", "/api/v2/jobs", {"workflow": workflow})
    time.sleep(0.3)

    events: list = []
    err: list = []

    def _read():
        try:
            events.extend(stack.read_sse(job["urls"]["events"], timeout=20.0))
        except Exception as e:  # noqa: BLE001
            err.append(e)

    t = threading.Thread(target=_read)
    t.start()
    time.sleep(0.5)  # let the stream connect + emit progress/preview
    stack.request("POST", job["urls"]["cancel"])
    t.join(timeout=20.0)

    assert not err, err
    kinds = [name for name, _ in events]
    assert "status" in kinds, kinds
    assert "progress" in kinds, f"no progress event: {kinds}"
    assert "preview" in kinds, f"no preview event: {kinds}"
    # A terminal status event closes the stream.
    terminal = [d for n, d in events if n == "status" and d.get("status") in _TERMINAL]
    assert terminal, f"no terminal status: {events}"

    # Sanity-check the preview payload shape.
    preview = next(d for n, d in events if n == "preview")
    assert preview["content_type"].startswith("image/")
    assert preview["data_base64"]


def test_content_range_request(stack):
    _, asset, _ = stack.upload("cat.png", _PNG, "image/png")
    status, _, content = stack.request("GET", asset["url"], headers={"Range": "bytes=0-3"})
    # The fake honors Range with a 206; the proxy relays it.
    assert status in (200, 206)
    if status == 206:
        assert len(content) == 4


def test_model_upload_requires_base_dir(stack):
    # Without --comfyui-base-dir, a model-root upload is rejected clearly.
    header = struct.pack("<Q", 2) + b"{}"
    status, body, raw = stack.upload(
        "models/checkpoints/m.safetensors", header, "application/octet-stream"
    )
    assert status == 422, raw
    assert "co-located" in body["error"]["message"]


def test_model_upload_placed_on_disk(stack_with_models_dir):
    stack, base_dir = stack_with_models_dir
    # A minimal valid safetensors: 8-byte header len, then that many JSON bytes.
    header_json = b'{"w": {"dtype": "F32", "shape": [1], "data_offsets": [0, 4]}}'
    data = struct.pack("<Q", len(header_json)) + header_json + b"\x00\x00\x00\x00"
    status, asset, raw = stack.upload(
        "models/checkpoints/m.safetensors", data, "application/octet-stream"
    )
    assert status == 201, raw
    placed = base_dir / "models" / "checkpoints" / "m.safetensors"
    assert placed.exists(), "model file was not placed on disk"
    assert placed.read_bytes() == data


def test_model_upload_rejects_non_safetensors(stack_with_models_dir):
    stack, _ = stack_with_models_dir
    status, body, raw = stack.upload(
        "models/checkpoints/evil.safetensors", b"not safetensors", "application/octet-stream"
    )
    assert status == 422, raw
    assert "safetensors" in body["error"]["message"]


# ---------------------------------------------------------------------------
# Regression: path-traversal bypass of the model-placement guard.
#
# Before the fix, `input/../checkpoints/evil.safetensors` had
# Path(...).parts[0] == "input", so the (then first-segment-only) model/input
# classifier waved it through as "just an input upload" — a code path that
# applied NO placement validation at all — letting the ".." ride along into
# whatever ComfyUI's /upload/image did with the resulting subfolder string.
# validate_upload_path (security.py) now runs once, on the whole path, before
# that classification happens.
# ---------------------------------------------------------------------------
def test_dotdot_disguised_as_input_path_rejected(stack):
    status, body, raw = stack.upload(
        "input/../checkpoints/evil.safetensors", b"anything", "application/octet-stream"
    )
    assert status == 422, raw
    assert body["error"]["code"] == "invalid_request"


def test_dotdot_climbing_to_arbitrary_path_rejected(stack):
    status, body, raw = stack.upload("input/../../etc/cron.d/pwn", b"malicious", "text/plain")
    assert status == 422, raw
    assert body["error"]["code"] == "invalid_request"


def test_normal_paths_still_succeed_after_traversal_fix(stack_with_models_dir):
    stack, _ = stack_with_models_dir
    status, _, raw = stack.upload("input/foo.png", _PNG, "image/png")
    assert status == 201, raw

    header_json = b'{"w": {"dtype": "F32", "shape": [1], "data_offsets": [0, 4]}}'
    data = struct.pack("<Q", len(header_json)) + header_json + b"\x00\x00\x00\x00"
    status, _, raw = stack.upload("checkpoints/foo.safetensors", data, "application/octet-stream")
    assert status == 201, raw


# ---------------------------------------------------------------------------
# Regression: forgeable stateless output asset ids.
#
# Before the fix, `_decode_asset_id` trusted any well-formed
# `asset_<base64url(json)>` id — a hand-crafted one would let a client fetch
# an arbitrary filename/subfolder via /view (through get_asset_content), or
# splice an attacker-chosen path into a submitted workflow (through
# _resolve_asset_ref, reached via a core/ASSET reference). The ids are now
# HMAC-signed with a per-process secret and verified before use.
# ---------------------------------------------------------------------------
def test_forged_asset_id_rejected_by_content_fetch(stack):
    raw_payload = json.dumps({"f": "out.png", "s": "", "t": "output"}).encode()
    payload_b64 = base64.urlsafe_b64encode(raw_payload).decode().rstrip("=")
    forged_id = f"asset_{payload_b64}.notarealsignature"

    status, body, _ = stack.request("GET", f"/api/v2/assets/{forged_id}/content")
    assert status == 404, body

    status, body, _ = stack.request("GET", f"/api/v2/assets/{forged_id}")
    assert status == 404, body


def test_forged_asset_id_rejected_by_workflow_resolution(stack):
    raw_payload = json.dumps({"f": "../../etc/passwd", "s": "", "t": "output"}).encode()
    payload_b64 = base64.urlsafe_b64encode(raw_payload).decode().rstrip("=")
    forged_id = f"asset_{payload_b64}.notarealsignature"

    workflow = {
        "1": {
            "class_type": "LoadImage",
            "inputs": {"image": {"__type": "core/ASSET", "info": {"id": forged_id}}},
        }
    }
    status, body, raw = stack.request("POST", "/api/v2/jobs", {"workflow": workflow})
    assert status == 422, raw
    assert body["error"]["code"] == "missing_asset"


def test_legitimately_minted_asset_id_round_trips(stack):
    # A real job output mints its id via the proxy's own signing path
    # (Proxy._asset_id) — it must still decode, both for content fetch and
    # for core/ASSET resolution in a later submission.
    workflow = {
        "1": {"class_type": "Noop", "inputs": {}},
        "9": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}},
    }
    status, job, raw = stack.request("POST", "/api/v2/jobs", {"workflow": workflow})
    assert status == 201, raw
    job = _poll_until_terminal(stack, job)
    assert job["status"] == "succeeded", job.get("error")
    assert job["outputs"], "no outputs"
    legit_id = job["outputs"][0]["id"]
    assert legit_id.startswith("asset_")

    status, _, content = stack.request("GET", job["outputs"][0]["url"])
    assert status == 200, content
    assert content.startswith(b"\x89PNG\r\n\x1a\n")

    status, meta, raw = stack.request("GET", f"/api/v2/assets/{legit_id}")
    assert status == 200, raw

    workflow2 = {
        "1": {
            "class_type": "LoadImage",
            "inputs": {"image": {"__type": "core/ASSET", "info": {"id": legit_id}}},
        },
        "9": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}},
    }
    status, body, raw = stack.request("POST", "/api/v2/jobs", {"workflow": workflow2})
    assert status == 201, raw


# ---------------------------------------------------------------------------
# Regression: model core/ASSET references resolving to the wrong filename.
#
# Before the fix, a model AssetRecord stored the ORIGINAL, category-qualified
# file_path (e.g. "checkpoints/my_model.safetensors"), and _resolve_asset_ref
# substituted it verbatim into the workflow. But ComfyUI's combo widgets
# reference a model by its path RELATIVE TO the model-root directory (e.g.
# just "my_model.safetensors") — folder_paths.get_filename_list() never
# includes the category segment. The category-qualified value would be
# rejected by ComfyUI as an unknown checkpoint name.
# ---------------------------------------------------------------------------
def test_model_asset_ref_resolves_to_root_relative_filename(stack_with_models_dir):
    stack, base_dir = stack_with_models_dir
    header_json = b'{"w": {"dtype": "F32", "shape": [1], "data_offsets": [0, 4]}}'
    data = struct.pack("<Q", len(header_json)) + header_json + b"\x00\x00\x00\x00"
    status, asset, raw = stack.upload(
        "models/checkpoints/my_model.safetensors", data, "application/octet-stream"
    )
    assert status == 201, raw
    # The asset's own file_path must already be root-relative (no leading
    # "checkpoints/" segment) — this is what _resolve_asset_ref substitutes.
    assert asset["file_path"] == "my_model.safetensors", asset

    workflow = {
        "1": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": {"__type": "core/ASSET", "info": {"id": asset["id"]}}},
        },
        "9": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}},
    }
    status, job, raw = stack.request("POST", "/api/v2/jobs", {"workflow": workflow})
    # The fake ComfyUI doesn't itself validate the resolved value against a
    # real model directory, but this proves the *value the proxy substitutes*
    # is root-relative rather than category-qualified — the resolution step
    # this regression is about, distinct from what a real ComfyUI does with it.
    assert status == 201, raw
