"""Direct unit tests for AssetStore (assets.py).

The dedup invariant is currently only exercised indirectly via one end-to-end
upload test, which never calls `add()` with two different byte sets sharing a
hash. These pin the store primitives directly.
"""

from __future__ import annotations

from comfy_api_proxy.assets import AssetStore


def test_lookups_on_empty_store_return_none():
    store = AssetStore()
    assert store.get("asset_nope") is None
    assert store.get_by_hash("blake3:none") is None
    assert store.has_hash("blake3:none") is False


def test_add_indexes_by_id_and_hash():
    store = AssetStore()
    rec = store.add(hash_="blake3:x", size_bytes=10, content_type="image/png", file_path="a.png")
    assert store.get(rec.id) is rec
    assert store.has_hash("blake3:x")
    assert store.get_by_hash("blake3:x").id == rec.id


def test_first_writer_wins_the_hash_slot():
    # A second add() with the same hash mints a distinct record, but the hash
    # index keeps resolving to the FIRST record — the dedup target contract.
    store = AssetStore()
    first = store.add(hash_="blake3:x", size_bytes=10, content_type="image/png", file_path="a.png")
    second = store.add(
        hash_="blake3:x", size_bytes=999, content_type="image/png", file_path="b.png"
    )
    assert second.id != first.id
    assert store.get_by_hash("blake3:x").id == first.id
    assert store.get(second.id) is second  # still individually retrievable by id


def test_register_comfy_output_has_null_hash_and_comfy_ref():
    store = AssetStore()
    rec = store.register_comfy_output(
        filename="out.png", subfolder="", type_="output", content_type="image/png"
    )
    assert rec.hash == ""  # outputs are addressed by id, hash left unset
    assert rec.comfy_ref == {"filename": "out.png", "subfolder": "", "type": "output"}
    assert store.get(rec.id) is rec
