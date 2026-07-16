"""Security guards for placing uploaded bytes on disk.

Two distinct trust boundaries meet in this module:

1. **Where** an uploaded file is allowed to land — ``resolve_placement_path``
   validates a caller-supplied ``file_path`` against an allowlist of real
   ComfyUI directory roots, rejects path traversal (including through a
   symlink), and never silently overwrites an existing file.
2. **What** may land in a *model* directory specifically —
   ``looks_like_safetensors`` is a cheap, safe, header-only check so a
   client cannot use the asset-upload path to plant an arbitrary (e.g.
   pickle-based) file where ComfyUI's model loaders would later load it.
   safetensors' header-length-prefixed-JSON format was designed exactly so
   this check never has to load the tensor data itself.
"""

from __future__ import annotations

import os
import posixpath
import struct
from pathlib import Path

# Directory roots a client may place a *non-model* upload under (workflow
# inputs). Mirrors ComfyUI's folder_paths.py `get_input_directory()` /
# `get_output_directory()` — inputs are the only place a plain upload
# (image/video/audio) is expected to land.
INPUT_ROOTS = {"input"}

# Model directory roots a client may place a MODEL file under — the exact
# keys registered in ComfyUI's folder_paths.py `folder_names_and_paths`
# (verified against that file directly, not from memory). Deliberately
# excludes the two keys that ARE registered there but are not weights
# directories: `custom_nodes` (arbitrary Python) and `configs` (arbitrary
# YAML consumed by loaders) — and excludes any key not in this list at all.
MODEL_ROOTS = {
    "checkpoints",
    "loras",
    "vae",
    "text_encoders",
    "diffusion_models",
    "clip_vision",
    "style_models",
    "embeddings",
    "diffusers",
    "vae_approx",
    "controlnet",
    "gligen",
    "upscale_models",
    "latent_upscale_models",
    "hypernetworks",
    "photomaker",
    "classifiers",
    "model_patches",
    "audio_encoders",
    "background_removal",
    "frame_interpolation",
    "geometry_estimation",
    "optical_flow",
}

# Extensions that route through MODEL_ROOTS validation (safetensors-only).
_MODEL_EXTENSIONS = {".safetensors"}


class PlacementError(ValueError):
    """A requested file_path is not an acceptable placement target."""


def validate_upload_path(file_path: str) -> tuple[bool, str]:
    """Validate and classify a caller-supplied upload ``file_path`` as the
    very first thing that happens to it — BEFORE the caller decides
    whether this is a model-directory placement or a plain input upload.

    This closes a misclassification bypass: a path like
    ``input/../checkpoints/evil.safetensors`` has ``Path(...).parts[0] ==
    "input"``, so a classifier that only looks at the first segment (as
    the model-vs-input branch used to) waves it through as "just an input
    upload" and hands it to a code path that never applies the
    model-placement guards at all — letting the ``..`` ride along into
    whatever the input-upload path does with the string. Checking for
    ``..``/absolute *before* classification, on the whole path, means
    there is no way to smuggle a traversal segment past this by picking a
    first segment that classifies as harmless.

    Returns ``(is_model, norm)``:

      * ``is_model`` — ``True`` if this is a model-directory placement
        (the first segment, after stripping an optional leading
        ``"models/"`` convention prefix, names a ``MODEL_ROOTS``
        category), ``False`` for an input upload.
      * ``norm`` — the validated remainder a caller should use from here
        on, *instead of re-parsing the raw client string*:
          - for a model path: the ``"models/"``-prefix-stripped,
            category-qualified path (e.g. ``"checkpoints/x.safetensors"``),
            i.e. exactly what ``resolve_placement_path`` expects.
          - for an input path: the ``"input/"``-prefix-stripped remainder
            (e.g. ``"x.png"``), or the original string unchanged if it
            was a bare filename with no root segment at all (the
            long-standing implicit-input convention).

    Raises ``PlacementError`` if ``file_path`` is empty, absolute, contains
    a ``..`` segment anywhere, or names an unrecognized root when a root
    segment is present.
    """
    if not file_path or file_path.startswith(("/", "\\")):
        raise PlacementError(f"file_path must be relative: {file_path!r}")

    # An optional "models/" prefix is a convention for model uploads
    # (mirrors the "input/" convention below) — normalize it away before
    # splitting, so classification and the "-.." check both see the same
    # shape regardless of whether the caller included it.
    stripped = file_path[len("models/") :] if file_path.startswith("models/") else file_path

    parts = Path(stripped).parts
    if not parts:
        raise PlacementError(f"file_path must include a filename: {file_path!r}")
    if any(p in ("..", "") for p in parts):
        raise PlacementError(f"file_path must not contain '..': {file_path!r}")

    if len(parts) < 2:
        # A single bare segment (e.g. "photo.png"): the long-standing
        # implicit-input convention. No root name to validate.
        return False, stripped

    root = parts[0]
    if root not in MODEL_ROOTS and root not in INPUT_ROOTS:
        raise PlacementError(
            f"'{root}' is not an allowlisted placement root "
            f"(allowed: {sorted(INPUT_ROOTS | MODEL_ROOTS)})"
        )
    if root in MODEL_ROOTS:
        return True, stripped
    # root in INPUT_ROOTS ("input"): strip the redundant root segment so a
    # caller lands in the same place whether or not they named it
    # explicitly, matching the pre-existing convention.
    return False, posixpath.join(*parts[1:])


def resolve_placement_path(base_dir: Path, file_path: str) -> Path:
    """Resolve a caller-supplied ``file_path`` to an absolute, validated
    location under ``base_dir``, or raise ``PlacementError``.

    Guards applied, in order:
      * ``file_path`` must be relative and must not climb outside its
        top-level root via ``..`` segments.
      * Its top-level segment must name an allowlisted root — an input
        root, or a model root (see ``MODEL_ROOTS`` / ``INPUT_ROOTS``).
        Anything else (``custom_nodes``, ``configs``, an unrecognized
        name, or no subdirectory at all) is rejected: uploads only ever
        land in a directory ComfyUI actually treats as data.
      * A file destined for a model root must have a ``.safetensors``
        extension — enforced here at the path-shape level; the caller is
        still responsible for calling ``looks_like_safetensors`` against
        the actual bytes before committing the file (a matching extension
        alone proves nothing about the content).
      * The resolved real path (after following any symlinks in
        ``base_dir`` itself) must still be inside ``base_dir`` — blocks a
        symlink planted at an intermediate path component from smuggling
        the write outside the sandboxed root.
      * No-clobber: the destination must not already exist. Callers write
        to a temp file and ``os.replace`` only after this check to keep
        the whole operation race-free (see ``atomic_no_clobber_write``).
    """
    if not file_path or file_path.startswith(("/", "\\")):
        raise PlacementError(f"file_path must be relative: {file_path!r}")

    parts = Path(file_path).parts
    if any(p in ("..", "") for p in parts):
        raise PlacementError(f"file_path must not contain '..': {file_path!r}")
    if len(parts) < 2:
        raise PlacementError(
            f"file_path must include a root directory (e.g. 'input/x.png' or "
            f"'checkpoints/x.safetensors'): {file_path!r}"
        )

    root, *rest = parts
    is_model = root in MODEL_ROOTS
    if not is_model and root not in INPUT_ROOTS:
        raise PlacementError(
            f"'{root}' is not an allowlisted placement root "
            f"(allowed: {sorted(INPUT_ROOTS | MODEL_ROOTS)})"
        )
    if is_model and Path(file_path).suffix.lower() not in _MODEL_EXTENSIONS:
        raise PlacementError(f"model uploads must be .safetensors, got '{file_path}'")

    base_real = base_dir.resolve()
    candidate = (base_real / root / Path(*rest)).resolve()

    # Belt-and-suspenders: the resolved real path must still be under the
    # resolved real base_dir. Catches a symlink anywhere along the path
    # (including one planted at `root` itself) pointing outside the sandbox.
    try:
        candidate.relative_to(base_real)
    except ValueError as e:
        raise PlacementError(
            f"resolved path '{candidate}' escapes the allowed root '{base_real}' "
            "(possible symlink escape)"
        ) from e

    if candidate.exists():
        raise PlacementError(f"refusing to overwrite existing file: {candidate}")

    return candidate


def atomic_no_clobber_write(destination: Path, data: bytes) -> None:
    """Write ``data`` to ``destination`` atomically, refusing to clobber an
    existing file.

    Writes to a sibling temp file in the same directory (so the final
    ``os.replace`` is on the same filesystem and therefore atomic), then
    uses ``os.open`` with ``O_EXCL`` on the *destination* as the final
    no-clobber gate — closing the TOCTOU gap between
    ``resolve_placement_path``'s existence check and this write.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
    try:
        tmp.write_bytes(data)
        fd = os.open(str(destination), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        os.replace(tmp, destination)
    except FileExistsError:
        tmp.unlink(missing_ok=True)
        raise PlacementError(f"refusing to overwrite existing file: {destination}") from None
    finally:
        tmp.unlink(missing_ok=True)


# ---- safetensors header-only validation ------------------------------------
#
# Format: 8 bytes little-endian header length N, then N bytes of UTF-8 JSON
# (a flat map of tensor-name -> {dtype, shape, data_offsets} plus an optional
# "__metadata__" key), then the raw tensor data. Validating the header alone
# (never touching the tensor bytes, and never using pickle/torch.load, which
# is exactly the attack safetensors was designed to close off) is enough to
# confirm "this is plausibly a safetensors file" before it's allowed to sit
# in a directory ComfyUI's model loaders will later read from.
_MAX_HEADER_LEN = 100 * 1024 * 1024  # safetensors' own sanity bound on header size


def looks_like_safetensors(data: bytes) -> bool:
    """Best-effort structural check that ``data`` is a well-formed
    safetensors file, without loading any tensor payload."""
    if len(data) < 8:
        return False
    (header_len,) = struct.unpack("<Q", data[:8])
    if header_len == 0 or header_len > _MAX_HEADER_LEN or header_len > len(data) - 8:
        return False
    import json

    try:
        header = json.loads(data[8 : 8 + header_len].decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return False
    if not isinstance(header, dict):
        return False
    for key, value in header.items():
        if key == "__metadata__":
            continue
        if not isinstance(value, dict):
            return False
        if not {"dtype", "shape", "data_offsets"} <= value.keys():
            return False
    return True
