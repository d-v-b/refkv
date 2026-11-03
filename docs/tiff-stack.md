# A stack of TIFF files as one array

A z-stack or time-lapse is often saved as one TIFF per plane. refkv zarrifies
one file at a time, but the stores it builds can be combined: this example
zarrifies each TIFF in a directory, stacks their arrays along a new leading axis
`z`, and saves the result as `stack.vzip`, a sidecar beside the files. Zarr then
reads the stack as one `(z, y, x)` array, and each chunk comes from the plane's
own file. No pixel is copied.

## How it works

- **One store per file.** `zarrify_async` zarrifies each TIFF; `asyncio.gather`
  runs them together.
- **The same layout in every file.** Every file must have the same arrays, with
  the same shape, data type, chunks and codecs, so that a chunk of the stack is
  a file's chunk, unchanged. The example raises a ValueError naming the first
  file that differs.
- **New chunk keys.** Chunk `(y, x)` of file `i` becomes chunk `(i, y, x)`: key
  `0/c/y/x` becomes `0/c/i/y/x`, and the array's metadata gains a leading `z`
  axis of chunk size 1.
- **One source table.** The stack's source table lists every file's sources in
  order, and each reference's source index is moved by the number of sources
  before its file's.
- **Relative URLs.** `save(..., relative_to=directory + os.sep)` writes each
  source URL relative to the sidecar (`z000.tif`), so the directory can move or
  be copied with its sidecar. Without it, the URLs are absolute local paths.
  `zarrify` writes a local file's URL as its path, with the platform's
  separator (`\` on Windows), so the prefix ends with `os.sep`.

The root group keeps the files' OME-Zarr `multiscales`, with the `z` axis in
front, and lists the files in stack order under `tiff_stack.files`. Each file's
own TIFF tags are not carried over; `zarrify(file)` gives them.

## The example

Without arguments, the example first writes a synthetic stack of 24 tiled,
zlib-compressed TIFFs to `./tiff_stack/`. With a directory, it stacks the
directory's `*.tif` files in name order. It then reopens the sidecar with zarr
and checks every plane against tifffile. The file is
[examples/tiff_stack.py](examples/tiff_stack.py):

<!-- include: examples/tiff_stack.py -->
```python
# /// script
# requires-python = ">=3.12"
# dependencies = ["refkv[zarr]>=0.2.0", "tifffile"]
# ///
"""A stack of TIFF files as one Zarr array, saved as a vzip sidecar beside them.

Each TIFF is zarrified on its own. The sidecar stacks their arrays along a new leading
axis `z`: chunk (y, x) of file i becomes chunk (i, y, x), a reference to the same bytes
of that file, so no pixel is copied. Source URLs are written relative to the sidecar, so
the directory and the sidecar can move together.

Run with `uv run docs/examples/tiff_stack.py [DIR]`. Without DIR, it first writes a
synthetic stack of 24 tiled TIFFs to ./tiff_stack/. It saves DIR/stack.vzip, reopens it
with zarr, and checks every plane against tifffile.
"""

import asyncio
import dataclasses
import json
import os
import sys
from pathlib import Path

import numpy as np
import tifffile
import zarr

import refkv
import refkv.zarr
from refkv.zarrify import zarrify_async


def synthetic_stack(directory: Path, planes: int = 24) -> None:
    """Write `planes` 256 x 256 uint16 TIFFs, tiled and zlib-compressed: slices of a ball."""
    directory.mkdir(exist_ok=True)
    y, x = np.mgrid[-128:128, -128:128]
    for z in range(planes):
        r2 = x**2 + y**2 + ((z - planes / 2) * 10) ** 2
        plane = (60000 * np.exp(-r2 / 2 / 60**2)).astype(np.uint16)
        tifffile.imwrite(directory / f"z{z:03}.tif", plane, tile=(128, 128), compression="zlib")


def rebase(ref: refkv.Range | refkv.Concat, offset: int) -> refkv.Range | refkv.Concat:
    """`ref` with its source indexes moved by `offset`, for a source table that holds
    other files' sources before this file's."""
    if isinstance(ref, refkv.Concat):
        return refkv.Concat(tuple(rebase_range(r, offset) for r in ref.parts))
    return rebase_range(ref, offset)


def rebase_range(r: refkv.Range, offset: int) -> refkv.Range:
    """`rebase` for one range; a literal range (`data`) has no source to move."""
    return r if r.data is not None else dataclasses.replace(r, source=r.source + offset)


def document(store: refkv.ReferenceStore, key: str) -> dict:
    """The JSON document at `key` of `store`, such as a `zarr.json`."""
    value = store.mapping[key]
    assert isinstance(value, bytes), f"{key} is a reference, not a document"
    return json.loads(value)


def stack(stores: list[refkv.ReferenceStore], names: list[str]) -> refkv.ReferenceStore:
    """One store whose arrays stack the same-named arrays of `stores` along a new axis `z`.

    Every store must have the same arrays, with the same shape, data type, chunks and
    codecs: a chunk of the stack is a chunk of one file, unchanged.
    """
    first = stores[0].mapping
    arrays = sorted(
        k.removesuffix("/zarr.json")
        for k in first
        if k.count("/") == 1 and k.endswith("/zarr.json") and document(stores[0], k)["node_type"] == "array"
    )
    for store, name in zip(stores, names, strict=True):
        for a in arrays:
            if store.mapping.get(f"{a}/zarr.json") != first[f"{a}/zarr.json"]:
                raise ValueError(
                    f"{name}: array {a!r} differs from {names[0]}'s "
                    "(shape, data type, chunks or codecs), so it cannot be stacked"
                )

    mapping: dict[str, bytes | refkv.Range | refkv.Concat] = {}
    sources: list[refkv.Source] = []
    for i, store in enumerate(stores):
        for key, value in store.mapping.items():
            a, sep, chunk = key.partition("/c/")
            if sep and a in arrays:
                mapping[f"{a}/c/{i}/{chunk}"] = value if isinstance(value, bytes) else rebase(value, len(sources))
        sources.extend(store.sources)

    for a in arrays:
        meta = document(stores[0], f"{a}/zarr.json")
        assert meta["chunk_key_encoding"] == {"name": "default", "configuration": {"separator": "/"}}
        meta["shape"] = [len(stores), *meta["shape"]]
        meta["chunk_grid"]["configuration"]["chunk_shape"] = [1, *meta["chunk_grid"]["configuration"]["chunk_shape"]]
        meta["dimension_names"] = ["z", *meta["dimension_names"]]
        mapping[f"{a}/zarr.json"] = json.dumps(meta).encode()

    # The root group: each file's OME-Zarr multiscales with the z axis in front, and the
    # files in stack order. Each file's own TIFF tags stay with zarrify(file).
    ome = document(stores[0], "zarr.json")["attributes"]["ome"]
    for ms in ome["multiscales"]:
        ms["axes"] = [{"name": "z", "type": "space"}, *ms["axes"]]
        for ds in ms["datasets"]:
            for t in ds["coordinateTransformations"]:
                t["scale"] = [1.0, *t["scale"]]
    root = {"zarr_format": 3, "node_type": "group", "attributes": {"ome": ome, "tiff_stack": {"files": names}}}
    mapping["zarr.json"] = json.dumps(root).encode()
    return refkv.ReferenceStore(mapping, sources)


async def zarrify_all(paths: list[Path]) -> list[refkv.ReferenceStore]:
    """Each file's store, the files zarrified together."""
    return list(await asyncio.gather(*(zarrify_async(str(p)) for p in paths)))


def main(directory: Path) -> None:
    directory = directory.resolve()
    paths = sorted(directory.glob("*.tif"))
    sidecar = directory / "stack.vzip"
    stacked = stack(asyncio.run(zarrify_all(paths)), [p.name for p in paths])
    print(stacked.save(sidecar, relative_to=f"{directory}{os.sep}"))  # zarrify keeps the path's own separator

    image = zarr.open_group(refkv.zarr.open(str(sidecar)), mode="r").get_array("0")
    print(f"{sidecar}: {image.shape} {image.dtype}, chunks {image.chunks}")
    for z, path in enumerate(paths):
        assert np.array_equal(image[z], tifffile.imread(path)), path
    print(f"all {len(paths)} planes match their files")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        directory = Path(sys.argv[1])
    else:
        directory = Path("tiff_stack")
        synthetic_stack(directory)
    main(directory)
```

It prints:

```text
.../tiff_stack/stack.vzip: 98 keys, 96 of them references to 24 sources; 13,156 bytes
.../tiff_stack/stack.vzip: (24, 256, 256) uint16, chunks (1, 128, 128)
all 24 planes match their files
```
