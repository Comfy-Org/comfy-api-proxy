# comfy-api-proxy

[![CI](https://github.com/Comfy-Org/comfy-api-proxy/actions/workflows/ci.yml/badge.svg)](https://github.com/Comfy-Org/comfy-api-proxy/actions/workflows/ci.yml)

A local service that puts the **Comfy API v2** in front of a self-hosted ComfyUI
instance. The same client SDK code that talks to Comfy Cloud can drive a
ComfyUI on your own machine instead — and, since this proxy is Python +
aiohttp (the same stack as ComfyUI core), the same adapter can later move
*into* core itself. That's the "one contract, three surfaces" idea: Comfy
Cloud, this proxy, and (eventually) ComfyUI core all speak the same `/api/v2/`
shape, so integrator code doesn't need to fork depending on where it's
pointed.

## Requirements & install

- Python **3.10+** (CI runs 3.10, 3.11, and 3.12 on every pull request).
- Install from PyPI:

  ```bash
  pip install comfy-api-proxy
  ```

## Quickstart

### Against a real ComfyUI

Point the proxy at an already-running ComfyUI and it serves `/api/v2/*` on
its own port. It runs in the foreground until you stop it with Ctrl+C:

```bash
# Defaults: proxy the ComfyUI at 127.0.0.1:8188, serve the v2 API on :8189.
comfy-api-proxy

# Point it elsewhere / co-locate with ComfyUI to enable model-directory uploads:
comfy-api-proxy --comfyui http://127.0.0.1:8188 --port 8189 \
  --comfyui-base-dir /path/to/ComfyUI

# Background it with your shell (& ) if you want your prompt back:
comfy-api-proxy &
```

### No-GPU demo

`demo/fake_comfyui.py` is a stand-in ComfyUI — just enough of the native
`/prompt` / `/history` / `/queue` / `/view` / `/ws` surface to run a workflow
end to end without a GPU (or ComfyUI installed at all):

```bash
python demo/fake_comfyui.py &          # a stand-in ComfyUI on :8188
comfy-api-proxy &                      # the proxy on :8189
python demo/run_demo.py                # submit → wait → download
```

`demo/run_demo.py` drives the proxy through the real Python SDK
(`comfy_sdk.Comfy`) — the same client code you'd point at Comfy Cloud — and
imports it from a sibling checkout, so clone the SDK repo (see
[SDKs and the API contract](#sdks-and-the-api-contract) below) next to this
one before running the demo:

```
some-parent-dir/
├── comfy-api-proxy/   (this repo)
└── ComfyPythonSDK/
```

CI never depends on that checkout being present: the test suite
(`tests/test_smoke.py`) drives the proxy's own HTTP surface directly with
only the standard library, so `pytest` works with nothing but this repo
installed.

## The `/api/v2/` surface

Wraps ComfyUI's native HTTP + WebSocket API one-to-one:

| v2 operation | Backed by |
|---|---|
| `POST /api/v2/jobs` | Resolves any `core/ASSET` reference in the workflow to the filename ComfyUI expects, then `POST /prompt` |
| `GET /api/v2/jobs/{id}` | `GET /history/{id}` (+ `/queue` while queued/running) — the authoritative, pollable state |
| `POST /api/v2/jobs/{id}/cancel` | ComfyUI's atomic `POST /api/jobs/{id}/cancel` |
| `GET /api/v2/jobs/{id}/events` | Server-Sent Events, driven by ComfyUI's `/ws` (the only live signal ComfyUI exposes) |
| `POST /api/v2/assets` | Multipart upload; blake3-hashed and deduped locally; routed to ComfyUI's `/upload/image` for workflow inputs, or placed directly in a model directory (see below) for model weights |
| `POST /api/v2/assets/from-hash`, `HEAD /api/v2/assets/by-hash/{hash}` | Local hash index |
| `GET /api/v2/assets/{id}`, `GET /api/v2/assets/{id}/content` | Local index / ComfyUI `/view`, Range-capable |

**Poll-first, same as the canonical contract:** `GET /api/v2/jobs/{id}` is
always the source of truth for a job's state; the SSE stream
(`GET /api/v2/jobs/{id}/events`) is a live convenience layered on top of
it — a client that never opens it still sees the same state by polling. See
[Live events (SSE)](#live-events-sse) below for what the stream carries and
its concurrent-connection limit.

### Partner (API) node auth — `extra_data`

Workflows that use partner/API nodes (Gemini, etc.) need a Comfy API key to
authenticate them. Pass it alongside the workflow on submit:

```jsonc
POST /api/v2/jobs
{
  "workflow": { /* API-format graph */ },
  "extra_data": { "api_key_comfy_org": "comfyui-…" }
}
```

`extra_data` is a closed, typed object — the only accepted field is
`api_key_comfy_org` (any other shape is rejected `400 invalid_request`). One key
authenticates every partner node in the workflow. The proxy forwards it verbatim
onto ComfyUI's `/prompt` call and **never stores or logs it** (ComfyUI likewise
strips it from history). Omit `extra_data` entirely when you have no key.

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

Once placed, the asset's `file_path` — and the value substituted for any
`core/ASSET` reference to it in a submitted workflow — is the filename
**relative to the model-root directory** (e.g. `my_model.safetensors`, not
`checkpoints/my_model.safetensors`). That matches how ComfyUI's own combo
widgets/loaders reference a model internally; a category-qualified path
would be rejected as an unknown filename.

### Live events (SSE)

`GET /api/v2/jobs/{id}/events` opens one WebSocket connection to ComfyUI,
performs its `feature_flags` handshake, and translates the native
`progress`/`progress_state`/preview/terminal messages into the v2 SSE event
catalog (`status`, `progress`, `preview`, `output`), throttled to ~2
progress/preview events per second. If ComfyUI's WebSocket is unreachable,
the stream falls back to polling `/history` so it still resolves to an
authoritative terminal `status` rather than failing outright.

A proxy instance also caps concurrent event streams (8 by default, since
each one holds open a ComfyUI WebSocket connection). Past that limit, a new
stream request gets `429 too_many_streams` with a `Retry-After` hint instead
of queuing or degrading — `GET /api/v2/jobs/{id}` polling is always
available regardless, and a slot frees up as soon as the stream it belongs
to ends (the job finishes, or the client disconnects).

### Security defaults

Everything here is on by default — no flags needed to get to the safe
configuration:

- **Binds to `127.0.0.1` only.** Widening `--host` to a non-loopback address
  is refused (the process exits with an error) unless `--token` is set, or
  `--allow-insecure-bind` is passed to explicitly opt out of that guard.
- **An optional static bearer token** (`--token`) gates all of `/api/v2/*`
  when configured; unset by default, since a self-hosted single-user
  ComfyUI usually has nothing to authenticate against.
- **A default-on origin-check middleware** — ported from ComfyUI core's own
  `create_origin_only_middleware` — rejects cross-site browser requests even
  when nothing else is configured, closing the DNS-rebinding / drive-by-CSRF
  hole any unauthenticated localhost server is exposed to.
- **Model-file uploads are safetensors-only, with path-traversal and
  symlink-escape guards** (see *Model-file uploads* above) — and are
  rejected outright unless the proxy was started co-located with
  `--comfyui-base-dir`.

## CLI reference

```bash
comfy-api-proxy --comfyui http://127.0.0.1:8188 --port 8189 [options]
```

| Flag | Default | What it does |
|---|---|---|
| `--comfyui` | `http://127.0.0.1:8188` | Base URL of the self-hosted ComfyUI to proxy. |
| `--host` | `127.0.0.1` | Address to bind. Widening past loopback requires `--token` or `--allow-insecure-bind` (see [Security defaults](#security-defaults)). |
| `--port` | `8189` | Port to serve the v2 API on. |
| `--token` | *(unset)* | Require `Authorization: Bearer <token>` on every `/api/v2/*` request. |
| `--comfyui-base-dir` | *(unset)* | Filesystem root of a co-located ComfyUI install. Required to enable direct model-directory placement of model-file uploads; without it, model uploads are rejected (workflow-input uploads still work). |
| `--max-upload-mb` | `100` | Max single-request upload size, in MB. |
| `--allow-insecure-bind` | `false` | Permit binding a non-loopback `--host` without a `--token`. Unsafe — exposes an unauthenticated proxy to the network. |

## SDKs and the API contract

Any real integration — and `demo/run_demo.py` — uses the same client SDKs
Comfy Cloud users use, just pointed at this proxy's `--host:--port` instead
of `api.comfy.org`:

- [ComfyPythonSDK](https://github.com/Comfy-Org/ComfyPythonSDK)
- [ComfyTypeScriptSDK](https://github.com/Comfy-Org/ComfyTypeScriptSDK)

`spec/openapi.yaml` in this repo is a synced, filtered copy of that same
Comfy API v2 contract — see `spec/README.md` for what "filtered" means, and
[Development](#development) below for how it's kept in sync.

## Development

```bash
pip install -e ".[dev]"
ruff check .            # lint
ruff format --check .   # format check
mypy src/comfy_api_proxy   # type-check (lenient - see pyproject.toml)
pytest -v                # unit + end-to-end tests
python3 scripts/generate_models.py && git diff --exit-code src/comfy_api_proxy/schemas/_generated.py
                          # spec-drift check (also runs in CI)
```

These are exactly the checks CI runs (`.github/workflows/ci.yml`), each as
its own job — lint/format, type-check, spec-drift, and test — with the test
job running across Python 3.10, 3.11, and 3.12.

`tests/test_smoke.py` and `tests/test_endpoints.py` start the fake ComfyUI
stand-in and the real proxy as subprocesses and drive both over plain HTTP
(standard library only — no SDK, no third-party client, no dependency on
another repo's credentials), covering upload → core/ASSET-reference →
run → download, cancel, from-hash/by-hash, the SSE stream (including its
concurrent-stream cap), and the model-placement security guards.

### Keeping `spec/openapi.yaml` in sync

The vendored spec is **generated, one-way (upstream → here), and never
hand-edited**. `scripts/sync-spec.sh` fetches the canonical spec from
wherever it's passed (a local path or a URL), runs it through
`scripts/filter_openapi.py` — which strips anything internal-only before a
byte lands in this public repo — and writes the result to `spec/openapi.yaml`
plus a `spec/VERSION` provenance pin. `scripts/generate_models.py` then
regenerates the pydantic models (`src/comfy_api_proxy/schemas/_generated.py`)
that `tests/test_schema_conformance.py` validates real handler responses
against — those models are used only in tests, never on the
request-handling hot path. CI's `spec-drift` job re-runs the generator and
fails the build if the checked-in models don't match, so a spec sync without
a regeneration gets caught immediately instead of drifting silently. See
`spec/README.md` and `docs/sync-workflow.md` for the full design.

## Scope

Implemented: submit (with `core/ASSET` resolution), poll, cancel, live SSE
events, asset upload/download, from-hash/by-hash dedup, and guarded
model-directory placement.

Known limitations:

- **State is in-memory only.** The asset index and job store live in memory (as
  does ComfyUI's own history), so all state is lost on restart. A job id or asset
  id issued before a restart no longer resolves afterward — this includes the
  HMAC-signed stateless output ids, since the signing secret is regenerated per
  process. A client that persists an id across a proxy restart must expect a 404;
  a durable store is a follow-up.
- **`Idempotency-Key` dedup is in-memory.** A reused key is rejected
  (`422 idempotency_key_reuse` — keys are single-use, no replay), but the claim
  set lives in the process and is cleared on restart, so a key reused across a
  proxy restart is not detected. A durable store is a follow-up.
- **Uploads are not zero-copy.** Large uploads are read fully into memory / a
  temp file rather than true streaming.
