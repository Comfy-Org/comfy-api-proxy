# comfy-api-proxy

[![CI](https://github.com/Comfy-Org/comfy-api-proxy/actions/workflows/ci.yml/badge.svg)](https://github.com/Comfy-Org/comfy-api-proxy/actions/workflows/ci.yml)

A local service that puts the **Comfy API v2** in front of a self-hosted ComfyUI
instance, so the same SDK code that talks to Comfy Cloud also drives a ComfyUI on
your own machine.

Python + aiohttp — the same stack as ComfyUI core, so the adapter can later move
into core itself.

## Run

```bash
pip install -e .
comfy-api-proxy --comfyui http://127.0.0.1:8188 --port 8189
```

Binds to `127.0.0.1` only by default.

## Demo (no GPU needed)

```bash
python demo/fake_comfyui.py &          # a stand-in ComfyUI on :8188
comfy-api-proxy &                      # the proxy on :8189
python demo/run_demo.py                # submit → wait → download
```

## Scope

First-iteration slice: submit a workflow, poll job status, download outputs.
Not yet here: file upload, the live-progress stream, idempotency, a durable job
store, and the resilient WebSocket client for progress. This slice serves job
status by plain polling.

## Contributing

```bash
pip install -e ".[dev]"
ruff check .            # lint
ruff format --check .   # format check
mypy src/comfy_api_proxy demo tests   # type-check (lenient - see pyproject.toml)
pytest -v                # unit + end-to-end smoke test
```

The smoke test (`tests/test_smoke.py`) starts the fake ComfyUI stand-in and the
real proxy, then drives both with the real demo client — the same check CI runs
on every pull request, across Python 3.10, 3.11, and 3.12.
