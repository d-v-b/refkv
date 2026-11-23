"""The package's vzip writer (rust/refkv-vzip, through `refkv._core.VzipWriter`) writes, byte for
byte, the archive the pure-Python writer it replaced writes (conformance/vzip/python_writer.py):
for every feature and option of the writer, and for the archives of zarrified fixtures."""

from __future__ import annotations

import io
import sys
import zlib
from pathlib import Path

import pytest

import refkv.vzip.archive
from refkv.pb import Range, Source
from refkv.zarrify import zarrify

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "conformance" / "vzip"))
from python_writer import VZipWriter as PyVZipWriter  # noqa: E402 (the reference writer)


def checksum(url: str, offset: int, length: int) -> int:
    return zlib.crc32(f"{url}:{offset}:{length}".encode())


def build(cls: type, tmp: Path, *, mirror_refs: bool, page_size: int | None, check: bool) -> bytes:
    """An archive of every kind of entry and source, written by `cls`."""
    data = tmp / "shard.bin"
    data.write_bytes(bytes(range(256)) * 9)
    f = io.BytesIO()
    w = cls(f, mirror_refs=mirror_refs, page_size=page_size, checksum=checksum if check else None)
    ext = w.url("https://example.org/a%20b/x.tif")
    blob = w.blob(b"shared header")
    w.add_bytes("zarr.json", b'{"zarr_format":3,"node_type":"group"}', late=True)
    w.add_bytes("a/zarr.json", b'{"zarr_format":3}' * 40, compress=True, late=True)
    w.add_bytes("a/c/0", b"x" * 5000, compress=True)
    w.add_bytes("a/c/1", b"stored")
    w.add_file("a/c/2", data)
    hidden = w.add_hidden("idx/0", b"0123456789", compress=False)
    w.add_ranges("b/c/0", [Range(source=ext, offset=10, length=100)])
    w.add_ranges("b/c/1", [Range(source=blob, length=6), Range(data=b"lit"), Range(source=ext, offset=7, length=3)])
    w.add_ranges("b/c/2", [Range(source=w.internal(hidden), offset=2, length=4)])
    w.add_ranges("b/c/3", [Range(source=w.internal("a/c/1"), length=6, crc32c=7)])
    w.add_ref("b/c/4", "relative.bin", 0, 1)
    w.add_url_refs(((f"d/c/{i}", f"https://example.org/o{i}", 100 + i) for i in range(300)), pin_size=True)
    w.add_url_refs([("e/c/0", "https://example.org/e", 5)])
    for i in range(200):
        w.add_ranges(f"f/c/{i:03}", [Range(source=ext, offset=i, length=1)])
    w.source(Source(url="https://example.org/pinned", size=12, etag='"e"'))
    w.close()
    assert w.format_sizes[refkv.vzip.archive.SOURCES_KEY] > 0
    return f.getvalue()


@pytest.mark.parametrize("mirror_refs", [True, False])
@pytest.mark.parametrize("page_size", [None, 64, 1 << 16])
@pytest.mark.parametrize("check", [False, True])
def test_writes_what_the_python_writer_writes(tmp_path: Path, mirror_refs: bool, page_size: int | None, check: bool):
    kw = {"mirror_refs": mirror_refs, "page_size": page_size, "check": check}
    assert build(refkv.vzip.archive.VZipWriter, tmp_path, **kw) == build(PyVZipWriter, tmp_path, **kw)


FIXTURES = ["tiff/strips_uint16_pyramid_short.ome.tif", "nifti/nifti_block_x_split.nii", "hdf5/hdf5_filters.h5",
            "mp4/mp4_h264_aac.mp4", "czi/czi_bgr24_raw.czi", "fits/fits_bintable.fits", "zarr2/zarr2_hierarchy"]


@pytest.mark.parametrize("fixture", FIXTURES)
def test_a_zarrified_archive_is_the_python_writer_s(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixture: str):
    path = ROOT / "fixtures" / fixture
    if not path.exists():
        pytest.skip(f"no fixture {fixture}")
    url = f"https://data.example/{fixture}/" if path.is_dir() else None
    store = zarrify(str(path), url)
    store.save_vzip(tmp_path / "rust.vzip")
    monkeypatch.setattr(refkv.vzip.archive, "VZipWriter", PyVZipWriter)
    store.save_vzip(tmp_path / "python.vzip")
    assert (tmp_path / "rust.vzip").read_bytes() == (tmp_path / "python.vzip").read_bytes()
