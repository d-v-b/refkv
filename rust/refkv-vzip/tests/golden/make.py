"""Writes the golden archives of tests/golden.rs with the Python writer (python/src/refkv/vzip/archive.py's
VZipWriter): the Rust writer must write each one byte for byte.

    uv run python rust/refkv-vzip/tests/golden/make.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from refkv.pb import Range, Source
from refkv.vzip.archive import VZipWriter

HERE = Path(__file__).parent


def crc(url: str, offset: int, length: int) -> int:
    """A stand-in for a range's CRC-32C: the Rust test computes the same."""
    return (len(url) * 1000 + offset * 10 + length) & 0xFFFFFFFF


def entries(w: VZipWriter) -> None:
    a = w.source(Source(url="data/a b.bin".replace(" ", "%20"), size=1000, etag='"v1"'))
    h = w.blob(b"HEADER")
    k = w.internal("__vz__/shared")
    w.source(Source(url="http://h/x", modified_not_after=-5))
    w.add_hidden("shared", b"0123456789", compress=False)
    w.add_bytes("x/zarr.json", b'{"zarr_format":3,"node_type":"array"}', late=True)
    w.add_bytes("zarr.json", b'{"zarr_format":3,"node_type":"group"}' * 3, late=True)
    w.add_bytes("x/attrs", b"attributes " * 50, compress=True)
    w.add_bytes("x/raw", bytes(range(256)))
    w.add_bytes("x/empty", b"")
    w.add_ranges("x/c/0", [Range(source=a, offset=10, length=4)])
    w.add_ranges("x/c/1", [Range(source=h, offset=0, length=6), Range(source=a, offset=300, length=70000),
                           Range(data=b"\x00\xff")])
    w.add_ranges("x/c/2", [Range(source=k, offset=2, length=3)])
    w.add_ranges("x/c/3", [Range(source=a, offset=0, length=0, crc32c=7)])
    for i in range(40):
        w.add_ranges(f"y/c/{i}", [Range(source=a, offset=i * 25, length=25)])


def main() -> None:
    cases = {
        "basic": dict(mirror_refs=True, page_size=None),
        "paged": dict(mirror_refs=False, page_size=200),
        "checksums": dict(mirror_refs=True, page_size=65536, checksum=crc),
    }
    for name, kw in cases.items():
        with open(HERE / f"{name}.vzip", "wb") as f:
            w = VZipWriter(f, **kw)
            entries(w)
            w.close()
    for name, page_size in (("empty", None), ("empty_paged", 1)):
        with open(HERE / f"{name}.vzip", "wb") as f:
            VZipWriter(f, page_size=page_size).close()
    with open(HERE / "bulk.vzip", "wb") as f:
        w = VZipWriter(f, page_size=100, checksum=crc)
        w.add_url_refs(((f"a/{i}", f"http://h/s/a/{i}", i * 3) for i in range(30)), pin_size=True)
        w.add_url_refs(((f"b/{i}", f"http://h/s/b/{i}", 5) for i in range(3)))
        w.add_bytes("a/zarr.json", b"{}", late=True)
        w.close()
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "f.bin"
        p.write_bytes(b"file bytes " * 100)
        with open(HERE / "file.vzip", "wb") as f:
            w = VZipWriter(f)
            w.add_file("f", p)
            w.add_bytes("g", b"g")
            w.close()


if __name__ == "__main__":
    sys.exit(main())
