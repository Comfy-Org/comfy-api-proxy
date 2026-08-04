# Cancellation (proxy + Python client)

Product/docs decision for GitHub [#18](https://github.com/Comfy-Org/comfy-api-proxy/issues/18)
("cancellation is in the TypeScript README but not the Python one").

## Proxy behavior

`POST /api/v2/jobs/{id}/cancel` maps to ComfyUI's atomic
`POST /api/jobs/{id}/cancel` (interrupt-if-running, or dequeue if still
pending). The response is the current job object:

- still running → status may be `canceling` until the interrupt lands at a
  node boundary
- already terminal → idempotent no-op; the terminal state is returned

Polling `GET /api/v2/jobs/{id}` remains authoritative. Cancellation is a
**request**, not a guarantee the GPU work has already stopped.

## Python client (`comfy-sdk`)

The helper already exists on the high-level handle:

```python
from comfy_sdk import Comfy

comfy = Comfy(base_url="http://127.0.0.1:8189")  # this proxy
job = comfy.jobs.submit(workflow)
job.cancel()          # POST job.urls.cancel
print(job.status)     # check — may be canceling, canceled, or already terminal
job.wait()            # optional: poll until terminal
```

Async:

```python
await job.cancel()
```

Low-level equivalent: `ComfyLow.cancel_job(job_id_or_url)`.

Cancellation docs live primarily in the
[comfy-python-sdk](https://github.com/Comfy-Org/comfy-python-sdk) README /
docstrings; this file is the proxy-side pointer so #18's docs gap is
addressed without forking SDK ownership into this repo.
