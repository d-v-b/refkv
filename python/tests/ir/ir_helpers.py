"""Helpers shared by the IR test modules (a plain module, named apart from python/tests/helpers.py:
both directories are on sys.path)."""

import io
import json
import sys
from contextlib import contextmanager
from pathlib import Path

import numpy as np
import pytest
import tifffile
import zarr

import refkv.zarr
from refkv.ir import virtualize as via_ir
from refkv.ir.cmirror import rebuild_from_archive
from refkv.virtualize import Rejected, virtualize
from refkv.zarr import ZarrStore

FIXTURES = Path(__file__).parents[3] / "fixtures"
ROOT = Path(__file__).parents[3]
# the frozen reference twins the IR is compared against (refkv_reference)
sys.path.insert(0, str(ROOT / "conformance" / "virtualize" / "reference"))
from refkv_reference import virtualize as reference  # noqa: E402  (the frozen reference)


def assert_rebuilds(out, data: bytes, tmp_path, name: str = "a.vzip") -> None:
    """The archive of `out`, written to `tmp_path / name`, rebuilds `data` from its IR mirror
    alone (conventions §8.4
    <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#84-reading-the-source-back>)."""
    out.write(str(tmp_path / name))
    buf = io.BytesIO()
    rebuild_from_archive(str(tmp_path / name), lambda o, n: data[o:o + n], buf.write)
    assert buf.getvalue() == data


def virtualized(path, tmp_path, fmt: str, url: str | None = None):
    """(the output, the hierarchy's root group, read in memory from the output, the IR) of the
    file at `path`, which the IR reads as `fmt`; the IR is valid and the archive rebuilds the
    file."""
    fmt_read, out, ir = via_ir(str(path), url)
    assert fmt_read == fmt
    ir.check()
    assert_rebuilds(out, path.read_bytes(), tmp_path)
    return out, zarr.open_group(refkv.zarr.store(out.to_store()), mode="r", zarr_format=3), ir


def as_the_reference(path, tmp_path, fmt: str, *, same_message: bool = False):
    """The IR's output of the file at `path` is the frozen reference's but `refkv_source`, its IR
    is valid and its archive rebuilds the file; or both reject it (with the same message, when
    `same_message`). The IR's output, or None when rejected."""
    url = f"https://data.test/{path.name}"
    try:
        _, ref = reference(str(path), url)
    except Rejected as e:
        with pytest.raises(Rejected) as got:
            via_ir(str(path), url)
        if same_message:
            assert str(got.value) == str(e)
        return None
    out, _, _ = virtualized(path, tmp_path, fmt, url)
    same_hierarchy(ref, out)
    return out


@contextmanager
def core_setting(setter, value):
    """The core's test setting `setter` (`_core._set_chunk_frames`, `_core._set_copy_threshold`)
    at `value` within the block, and at its default (None) after it."""
    setter(value)
    try:
        yield
    finally:
        setter(None)


# The members of a frozen root's source metadata the IR's mirror holds instead, for the
# profiles frozen at revision 25 (conformance/virtualize/compare.py's MIRRORED).
MIRRORED = {"nifti": ("header_rest", "extender", "extensions", "extensions_truncated", "unparsed", "trailing")}
# The members of the root image's `ome` the IR writes and the frozen reference does not, by
# profile (conformance/virtualize/compare.py's ADDED): TIFF's and NDPI's `omero`, added after
# the freeze (spec/virtualize/tiff.md §4.4
# <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/tiff.md#44-arrays>, spec/virtualize/ndpi.md §4
# <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/ndpi.md#4-the-image>).
ADDED = {"tiff": ("omero",), "ndpi": ("omero",)}
# The fixtures whose layout the profile changed after the freeze, which the frozen reference
# does not give (conformance/virtualize/compare.py's REVISED): NDPI's frames that are not their
# level's size (spec/virtualize/ndpi.md §7
# <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/ndpi.md#7-ndpi-profile>).
REVISED = {"edge_ndpi_frames.ndpi", "edge_reject_ndpi_frame.ndpi"}


def same_hierarchy(a, b) -> None:
    """The frozen reference's output `a` and the IR's `b` hold the same hierarchy but
    the source metadata node: documents, references and data sources. The reference
    writes the names of the revision it implements (refkv_reference.legacy), read as the
    current ones, and its root records that revision (refkv_reference.revision: 20, or 25
    for DICOM, NIfTI, IMS and NDPI), which is dropped: the shipped code records none.
    The members of `b`'s root image the reference does not write (ADDED) are dropped."""
    from refkv_reference.legacy import SOURCE_NODE as LEGACY_NODE
    from refkv_reference.legacy import current_names
    from refkv_reference.revision import DICOM_REVISION, IMS_REVISION, REVISION_25
    from refkv_reference.revision import REVISION as FROZEN

    def node(o):
        # the source metadata node; under its old name only in the frozen reference's output
        return ("refkv_source", LEGACY_NODE) if o is a else ("refkv_source",)

    def docs(o):
        d = {k: json.loads(v) if k.endswith(".json") else v for k, v in o.bytes_entries.items()
             if not k.startswith(node(o))}
        if o is a:
            d = current_names(d)
        prop = d.get("zarr.json", {}).get("attributes", {}).get("refkv_virtualized", {})
        moved = MIRRORED.get(prop.get("profile"))
        frozen = REVISION_25 if moved else {"dicom": DICOM_REVISION, "ims": IMS_REVISION, "ndpi": 25}.get(prop.get("profile"), FROZEN)
        if o is a and prop.get("revision") == frozen:
            del prop["revision"]
            for k in moved or ():
                prop[prop["profile"]].pop(k, None)
        if o is b:
            for k in ADDED.get(prop.get("profile"), ()):
                d["zarr.json"]["attributes"].get("ome", {}).pop(k, None)
        return d

    def refs(o):
        return {k: v for k, v in o.refs.items() if not k.startswith(node(o))}

    assert docs(a) == docs(b)
    assert refs(a) == refs(b)
    assert list(a.data) == list(b.data)


# The frozen twins' source metadata node, of their revision, and a key of theirs as the
# shipped code names it.
LEGACY_SOURCE = "vzip_source/"


def as_current(key: str) -> str:
    return "refkv_source/" + key[len(LEGACY_SOURCE):] if key.startswith(LEGACY_SOURCE) else key


def current_names(value):
    """A frozen twin's documents with the current names (refkv_reference.legacy)."""
    from refkv_reference.legacy import current_names

    return current_names(value)


# The codecs whose chunks the TIFF and QPTIFF profiles decode to sample their `omero` windows:
# uncompressed, Deflate and LZW tiles or strips, with or without interleaved samples.
DECODED = {"transpose", "bytes", "zlib", "imagecodecs_lzw"}


def omero_and_expected_windows(path, profile: str) -> tuple[dict | None, list]:
    """The `omero` of the file at `path` virtualized, and each channel's window as the TIFF and
    QPTIFF profiles give it (spec/virtualize/tiff.md §4.4
    <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/tiff.md#44-arrays>,
    spec/virtualize/qptiff.md §4.3
    <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/qptiff.md#43-arrays>), computed here
    with numpy from the coarsest level read through zarr-python: for a channel of a decoded codec
    whose samples are not RGB, the 5th and 99.9th percentiles of the middle chunk's values inside
    the array, without the fill value 0 (and values that are not finite), when they differ;
    `min`, `max` an integer type's range or the float values' extremes. Otherwise an integer
    type's range, else None."""
    g = zarr.open_group(ZarrStore(virtualize(str(path))), mode="r", zarr_format=3)
    ms = g.attrs["ome"]["multiscales"][0]
    a = g[ms["datasets"][-1]["path"]]
    axes = [x["name"] for x in ms["axes"]]
    count = a.shape[axes.index("c")] if "c" in axes else 1
    with tifffile.TiffFile(path) as tf:
        page = tf.pages[0]
        spp, pi, comp = page.samplesperpixel, page.photometric, page.compression
    rgb = spp == 3 if profile == "qptiff" else spp > 1 and (pi == 2 or (pi == 6 and comp == 7))
    decoded = {c["name"] for c in a.metadata.to_dict()["codecs"]} <= DECODED
    integer = np.issubdtype(a.dtype, np.integer)
    windows = []
    for ch in range(count):
        window = None
        if decoded and not rgb:
            at = []
            for name, n, k in zip(axes, a.shape, a.chunks):
                start = 0 if name == "t" else ch if name == "c" else n // 2 // k * k
                at.append(slice(start, start + (1 if name in "tc" else k)))
            v = np.sort(a[tuple(at)].ravel())
            v = v[v != 0] if integer else v[np.isfinite(v) & (v != 0)]
            n = len(v)
            if n and v[n * 5 // 100] < v[min(n - 1, n * 999 // 1000)]:
                lo, hi = v[n * 5 // 100].item(), v[min(n - 1, n * 999 // 1000)].item()
                extremes = [np.iinfo(a.dtype).min, np.iinfo(a.dtype).max] if integer else [v[0].item(), v[-1].item()]
                window = {"min": int(extremes[0]) if integer else extremes[0],
                          "max": int(extremes[1]) if integer else extremes[1], "start": lo, "end": hi}
        if window is None and integer:
            info = np.iinfo(a.dtype)
            window = {"min": int(info.min), "max": int(info.max), "start": int(info.min), "end": int(info.max)}
        windows.append(window)
    return g.attrs["ome"].get("omero"), windows
