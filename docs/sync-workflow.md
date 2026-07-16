# Proposed cloud-side sync workflow

This repo (`comfy-api-proxy`) cannot push a workflow into the `cloud`
monorepo — that repo isn't checked out here. This document is the
ready-to-drop workflow file for whoever has write access to `cloud`
(`Comfy-Org/cloud`) to add it there, plus the reasoning behind its shape.

It is modeled directly on `cloud`'s existing
`.github/workflows/push-ingest-types-to-frontend.yml`, which already does
this exact "push, filter, PR" dance for the frontend's TypeScript types
whenever the ingest OpenAPI spec changes. Same trigger shape, same
fixed-branch-plus-concurrency-group pattern, same "filter before it ever
touches the public repo" security posture — just pointed at a different
spec file, a different target repo, and a different filter (this repo's
own `scripts/filter_openapi.py`, not `cloud`'s Go `openapi-project` tool).

## Where to add it

Drop the file below at `cloud/.github/workflows/push-comfy-api-v2-to-proxy.yml`.

## Design notes

- **Reuses this repo's filter, doesn't reimplement it.** Rather than
  porting `scripts/filter_openapi.py`'s logic into `cloud` (which would
  create a second copy of the filtering rule that could drift from this
  one), the workflow checks out `comfy-api-proxy` and calls
  `scripts/sync-spec.sh` from *this* repo against `cloud`'s spec file.
  There is exactly one place the filter lives, and it lives in the public
  repo — the side that actually has to live with a filtering bug.
- **Fixed branch, single open PR at a time** — same as the ingest-types
  workflow, so repeated cloud commits before someone reviews just update
  the one PR instead of piling up duplicates.
- **Regenerates the pydantic models in the same job**, so the PR always
  has spec + models in sync — never a PR that updates one and not the
  other.
- **Trigger paths** are the cloud-side spec file and the projection logic
  itself (so a filter-behavior change also re-triggers a sync even if the
  spec bytes didn't move).

```yaml
# When the Comfy API v2 canonical spec changes, sync a filtered copy (and
# the models generated from it) into the public comfy-api-proxy repo via a
# pull request.
#
# This is the "push" model, matching push-ingest-types-to-frontend.yml:
# cloud pushes to comfy-api-proxy; comfy-api-proxy never clones cloud.
#
# Security boundary: the spec is NOT copied verbatim. comfy-api-proxy's own
# scripts/filter_openapi.py (checked out from that repo, not duplicated
# here) strips any operation tagged `internal` / `x-internal: true` and the
# components that existed only to support it, before the filtered file
# ever lands in the public repo. See comfy-api-proxy's spec/README.md.
#
# Uses a fixed branch name so only one sync PR is ever open at a time.
name: 'Push Comfy API v2 spec to comfy-api-proxy'

on:
  push:
    branches: [main]
    paths:
      - 'api/v2/openapi.yaml'
      - '.github/workflows/push-comfy-api-v2-to-proxy.yml'

  workflow_dispatch:

concurrency:
  group: push-comfy-api-v2-to-proxy
  cancel-in-progress: true

jobs:
  push-comfy-api-v2:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - name: Checkout cloud repo (for the canonical spec)
        uses: actions/checkout@v6
        with:
          path: cloud
          persist-credentials: false

      - name: Checkout comfy-api-proxy repo
        uses: actions/checkout@v6
        with:
          repository: Comfy-Org/comfy-api-proxy
          token: ${{ secrets.PR_GH_TOKEN }}
          path: comfy-api-proxy
          persist-credentials: false

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.12'
          cache: pip

      - name: Install comfy-api-proxy (+ dev deps, for model regeneration)
        working-directory: comfy-api-proxy
        run: pip install -e ".[dev]"

      - name: Get cloud commit info
        id: cloud-info
        working-directory: cloud
        run: echo "commit=$(git rev-parse --short HEAD)" >> "$GITHUB_OUTPUT"

      # Filter + vendor the spec using comfy-api-proxy's own script (see
      # "Reuses this repo's filter, doesn't reimplement it" above). Any
      # internal operation / x-internal / orphaned-component stripping and
      # the leak self-check all happen inside this one call.
      - name: Sync + filter the spec into comfy-api-proxy
        working-directory: comfy-api-proxy
        run: |
          ./scripts/sync-spec.sh \
            "$GITHUB_WORKSPACE/cloud/api/v2/openapi.yaml" \
            "$(git -C "$GITHUB_WORKSPACE/cloud" rev-parse HEAD)"

      - name: Regenerate pydantic models from the synced spec
        working-directory: comfy-api-proxy
        run: python3 scripts/generate_models.py

      - name: Validate generated files
        working-directory: comfy-api-proxy
        run: |
          for file in spec/openapi.yaml spec/VERSION src/comfy_api_proxy/schemas/_generated.py; do
            if [ ! -s "$file" ]; then
              echo "Error: $file is missing or empty."
              exit 1
            fi
          done

      - name: Create Pull Request on comfy-api-proxy
        uses: peter-evans/create-pull-request@5f6978faf089d4d20b00c7766989d076bb2fc7f1 # v8.1.1
        with:
          token: ${{ secrets.PR_GH_TOKEN }}
          path: comfy-api-proxy
          commit-message: '[chore] Sync Comfy API v2 spec from cloud@${{ steps.cloud-info.outputs.commit }}'
          title: '[chore] Sync Comfy API v2 spec from cloud@${{ steps.cloud-info.outputs.commit }}'
          body: |
            ## Automated spec sync

            This PR updates the filtered Comfy API v2 spec and the pydantic
            models generated from it.

            - Source: `cloud@${{ steps.cloud-info.outputs.commit }}`, `api/v2/openapi.yaml`
            - Filtered by `scripts/filter_openapi.py` (strips internal-only
              operations and the components that existed only to support
              them; see `spec/README.md`)
            - Models regenerated by `scripts/generate_models.py`

            Review the diff in `spec/openapi.yaml` before merging — this is
            the one place a filtering regression would first be visible.
          branch: sync-comfy-api-v2-spec
          base: main
          delete-branch: true
          add-paths: |
            spec/openapi.yaml
            spec/VERSION
            src/comfy_api_proxy/schemas/_generated.py
```

## What this does NOT cover (open follow-ups, for whoever picks this up in `cloud`)

- **`PR_GH_TOKEN`** needs repo-write scope on `Comfy-Org/comfy-api-proxy`
  specifically — confirm the existing secret used by
  `push-ingest-types-to-frontend.yml` is already scoped that broadly, or a
  new token/App installation is needed.
- **No reverse guard job** (the ingest-types workflow's second grep pass
  over the *generated TypeScript*, since a Go/TS codegen step could
  theoretically re-embed a literal internal path in a schema example).
  `filter_openapi.py`'s own leak check runs before the file is even
  written, so the equivalent protection already exists earlier in this
  pipeline — call this out in cloud-side review as the reason there's no
  separate grep step here, rather than a gap.
- **This file is not wired up in the cloud repo** — implementing this
  requires cloud repo write access, which this proxy-repo PR does not
  have. Whoever integrates this should add it as
  `cloud/.github/workflows/push-comfy-api-v2-to-proxy.yml`.
