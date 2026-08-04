"""Production batch-workload feedback MVP: persistence, cache flag, typed output
errors, metadata, list-jobs, from-path, health (GitHub #18).
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from tests.conftest import _make_stack

_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
    "53de0000000c49444154789c6360f8cf00000301010018dd8db10000000049454e44ae426082"
)
_TERMINAL = {"succeeded", "failed", "expired", "canceled"}


def _wait_terminal(stack, job: dict[str, Any]) -> dict[str, Any]:
    deadline = time.monotonic() + 15
    while job["status"] not in _TERMINAL:
        assert time.monotonic() < deadline, job
        time.sleep(0.1)
        _, job, _ = stack.request("GET", job["urls"]["self"])
    return job


def _simple_workflow(asset_id: str | None = None, **node_inputs: Any) -> dict[str, Any]:
    inputs: dict[str, Any] = dict(node_inputs)
    if asset_id is not None:
        inputs["image"] = {"__type": "core/ASSET", "info": {"id": asset_id}}
    return {
        "1": {"class_type": "LoadImage", "inputs": inputs or {"image": "x.png"}},
        "9": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}},
    }


@pytest.fixture
def stack_with_state(tmp_path) -> Any:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    s, cleanup = _make_stack(tmp_path, state_dir=str(state_dir))
    try:
        yield s, state_dir
    finally:
        cleanup()


def test_health_is_cheap_and_unauthenticated(stack_with_token):
    status, body, raw = stack_with_token.request("GET", "/api/v2/health")
    assert status == 200, raw
    assert body["status"] == "healthy"
    assert "upstream" in body


def test_metadata_and_advisory_priority_echoed(stack):
    _, asset, _ = stack.upload("cat.png", _PNG, "image/png")
    status, job, raw = stack.request(
        "POST",
        "/api/v2/jobs",
        {
            "workflow": _simple_workflow(asset["id"]),
            "metadata": "char=2807 angry straight",
            "priority": 10,
        },
    )
    assert status == 201, raw
    assert job["metadata"] == "char=2807 angry straight"
    assert job["priority"] == 10
    # Advisory only — status is still a normal queue admission.
    assert job["status"] == "queued"


def test_metadata_rejects_oversize_and_non_string(stack):
    _, asset, _ = stack.upload("cat.png", _PNG, "image/png")
    wf = _simple_workflow(asset["id"])
    status, body, _ = stack.request(
        "POST", "/api/v2/jobs", {"workflow": wf, "metadata": "x" * 2000}
    )
    assert status == 422
    assert body["error"]["code"] == "invalid_request"
    status, body, _ = stack.request(
        "POST", "/api/v2/jobs", {"workflow": wf, "metadata": {"no": "object"}}
    )
    assert status == 422


def test_list_jobs_returns_recorded_jobs(stack):
    _, asset, _ = stack.upload("cat.png", _PNG, "image/png")
    status, job, raw = stack.request(
        "POST", "/api/v2/jobs", {"workflow": _simple_workflow(asset["id"])}
    )
    assert status == 201, raw
    status, listing, raw = stack.request("GET", "/api/v2/jobs")
    assert status == 200, raw
    ids = {j["id"] for j in listing["jobs"]}
    assert job["id"] in ids


def test_outputs_reused_on_cache_hit(stack):
    _, asset, _ = stack.upload("cat.png", _PNG, "image/png")
    status, job, raw = stack.request(
        "POST",
        "/api/v2/jobs",
        {"workflow": _simple_workflow(asset["id"], cache_hit=True)},
    )
    assert status == 201, raw
    job = _wait_terminal(stack, job)
    assert job["status"] == "succeeded"
    assert job["outputs_reused"] is True
    assert job["outputs"] == []


def test_from_path_and_output_unavailable(stack_with_models_dir):
    stack, base_dir = stack_with_models_dir
    input_dir = base_dir / "input"
    input_dir.mkdir(parents=True)
    src = input_dir / "shared.png"
    src.write_bytes(_PNG)

    status, asset, raw = stack.request(
        "POST",
        "/api/v2/assets/from-path",
        {"path": str(src), "file_path": "input/shared.png"},
    )
    assert status == 201, raw
    assert asset["created_new"] is True
    assert asset["hash"].startswith("blake3:")

    # Content is readable while the host file exists.
    status, _, content = stack.request("GET", asset["url"])
    assert status == 200
    assert content == _PNG

    src.unlink()
    status, body, _ = stack.request("GET", asset["url"])
    assert status == 404
    assert body["error"]["code"] == "output_unavailable"


def test_state_dir_survives_proxy_restart(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    run_a = tmp_path / "run_a"
    run_a.mkdir()
    stack, cleanup = _make_stack(run_a, state_dir=str(state_dir))
    try:
        _, asset, _ = stack.upload("cat.png", _PNG, "image/png")
        asset_id = asset["id"]
        asset_hash = asset["hash"]
        status, job, raw = stack.request(
            "POST",
            "/api/v2/jobs",
            {
                "workflow": _simple_workflow(asset_id),
                "metadata": "persist-me",
                "priority": 3,
            },
            headers={"Idempotency-Key": "batch-key-1"},
        )
        assert status == 201, raw
        job_id = job["id"]
    finally:
        cleanup()

    # New proxy process, same state dir. ComfyUI is a fresh fake (so upstream
    # history is gone) — proxy records (metadata / idempotency / asset index)
    # must still resolve from SQLite.
    run_b = tmp_path / "run_b"
    run_b.mkdir()
    stack2, cleanup2 = _make_stack(run_b, state_dir=str(state_dir))
    try:
        status, asset2, raw = stack2.request("GET", f"/api/v2/assets/{asset_id}")
        assert status == 200, raw
        assert asset2["hash"] == asset_hash

        status, body, _ = stack2.request(
            "POST",
            "/api/v2/jobs",
            {"workflow": _simple_workflow(asset_id)},
            headers={"Idempotency-Key": "batch-key-1"},
        )
        assert status == 422
        assert body["error"]["code"] == "idempotency_key_reuse"

        # Job id is still known to the proxy (may be expired vs ComfyUI).
        status, job2, raw = stack2.request("GET", f"/api/v2/jobs/{job_id}")
        assert status == 200, raw
        assert job2["metadata"] == "persist-me"
        assert job2["priority"] == 3
    finally:
        cleanup2()


def test_server_argv_includes_state_dir():
    from argparse import Namespace

    from comfy_api_proxy.cli import _server_argv

    args = Namespace(
        comfyui="http://127.0.0.1:8188",
        host="127.0.0.1",
        port=8189,
        max_upload_mb=100,
        token=None,
        comfyui_base_dir=None,
        allow_insecure_bind=False,
        state_dir="/tmp/proxy-state",
    )
    argv = _server_argv(args)
    assert "--state-dir" in argv
    assert "/tmp/proxy-state" in argv
