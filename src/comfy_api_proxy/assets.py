"""In-process asset store for the self-hosted proxy.

A self-hosted ComfyUI is single-user, so "account scoping" collapses to a
single namespace — but the v2 *shape* is preserved (UUID-identified asset
records over content-addressed blobs, keyed by a server-computed blake3
hash) so the same SDK code works against Cloud and against this proxy.

Two things are tracked per asset:

  * the v2-facing record (``id``, ``hash``, ``size_bytes``, ``content_type``,
    ``file_path``, ``created_at``), and
  * how to actually retrieve the bytes — either a ComfyUI ``/view`` reference
    (``filename``/``subfolder``/``type``) for inputs proxied to ComfyUI, or an
    absolute on-disk path for model files the proxy placed itself.

The store is in-memory and lost on restart (same durability as ComfyUI's own
history). A durable index is a later milestone; nothing in the contract
requires persistence, and re-uploading recomputes the same hash and dedups.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class AssetRecord:
    """One asset: a v2 record plus its retrieval route."""

    id: str
    hash: str  # "blake3:<hex>"
    size_bytes: int
    content_type: str
    file_path: str | None
    created_at: str
    # Retrieval — exactly one of these is set.
    comfy_ref: dict[str, str] | None = None  # {filename, subfolder, type} for /view
    disk_path: str | None = None  # absolute path for proxy-placed model files
    tags: list[str] = field(default_factory=list)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_asset_id() -> str:
    return "asset_" + uuid.uuid4().hex


class AssetStore:
    """Thread-unsafe (single event loop) asset + hash index."""

    def __init__(self) -> None:
        self._by_id: dict[str, AssetRecord] = {}
        self._id_by_hash: dict[str, str] = {}

    def get(self, asset_id: str) -> AssetRecord | None:
        return self._by_id.get(asset_id)

    def get_by_hash(self, hash_: str) -> AssetRecord | None:
        asset_id = self._id_by_hash.get(hash_)
        return self._by_id.get(asset_id) if asset_id else None

    def has_hash(self, hash_: str) -> bool:
        return hash_ in self._id_by_hash

    def add(
        self,
        *,
        hash_: str,
        size_bytes: int,
        content_type: str,
        file_path: str | None,
        comfy_ref: dict[str, str] | None = None,
        disk_path: str | None = None,
        tags: list[str] | None = None,
    ) -> AssetRecord:
        """Mint and store a new asset record, indexing it by hash."""
        record = AssetRecord(
            id=new_asset_id(),
            hash=hash_,
            size_bytes=size_bytes,
            content_type=content_type,
            file_path=file_path,
            created_at=_now_iso(),
            comfy_ref=comfy_ref,
            disk_path=disk_path,
            tags=tags or [],
        )
        self._by_id[record.id] = record
        # First writer wins the hash slot (dedup target); a later identical
        # upload resolves to this same record rather than overwriting it.
        self._id_by_hash.setdefault(hash_, record.id)
        return record

    def register_comfy_output(
        self, *, filename: str, subfolder: str, type_: str, content_type: str
    ) -> AssetRecord:
        """Register a ComfyUI-produced output (job result) as an asset so it
        is retrievable via GET /assets/{id}. Hash is left unset (lazily
        computed / null) — outputs are addressed by asset id, and hashing
        every output on the retrieval hot path is deliberately avoided."""
        record = AssetRecord(
            id=new_asset_id(),
            hash="",  # rendered as null; lazily computed, per the Output schema
            size_bytes=0,
            content_type=content_type,
            file_path=filename,
            created_at=_now_iso(),
            comfy_ref={"filename": filename, "subfolder": subfolder, "type": type_},
        )
        self._by_id[record.id] = record
        return record
