# Migrating to ComfyUI core

ComfyUI core serves the Comfy API v2 (`/api/v2/*`) natively when it is started
with `--enable-assets`. No separate proxy process is needed.

```bash
python main.py --enable-assets
```

`--windows-standalone-build` also turns the assets system, and with it
`/api/v2`, on by default. `--disable-assets` turns it off again.

Then point your SDK at ComfyUI itself:

```bash
export COMFY_BASE_URL=http://127.0.0.1:8188   # or your ComfyUI --listen / --port
```

## Flag mapping

| comfy-api-proxy | ComfyUI core |
|---|---|
| `--comfyui` | n/a, the API runs in the same process as ComfyUI. |
| `--host` / `--port` | `--listen` / `--port` (default `127.0.0.1:8188`). |
| `--token` | No equivalent. Keep ComfyUI on loopback or behind your own auth proxy. |
| `--comfyui-base-dir` | n/a, ComfyUI places model uploads into its own model folders by `file_path` prefix (see *Uploads*). |
| `--max-upload-mb` | `--max-upload-size`. |
| `--state-dir` | None. State is in memory and lost on restart. |
| `--enable-cors-header` | Same flag name in ComfyUI. |

## Behaviour changes

- `metadata` is an object map, not a string.
- `GET /api/v2/jobs` paginates with `cursor` / `next_cursor`. The default and
  maximum `limit` is 500.
- A missing content file answers `404 not_found` instead of `output_unavailable`.
- `upstream_unreachable` (503) no longer occurs, since there is no upstream to
  reach. `upstream_error` remains: core answers `500 upstream_error` when
  enqueueing a job or registering an upload fails (the proxy used `502` for
  some of its cases).
- There is no bearer `--token`, no `--state-dir` and no HMAC-signed output ids.
- Output ids are asset UUIDs.
- `GET /api/v2/health` has no `upstream` field.
- `POST /api/v2/assets/from-path` answers `501 not_implemented`. Core does not
  register host files.
- The URL is ComfyUI's own `--listen` / `--port`, default `127.0.0.1:8188`.

## Endpoint parity

All 14 proxy endpoints exist in ComfyUI core:

| Endpoint | Notes |
|---|---|
| `GET /api/v2/health` | Works. |
| `POST /api/v2/jobs` | |
| `GET /api/v2/jobs` | Cursor pagination, see above. |
| `GET /api/v2/jobs/{id}` | |
| `GET /api/v2/jobs/{id}/workflow` | |
| `POST /api/v2/jobs/{id}/cancel` | |
| `GET /api/v2/jobs/{id}/events` | |
| `POST /api/v2/assets` | Upload. |
| `POST /api/v2/assets/from-hash` | For bytes ComfyUI already indexed. Needs `--enable-asset-hashing`, see *Uploads*. |
| `POST /api/v2/assets/from-path` | Exists, but answers HTTP 501 `not_implemented`. Use `POST /api/v2/assets` (upload) or `POST /api/v2/assets/from-hash` instead. |
| `HEAD /api/v2/assets/by-hash/{hash}` | |
| `GET /api/v2/assets/{id}` | |
| `GET /api/v2/assets/{id}/content` | |
| `DELETE /api/v2/assets/{id}` | |

## Uploads

`POST /api/v2/assets` goes through ComfyUI's asset service.

- Files are stored as `<blake3 hash><ext>`, not under the requested name. The
  asset name is the last segment of `file_path`; subfolders are not kept.
- The leading folder of `file_path` picks the destination: `input/`, `output/`,
  `models/<folder>/` or `<folder>/` for a ComfyUI model folder (for example
  `checkpoints` or `loras`) goes to that folder. A bare file name (or
  `models/<name>`) names no folder, so the routing tags decide: `input`,
  `output`, or `models` + `model_type:<folder>`. With no routing tag it goes to
  input. Any other folder, an absolute path, or an empty, `.` or `..` segment
  answers `422`.
- A `file_path` that names a folder plus tags that name a different destination
  answers `422`.
- Reference uploaded assets in workflows by asset id (a `core/ASSET` input
  value); ComfyUI substitutes the loader value. If you reference a file by name
  instead, files in output need the `[output]` annotation (`<name> [output]`).
- Model uploads are not limited to `.safetensors`, unlike the proxy.
- Deduplication needs ComfyUI started with `--enable-asset-hashing`. Then
  identical bytes already in the same destination folder are reused: the upload
  answers `200` with `created_new: false`. Identical bytes in a different folder
  are stored again in the requested one. `from-hash` and `HEAD by-hash` find
  indexed bytes. Without the flag every upload is stored,
  and `from-hash` and `HEAD by-hash` always answer `404`, so SDKs upload normally.

## Things to know

- **No restart persistence.** Job labels (`metadata`, `priority`), job records
  and idempotency keys live in memory and are lost when ComfyUI restarts.
- **No tenant isolation.** The `comfy-user` header is not authentication.
  v2 only separates labels, job listing, idempotency keys and event-stream caps
  per user. History, queue, `/view` and assets remain shared.
- **No token gate.** Starting ComfyUI with `--listen 0.0.0.0` exposes v2
  unauthenticated, like the rest of the ComfyUI API.
- **Indexed files are downloadable.** With assets enabled,
  `GET /api/v2/assets/{id}/content` serves any indexed file, including model
  files. The proxy only served files it had placed or registered itself.
- **Delete removes the reference only.** `DELETE /api/v2/assets/{id}` leaves the
  bytes on disk.
