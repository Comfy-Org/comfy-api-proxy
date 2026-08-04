# Topology & deployment expectations

This document is the explicit multi-backend / durability boundary for
`comfy-api-proxy` (GitHub [#18](https://github.com/Comfy-Org/comfy-api-proxy/issues/18)
items 1 and 8).

## Supported topology

**One proxy process ↔ one ComfyUI instance.**

| Role | Ownership |
|---|---|
| Single ComfyUI base URL (`--comfyui`) | This proxy |
| Eligibility (which GPU / VRAM / model lane) | Caller's scheduler |
| Multi-machine / multi-GPU dispatch | Caller's scheduler (or Comfy Cloud) |
| Lease heartbeats, attempt caps, claim ordering | Caller's scheduler |

Running four ComfyUI instances on one box means running **four proxies**,
each with its own `--comfyui`, `--port`, and ideally its own `--state-dir`.
A single proxy does **not** load-balance or route across backends.

The SDKs likewise speak to one base URL. Point them at Cloud when you want
a multi-backend surface; point them at this proxy when you want a local
single-instance surface that matches the same `/api/v2/` shape.

## Persistence (`--state-dir`)

Without `--state-dir`, job records, `Idempotency-Key` claims, the asset
index, and the output-id signing secret are **process-local** (lost on
restart). That is fine for short local development.

With `--state-dir /path/to/dir`, the proxy write-throughs those records to
SQLite (`state.sqlite3` under that directory) and reloads them on startup.

What durability **does** mean:

- After a proxy restart, `GET /api/v2/jobs/{id}` still knows jobs this
  proxy previously accepted (and can reconcile live status from ComfyUI).
- Reusing an `Idempotency-Key` across a proxy restart is still rejected.
- Uploaded / registered assets remain resolvable by id and blake3 hash.
- HMAC-signed output asset ids minted before the restart still verify.

What durability **does not** mean (and is never claimed):

- ComfyUI's own `/history` ring and on-disk output files. If ComfyUI
  restarts, evicts history, or the output file is deleted, those facts
  remain ComfyUI's — the proxy cannot invent missing history or bytes.
- Cross-proxy sharing. Each `--state-dir` belongs to one proxy↔ComfyUI pair.

For multi-day batch workloads, **always** pass `--state-dir` and treat
ComfyUI's history / output retention as a separate operational concern.

## Proxy-local API extensions

These exist on the proxy today for self-hosted operators. They are **not**
guaranteed Cloud-contract parity until they land in the upstream OpenAPI
sync (the vendored `spec/openapi.yaml` is one-way from upstream and is not
hand-edited here):

| Extension | Notes |
|---|---|
| `GET /api/v2/health` | Cheap process probe; does not call ComfyUI. Unauthenticated even when `--token` is set. |
| `GET /api/v2/jobs` | Lists jobs this proxy recorded; optional `status` / `limit`. |
| `metadata` / `priority` on submit | Opaque string (≤1 KiB UTF-8) and advisory int; echoed on the job. |
| `outputs_reused` on the job | `true` when ComfyUI history includes `execution_cached`. |
| `POST /api/v2/assets/from-path` | Zero-copy register of a host file under `--comfyui-base-dir`. |
| `output_unavailable` (404) | Typed download-path error when output bytes are gone. |

See also [advisory-priority.md](./advisory-priority.md) and
[cancellation.md](./cancellation.md).
