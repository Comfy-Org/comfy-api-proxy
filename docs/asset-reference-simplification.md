# Simplifying Asset References in `LoadImage`

## Current scheme

At the moment, an asset in an API-format workflow is represented by a special
object:

```json
{
  "10": {
    "class_type": "LoadImage",
    "inputs": {
      "image": {
        "__type": "core/ASSET",
        "info": {
          "id": "<asset_id>"
        }
      }
    }
  }
}
```

Before sending the workflow to ComfyUI, the proxy recursively walks the graph.
For each `core/ASSET` object it:

1. looks up the asset by `info.id`;
2. optionally looks it up by `info.hash`;
3. obtains the name/path expected by ComfyUI;
4. replaces the object with that string;
5. returns an error if the asset cannot be found.

This scheme is generic, but it is unnecessarily verbose for a regular
`LoadImage` upload: the workflow only needs to provide the uploaded file's ID.

## Proposed simplified scheme

The `LoadImage` node's `image` input could use a string marker:

```json
{
  "10": {
    "class_type": "LoadImage",
    "inputs": {
      "image": "asset:<asset_id>"
    }
  }
}
```

In documentation, the same format may be written as `asset:[asset_id]`, where
the square brackets indicate a placeholder and are not part of the actual
string. The real JSON would contain, for example:

```json
"image": "asset:550e8400-e29b-41d4-a716-446655440000"
```

## Replacement algorithm

The replacement happens before `POST /prompt`:

```text
if node.class_type == "LoadImage":
    value = node.inputs.get("image")
    if value is a string and starts with "asset:":
        asset_id = value after the "asset:" prefix
        asset = find asset by asset_id
        if asset is not found:
            return missing_asset
        node.inputs["image"] = asset.comfy_filename
```

Python-like pseudocode:

```python
def resolve_load_image(node: dict, assets) -> None:
    if node.get("class_type") != "LoadImage":
        return

    inputs = node.get("inputs")
    if not isinstance(inputs, dict):
        return

    image = inputs.get("image")
    if not isinstance(image, str) or not image.startswith("asset:"):
        return

    asset_id = image.removeprefix("asset:")
    asset = assets.get(asset_id)
    if asset is None:
        raise MissingAsset(asset_id)

    inputs["image"] = asset.comfy_filename
```

## What should be substituted

The proxy should substitute the value understood by ComfyUI, not the proxy URL
and not the internal UUID:

- for a regular input file: `subfolder/filename`, for example
  `uploads/550e8400-e29b-41d4-a716-446655440000.png`;
- for a file placed in a model directory: a path relative to the appropriate
  model directory, if this asset type is supported by the workflow.

This value is already stored in the asset record or can be derived from the
ComfyUI upload response.

## Compatibility with the current format

A practical migration strategy is to support both formats:

1. handle the new short form, `asset:<asset_id>`, only in
   `LoadImage.inputs.image`;
2. retain the existing recursive `core/ASSET` handling for older clients and
   other input types;
3. use the string format in new documentation and examples.

This preserves existing workflows and allows the object form to be removed
later if no other nodes need it.

## Validation and limitations

- Match the exact beginning of the string, `asset:`, rather than any occurrence
  inside a filename.
- Treat an empty ID (`"asset:"`) as an invalid request.
- Look up the ID only among assets belonging to the current proxy instance and
  apply the same ownership, readiness, and retention checks as for `core/ASSET`.
- Pass ordinary values such as `photo.png` to ComfyUI unchanged.
- If this format is intended only for `LoadImage`, do not globally replace every
  string beginning with `asset:` in every workflow field: that could alter text
  parameters belonging to other nodes.
