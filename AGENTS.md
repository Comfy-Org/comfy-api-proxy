# AGENTS.md — comfy-api-proxy

`comfy-api-proxy` is a local Python/aiohttp service that puts the **Comfy API v2** (`/api/v2/*`) in front of a self-hosted ComfyUI, so client SDK code written against Comfy Cloud runs unchanged against a ComfyUI on the user's own machine. It wraps ComfyUI's native HTTP + WebSocket API one-to-one: submit / poll / cancel jobs, a Server-Sent-Events stream bridged from ComfyUI's `/ws`, and asset upload/download with local blake3 dedup and guarded model-directory placement. The repo is **public** and ships to PyPI as `comfy-api-proxy`.

## Commands

These are exactly the checks CI runs (`.github/workflows/ci.yml`). Run them from the repo root in a virtualenv:

```bash
pip install -e ".[dev]"      # package + dev tools
ruff check .                 # lint
ruff format --check .        # format check (drop --check to fix in place)
mypy src/comfy_api_proxy     # type-check (lenient — see [tool.mypy] in pyproject.toml)
pytest -v                    # unit + end-to-end tests (~130; spawns local servers)

# spec-drift check: regenerate the models and prove nothing changed
python3 scripts/generate_models.py
git diff --exit-code src/comfy_api_proxy/schemas/_generated.py
```

Run the proxy against an already-running ComfyUI (foreground, Ctrl+C to stop):

```bash
comfy-api-proxy --comfyui http://127.0.0.1:8188 --port 8189
```

No-GPU end-to-end demo (`run_demo.py` drives the proxy through the real Python SDK, which it imports from a sibling `comfy-python-sdk` checkout — clone that next to this repo first):

```bash
python demo/fake_comfyui.py &   # stand-in ComfyUI on :8188
comfy-api-proxy &               # the proxy on :8189
python demo/run_demo.py
```

CI jobs: `lint`, `typecheck`, `spec-drift`, `test` (Python 3.10 / 3.11 / 3.12 matrix), `build-check` (`python -m build` + `twine check`), plus a `zizmor` workflow audit.

## Layout

| Path | What's in it |
|---|---|
| `src/comfy_api_proxy/` | The package. `app.py` is the whole v2 HTTP surface (routes + handlers); `realtime.py` the ComfyUI-WebSocket→SSE bridge; `assets.py` the in-memory asset + hash index; `security.py` the upload-placement guards; `middleware.py` the origin guard; `auth.py` optional bearer-token auth; `cli.py` the argparse entry point; `service.py` background start/stop; `schemas/` the generated pydantic models. |
| `tests/` | pytest suite. `conftest.py` spawns the fake ComfyUI and the real proxy as subprocesses on free ports; `test_endpoints.py` / `test_smoke.py` drive them over plain HTTP. |
| `spec/` | Vendored, filtered copy of the canonical Comfy API v2 OpenAPI contract, plus a `VERSION` provenance pin. Generated — never hand-edit. |
| `scripts/` | `sync-spec.sh` (fetch + filter the upstream spec), `filter_openapi.py` (the redaction filter), `generate_models.py` (spec → pydantic models). |
| `demo/` | `fake_comfyui.py` (a GPU-free ComfyUI stand-in) and `run_demo.py` (SDK-driven demo). Human-facing; CI never runs `run_demo.py`. |
| `docs/` | `sync-workflow.md` — the proposed upstream-side spec-sync workflow. |
| `.github/workflows/` | `ci.yml`, `ci-audit-workflows.yml` (zizmor), `publish.yml` (PyPI), `cla.yml`. |

## Conventions & gotchas

- **This repo is public.** Never put an internal ticket id, internal repo slug, internal service/tool name, or private-spec text into any file, commit message, or PR. `tests/test_no_internal_leaks.py` enforces that with a pattern scan over `docs/`, `spec/`, `scripts/` **and every root-level `*.md` — including this file** — so a leak fails `pytest`, not review.
- **`spec/openapi.yaml`, `spec/VERSION`, and `src/comfy_api_proxy/schemas/_generated.py` are generated; never hand-edit them.** Contract changes flow upstream → here only: `scripts/sync-spec.sh <path-or-url> [sha]`, then `python3 scripts/generate_models.py`. CI's `spec-drift` job regenerates the models and fails the build on any diff, so a spec sync without a regeneration is caught immediately.
- **The generated models are test-only.** `tests/test_schema_conformance.py` validates real handler responses against them; nothing on the request-handling path imports them. Don't wire them into handlers.
- **The security defaults are the product, not a suggestion**: loopback-only bind (widening `--host` requires `--token` or `--allow-insecure-bind`), a default-on origin-check middleware ported from ComfyUI core, model uploads verified safetensors-only by parsing the file's own header, an allowlist of destination roots (`configs` and `custom_nodes` deliberately excluded), symlink-resolved traversal checks, and atomic `O_EXCL` writes. Any change to `security.py`, `middleware.py`, or `auth.py` ships with a test proving the guard still holds — see `tests/test_security.py`, `test_middleware.py`, `test_auth.py`, `test_upload_validation.py`.
- **Tests use the standard library only** — no SDK, no third-party HTTP client, nothing beyond localhost — so CI never depends on another repo or a credential. Keep it that way.
- **Don't bump `version` in `pyproject.toml`.** Publishing is tag-driven: `.github/workflows/publish.yml` injects the release tag (`vX.Y.Z`) at build time, so the committed value is a placeholder.
- **Workflows are zizmor-audited.** Pin every action to a full commit SHA with a trailing `# vX.Y.Z` comment, and keep `persist-credentials: false` on checkouts.
- **Commits and PRs:** conventional-commit subjects (`feat:`, `fix:`, `docs:`, `ci:`, `test:`, `chore:`), normally squash-merged with the PR number in the subject. `.github/CODEOWNERS` owns every path, so each PR needs an approving review from the code-owner teams.
- **Style:** Python 3.10+ (`from __future__ import annotations` at the top of every module but the package `__init__.py`), ruff with `line-length = 100` and the `E,F,I,UP,W` rule set, mypy deliberately lenient — not `--strict`, so full annotation coverage is not required.
- **CodeRabbit reviews this repo** using the per-path instructions in `.coderabbit.yaml` (`demo/` is held to a lower bar than `src/`; ruff findings are suppressed there because CI already runs them).

## Deeper docs

- `README.md` — user-facing overview, the full `/api/v2/` operation table, the CLI flag reference, the security defaults, and the known limitations (in-memory state, non-streaming uploads).
- `spec/README.md` — what "filtered" means, what the filter deliberately keeps, and the one-way sync rule.
- `docs/sync-workflow.md` — the upstream-side workflow that pushes spec changes down here.
