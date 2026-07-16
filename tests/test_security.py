"""Unit tests for the model-placement security guards in security.py."""

from __future__ import annotations

import struct

import pytest

from comfy_api_proxy.security import (
    PlacementError,
    atomic_no_clobber_write,
    looks_like_safetensors,
    resolve_placement_path,
    validate_upload_path,
)


def _fake_safetensors(header: dict) -> bytes:
    import json

    body = json.dumps(header).encode("utf-8")
    return struct.pack("<Q", len(body)) + body + b"\x00" * 16


class TestResolvePlacementPath:
    def test_accepts_input_root(self, tmp_path):
        p = resolve_placement_path(tmp_path, "input/photo.png")
        assert p == (tmp_path / "input" / "photo.png").resolve()

    def test_accepts_model_root_with_safetensors_extension(self, tmp_path):
        p = resolve_placement_path(tmp_path, "checkpoints/model.safetensors")
        assert p.name == "model.safetensors"

    def test_rejects_absolute_path(self, tmp_path):
        with pytest.raises(PlacementError, match="relative"):
            resolve_placement_path(tmp_path, "/etc/passwd")

    def test_rejects_dotdot_traversal(self, tmp_path):
        with pytest.raises(PlacementError, match=r"\.\."):
            resolve_placement_path(tmp_path, "input/../../etc/passwd")

    def test_rejects_unlisted_root(self, tmp_path):
        with pytest.raises(PlacementError, match="not an allowlisted"):
            resolve_placement_path(tmp_path, "custom_nodes/evil.py")

    def test_rejects_configs_root(self, tmp_path):
        with pytest.raises(PlacementError, match="not an allowlisted"):
            resolve_placement_path(tmp_path, "configs/whatever.yaml")

    def test_rejects_bare_root_with_no_subpath(self, tmp_path):
        with pytest.raises(PlacementError, match="root directory"):
            resolve_placement_path(tmp_path, "checkpoints")

    def test_rejects_non_safetensors_extension_in_model_root(self, tmp_path):
        with pytest.raises(PlacementError, match="safetensors"):
            resolve_placement_path(tmp_path, "checkpoints/model.ckpt")

    def test_rejects_pickle_extension_in_model_root(self, tmp_path):
        with pytest.raises(PlacementError, match="safetensors"):
            resolve_placement_path(tmp_path, "loras/model.pt")

    def test_rejects_symlink_escape(self, tmp_path):
        # Plant a symlink at the "input" root pointing outside tmp_path.
        outside = tmp_path.parent / "outside_target"
        outside.mkdir(exist_ok=True)
        (tmp_path / "input").symlink_to(outside, target_is_directory=True)
        with pytest.raises(PlacementError, match="escapes"):
            resolve_placement_path(tmp_path, "input/photo.png")

    def test_refuses_to_overwrite_existing_file(self, tmp_path):
        (tmp_path / "input").mkdir()
        existing = tmp_path / "input" / "photo.png"
        existing.write_bytes(b"already here")
        with pytest.raises(PlacementError, match="overwrite"):
            resolve_placement_path(tmp_path, "input/photo.png")


class TestValidateUploadPath:
    """Regression coverage for the shared pre-classification path
    validator (see app.py's ``upload_asset``): a file_path like
    ``input/../checkpoints/evil.safetensors`` used to be classified as a
    plain input upload (its first path segment is "input"), which skipped
    the model-placement guard entirely and let the ".." ride along into an
    unvalidated posixpath.dirname/basename split. validate_upload_path is
    the single choke point that now runs before that classification."""

    def test_rejects_dotdot_disguised_as_input_path(self):
        with pytest.raises(PlacementError, match=r"\.\."):
            validate_upload_path("input/../checkpoints/evil.safetensors")

    def test_rejects_dotdot_climbing_multiple_levels(self):
        with pytest.raises(PlacementError, match=r"\.\."):
            validate_upload_path("input/../../etc/cron.d/pwn")

    def test_rejects_dotdot_via_models_prefix(self):
        with pytest.raises(PlacementError, match=r"\.\."):
            validate_upload_path("models/../../etc/passwd")

    def test_rejects_absolute_path(self):
        with pytest.raises(PlacementError, match="relative"):
            validate_upload_path("/etc/passwd")

    def test_accepts_normal_input_path(self):
        is_model, norm = validate_upload_path("input/foo.png")
        assert is_model is False
        assert norm == "foo.png"

    def test_accepts_bare_filename_as_implicit_input(self):
        is_model, norm = validate_upload_path("foo.png")
        assert is_model is False
        assert norm == "foo.png"

    def test_accepts_normal_model_path(self):
        is_model, norm = validate_upload_path("checkpoints/foo.safetensors")
        assert is_model is True
        assert norm == "checkpoints/foo.safetensors"

    def test_accepts_model_path_with_models_prefix(self):
        is_model, norm = validate_upload_path("models/checkpoints/foo.safetensors")
        assert is_model is True
        assert norm == "checkpoints/foo.safetensors"

    def test_rejects_unlisted_root(self):
        with pytest.raises(PlacementError, match="not an allowlisted"):
            validate_upload_path("custom_nodes/evil.py")


class TestAtomicNoClobberWrite:
    def test_writes_new_file(self, tmp_path):
        dest = tmp_path / "input" / "photo.png"
        atomic_no_clobber_write(dest, b"hello")
        assert dest.read_bytes() == b"hello"
        # No leftover temp files.
        assert list(dest.parent.iterdir()) == [dest]

    def test_refuses_to_clobber_concurrently_created_file(self, tmp_path):
        dest = tmp_path / "input" / "photo.png"
        dest.parent.mkdir(parents=True)
        dest.write_bytes(b"raced you")
        with pytest.raises(PlacementError, match="overwrite"):
            atomic_no_clobber_write(dest, b"too late")
        assert dest.read_bytes() == b"raced you"


class TestLooksLikeSafetensors:
    def test_accepts_well_formed_header(self):
        data = _fake_safetensors(
            {"weight": {"dtype": "F32", "shape": [2, 2], "data_offsets": [0, 16]}}
        )
        assert looks_like_safetensors(data)

    def test_accepts_metadata_only_key(self):
        data = _fake_safetensors(
            {
                "__metadata__": {"format": "pt"},
                "weight": {"dtype": "F32", "shape": [1], "data_offsets": [0, 4]},
            }
        )
        assert looks_like_safetensors(data)

    def test_rejects_too_short(self):
        assert not looks_like_safetensors(b"short")

    def test_rejects_garbage_header(self):
        assert not looks_like_safetensors(struct.pack("<Q", 4) + b"\xff\xff\xff\xff")

    def test_rejects_header_len_exceeding_buffer(self):
        assert not looks_like_safetensors(struct.pack("<Q", 10_000) + b"{}")

    def test_rejects_pickle_magic_bytes(self):
        # A pickle stream's opening bytes, not a safetensors header at all.
        assert not looks_like_safetensors(b"\x80\x04\x95\x00\x00\x00\x00\x00\x00\x00\x00")

    def test_rejects_tensor_entry_missing_required_keys(self):
        data = _fake_safetensors({"weight": {"dtype": "F32"}})
        assert not looks_like_safetensors(data)
