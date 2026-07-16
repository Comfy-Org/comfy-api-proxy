# comfy-api-proxy

[![CI](https://github.com/Comfy-Org/comfy-api-proxy/actions/workflows/ci.yml/badge.svg)](https://github.com/Comfy-Org/comfy-api-proxy/actions/workflows/ci.yml)

A local service that puts the **Comfy API v2** in front of a self-hosted ComfyUI
instance, so the same SDK code that talks to Comfy Cloud also drives a ComfyUI on
your own machine.

Python + aiohttp — the same stack as ComfyUI core, so the adapter can later move
into core itself.

## Capabilities

The full `/api/v2/` surface, wrapping ComfyUI's native HTTP + WebSocket API:

| v2 operation | Backed by |
|---|---|
| `POST /api/v2/jobs` | Resolves any `core/ASSET` reference in the workflow to the filename ComfyUI expects, then `POST /prompt` |
| `GET /api/v2/jobs/{id}` | `GET /history/{id}` (+ `/queue` while queued/running) — the authoritative, pollable state |
| `POST /api/v2/jobs/{id}/cancel` | ComfyUI's atomic `POST /api/jobs/{id}/cancel` |
| `GET /api/v2/jobs/{id}/events` | Server-Sent Events, driven by ComfyUI's `/ws` (the only live signal ComfyUI exposes) |
| `POST /api/v2/assets` | Multipart upload; blake3-hashed and deduped locally; routed to ComfyUI's `/upload/image` for workflow inputs, or placed directly in a model directory (see below) for model weights |
| `POST /api/v2/assets/from-hash`, `HEAD /api/v2/assets/by-hash/{hash}` | Local hash index |
| `GET /api/v2/assets/{id}`, `GET /api/v2/assets/{id}/content` | Local index / ComfyUI `/view`, Range-capable |

Poll-first, same as the canonical contract: `GET /api/v2/jobs/{id}` is always
the source of truth; the SSE stream is a live convenience on top of it.

### Model-file uploads (`checkpoints/`, `loras/`, `vae/`, ...)

ComfyUI's own `/upload/image` only understands `input`/`output`/`temp` — it
has no endpoint for placing a file into a model directory. This proxy can do
that itself, but only when it is **co-located** with ComfyUI (same host,
sharing a filesystem) and started with `--comfyui-base-dir` pointing at the
ComfyUI install root. Without that flag, model-directory uploads are rejected
with a clear error; workflow-input uploads (images, etc.) work either way.

When enabled, a model upload must clear all of the following before a byte
touches disk:

- **safetensors-only**, verified by parsing the file's own header (the
  length-prefixed JSON tensor index) — never a pickle/`torch.load` path.
- **Allowlisted destination roots only** — the real ComfyUI model
  directories (`checkpoints`, `loras`, `vae`, `controlnet`, ...). `configs`
  and `custom_nodes` are deliberately excluded even though ComfyUI itself
  has directories by those names, since one holds arbitrary YAML and the
  other arbitrary Python.
- **No path traversal, including through a symlink** — the resolved,
  real (symlink-followed) destination path must still land inside the
  configured model directory.
- **Atomic, no-clobber writes** — a temp file plus `O_EXCL` on the final
  destination, so two uploads can never race into a torn or silently
  overwritten file.

### Live events (SSE)

`GET /api/v2/jobs/{id}/events` opens one WebSocket connection to ComfyUI,
performs its `feature_flags` handshake, and translates the native
`progress`/`progress_state`/preview/terminal messages into the v2 SSE event
catalog (`status`, `progress`, `preview`, `output`). If ComfyUI's WebSocket
is unreachable, the stream falls back to polling `/history` so it still
resolves to an authoritative terminal `status` rather than failing outright.

### Security defaults

- Binds to `127.0.0.1` only by default. Widening `--host` to a non-loopback
  address is refused unless `--token` is set (or `--allow-insecure-bind` is
  passed to explicitly opt out of that guard).
- A default-on origin-check middleware — ported from ComfyUI core's own
  `create_origin_only_middleware` — rejects cross-site browser requests
  even when nothing else is configured.
- An optional static bearer token (`--token`) gates all of `/api/v2/*`.

## Run

```bash
pip install -e .
comfy-api-proxy --comfyui http://127.0.0.1:8188 --port 8189

# co-located with ComfyUI, to also enable model-directory uploads:
comfy-api-proxy --comfyui http://127.0.0.1:8188 --port 8189 \
  --comfyui-base-dir /path/to/ComfyUI
```

## Demo (no GPU needed)

```bash
python demo/fake_comfyui.py &          # a stand-in ComfyUI on :8188
comfy-api-proxy &                      # the proxy on :8189
python demo/run_demo.py                # submit → wait → download
```

## The vendored API contract (`spec/`)

`spec/openapi.yaml` is a synced, filtered copy of the canonical Comfy API v2
contract that lives in the Comfy Org `cloud` monorepo — filtered because that
source repo is private and this one is public. It flows **one way**
(cloud → here) via a sync workflow; never hand-edit it. See `spec/README.md`
for what "filtered" means, `docs/sync-workflow.md` for the sync design, and
`scripts/sync-spec.sh` / `scripts/generate_models.py` for the tooling that
performs it. Pydantic models generated from the spec live at
`src/comfy_api_proxy/schemas/_generated.py` and are used only in tests (see
`tests/test_schema_conformance.py`) to check real handler responses against
the contract — never on the request-handling hot path.

## Contributing

```bash
pip install -e ".[dev]"
ruff check .            # lint
ruff format --check .   # format check
mypy src/comfy_api_proxy demo tests   # type-check (lenient - see pyproject.toml)
pytest -v                # unit + end-to-end tests
python3 scripts/generate_models.py && git diff --exit-code src/comfy_api_proxy/schemas/_generated.py
                          # spec-drift check (also runs in CI)
```

`tests/test_smoke.py` and `tests/test_endpoints.py` start the fake ComfyUI
stand-in and the real proxy as subprocesses and drive both over plain HTTP
(standard library only — no SDK, no third-party client, no dependency on
another repo's credentials), covering upload → core/ASSET-reference →
run → download, cancel, from-hash/by-hash, and the SSE stream. The same
checks CI runs on every pull request, across Python 3.10, 3.11, and 3.12.

## Scope

Implemented: submit (with `core/ASSET` resolution), poll, cancel, live SSE
events, asset upload/download, from-hash/by-hash dedup, and guarded
model-directory placement.

Known limitations: the asset index and job store are in-memory only (lost on
restart, same as ComfyUI's own history); the `Idempotency-Key` header
documented in the contract is not yet implemented (a retried request is not
deduped or replayed — it submits again); large uploads are read fully into
memory/a temp file rather than true zero-copy streaming.
