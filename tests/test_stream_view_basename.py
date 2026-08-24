"""Test that _stream_view extracts basename from full path before calling /view.

Regression test for: when ComfyUI returns a full file path in the output
filename (e.g. /tmp/file.png), the proxy must pass only the basename
to ComfyUI's /view endpoint — /view expects just the filename, not the full
path.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from comfy_api_proxy.app import Proxy


async def _async_iter_chunks():
    yield b"x" * 100


@pytest.mark.asyncio
async def test_stream_view_extractes_basename_from_full_path():
    """_stream_view should pass only the basename to upstream /view."""
    proxy = Proxy("http://comfy")

    # ref with a full path (what ComfyUI returns for saved outputs)
    ref = {
        "filename": "/tmp/example.png",
        "subfolder": "",
        "type": "output",
    }

    # Mock the upstream response
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.content_type = "image/png"
    mock_resp.headers = {"Content-Length": "100"}
    mock_resp.content.iter_chunked = MagicMock(return_value=_async_iter_chunks())
    mock_resp.release = MagicMock()

    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=mock_resp)
    proxy._session = mock_session

    req = make_mocked_request(
        "GET",
        "/api/v2/assets/fake-id/content",
        match_info={"id": "fake-id"},
    )

    # Call _stream_view directly
    with patch.object(web.StreamResponse, "prepare", new_callable=AsyncMock):
        with patch.object(web.StreamResponse, "write", new_callable=AsyncMock):
            with patch.object(web.StreamResponse, "write_eof", new_callable=AsyncMock):
                await proxy._stream_view(req, ref)

    # Verify the upstream /view was called with just the basename
    mock_session.get.assert_called_once()
    call_args = mock_session.get.call_args
    params = call_args.kwargs.get("params") or call_args[1].get("params")
    assert params is not None, f"Expected params in call, got: {call_args}"
    assert params["filename"] == "example.png", f"Expected basename only, got: {params['filename']}"
    assert params["subfolder"] == ""
    assert params["type"] == "output"


@pytest.mark.asyncio
async def test_stream_view_handles_plain_filename():
    """_stream_view should work when filename is already a plain name."""
    proxy = Proxy("http://comfy")

    ref = {
        "filename": "out.png",
        "subfolder": "",
        "type": "output",
    }

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.content_type = "image/png"
    mock_resp.headers = {"Content-Length": "100"}
    mock_resp.content.iter_chunked = MagicMock(return_value=_async_iter_chunks())
    mock_resp.release = MagicMock()

    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=mock_resp)
    proxy._session = mock_session

    req = make_mocked_request(
        "GET",
        "/api/v2/assets/fake-id/content",
        match_info={"id": "fake-id"},
    )

    with patch.object(web.StreamResponse, "prepare", new_callable=AsyncMock):
        with patch.object(web.StreamResponse, "write", new_callable=AsyncMock):
            with patch.object(web.StreamResponse, "write_eof", new_callable=AsyncMock):
                await proxy._stream_view(req, ref)

    call_args = mock_session.get.call_args
    params = call_args.kwargs.get("params") or call_args[1].get("params")
    assert params["filename"] == "out.png"
