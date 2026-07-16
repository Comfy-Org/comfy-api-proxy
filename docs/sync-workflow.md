# Proposed upstream sync workflow

This repo (`comfy-api-proxy`) cannot push a workflow into the private
upstream backend repo that holds the canonical spec — that repo isn't
checked out here. This document is the ready-to-drop workflow file for
whoever has write access to that repo to add it there, plus the reasoning
behind its shape.

It is modeled on an existing sync pattern already used internally for
other generated, cross-repo artifacts derived from the same upstream
source — same trigger shape, same fixed-branch-plus-concurrency-group
pattern, same "filter before it ever touches the public repo" security
posture — just pointed at this repo's spec file, this target repo, and
this repo's own filter (`scripts/filter_openapi.py`), rather than
reimplementing the filtering logic on the upstream side.

## Where to add it

Drop the file below at
`<upstream-repo>/.github/workflows/push-comfy-api-v2-to-proxy.yml`.

## Design notes

- **Reuses this repo's filter, doesn't reimplement it.** Rather than
  porting `scripts/filter_openapi.py`'s logic into the upstream repo
  (which would create a second copy of the filtering rule that could
  drift from this one), the workflow checks out `comfy-api-proxy` and
  calls `scripts/sync-spec.sh` from *this* repo against the upstream
  spec file. There is exactly one place the filter lives, and it lives
  in the public repo — the side that actually has to live with a
  filtering bug.
- **Fixed branch, single open PR at a time** — the same pattern used by
  other sync workflows in this family, so repeated upstream commits
  before someone reviews just update the one PR instead of piling up
  duplicates.
- **Regenerates the pydantic models in the same job**, so the PR always
  has spec + models in sync — never a PR that updates one and not the
  other.
- **Trigger paths** are the upstream spec file and the projection logic
  itself (so a filter-behavior change also re-triggers a sync even if the
  spec bytes didn't move). The placeholder path below needs to be filled
  in with the real location of the canonical spec in the upstream repo.

```yaml
# When the canonical Comfy API v2 spec changes upstream, sync a filtered
# copy (and the models generated from it) into the public comfy-api-proxy
# repo via a pull request.
#
# This is the "push" model: the upstream repo pushes to comfy-api-proxy;
# comfy-api-proxy never clones the upstream repo.
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
      # Update these to the actual location of the canonical spec (and of
      # this workflow file itself) in the upstream repo.
      - 'PATH/TO/canonical/openapi.yaml'
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
      - name: Checkout upstream repo (for the canonical spec)
        uses: actions/checkout@v6
        with:
          path: upstream
          persist-credentials: false

      - name: Checkout comfy-api-proxy repo
        uses: actions/checkout@v6
        with:
          repository: Comfy-Org/comfy-api-proxy
          token: ${{ secrets.CROSS_REPO_SYNC_TOKEN }}
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

      - name: Get upstream commit info
        id: upstream-info
        working-directory: upstream
        run: echo "commit=$(git rev-parse --short HEAD)" >> "$GITHUB_OUTPUT"

      # Filter + vendor the spec using comfy-api-proxy's own script (see
      # "Reuses this repo's filter, doesn't reimplement it" above). Any
      # internal operation / x-internal / orphaned-component stripping and
      # the leak self-check all happen inside this one call.
      - name: Sync + filter the spec into comfy-api-proxy
        working-directory: comfy-api-proxy
        run: |
          ./scripts/sync-spec.sh \
            "$GITHUB_WORKSPACE/upstream/PATH/TO/canonical/openapi.yaml" \
            "$(git -C "$GITHUB_WORKSPACE/upstream" rev-parse HEAD)"

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
          token: ${{ secrets.CROSS_REPO_SYNC_TOKEN }}
          path: comfy-api-proxy
          commit-message: '[chore] Sync Comfy API v2 spec from upstream@${{ steps.upstream-info.outputs.commit }}'
          title: '[chore] Sync Comfy API v2 spec from upstream@${{ steps.upstream-info.outputs.commit }}'
          body: |
            ## Automated spec sync

            This PR updates the filtered Comfy API v2 spec and the pydantic
            models generated from it.

            - Source: `upstream@${{ steps.upstream-info.outputs.commit }}`
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

## What this does NOT cover (open follow-ups, for whoever picks this up upstream)

- **`CROSS_REPO_SYNC_TOKEN`** needs repo-write scope on
  `Comfy-Org/comfy-api-proxy` specifically — confirm an existing token
  already used for similar cross-repo sync jobs is scoped that broadly,
  or provision a new token / App installation.
- **No reverse guard job** (some other sync workflows in this family run
  a second grep pass over their *generated* output, in case a codegen step
  could theoretically re-embed a literal internal reference in a schema
  example). `filter_openapi.py`'s own leak check runs before the file is
  even written, so the equivalent protection already exists earlier in
  this pipeline — call this out in upstream-side review as the reason
  there's no separate grep step here, rather than a gap.
- **This file is not wired up in the upstream repo** — implementing this
  requires upstream repo write access, which this proxy-repo PR does not
  have. Whoever integrates this should add it as
  `<upstream-repo>/.github/workflows/push-comfy-api-v2-to-proxy.yml`.
