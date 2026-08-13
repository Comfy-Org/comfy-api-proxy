"""Production batch-workload feedback MVP: persistence, cache flag, typed output
errors, metadata, list-jobs, from-path, health (GitHub #18).
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

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
def stack_with_state(tmp_path, make_stack) -> Any:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    s, cleanup = make_stack(tmp_path, state_dir=str(state_dir))
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


def test_outputs_reused_false_when_nothing_was_cached(stack):
    """ComfyUI emits execution_cached on every run, empty when it cached
    nothing — a normal execution must not report reuse."""
    _, asset, _ = stack.upload("cat.png", _PNG, "image/png")
    status, job, raw = stack.request(
        "POST", "/api/v2/jobs", {"workflow": _simple_workflow(asset["id"])}
    )
    assert status == 201, raw
    job = _wait_terminal(stack, job)
    assert job["status"] == "succeeded"
    assert job["outputs_reused"] is False
    assert job["outputs"], "a real execution should still produce outputs"


def test_outputs_reused_on_cache_hit(stack):
    """A fully-cached resubmit still reports the first run's outputs listing —
    the incident shape: reuse flagged, bytes possibly already gone."""
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
    assert job["outputs"], "a cached resubmit still lists the original outputs"


def test_outputs_reused_on_partial_cache_hit(stack):
    """Partial caching is ComfyUI's common case: some nodes served from cache,
    the rest freshly executed. Reuse is still flagged."""
    _, asset, _ = stack.upload("cat.png", _PNG, "image/png")
    status, job, raw = stack.request(
        "POST",
        "/api/v2/jobs",
        {"workflow": _simple_workflow(asset["id"], partial_cache_hit=True)},
    )
    assert status == 201, raw
    job = _wait_terminal(stack, job)
    assert job["status"] == "succeeded"
    assert job["outputs_reused"] is True
    assert job["outputs"]


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


def test_state_dir_survives_proxy_restart(tmp_path, make_stack):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    run_a = tmp_path / "run_a"
    run_a.mkdir()
    stack, cleanup = make_stack(run_a, state_dir=str(state_dir))
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
    stack2, cleanup2 = make_stack(run_b, state_dir=str(state_dir))
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


def test_job_outside_reload_window_reads_through_to_state_dir(tmp_path, make_stack, fake_comfyui):
    """The by-id read must reach SQLite, not just the reloaded window.

    Startup hydrates only the newest `_MAX_JOB_SCAN` rows (~12h at production
    rates), while ComfyUI's history ring answers for days. A job in that gap
    resolves upstream, so it must not come back with its proxy-layer fields
    dropped and a fabricated `created_at`.
    """
    import uuid
    from datetime import datetime, timedelta, timezone

    from comfy_api_proxy.app import _MAX_JOB_SCAN
    from comfy_api_proxy.persist import StateStore

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    run_a = tmp_path / "run_a"
    run_a.mkdir()
    stack, cleanup = make_stack(run_a, state_dir=str(state_dir), comfyui_port=fake_comfyui)
    try:
        _, asset, _ = stack.upload("cat.png", _PNG, "image/png")
        status, job, raw = stack.request(
            "POST",
            "/api/v2/jobs",
            {
                "workflow": _simple_workflow(asset["id"]),
                "metadata": "persist-me",
                "priority": 3,
            },
        )
        assert status == 201, raw
        job_id = job["id"]
        created_at = job["created_at"]
        assert _wait_terminal(stack, job)["status"] == "succeeded"
    finally:
        cleanup()  # proxy only — this fake ComfyUI keeps its history

    # Push the job past the reload window, plus one row ComfyUI never saw so
    # the gone-upstream half of the same branch is covered too.
    gone_id = str(uuid.uuid4())
    store = StateStore(state_dir / "state.sqlite3")
    try:
        now = datetime.now(timezone.utc)
        store.upsert_job(
            {
                "id": gone_id,
                "created_at": now,
                "client_id": gone_id,
                "metadata": "also-persist-me",
                "priority": -5,
                "workflow": {},
            }
        )
        for i in range(_MAX_JOB_SCAN):
            store.upsert_job(
                {
                    "id": str(uuid.uuid4()),
                    "created_at": now + timedelta(seconds=i + 1),
                    "client_id": "filler",
                    "metadata": None,
                    "priority": None,
                    "workflow": {},
                }
            )
    finally:
        store.close()

    run_b = tmp_path / "run_b"
    run_b.mkdir()
    stack2, cleanup2 = make_stack(run_b, state_dir=str(state_dir), comfyui_port=fake_comfyui)
    try:
        status, job2, raw = stack2.request("GET", f"/api/v2/jobs/{job_id}")
        assert status == 200, raw
        assert job2["status"] == "succeeded", "upstream still has it"
        assert job2["metadata"] == "persist-me"
        assert job2["priority"] == 3
        assert job2["created_at"] == created_at, "created_at must not be refabricated"

        # Known to SQLite, gone from ComfyUI => expired with its fields, not 404.
        status, job3, raw = stack2.request("GET", f"/api/v2/jobs/{gone_id}")
        assert status == 200, raw
        assert job3["status"] == "expired"
        assert job3["metadata"] == "also-persist-me"
        assert job3["priority"] == -5

        status, body, _ = stack2.request("GET", f"/api/v2/jobs/{uuid.uuid4()}")
        assert status == 404
        assert body["error"]["code"] == "not_found"
    finally:
        cleanup2()


def test_state_store_is_owner_only(tmp_path):
    """The DB holds the output-id HMAC secret — no group/other access."""
    import os
    import stat

    from comfy_api_proxy.persist import StateStore

    state_dir = tmp_path / "state"
    store = StateStore(state_dir / "state.sqlite3")
    try:
        dir_mode = stat.S_IMODE(os.stat(state_dir).st_mode)
        db_mode = stat.S_IMODE(os.stat(state_dir / "state.sqlite3").st_mode)
        assert dir_mode & (stat.S_IRWXG | stat.S_IRWXO) == 0, oct(dir_mode)
        assert db_mode & (stat.S_IRWXG | stat.S_IRWXO) == 0, oct(db_mode)
    finally:
        store.close()


async def test_list_jobs_scan_is_bounded():
    """A `status` filter must not walk every record a long-lived --state-dir
    accumulated: GET /jobs costs up to two upstream calls per candidate."""
    from datetime import datetime, timezone

    from comfy_api_proxy.app import _MAX_JOB_SCAN, Proxy

    calls = {"n": 0}

    class _Resp:
        status = 200
        content_type = "application/json"

        async def json(self):
            return {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

    class _Session:
        def get(self, url, params=None, headers=None):  # noqa: ANN001
            calls["n"] += 1
            return _Resp()

    class _Req:
        scheme = "http"
        host = "127.0.0.1:8189"
        headers: dict[str, str] = {}
        rel_url = type("_U", (), {"query": {"status": "queued"}})()

    proxy = Proxy("http://comfy")
    proxy._session = _Session()  # type: ignore[assignment]
    overflow = _MAX_JOB_SCAN * 2
    for i in range(overflow):
        proxy._jobs[f"{i:08d}-0000-0000-0000-000000000000"] = {
            "created_at": datetime.now(timezone.utc),
            "client_id": "c",
            "metadata": None,
            "priority": None,
        }

    resp = await proxy.list_jobs(_Req())  # type: ignore[arg-type]
    body = json.loads(resp.body)
    assert body["jobs"] == []
    assert body["truncated"] is True
    # Two upstream calls per scanned candidate, and never more than the cap.
    assert calls["n"] <= 2 * _MAX_JOB_SCAN
    assert calls["n"] < 2 * overflow


def test_server_argv_includes_state_dir():
    """Parse through the real parser — a hand-built Namespace goes stale the
    moment any other server flag is added."""
    import argparse

    from comfy_api_proxy.cli import _add_server_args, _server_argv

    parser = argparse.ArgumentParser()
    _add_server_args(parser)
    args = parser.parse_args(
        ["--comfyui", "http://127.0.0.1:8188", "--state-dir", "/tmp/proxy-state"]
    )
    argv = _server_argv(args)
    assert "--state-dir" in argv
    assert "/tmp/proxy-state" in argv
