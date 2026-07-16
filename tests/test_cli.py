"""Unit tests for the CLI's bind-safety logic — no server is ever started."""

from __future__ import annotations

from comfy_api_proxy.cli import _is_loopback_host, main


def test_is_loopback_host_treats_empty_string_as_all_interfaces():
    # "" (like None) binds ALL interfaces at the socket layer, so it must NOT
    # be treated as loopback — otherwise `--host ""` skips the token gate.
    assert _is_loopback_host("") is False
    assert _is_loopback_host("0.0.0.0") is False
    assert _is_loopback_host("127.0.0.1") is True
    assert _is_loopback_host("localhost") is True
    assert _is_loopback_host("::1") is True


def test_main_refuses_empty_host_without_token(capsys):
    # The refusal happens before make_app / web.run_app, so nothing binds.
    rc = main(["--host", "", "--port", "0", "--comfyui", "http://127.0.0.1:8188"])
    assert rc == 2
    assert "refusing to bind" in capsys.readouterr().err
