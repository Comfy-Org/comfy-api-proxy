"""End-to-end tests for the full v2 surface over the fake ComfyUI.

Covers the story the demo couldn't: upload an input asset, reference it from
a workflow via a core/ASSET object, run, and download — plus cancel, the
dedup/from-hash/by-hash paths, SSE, and the model-placement security guards.
Stdlib-only HTTP (see conftest.Stack); no SDK, no third-party client.
"""

from __future__ import annotations

import base64
import contextlib
import json
import struct
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

from comfy_api_proxy.app import _MAX_CONCURRENT_STREAMS

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


def test_job_id_is_a_valid_uuid(stack):
    # ComfyUI's POST /prompt requires prompt_id to be a canonical UUID; the
    # proxy reuses the job id as prompt_id, so the job id must be a UUID.
    import uuid as _uuid

    wf = {"9": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}}}
    status, job, raw = stack.request("POST", "/api/v2/jobs", {"workflow": wf})
    assert status == 201, raw
    _uuid.UUID(job["id"])  # raises if not a valid UUID


def test_job_and_output_urls_are_absolute(stack):
    # The contract types Output.url / Asset.url as absolute URIs (format: uri)
    # and job.urls.* must be followable as-is. A relative url here makes a
    # strict SDK model reject the response, so guard that they are absolute and
    # point back at the surface the client reached us on.
    wf = {"9": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}}}
    _, job, raw = stack.request("POST", "/api/v2/jobs", {"workflow": wf})
    assert job["urls"]["self"] == f"{stack.base}/api/v2/jobs/{job['id']}"
    assert job["urls"]["events"].startswith(f"{stack.base}/")
    assert job["urls"]["cancel"].startswith(f"{stack.base}/")
    job = _poll_until_terminal(stack, job)
    assert job["status"] == "succeeded", job
    assert job["outputs"], job
    for out in job["outputs"]:
        assert out["url"].startswith(f"{stack.base}/api/v2/assets/"), out["url"]
        assert out["url"].endswith("/content")


def test_upload_asset_returns_asset_shape(stack):
    import uuid as _uuid

    status, asset, raw = stack.upload("cat.png", _PNG, "image/png", tags="input")
    assert status == 201, raw
    _uuid.UUID(asset["id"])  # bare UUID, matching the shape other v2 surfaces emit
    assert asset["hash"].startswith("blake3:")
    assert asset["size_bytes"] == len(_PNG)
    assert asset["content_type"] == "image/png"
    assert asset["created_new"] is True
    assert asset["url"] == f"{stack.base}/api/v2/assets/{asset['id']}/content"


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


def test_malformed_asset_ref_rejected_not_forwarded(stack):
    # A node tagged core/ASSET but with a missing or non-object `info` must be
    # rejected with 422 missing_asset, not forwarded to ComfyUI as a literal dict.
    for bad in (
        {"__type": "core/ASSET"},
        {"__type": "core/ASSET", "info": "nope"},
        {"__type": "core/ASSET", "info": {}},
    ):
        workflow = {"1": {"class_type": "LoadImage", "inputs": {"image": bad}}}
        status, body, raw = stack.request("POST", "/api/v2/jobs", {"workflow": workflow})
        assert status == 422, f"{bad!r} -> {raw!r}"
        assert body["error"]["code"] == "missing_asset", raw


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


def _fake_prompt_extra_data(stack, job_id):
    # Read back what the proxy forwarded on the upstream /prompt call, straight
    # from the fake ComfyUI (job_id == the proxy-minted prompt_id). Returns
    # (present, value): `present` is genuine wire-level key membership, so a
    # test can tell "omitted" from an explicit null.
    url = f"http://127.0.0.1:{stack.comfyui_port}/_debug/extra_data/{job_id}"
    status, body, raw = stack.request("GET", url)
    assert status == 200, raw
    return body["present"], body["extra_data"]


def test_extra_data_api_key_forwarded_to_prompt(stack):
    # extra_data.api_key_comfy_org (partner-node auth) must ride the upstream
    # /prompt body verbatim.
    status, job, raw = stack.request(
        "POST",
        "/api/v2/jobs",
        {"workflow": {"1": {}}, "extra_data": {"api_key_comfy_org": "comfyui-secret"}},
    )
    assert status == 201, raw
    present, value = _fake_prompt_extra_data(stack, job["id"])
    assert present and value == {"api_key_comfy_org": "comfyui-secret"}


def test_extra_data_absent_when_not_supplied(stack):
    # No extra_data in the request => the proxy must not send an extra_data key
    # upstream AT ALL (genuine absence, not an explicit null).
    status, job, raw = stack.request("POST", "/api/v2/jobs", {"workflow": {"1": {}}})
    assert status == 201, raw
    present, _ = _fake_prompt_extra_data(stack, job["id"])
    assert present is False


def test_extra_data_empty_object_accepted_but_not_forwarded(stack):
    # An empty extra_data is schema-valid (a closed object with no keys) — accept
    # it, but forward nothing (never send an empty object upstream).
    status, job, raw = stack.request(
        "POST", "/api/v2/jobs", {"workflow": {"1": {}}, "extra_data": {}}
    )
    assert status == 201, raw
    present, _ = _fake_prompt_extra_data(stack, job["id"])
    assert present is False


def test_extra_data_rejects_uncontracted_shapes(stack):
    # Closed, typed object: only a string api_key_comfy_org is accepted.
    for bad in (
        {"api_key_comfy_org": "k", "surprise": 1},  # unknown key
        {"auth_token_comfy_org": "t"},  # not in the v2 contract
        {"api_key_comfy_org": 123},  # wrong type
        {"api_key_comfy_org": None},  # present-but-null (schema says string)
        "not-an-object",
    ):
        status, body, raw = stack.request(
            "POST", "/api/v2/jobs", {"workflow": {"1": {}}, "extra_data": bad}
        )
        assert status == 400, f"{bad!r} -> {raw!r}"
        assert body["error"]["code"] == "invalid_request", raw


def test_idempotency_key_reuse_ignores_differing_api_key(stack):
    # api_key_comfy_org is excluded from the idempotency comparison: a resubmit
    # under the same key is rejected as reuse even with a different api_key —
    # the key is single-use (reject-on-duplicate), body is never compared.
    hdr = {"Idempotency-Key": "reuse-extradata-1"}
    status, _, raw = stack.request(
        "POST",
        "/api/v2/jobs",
        {"workflow": {"1": {}}, "extra_data": {"api_key_comfy_org": "comfyui-a"}},
        headers=hdr,
    )
    assert status == 201, raw
    status, body, raw = stack.request(
        "POST",
        "/api/v2/jobs",
        {"workflow": {"1": {}}, "extra_data": {"api_key_comfy_org": "comfyui-b"}},
        headers=hdr,
    )
    assert status == 422, raw
    assert body["error"]["code"] == "idempotency_key_reuse", raw


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


def test_sse_stream_delivers_output_event_and_no_log(stack):
    # A normally-completing job: the bridge connects while it is running, then
    # the fake fires `executed` (a node's outputs committed) before the terminal
    # `execution_success`, so the stream surfaces a live `output` event — exactly
    # once (deduped against the terminal reconcile). And it emits no `log` event,
    # matching the Comfy Cloud surface (feature parity).
    #
    # `complete_after_seconds` gives the SSE stream a generous, test-controlled
    # window to connect and take its initial snapshot BEFORE the job goes
    # terminal, so this deterministically exercises the live executed→output
    # path rather than racing the fake's default 0.2s auto-complete.
    workflow = {"9": {"class_type": "SaveImage", "inputs": {"complete_after_seconds": 2.0}}}
    _, job, _ = stack.request("POST", "/api/v2/jobs", {"workflow": workflow})

    events = stack.read_sse(job["urls"]["events"], timeout=20.0)
    kinds = [name for name, _ in events]

    outputs = [d for n, d in events if n == "output"]
    assert len(outputs) == 1, f"expected exactly one live output event, got kinds={kinds}"
    out = outputs[0]
    assert out.get("id"), out
    assert out.get("url"), out
    assert out.get("node_id") == "9", out

    assert "log" not in kinds, f"`log` must not be emitted (parity with public-api): {kinds}"

    # The `output` event arrives before the terminal status (it's a live,
    # ahead-of-terminal delivery, not a terminal batch).
    assert "output" in kinds and "status" in kinds
    assert kinds.index("output") < len(kinds) - 1, kinds
    terminal = [d for n, d in events if n == "status" and d.get("status") in _TERMINAL]
    assert terminal, f"no terminal status: {kinds}"


def test_on_text_drops_log_on_execution_error():
    # Unit-level parity check: `execution_error` yields NO `log` frame (it is
    # handled purely as a terminal transition elsewhere). _on_text needs neither
    # the snapshot fn nor the session for this path, so both are left unset.
    from comfy_api_proxy.realtime import JobEventBridge

    bridge = JobEventBridge(
        "http://comfy.invalid",
        "11111111-1111-1111-1111-111111111111",
        client_id="c",
        snapshot=None,  # type: ignore[arg-type]
        session=None,  # type: ignore[arg-type]
    )
    frames = bridge._on_text(
        json.dumps(
            {
                "type": "execution_error",
                "data": {
                    "prompt_id": "11111111-1111-1111-1111-111111111111",
                    "exception_message": "boom",
                },
            }
        )
    )
    assert frames == [], f"execution_error must not emit a `log` frame: {frames}"


def test_get_job_rejects_non_uuid_id_before_any_upstream_call(stack):
    # A dot-segment id would otherwise be spliced into the upstream ComfyUI
    # path and normalized into an arbitrary endpoint. Reject with 404 first.
    status, body, _ = stack.request("GET", "/api/v2/jobs/..%2F..%2Ffree")
    assert status == 404, body
    assert body["error"]["code"] == "not_found"


def test_cancel_job_rejects_path_traversal_id(stack):
    status, body, _ = stack.request("POST", "/api/v2/jobs/..%2F..%2Ffree/cancel")
    assert status == 404, body
    assert body["error"]["code"] == "not_found"


def test_job_events_rejects_non_uuid_id(stack):
    status, body, _ = stack.request("GET", "/api/v2/jobs/not-a-uuid/events")
    assert status == 404, body
    assert body["error"]["code"] == "not_found"


def test_output_asset_ids_are_scoped_per_job(stack):
    # The fake ComfyUI names every output "out.png" (demo/fake_comfyui.py), so
    # any two jobs collide on filename today — their asset ids must NOT collide,
    # or a cached URL from job A could later serve job B's bytes.
    wf = {"9": {"class_type": "SaveImage", "inputs": {}}}
    _, job1, _ = stack.request("POST", "/api/v2/jobs", {"workflow": wf})
    _, job2, _ = stack.request("POST", "/api/v2/jobs", {"workflow": wf})
    job1 = _poll_until_terminal(stack, job1)
    job2 = _poll_until_terminal(stack, job2)
    out1, out2 = job1["outputs"][0], job2["outputs"][0]
    assert out1["name"] == out2["name"] == "out.png"
    assert out1["id"] != out2["id"], "output asset ids must be job-scoped"
    assert out1["url"] != out2["url"]


def test_realtime_parsers_ignore_non_dict_json_frames():
    # A valid-JSON but non-object WS frame (plausibly from a buggy/malicious
    # custom node) must not crash the stream with an AttributeError.
    from comfy_api_proxy.realtime import JobEventBridge

    bridge = JobEventBridge(
        "http://comfy.invalid",
        "11111111-1111-1111-1111-111111111111",
        client_id="c",
        snapshot=None,  # type: ignore[arg-type]
        session=None,  # type: ignore[arg-type]
    )
    for frame in ("[1,2,3]", "42", "null", '"a string"'):
        assert bridge._on_text(frame) == []
        assert bridge._is_terminal_text(frame) is False
        assert bridge._is_executed_text(frame) is False


async def test_reconcile_outputs_is_throttled():
    # Two executed-driven reconciles within the throttle window must trigger
    # only ONE re-snapshot (the /history re-fetch), not one per executed frame.
    from comfy_api_proxy.realtime import JobEventBridge

    calls: list[int] = []

    async def fake_snapshot() -> dict:
        calls.append(1)
        return {"status": "running", "outputs": []}

    bridge = JobEventBridge(
        "http://comfy.invalid",
        "11111111-1111-1111-1111-111111111111",
        client_id="c",
        snapshot=fake_snapshot,
        session=None,  # type: ignore[arg-type]
    )
    seen: set[str] = set()
    await bridge._reconcile_outputs(seen)
    await bridge._reconcile_outputs(seen)
    assert len(calls) == 1, "back-to-back executed reconciles must coalesce into one snapshot"


async def test_status_of_raises_upstream_unreachable_when_comfyui_down():
    from comfy_api_proxy.app import Proxy, UpstreamUnreachable

    proxy = Proxy("http://127.0.0.1:1")  # port 1 → connection refused
    await proxy.on_startup(None)
    try:
        with pytest.raises(UpstreamUnreachable):
            await proxy._status_of("11111111-1111-1111-1111-111111111111", "http://x")
    finally:
        await proxy.on_cleanup(None)


async def test_get_job_maps_upstream_unreachable_to_503_envelope():
    from aiohttp.test_utils import make_mocked_request

    from comfy_api_proxy.app import Proxy

    proxy = Proxy("http://127.0.0.1:1")
    await proxy.on_startup(None)
    try:
        jid = "11111111-1111-1111-1111-111111111111"
        req = make_mocked_request("GET", f"/api/v2/jobs/{jid}", match_info={"id": jid})
        resp = await proxy.get_job(req)
        assert resp.status == 503
        assert json.loads(resp.body)["error"]["code"] == "upstream_unreachable"
    finally:
        await proxy.on_cleanup(None)


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
# `<base64url(json)>.<tag>` id — a hand-crafted one would let a client fetch
# an arbitrary filename/subfolder via /view (through get_asset_content), or
# splice an attacker-chosen path into a submitted workflow (through
# _resolve_asset_ref, reached via a core/ASSET reference). The ids are now
# HMAC-signed with a per-process secret and verified before use.
# ---------------------------------------------------------------------------
def test_forged_asset_id_rejected_by_content_fetch(stack):
    raw_payload = json.dumps({"f": "out.png", "s": "", "t": "output"}).encode()
    payload_b64 = base64.urlsafe_b64encode(raw_payload).decode().rstrip("=")
    forged_id = f"{payload_b64}.notarealsignature"

    status, body, _ = stack.request("GET", f"/api/v2/assets/{forged_id}/content")
    assert status == 404, body

    status, body, _ = stack.request("GET", f"/api/v2/assets/{forged_id}")
    assert status == 404, body


def test_forged_asset_id_rejected_by_workflow_resolution(stack):
    raw_payload = json.dumps({"f": "../../etc/passwd", "s": "", "t": "output"}).encode()
    payload_b64 = base64.urlsafe_b64encode(raw_payload).decode().rstrip("=")
    forged_id = f"{payload_b64}.notarealsignature"

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
    # Signed `payload.tag` form, unlike bare-UUID upload ids. Both halves are
    # base64url, which has no ".", so exactly one separator is the whole shape —
    # and the prefix carries no "." either, so the count alone would still admit
    # the retired `asset_payload.tag`. Both assertions are load-bearing.
    assert not legit_id.startswith("asset_")
    assert legit_id.count(".") == 1

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


def test_uploaded_asset_id_has_no_dot_and_still_resolves(stack):
    # Uploaded ids are bare UUIDs (assets.new_asset_id); signed job-output ids
    # are `<payload_b64>.<tag_b64>`. Readers hit the store first and only
    # decode on a miss, so what keeps the two apart is that a bare UUID never
    # contains a "." and so can never decode as the signed form — pin that.
    status, asset, raw = stack.upload("cat.png", _PNG, "image/png", tags="input")
    assert status == 201, raw
    assert "." not in asset["id"]

    status, meta, raw = stack.request("GET", f"/api/v2/assets/{asset['id']}")
    assert status == 200, raw
    assert meta["id"] == asset["id"]


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


# ---------------------------------------------------------------------------
# 422 mapping: reserved top-level fields and a missing 'workflow' key.
#
# test_reserved_fields_rejected (above) only exercises 'webhook_url'; submit()
# rejects 'inputs' the same way (a reserved field, not accepted in v1), and a
# request with no 'workflow' key at all is a distinct code path from both the
# reserved-field check and the UI-format-graph check.
# ---------------------------------------------------------------------------
def test_submit_reserved_inputs_field_rejected(stack):
    status, body, raw = stack.request(
        "POST", "/api/v2/jobs", {"workflow": {"1": {}}, "inputs": {"foo": "bar"}}
    )
    assert status == 422, raw
    assert body["error"]["code"] == "invalid_workflow"


def test_submit_missing_workflow_key_rejected(stack):
    status, body, raw = stack.request("POST", "/api/v2/jobs", {})
    assert status == 422, raw
    assert body["error"]["code"] == "invalid_workflow"


def test_idempotency_key_reuse_rejected(stack):
    # Single-use, reject-on-duplicate (no replay): the first submit with a key
    # succeeds; a second submit reusing the same key is 422 idempotency_key_reuse;
    # a different key is accepted.
    wf = {"workflow": {"1": {"class_type": "Noop", "inputs": {}}}}
    s1, _, r1 = stack.request("POST", "/api/v2/jobs", wf, headers={"Idempotency-Key": "key-abc"})
    assert s1 == 201, r1
    s2, b2, r2 = stack.request("POST", "/api/v2/jobs", wf, headers={"Idempotency-Key": "key-abc"})
    assert s2 == 422, r2
    assert b2["error"]["code"] == "idempotency_key_reuse"
    s3, _, r3 = stack.request("POST", "/api/v2/jobs", wf, headers={"Idempotency-Key": "key-xyz"})
    assert s3 == 201, r3


def test_idempotency_key_released_on_rejection_allows_retry(stack):
    # A submit that ComfyUI DEFINITELY rejects (no job created) must release
    # the key so a corrected retry with the SAME key can go through. An empty
    # graph makes the fake ComfyUI return 400 -> the proxy maps it to 422
    # invalid_workflow and releases the key.
    bad = {"workflow": {}}
    s1, b1, r1 = stack.request("POST", "/api/v2/jobs", bad, headers={"Idempotency-Key": "rel-1"})
    assert s1 == 422, r1
    assert b1["error"]["code"] == "invalid_workflow"
    good = {"workflow": {"1": {"class_type": "Noop", "inputs": {}}}}
    s2, _, r2 = stack.request("POST", "/api/v2/jobs", good, headers={"Idempotency-Key": "rel-1"})
    assert s2 == 201, r2  # key was released by the rejection, retry allowed


def test_idempotency_key_not_consumed_by_earlier_validation(stack):
    # Validation that runs BEFORE the key is claimed (UI-format detection here)
    # must not consume the key: a corrected retry with the same key succeeds.
    ui = {"workflow": {"nodes": [], "links": []}}
    s1, b1, r1 = stack.request("POST", "/api/v2/jobs", ui, headers={"Idempotency-Key": "val-1"})
    assert s1 == 422, r1
    assert b1["error"]["code"] == "workflow_format_ui"
    good = {"workflow": {"1": {"class_type": "Noop", "inputs": {}}}}
    s2, _, r2 = stack.request("POST", "/api/v2/jobs", good, headers={"Idempotency-Key": "val-1"})
    assert s2 == 201, r2


def test_idempotency_key_empty_header_rejected(stack):
    # A present-but-empty Idempotency-Key is malformed input (400), not "no key"
    # (which would silently bypass enforcement).
    wf = {"workflow": {"1": {"class_type": "Noop", "inputs": {}}}}
    s, b, r = stack.request("POST", "/api/v2/jobs", wf, headers={"Idempotency-Key": ""})
    assert s == 400, r
    assert b["error"]["code"] == "invalid_request"
    # An over-long key is likewise rejected before it can be stored.
    s2, b2, r2 = stack.request("POST", "/api/v2/jobs", wf, headers={"Idempotency-Key": "x" * 256})
    assert s2 == 400, r2
    assert b2["error"]["code"] == "invalid_request"


def test_idempotency_key_concurrent_same_key_one_wins(stack):
    # Two genuinely concurrent submits with the same key: exactly one is
    # accepted (201) and the other is rejected (422). This is the property the
    # synchronous check-then-claim (no await between test and add) guarantees.
    import concurrent.futures

    wf = {"workflow": {"1": {"class_type": "Noop", "inputs": {"hang": True}}}}

    def submit():
        return stack.request("POST", "/api/v2/jobs", wf, headers={"Idempotency-Key": "conc-1"})[0]

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        statuses = sorted(f.result() for f in [ex.submit(submit), ex.submit(submit)])
    assert statuses == [201, 422], statuses


async def test_idempotency_key_held_when_upstream_unreachable():
    # An ambiguous outcome (ComfyUI unreachable) HOLDS the key: ComfyUI may
    # already hold the prompt, so a same-key retry must not double-submit. The
    # first submit is 503; a retry with the same key is 422 idempotency_key_reuse.
    from aiohttp.test_utils import make_mocked_request

    from comfy_api_proxy.app import Proxy

    def _submit_req(key):
        req = make_mocked_request(
            "POST",
            "/api/v2/jobs",
            headers={"Content-Type": "application/json", "Idempotency-Key": key},
        )

        async def _json():
            return {"workflow": {"1": {"class_type": "Noop", "inputs": {}}}}

        req.json = _json
        return req

    proxy = Proxy("http://127.0.0.1:1")  # port 1 -> connection refused
    await proxy.on_startup(None)
    try:
        first = await proxy.submit(_submit_req("hold-1"))
        assert first.status == 503
        assert json.loads(first.body)["error"]["code"] == "upstream_unreachable"
        second = await proxy.submit(_submit_req("hold-1"))
        assert second.status == 422
        assert json.loads(second.body)["error"]["code"] == "idempotency_key_reuse"
    finally:
        await proxy.on_cleanup(None)


# ---------------------------------------------------------------------------
# Cancel of an already-terminal job must be a harmless no-op: cancel_job()
# calls ComfyUI's cancel unconditionally when the job is known to the proxy,
# then only overrides the status to "canceling" if the post-cancel state is
# "running". A succeeded job must come back unchanged, not corrupted into
# "canceling" or an error.
# ---------------------------------------------------------------------------
def test_cancel_of_already_succeeded_job_is_idempotent(stack):
    workflow = {"9": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}}}
    _, job, raw = stack.request("POST", "/api/v2/jobs", {"workflow": workflow})
    assert job.get("id"), raw
    job = _poll_until_terminal(stack, job)
    assert job["status"] == "succeeded", job

    status, body, raw = stack.request("POST", job["urls"]["cancel"])
    assert status == 200, raw
    assert body["status"] == "succeeded", body


# ---------------------------------------------------------------------------
# Retention shape: Job.expires_at and Asset.url_expires_at are contractual
# fields (required by spec/openapi.yaml), but schema-conformance only checks
# that they parse as a date-time — not that they carry the actual 24h
# retention window the README documents. Lock in the real duration so a
# change to _RETENTION (app.py) or a broken computation is caught here.
# ---------------------------------------------------------------------------
def test_job_expires_at_is_24h_after_created_at(stack):
    workflow = {"9": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}}}
    status, job, raw = stack.request("POST", "/api/v2/jobs", {"workflow": workflow})
    assert status == 201, raw
    created = datetime.fromisoformat(job["created_at"])
    expires = datetime.fromisoformat(job["expires_at"])
    assert expires - created == timedelta(hours=24), (job["created_at"], job["expires_at"])


def test_asset_url_expires_at_is_24h_out(stack):
    before = datetime.now(timezone.utc)
    status, asset, raw = stack.upload("cat.png", _PNG, "image/png")
    after = datetime.now(timezone.utc)
    assert status == 201, raw
    expires = datetime.fromisoformat(asset["url_expires_at"])
    assert before + timedelta(hours=24) <= expires <= after + timedelta(hours=24, minutes=1), (
        before,
        expires,
        after,
    )


# ---------------------------------------------------------------------------
# Range requests: the existing test_content_range_request only checks that
# *a* status in (200, 206) comes back. The fake always honors a Range header
# with a 206, so the proxy's pass-through (app._stream_view) should too —
# assert the deterministic outcome: the exact byte slice and the Content-Range
# header the proxy relays from upstream, not just "didn't error."
# ---------------------------------------------------------------------------
def test_range_request_returns_exact_slice_and_content_range_header(stack):
    _, asset, raw = stack.upload("cat.png", _PNG, "image/png")
    req = urllib.request.Request(asset["url"], headers={"Range": "bytes=2-5"})
    with urllib.request.urlopen(req, timeout=10) as r:
        body = r.read()
        assert r.status == 206, raw
        assert r.headers.get("Content-Range") == f"bytes 2-5/{len(_PNG)}"
        assert body == _PNG[2:6]


# ---------------------------------------------------------------------------
# Absolute-URL behavior: _external_base() (app.py) builds job/asset URLs from
# request.scheme/request.host. make_app() wires no "trust the reverse proxy"
# middleware, so those must reflect the connection the client actually used —
# a client-supplied X-Forwarded-Host/-Proto must NOT be able to redirect the
# URLs a caller is handed to an attacker-chosen origin.
# ---------------------------------------------------------------------------
def test_absolute_urls_ignore_client_supplied_forwarded_headers(stack):
    workflow = {"9": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}}}
    status, job, raw = stack.request(
        "POST",
        "/api/v2/jobs",
        {"workflow": workflow},
        headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "evil.example"},
    )
    assert status == 201, raw
    assert job["urls"]["self"] == f"{stack.base}/api/v2/jobs/{job['id']}"
    assert "evil.example" not in json.dumps(job["urls"])

    _, asset, raw = stack.upload("cat.png", _PNG, "image/png")
    assert asset["url"].startswith(stack.base + "/"), raw


# ---------------------------------------------------------------------------
# Concurrent SSE stream cap (429 too_many_streams) and _open_streams
# accounting. Regression target: a client opening more than
# _MAX_CONCURRENT_STREAMS live event streams must be turned away with the
# contract's dedicated code (not a generic error) and a Retry-After hint,
# rather than the proxy accepting unbounded WS connections to ComfyUI. And
# the counter must actually free a slot when a stream ends, or the proxy
# would wedge itself into permanent 429s after any burst of clients.
# ---------------------------------------------------------------------------
def _open_sse_connection(url: str, timeout: float = 10.0):
    """Open an SSE connection and return the raw response without reading its
    body, so it keeps occupying one of the proxy's limited concurrent-stream
    slots until the caller closes it (or the server ends the stream)."""
    req = urllib.request.Request(url, method="GET")
    return urllib.request.urlopen(req, timeout=timeout)


def test_sse_stream_cap_returns_429_and_frees_slot_when_job_finishes(stack):
    workflow = {"1": {"class_type": "Noop", "inputs": {"hang": True}}}
    _, job, raw = stack.request("POST", "/api/v2/jobs", {"workflow": workflow})
    assert job.get("id"), raw
    events_url = job["urls"]["events"]

    opened = []
    try:
        for _ in range(_MAX_CONCURRENT_STREAMS):
            resp = _open_sse_connection(events_url)
            assert resp.status == 200
            opened.append(resp)

        # One more than the cap: the dedicated 429, not a generic failure —
        # and Retry-After tells a well-behaved client when to try again
        # instead of hammering the endpoint.
        status, body, raw = stack.request("GET", events_url)
        assert status == 429, raw
        assert body["error"]["code"] == "too_many_streams"

        # Finish the underlying job: every open bridge reaches its own
        # terminal `status` event and ends its stream on its own, freeing
        # every slot it held — without waiting out the (10s+) WS-timeout
        # fallback path.
        stack.request("POST", job["urls"]["cancel"])

        deadline = time.monotonic() + 10.0
        last = (None, None, None)
        while time.monotonic() < deadline:
            last = stack.request("GET", events_url)
            if last[0] == 200:
                break
            time.sleep(0.2)
        assert last[0] == 200, f"stream slot was never freed after job completion: {last}"
    finally:
        for resp in opened:
            with contextlib.suppress(Exception):
                resp.close()


async def test_asset_content_upstream_5xx_maps_to_502_but_404_stays_404():
    # A genuine upstream 404 means the output isn't there (-> 404); any other
    # non-2xx from ComfyUI is an upstream failure (-> 502 upstream_error), not
    # a "never existed" 404.
    from aiohttp.test_utils import make_mocked_request

    from comfy_api_proxy.app import Proxy

    class _Resp:
        def __init__(self, status: int) -> None:
            self.status = status
            self.content_type = "image/png"
            self.headers: dict[str, str] = {}

        def release(self) -> None:
            pass

    class _Session:
        def __init__(self, status: int) -> None:
            self._status = status

        async def get(self, url, params=None, headers=None):  # noqa: ANN001
            return _Resp(self._status)

    cases = [
        (500, 502, "upstream_error"),
        (403, 502, "upstream_error"),
        (404, 404, "output_unavailable"),
    ]
    for upstream_status, expected_status, expected_code in cases:
        proxy = Proxy("http://comfy")
        proxy._session = _Session(upstream_status)  # type: ignore[assignment]
        rec = proxy.assets.register_comfy_output(
            filename="out.png", subfolder="", type_="output", content_type="image/png"
        )
        req = make_mocked_request(
            "GET", f"/api/v2/assets/{rec.id}/content", match_info={"id": rec.id}
        )
        resp = await proxy.get_asset_content(req)
        assert resp.status == expected_status, (upstream_status, resp.status)
        assert json.loads(resp.body)["error"]["code"] == expected_code
