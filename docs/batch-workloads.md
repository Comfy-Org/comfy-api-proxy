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

With `--state-dir`, those **proxy-layer** records write through to SQLite and
reload on startup. Each `--state-dir` is local to one proxy↔ComfyUI pair.

The directory is created `0700` and the database `0600`: it stores the HMAC
secret that signs output asset ids, so any local user who could read it could
mint valid ids.

Only the newest 500 job records are reloaded into memory at startup; older rows
stay on disk, and `GET /jobs/{id}` reads through to SQLite for them, so their
`metadata`, `priority`, and `created_at` survive a restart. `GET /jobs` lists
only the in-memory window. That window is much shorter than ComfyUI's history
ring (`MAXIMUM_HISTORY_SIZE = 10000`, days at a typical rate), which is why the
read-through matters: jobs in between still resolve upstream.

**The directory grows without bound.** Every accepted submit stores its full
workflow graph, which nothing reads back (kept for forensics) and nothing
prunes — order 10 MB/day per proxy at ~1 000 jobs/day with a large graph. Age
out the state file on your own schedule if you run one proxy for months.

This is separate from ComfyUI's own SQLite (`--database-url`, used by the
optional `--enable-assets` catalog of models/files/tags). ComfyUI does not
persist the v2 job queue, history, or `Idempotency-Key` mappings across
process restart — `PromptQueue.history` stays in memory — so the proxy cannot
delegate those concerns upstream today. Enabling ComfyUI's asset DB does
**not** replace `--state-dir`.

`--state-dir` also does **not** make ComfyUI `/history` or on-disk outputs
durable — missing upstream bytes surface as `404 output_unavailable`.

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
| `GET /api/v2/jobs` | Jobs this proxy recorded. Resolves each candidate against ComfyUI, so the walk stops after 500 records; `truncated: true` means it stopped early rather than running out of matches |
| `metadata` / `priority` | Opaque ≤1 KiB string; advisory int. Proxy-local — neither is forwarded, so ComfyUI's own `/queue` shows these jobs unlabeled. Point dashboards at `GET /api/v2/jobs` for attribution |
| `outputs_reused` | `true` when `execution_cached` names at least one node. ComfyUI emits that message on every run, empty when it cached nothing, so presence alone does not mean reuse. A cached job still lists the *original* run's outputs, which may point at bytes that are already gone (`404 output_unavailable`) |
| `POST /api/v2/assets/from-path` | Register a host file under `--comfyui-base-dir` |
| `output_unavailable` | Typed 404 when output bytes are gone |
