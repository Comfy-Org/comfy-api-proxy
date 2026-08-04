# Batch workloads (topology, durability, priority, cancel)

Decisions for multi-day / headless use (GitHub [#18](https://github.com/Comfy-Org/comfy-api-proxy/issues/18)).

## Topology

**One proxy process ↔ one ComfyUI instance.** Multi-machine / multi-GPU
dispatch, eligibility, and leases belong to the caller's scheduler (or Comfy
Cloud). Four ComfyUI instances ⇒ four proxies (each with its own `--comfyui`,
`--port`, and ideally `--state-dir`).

## Persistence (`--state-dir`)

Without `--state-dir`, job records, `Idempotency-Key` claims, the asset index,
and the output-id signing secret are process-local.

With `--state-dir`, those proxy records write through to SQLite and reload on
startup. That does **not** make ComfyUI `/history` or on-disk outputs durable —
missing upstream bytes surface as `404 output_unavailable`. Each `--state-dir`
is local to one proxy↔ComfyUI pair.

## Advisory priority

`POST /api/v2/jobs` accepts optional integer `priority`
(−1_000_000…1_000_000). Stored and echoed only; backends may ignore. This
proxy never maps it to ComfyUI's `front: true` stack push.

## Cancellation

`POST /api/v2/jobs/{id}/cancel` → ComfyUI `POST /api/jobs/{id}/cancel`. Use the
existing Python SDK helper: `job.cancel()` / `await job.cancel()`
([comfy-python-sdk](https://github.com/Comfy-Org/comfy-python-sdk)). Cancel is a
request; poll `GET /jobs/{id}` for terminal state.

## Proxy-local extensions

Not Cloud OpenAPI parity (`spec/openapi.yaml` is one-way from upstream):

| Extension | Notes |
|---|---|
| `GET /api/v2/health` | Process probe; does not call ComfyUI; unauthenticated |
| `GET /api/v2/jobs` | Jobs this proxy recorded |
| `metadata` / `priority` | Opaque ≤1 KiB string; advisory int |
| `outputs_reused` | `true` when history has `execution_cached` |
| `POST /api/v2/assets/from-path` | Register a host file under `--comfyui-base-dir` |
| `output_unavailable` | Typed 404 when output bytes are gone |
