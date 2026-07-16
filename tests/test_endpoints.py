"""End-to-end tests for the full v2 surface over the fake ComfyUI.

Covers the story the demo couldn't: upload an input asset, reference it from
a workflow via a core/ASSET object, run, and download — plus cancel, the
dedup/from-hash/by-hash paths, SSE, and the model-placement security guards.
Stdlib-only HTTP (see conftest.Stack); no SDK, no third-party client.
"""

from __future__ import annotations

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
