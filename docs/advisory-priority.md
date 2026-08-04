# Advisory numeric priority

Product decision for GitHub [#18](https://github.com/Comfy-Org/comfy-api-proxy/issues/18) item 6.

## Semantics

`POST /api/v2/jobs` accepts an optional integer `priority`
(range −1_000_000 … 1_000_000). The proxy **stores and echoes** it on the
job object. Higher numbers mean "prefer sooner" only as a **suggestion**.

Backends **MAY ignore or reorder**. There is no promise of global FIFO
across submitters, across machines, or across queue classes (e.g. Cloud
CPU vs GPU lanes).

## What this proxy does *not* do

Local ComfyUI only exposes a `front: true` stack push on `/prompt`, which
is exactly the trap #18 described (two submitters bury each other). This
proxy therefore **never** maps `priority` onto `front: true` or any other
ComfyUI reordering primitive.

If you need exclusive control of a local queue, keep one submitter per
ComfyUI instance by construction (the production pattern described in #18).

## Stable expectation for callers

1. Treat `priority` as advisory metadata for *your* scheduler / dashboard.
2. Do not assume Cloud or this proxy will honor relative ordering.
3. Prefer external scheduling (leases, eligibility) over in-queue priority
   when correctness matters.
