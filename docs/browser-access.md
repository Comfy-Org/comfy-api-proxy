# Browser access to a local proxy

Hosted web apps can call a user's local `comfy-api-proxy` on `127.0.0.1` after
the user allowlists their Origin. Cross-site browser access is **off by
default** (DNS-rebinding / CSRF guard ported from ComfyUI core).

## Configure

```bash
comfy-api-proxy \
  --enable-cors-header https://app.example.com \
  --token "$LOCAL_PROXY_TOKEN"   # optional; recommended
```

- `--enable-cors-header` is repeatable. Explicit `http(s)://host[:port]` only —
  `*` is refused.
- Allowlisted Origins get CORS preflight (`OPTIONS` → `204`), may send
  `Authorization` / `Content-Type` / `Idempotency-Key`, and can read
  `Retry-After`, `Content-Range`, and `Accept-Ranges` on responses.
- `GET /api/v2/health` is readable cross-origin when the Origin is allowlisted.
  Streaming responses (SSE) get the same CORS headers via `on_response_prepare`.
- Non-allowlisted Origins keep today's `403 forbidden_origin` behaviour when
  the request is cross-site or its `Origin` disagrees with a loopback `Host`.
  A same-origin request is unaffected — the guard exists to stop cross-site
  callers, not to require an allowlist entry for the proxy's own page.

## Security posture

The allowlist does the anti-CSRF work: attacker Origins are not listed.
`--token` is an extra local lock (useful when widening `--host`); for a
loopback-only single-user setup it is not a Cloud-grade secret.

## TypeScript / browser clients

`@comfyorg/sdk` is **Node-only for v1** (browser support is out of scope).
Call `/api/v2/*` with `fetch` against the proxy base URL. A browser SDK build
is a follow-up, not required to use this path.

```js
const r = await fetch("http://127.0.0.1:8189/api/v2/health", {
  headers: { Authorization: `Bearer ${token}` }, // omit if no --token
});
```
