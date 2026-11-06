# A stack of TIFF files as one TIFF file

refkv stores are not only Zarr: a key can hold any bytes, and a reference can
assemble them from pieces of other files. This example turns a directory of
TIFF files, one per plane, into one multi-page TIFF file, `stack.tif`, held as
the only key of `stack.vzip`, a sidecar beside the files. Reading the key gives
a byte-exact classic TIFF file that tifffile, Pillow or libtiff opens, and whose
pages are the files' pages in name order. No pixel is copied or decoded. For
the same stack as one Zarr array, see [tiff-stack.md](tiff-stack.md).

## How it works

- **New IFDs, as literal bytes.** The example reads each file's IFDs with
  tifffile (not its pixels) and writes a TIFF header and one IFD per page,
  with the page's tags as they are stored. These bytes are one `data` source
  of the sidecar, which a reader does not fetch from anywhere.
- **Strips and tiles, as ranges.** Each page's strips or tiles stay in its
  file: the value of `stack.tif` is a `Concat` of the header's bytes, then
  every strip or tile as a range of its file, in order. The new IFDs'
  `StripOffsets` or `TileOffsets` point at where those ranges land in
  `stack.tif`. Strips that follow each other in a file are one range.
- **Every coding.** A page keeps its compression, predictor, tiling, data
  type and samples, so the files need not agree on any of them. A reader that
  stacks pages into one array, such as `tifffile.imread(..., key=slice(None))`,
  still needs them to agree.
- **Relative URLs.** As in [tiff-stack.md](tiff-stack.md), the source URLs
  are written relative to the sidecar, so the directory can move with it.

What the new file cannot hold is refused with a ValueError: a BigTIFF file,
files of different byte orders, a page without strips or tiles, an old-style
JPEG page (its `JPEGInterchangeFormat` offset is not rewritten), and a stack of
4 GiB or more. So is a stack of more than 1024 files: reading `stack.tif`
reads each file at least once, and a reader with the default policy makes at
most 1024 reads for one value
([§6.7](../spec/refkv.md#67-reader-policy)). Tags that point at other
structures of a file (SubIFDs, Exif, GPS and Interoperability IFDs, and free
space) are left out, so a pyramid's levels in SubIFDs are not in the stack. A
tag that describes a whole file, such as OME-XML or ImageJ metadata in
`ImageDescription`, is copied with its page and still describes that file.

## The example

Without arguments, the example first writes a synthetic stack of 24 TIFFs to
`./tiff_concat/`, in turn as strips, zlib-compressed strips, tiles and
zlib-compressed tiles. With a directory, it concatenates the directory's
`*.tif` files in name order. It then reads `stack.tif` back with refkv and
checks every page against tifffile. The file is
[examples/tiff_concat.py](examples/tiff_concat.py). uv builds refkv from
GitHub, which needs a Rust toolchain, until a release on PyPI has `save_vzip`;
refkv 0.2.0 does not:

<!-- include: examples/tiff_concat.py -->
```python
# /// script
# requires-python = ">=3.12"
# dependencies = ["refkv", "tifffile", "numpy"]
#
# [tool.uv.sources]
# refkv = { git = "https://github.com/d-v-b/refkv", subdirectory = "python" }
# ///
"""A stack of TIFF files as one multi-page TIFF file, saved as a vzip sidecar beside them.

The sidecar holds one key, `stack.tif`, whose value is a reference: a classic TIFF header
and one new IFD per input page (literal bytes, a `data` source), then every page's strips
or tiles as ranges of the file they are in. Reading the key gives a byte-exact TIFF file,
which tifffile or any TIFF reader opens; no pixel is copied or decoded, and nothing of it
is Zarr. Each page keeps its own compression, tiling and tags.

Run with `uv run docs/examples/tiff_concat.py [DIR]`. Without DIR, it first writes a
synthetic stack of 24 TIFFs to ./tiff_concat/, in four codings. It saves DIR/stack.vzip,
reads `stack.tif` back with refkv and checks every page against tifffile.
"""

import io
import struct
import sys
from pathlib import Path
from typing import Any

import numpy as np
import tifffile

import refkv
from refkv.policy import MAX_READS
from refkv.uri import path_reference

# offset tags, by the tag of their byte counts: rewritten to the new file's offsets
OFFSETS = {273: 279, 324: 325}  # StripOffsets, TileOffsets
# tags that point at other structures of the file they came from, which the new file does not
# have: left out (SubIFDs, Exif, GPS and Interoperability IFDs, FreeOffsets and FreeByteCounts)
DROPPED = {330, 34665, 34853, 40965, 288, 289}
# tags that point at a page's data in a way this example does not rewrite: refused
REFUSED = {513: "an old-style JPEG page (JPEGInterchangeFormat)"}
SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4}
LIMIT = 2**32  # the size of a classic TIFF file, whose offsets are 32-bit


def synthetic_stack(directory: Path, planes: int = 24) -> None:
    """Write `planes` 256 x 256 uint16 TIFFs, slices of a ball, in turn: strips, zlib strips,
    tiles and zlib tiles."""
    directory.mkdir(exist_ok=True)
    y, x = np.mgrid[-128:128, -128:128]
    tiles = {"tile": (128, 128)}
    codings: list[dict[str, Any]] = [{}, {"compression": "zlib"}, tiles, {**tiles, "compression": "zlib"}]
    for z in range(planes):
        r2 = x**2 + y**2 + ((z - planes / 2) * 10) ** 2
        plane = (60000 * np.exp(-r2 / 2 / 60**2)).astype(np.uint16)
        tifffile.imwrite(directory / f"z{z:03}.tif", plane, photometric="minisblack", **codings[z % 4])


def read_pages(paths: list[Path]) -> tuple[str, list[tuple[int, list, int, list[tuple[int, int]]]]]:
    """The byte order of the inputs, and each page of them in order: (input, [(tag, type,
    count, value bytes)], offset tag, [(offset, length)] of its strips or tiles). Reads the
    inputs' IFDs, not their pixels."""
    order, pages = None, []
    for i, path in enumerate(paths):
        with tifffile.TiffFile(path) as tf:
            if tf.is_bigtiff:
                raise ValueError(f"{path.name}: a BigTIFF file, which a classic TIFF cannot hold")
            if order not in (None, tf.byteorder):
                raise ValueError(f"{path.name}: byte order {tf.byteorder!r}, not the first file's {order!r}")
            order, fh = tf.byteorder, tf.filehandle
            for n, page in enumerate(tf.pages):
                assert isinstance(page, tifffile.TiffPage)  # a page, not a frame: tifffile's default
                tags, data = [], None
                for tag in page.tags:
                    if tag.code in REFUSED:
                        raise ValueError(f"{path.name}, page {n}: {REFUSED[tag.code]}")
                    if tag.code in DROPPED or tag.code in OFFSETS.values():
                        continue
                    if tag.code in OFFSETS:
                        counts = page.tags[OFFSETS[tag.code]].value
                        data = (tag.code, list(zip(tag.value, counts, strict=True)))
                        continue
                    fh.seek(tag.valueoffset)  # the value's bytes as stored, inline or not
                    tags.append((tag.code, tag.dtype, tag.count, fh.read(tag.count * SIZES[tag.dtype])))
                if data is None:
                    raise ValueError(f"{path.name}, page {n}: no strips or tiles")
                pages.append((i, tags, *data))
    if order is None:
        raise ValueError("no TIFF files to concatenate")
    return order, pages


def concat(paths: list[Path]) -> refkv.ReferenceStore:
    """A store of one key, `stack.tif`: the pages of `paths`, in order, as one classic TIFF.

    Raises ValueError for a BigTIFF input, inputs of different byte orders, a page without
    strips or tiles, an old-style JPEG page, a stack of 4 GiB or more, or more files than a
    reader with the default policy reads for one value.
    """
    if len(paths) > MAX_READS:
        raise ValueError(f"{len(paths)} files: reading the stack reads each of them, and a reader "
                         f"with the default policy makes at most {MAX_READS} reads for one value")
    order, pages = read_pages(paths)
    bo = "<" if order == "<" else ">"

    def entries(tags, code, parts, offsets):
        """A page's IFD entries, its offset and byte-count tables included, sorted by tag."""
        n = len(parts)
        return sorted([*tags, (code, 4, n, struct.pack(f"{bo}{n}I", *offsets)),
                       (OFFSETS[code], 4, n, struct.pack(f"{bo}{n}I", *(length for _, length in parts)))])

    # where each IFD goes: after the header, each IFD followed by its values that are not inline
    ifd_at, at = [], 8
    for _, tags, code, parts in pages:
        ifd_at.append(at)
        es = entries(tags, code, parts, [0] * len(parts))
        at += 2 + 12 * len(es) + 4 + sum(len(v) + len(v) % 2 for *_, v in es if len(v) > 4)
    if at + sum(length for *_, parts in pages for _, length in parts) >= LIMIT:
        raise ValueError("the stack is 4 GiB or more, which a classic TIFF cannot hold")

    head = bytearray((b"II*\0" if bo == "<" else b"MM\0*") + struct.pack(bo + "I", ifd_at[0]))
    ranges: list[list[int]] = []  # [input, offset, length], in the new file's order
    cursor = at  # where the next strip or tile goes: after the IFDs
    for k, (i, tags, code, parts) in enumerate(pages):
        offsets = []
        for offset, length in parts:
            offsets.append(cursor if length else 0)
            cursor += length
            if not length:
                continue
            if ranges and ranges[-1][0] == i and ranges[-1][1] + ranges[-1][2] == offset:
                ranges[-1][2] += length  # the next bytes of the same file: one range
            else:
                ranges.append([i, offset, length])
        es = entries(tags, code, parts, offsets)
        values_at = ifd_at[k] + 2 + 12 * len(es) + 4
        ifd, values = bytearray(struct.pack(bo + "H", len(es))), bytearray()
        for c, t, n, v in es:
            if len(v) <= 4:
                field = v.ljust(4, b"\0")
            else:
                field = struct.pack(bo + "I", values_at + len(values))
                values += v + b"\0" * (len(v) % 2)  # a value starts on a word boundary
            ifd += struct.pack(bo + "HHI", c, t, n) + field
        ifd += struct.pack(bo + "I", ifd_at[k + 1] if k + 1 < len(pages) else 0)
        head += ifd + values

    sources = [refkv.Source(url=path_reference(str(p)), size=p.stat().st_size) for p in paths]
    sources.append(refkv.Source(data=bytes(head)))
    value = refkv.Concat((refkv.Range(source=len(paths), offset=0, length=len(head)),
                          *(refkv.Range(source=i, offset=o, length=n) for i, o, n in ranges)))
    return refkv.ReferenceStore({"stack.tif": value}, sources)


def main(directory: Path) -> None:
    directory = directory.resolve()
    paths = sorted(directory.glob("*.tif"))
    sidecar = directory / "stack.vzip"
    print(concat(paths).save_vzip(sidecar, relative_to=path_reference(str(directory)) + "/"))

    data = refkv.open(str(sidecar)).get("stack.tif")
    assert data is not None
    with tifffile.TiffFile(io.BytesIO(data)) as tf:
        print(f"stack.tif: {len(data):,} bytes, {len(tf.pages)} pages")
        planes = [page.asarray() for page in tf.pages]
    expected = []
    for p in paths:
        with tifffile.TiffFile(p) as tf:
            expected += [page.asarray() for page in tf.pages]
    assert len(planes) == len(expected)
    for z, (got, want) in enumerate(zip(planes, expected, strict=True)):
        assert np.array_equal(got, want), z
    print(f"all {len(planes)} pages match their files")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        directory = Path(sys.argv[1])
    else:
        directory = Path("tiff_concat")
        synthetic_stack(directory)
    main(directory)
```

It prints:

```text
.../tiff_concat/stack.vzip: 1 keys, 1 of them references to 25 sources; 2,059 bytes
stack.tif: 2,846,790 bytes, 24 pages
all 24 pages match their files
```
