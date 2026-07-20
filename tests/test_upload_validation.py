"""Upload validation paths that the happy-path `stack.upload` helper skips.

`Stack.upload` always sends a well-formed multipart body (file + file_path +
content_type), so the missing-part branches and the streaming size cap have no
coverage. These build the multipart body by hand to omit parts / exceed the cap.
"""

from __future__ import annotations

import urllib.error
import urllib.request

BOUNDARY = "----comfyproxytestVALIDATION"


def _post_multipart(base: str, *, include_file=True, include_file_path=True, file_bytes=b"x"):
    parts: list[bytes] = []
    if include_file:
        parts.append(f"--{BOUNDARY}\r\n".encode())
        parts.append(b'Content-Disposition: form-data; name="file"; filename="a.png"\r\n')
        parts.append(b"Content-Type: image/png\r\n\r\n")
        parts.append(file_bytes + b"\r\n")

    def field(name: str, value: str) -> None:
        parts.append(f"--{BOUNDARY}\r\n".encode())
        parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        parts.append(value.encode() + b"\r\n")

    if include_file_path:
        field("file_path", "a.png")
    field("content_type", "image/png")
    parts.append(f"--{BOUNDARY}--\r\n".encode())
    body = b"".join(parts)

    req = urllib.request.Request(
        base + "/api/v2/assets",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={BOUNDARY}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            import json

            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        import json

        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, None


def test_upload_missing_file_part_rejected(stack):
    status, body = _post_multipart(stack.base, include_file=False)
    assert status == 422, body
    assert body["error"]["code"] == "invalid_request"


def test_upload_missing_file_path_field_rejected(stack):
    status, body = _post_multipart(stack.base, include_file_path=False)
    assert status == 422, body
    assert body["error"]["code"] == "invalid_request"


def test_upload_exceeding_max_bytes_rejected(stack_small_upload):
    # The proxy was started with a 1 MB cap; a ~2 MB body must be refused with
    # 413 during the streaming read, not buffered whole.
    big = b"\0" * (2 * 1024 * 1024)
    status, body = _post_multipart(stack_small_upload.base, file_bytes=big)
    assert status == 413, body
    assert body["error"]["code"] == "payload_too_large"
