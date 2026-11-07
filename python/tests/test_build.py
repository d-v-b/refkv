"""refkv.build, the tools to build an output from the IR (the design of d-v-b/refkv#278): `read`
and `Parsed`, values, `Array` and `Written`, `seal` and `project`."""

import dataclasses
import importlib
import io
import json
import struct
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import tifffile

import refkv
from refkv import _sync
from refkv.build import (
    MAX_PAYLOAD,
    Array,
    Binding,
    Check,
    Copy,
    Data,
    Literal,
    Ref,
    SealError,
    Shared,
    Value,
    Written,
    Zeros,
    concat,
    cut,
    fits,
    payload_size,
    project,
    read,
    seal,
    value,
)
from refkv.build.seal import _binding_holds
from refkv.build.values import _issue
from refkv.policy import Policy
from refkv.zarrify import zarrify

# the module, which the package's `seal` function shadows as an attribute
seal_module = importlib.import_module("refkv.build.seal")
ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "fixtures"

# a fixture or two of each file profile (and files read as stores), and whether the call asks for checksums
CASES = [
    ("czi/czi_gray16_tczyx.czi", False),
    ("mp3/mp3_abr_xing.mp3", False),
    ("nifti/nifti_block_rows_edge_rgba.nii", False),
    ("zarr2/zarr2_codecs/", False),  # a store, named under a URL it is served from
    ("n5/n5_compressions/", False),
    ("zarr2/zarr2_root_array_objects/", False),  # a root array, its mirror's objects below it
    ("dicom/dicom_bigendian_mono16.dcm", False),
    ("fits/fits_gzip_image.fits.gz", False),
    ("flac/flac_mono_16.flac", False),
    ("grib/grib_complex.grib2", False),
    ("hdf5/hdf5_compounds.h5", False),
    ("ims/ims_2d_no_metadata.ims", False),
    ("lif/lif_xlef_tree.xlef", False),
    ("mp4/mov_h264_aac.mov", False),
    ("mrc/mrc_chunk_rows.mrc", True),
    ("nd2/nd2_padded_rgb.nd2", False),
    ("ndpi/ndpi_levels.ndpi", False),
    ("npy/npy_c_float64.npy", False),
    ("nrrd/nrrd_detached_raw.nhdr", False),
    ("qptiff/qptiff_fl_uint8_lzw.qptiff", False),
    ("safe/safe_l1c.SAFE.zip", False),
    ("safetensors/st_dtypes.safetensors", False),
    ("tiff/bilevel_tiled.tif", False),
    ("tiff/strips_uint8.tif", True),
    ("vsi/vsi_png.vsi", False),
    ("zarr3/zarr3_hierarchy.zip", False),
]


@pytest.mark.parametrize(("name", "checksums"), CASES, ids=[n for n, _ in CASES])
def test_seal_of_the_built_in_output_reads_what_zarrify_writes(name, checksums):
    """`seal` of `project`'s output is the store `zarrify` writes: the same source table, numbered
    by the core's one rule for both, and the same entries, the values the built-in projection made
    as checked values of the input's sources."""
    path = str(FIXTURES / name) + ("/" if name.endswith("/") else "")
    url = f"https://example.invalid/{name}" if name.endswith("/") else None  # a local store needs one
    with read(path, url, checksums=checksums) as p:
        store = seal(project(p).written, [p])
        built = zarrify(path, url, checksums=checksums)
        assert store.sources == built.sources
        assert store.mapping == built.mapping
        assert sorted(store.read_at_open) == sorted(built.read_at_open)


# ---- the TIFF stack, built from the IR: one TIFF file of every page of several (test case 2)

OFFSETS = {273: 279, 324: 325}  # StripOffsets, TileOffsets, by their byte counts
LEFT_OUT = {330, 34665, 34853, 40965, 288, 289, 400}  # tags that point elsewhere in the old file
SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}


def _pages(p):
    """Each page of `p`'s main IFD chain: its entries (tag, type, count, value bytes), and its strips
    or tiles as (offset, length), from the IR: tags by `ir.value`, their values by `bytes`."""
    ir, out, n = p.ir, [], 0
    while ir.at(f"ifds/{n}") is not None:
        entries, parts = [], None
        tags = sorted({int(path.split("/")[3]) for i in range(len(ir))
                       if (path := ir.path(i)).startswith(f"ifds/{n}/tags/") and "~" not in path.split("/")[3]})
        for tag in tags:
            entry = ir.value(ir.index(f"ifds/{n}/tags/{tag}/entry"))
            held = ir.at(f"ifds/{n}/tags/{tag}/value")
            if tag in LEFT_OUT or tag in OFFSETS.values() or held is None or entry["type"] not in SIZES:
                continue
            if tag in OFFSETS:
                counts = ir.value(ir.index(f"ifds/{n}/tags/{OFFSETS[tag]}/value"))
                parts = (tag, list(zip(ir.value(held), counts, strict=True)))
                continue
            entries.append((tag, entry["type"], entry["count"], p.bytes(held)))
        assert parts is not None
        out.append((entries, *parts))
        n += 1
    return out


def stack(inputs):
    """`stack.tif`: the pages of `inputs`, in order, as one classic TIFF, a header and new IFDs as a
    `Data` part, then every strip or tile as a range of its file."""
    order = {p.ir.value(p.ir.index("header/byte_order")) for p in inputs}
    assert len(order) == 1
    bo = "<" if order.pop() == "II" else ">"
    pages = [(p, *page) for p in inputs for page in _pages(p)]

    def entries(tags, code, parts, offsets):
        n = len(parts)
        return sorted([*tags, (code, 4, n, struct.pack(f"{bo}{n}I", *offsets)),
                       (OFFSETS[code], 4, n, struct.pack(f"{bo}{n}I", *(m for _, m in parts)))])

    ifd_at, at = [], 8
    for _, tags, code, parts in pages:
        ifd_at.append(at)
        es = entries(tags, code, parts, [0] * len(parts))
        at += 2 + 12 * len(es) + 4 + sum(len(v) + len(v) % 2 for *_, v in es if len(v) > 4)
    head = bytearray((b"II*\0" if bo == "<" else b"MM\0*") + struct.pack(bo + "I", ifd_at[0]))
    strips, cursor = [], at
    for k, (p, tags, code, parts) in enumerate(pages):
        offsets = []
        for offset, length in parts:
            offsets.append(cursor)
            cursor += length
            strips.append(Ref(p, 0, offset, length))
        es = entries(tags, code, parts, offsets)
        values_at = ifd_at[k] + 2 + 12 * len(es) + 4
        ifd, values = bytearray(struct.pack(bo + "H", len(es))), bytearray()
        for c, t, n, v in es:
            if len(v) <= 4:
                field = v.ljust(4, b"\0")
            else:
                field = struct.pack(bo + "I", values_at + len(values))
                values += v + b"\0" * (len(v) % 2)
            ifd += struct.pack(bo + "HHI", c, t, n) + field
        ifd += struct.pack(bo + "I", ifd_at[k + 1] if k + 1 < len(pages) else 0)
        head += ifd + values
    return concat(Data(bytes(head)), *strips, merge=True)


@pytest.mark.parametrize("byteorder", ["<", ">"])
def test_a_stack_of_tiffs_built_from_the_ir(tmp_path, byteorder):
    """One TIFF of the pages of several, every tag but the pointers copied from the IR's bytes:
    its pages read back equal to the files', in both byte orders, across strips, tiles, zlib with
    a predictor, RGB, floats, and an OME-TIFF of several pages. A file of pages without OME-XML
    is not among them: until TIFF's plan lands, `read` still raises the image layout's rejection
    of it (the design of d-v-b/refkv#278, "What a caller writes")."""
    rng = np.random.default_rng(0)
    plane = rng.integers(0, 4000, (64, 96), dtype=np.uint16)
    inputs: dict[str, tuple[np.ndarray, dict[str, Any]]] = {
        "a": (plane, {}),
        "b": (plane, {"rowsperstrip": 7, "compression": "zlib", "predictor": True}),
        "c": (plane, {"tile": (32, 32)}),
        "d": (rng.integers(0, 255, (64, 96, 3), dtype=np.uint8), {"photometric": "rgb", "compression": "zlib"}),
        "e": (rng.random((3, 64, 96), dtype=np.float32), {"ome": True}),  # three planes, as OME-XML maps them
    }
    paths = []
    for name, (array, options) in inputs.items():
        paths.append(tmp_path / f"{name}.tif")
        kwargs: dict[str, Any] = {"photometric": "minisblack", **options}
        tifffile.imwrite(paths[-1], array, byteorder=byteorder, **kwargs)

    parsed = [read(p) for p in paths]
    store = seal(Written({"stack.tif": stack(parsed)}), parsed)
    for p in parsed:
        p.close()  # without checksums, sealing and saving read nothing more of an input
    store.save_vzip(tmp_path / "stack.vzip")
    data = refkv.open(str(tmp_path / "stack.vzip")).get("stack.tif")
    expected = []
    for path in paths:
        with tifffile.TiffFile(path) as tf:
            expected += [page.asarray() for page in tf.pages]
    assert data is not None
    with tifffile.TiffFile(io.BytesIO(data)) as tf:
        assert tf.byteorder == byteorder
        got = [page.asarray() for page in tf.pages]
    assert len(got) == len(expected) == 7
    for g, e in zip(got, expected, strict=True):
        np.testing.assert_array_equal(g, e)
    assert len(store.sources) == len(paths) + 1  # each file, then the header's data source


# ---- values

def test_values(tmp_path):
    """`cut`, `concat` and `payload_size` over every kind of part: what they read, and a payload
    measured with every source index at its largest, so that `fits` never passes a value `seal`
    refuses."""
    np.save(tmp_path / "x.npy", np.zeros(512))
    with read(tmp_path / "x.npy") as p:
        blob = b"header!!"
        v = concat(Literal(b"ab"), Ref(p, 0, 10, 20), Data(blob), Zeros(5), Shared(b"tables"))
        assert v.size == 2 + 20 + 8 + 5 + 6
        assert cut(v, 0, 2).parts == (Literal(b"ab"),)
        assert cut(v, 5, 12).parts == (Ref(p, 0, 13, 7),)
        assert cut(v, 25, 30).parts == (Data(blob, 3, 5),)
        assert cut(v, 30, 33).parts == (Zeros(3),)
        assert cut(v, 36, 38).parts == (Data(b"tables", 1, 2),)
        assert concat(Ref(p, 0, 0, 4), Ref(p, 0, 4, 4), merge=True).parts == (Ref(p, 0, 0, 8),)
        assert concat(Data(blob, 0, 3), Data(blob, 3, 5), merge=True).parts == (Data(blob, 0, 8),)
        assert concat(Ref(p, 0, 0, 4), Ref(p, 0, 4, 4)).parts == (Ref(p, 0, 0, 4), Ref(p, 0, 4, 4))
        assert payload_size(Ref(p, 0, 1, 1)) == 1 + 4 + 1 + 1 + 1 + 1  # source at its largest: 5 bytes
        assert payload_size(Ref(p, 0, 1, 1), checksums=True) == payload_size(Ref(p, 0, 1, 1)) + 6
        many = concat(*(Ref(p, 0, 2 * k, 1) for k in range(7000)))
        assert fits(many) is not None
        assert fits(Ref(p, 0, 0, 1)) is None
        sealed = seal(Written({"small": concat(*(Ref(p, 0, 2 * k, 1) for k in range(100)))}), [p])
        with read(tmp_path / "x.npy", checksums=True) as q:  # fits counts the checksums its input takes
            crcs = concat(*(Ref(q, 0, k, 1) for k in range(4096)))
            assert fits(crcs) is not None
            assert fits(crcs, checksums=False) is None
        small = sealed.mapping["small"]
        assert not isinstance(small, bytes)
        assert len(small.encode()) <= payload_size(
            concat(*(Ref(p, 0, 2 * k, 1) for k in range(100))))


def test_written_keeps_its_placements():
    """`Written` merges entries and placements with `|` and `|=`; a plain dict update keeps the
    entries alone."""
    a = Written({"x/zarr.json": b"{}"}, at_open={"x/zarr.json": False}, stored={"x/zarr.json"})
    b = Written({"y": b"y"}, at_open={"y": True})
    both = a | b
    assert set(both) == {"x/zarr.json", "y"}
    assert both.at_open == {"x/zarr.json": False, "y": True}
    assert both.stored == {"x/zarr.json"}
    a |= {"z": Literal(b"z")}
    assert a["z"] == Value((Literal(b"z"),))
    both["y"] = Literal(b"now a value")
    assert "y" not in both.at_open
    plain = dict(both)
    assert set(plain) == {"x/zarr.json", "y"}


def test_parsed(tmp_path):
    """The accessors `read` gives over a TIFF's IR: a strip's reference and span, a tag's bytes
    from the IR and a range's from the file, the element at a byte, and the gaps."""
    path = tmp_path / "a.tif"
    tifffile.imwrite(path, np.arange(64 * 96, dtype=np.uint16).reshape(64, 96), photometric="minisblack",
                     rowsperstrip=16, compression="zlib")
    with read(path) as p, tifffile.TiffFile(path) as tf:
        page = tf.pages[0]
        assert p.profile == "tiff"
        assert p.version == 0
        assert p.plain_file
        assert p.description is None
        assert p.sources[0].size == path.stat().st_size
        strip = p.ir.index("ifds/0/strips/1")
        assert p.reference(strip) == Value((Ref(p, 0, page.dataoffsets[1], page.databytecounts[1]),))
        assert p.span(strip) == Ref(p, 0, page.dataoffsets[1], page.databytecounts[1])
        assert p.element_at(0, page.dataoffsets[1] + 3) == (strip, None)
        assert p.bytes(p.ir.index("ifds/0/tags/305/value")).startswith(b"tifffile")
        whole = path.read_bytes()
        assert p.bytes((0, 0, 8), (0, 4, 8), (0, 0, 8)) == [whole[0:8], whole[4:12], whole[0:8]]  # read once
        head, raw = p.bytes((0, 0, 4), strip)
        assert head == b"II*\0"
        assert len(raw) == page.databytecounts[1]
        size = p.sources[0].size
        assert size is not None
        for gap in p.gaps():
            assert 0 <= gap.offset < size


SHARDED = [{"name": "sharding_indexed", "configuration": {
    "chunk_shape": [2, 3], "codecs": [{"name": "bytes"}],
    "index_codecs": [{"name": "bytes", "configuration": {"endian": "little"}}, {"name": "crc32c"}]}}]
COPIED = [{"name": "sharding_indexed", "configuration": {  # as hierarchy.copy transposes a sharded array
    "chunk_shape": [3, 2], "codecs": [{"name": "transpose", "configuration": {"order": [1, 0]}}, {"name": "bytes"}],
    "index_codecs": [{"name": "transpose", "configuration": {"order": [1, 0, 2]}},
                     {"name": "bytes", "configuration": {"endian": "little"}}, {"name": "crc32c"}]}}]
INNER_ONLY = [{"name": "sharding_indexed", "configuration": {  # an inner transpose alone: another array
    "chunk_shape": [3, 2], "codecs": [{"name": "transpose", "configuration": {"order": [1, 0]}}, {"name": "bytes"}],
    "index_codecs": [{"name": "bytes", "configuration": {"endian": "little"}}, {"name": "crc32c"}]}}]
TRANSPOSE = {"name": "transpose", "configuration": {"order": [1, 0]}}


@pytest.mark.parametrize(("built", "written", "expected"), [
    # (the array a chunk was built in, the array it is written under, what _binding_holds says)
    (([4, 6], [4, 6], [{"name": "bytes"}], 0), ([4, 6], [4, 6], [{"name": "bytes"}], 0), False),
    (([4, 6], [4, 6], [{"name": "bytes"}], 0), ([6, 4], [6, 4], [TRANSPOSE, {"name": "bytes"}], 0), False),
    (([4, 6], [4, 6], SHARDED, 0), ([4, 6], [4, 6], SHARDED, 0), True),
    (([4, 6], [4, 6], SHARDED, 0), ([6, 4], [6, 4], [TRANSPOSE, *SHARDED], 0), True),
    (([4, 6], [4, 6], SHARDED, 0), ([6, 4], [6, 4], COPIED, 0), True),
    (([4, 6], [4, 6], SHARDED, 0), ([6, 4], [6, 4], INNER_ONLY, 0), None),
    (([4, 6], [4, 6], [{"name": "bytes"}], 0), ([4, 7], [4, 7], [{"name": "bytes"}], 0), None),
    (([4, 6], [4, 6], [{"name": "bytes"}], {"b": 0, "a": 1}), ([4, 6], [4, 6], [{"name": "bytes"}], {"a": 1, "b": 0}),
     False),  # a struct's fill value, its members in another order
    (([4, 6], [4, 6], [{"name": "bytes"}], 0), ([4, 6], [4, 6], [{"name": "bytes"}], 0.0), None),
], ids=["as-built", "transposed", "sharded", "sharded-transposed", "copy-of-sharded", "inner-transpose-only",
        "other-shape", "struct-fill", "other-fill"])
def test_where_a_check_holds(tmp_path, built, written, expected):
    """A checked chunk's binding holds in the array it was built in, and under Zarr's transpose codec
    put first (outside a shard, or as `copy` writes it inside one) with the shape, grid and key
    permuted to match: True in a sharded array, where its rule lifts the read rule, False in another;
    None anywhere else."""
    np.save(tmp_path / "x.npy", np.zeros((4, 6), dtype=np.uint8))
    with read(tmp_path / "x.npy") as p:
        shape, chunks, codecs, fill = built
        doc = {"shape": shape, "data_type": "uint8", "codecs": codecs, "fill_value": fill,
               "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": chunks}}}
        parts = (Ref(p, 0, 0, 1),)
        checked = Value(parts, _issue("builtin", Binding.of(doc, (0, 0)), parts))
        shape, chunks, codecs, fill = written
        out = Array(shape, "uint8", chunks, codecs, {(0, 0): checked}, fill).entries(to="a")
        v = out["a/c/0/0"]
        assert isinstance(v, Value)
        assert _binding_holds(v, "a/c/0/0", out) is expected
        out["a/zarr.json"] = Data(out["a/zarr.json"])  # type: ignore[arg-type]  # the nearest document, not bytes
        assert _binding_holds(v, "a/c/0/0", out) is None


# ---- what seal refuses: one test each

def _npy(tmp_path, name="x.npy", n=4096):
    """A NumPy file of `n` zero bytes, written sparse."""
    path = tmp_path / name
    with path.open("wb") as f:
        np.lib.format.write_array_header_1_0(f, {"descr": "|u1", "fortran_order": False, "shape": (n,)})
        f.truncate(f.tell() + n)
    return path


def test_seal_refuses_a_key_the_archive_cannot_hold(tmp_path):
    with read(_npy(tmp_path)) as p, pytest.raises(SealError) as e:
        seal(Written({"__vz__/x": b"x"}), [p])
    assert e.value.code == "key"


def test_seal_refuses_a_range_past_its_source(tmp_path):
    with read(_npy(tmp_path)) as p:
        size = p.sources[0].size
        assert size is not None
        with pytest.raises(SealError) as e:
            seal(Written({"v": Ref(p, 0, size - 1, 2)}), [p])
    assert e.value.code == "range"


def test_seal_refuses_a_payload_past_the_limit(tmp_path):
    with read(_npy(tmp_path)) as p, pytest.raises(SealError) as e:
        seal(Written({"v": Literal(bytes(MAX_PAYLOAD))}), [p])
    assert e.value.code == "payload"


def test_seal_refuses_a_value_of_more_reads_than_a_reader_makes(tmp_path):
    with read(_npy(tmp_path, n=1100 * (1 << 17))) as p, pytest.raises(SealError) as e:
        seal(Written({"v": concat(*(Ref(p, 0, k << 17, 1) for k in range(1025)))}), [p])
    assert e.value.code == "reads"


def test_seal_refuses_a_copy_left_unfilled(tmp_path):
    with read(_npy(tmp_path)) as p, pytest.raises(SealError) as e:
        seal(Written({"v": Copy(Ref(p, 0, 0, 4))}), [p])
    assert e.value.code == "copy"


def test_seal_refuses_a_ref_to_an_input_it_was_not_given(tmp_path):
    with read(_npy(tmp_path)) as p, read(_npy(tmp_path, "y.npy")) as q, pytest.raises(SealError) as e:
        seal(Written({"v": Ref(q, 0, 0, 4)}), [p])
    assert e.value.code == "inputs"


def test_seal_refuses_inputs_read_with_and_without_checksums(tmp_path):
    with read(_npy(tmp_path)) as p, read(_npy(tmp_path, "y.npy"), checksums=True) as q, \
            pytest.raises(SealError) as e:
        seal(Written({"v": Ref(p, 0, 0, 4)}), [p, q])
    assert e.value.code == "inputs"


def test_seal_refuses_one_file_read_with_two_sizes(tmp_path):
    path = _npy(tmp_path)
    p = read(path)
    _npy(tmp_path, n=8192)  # the file changes between two reads of it
    q = read(path)
    with pytest.raises(SealError) as e:
        seal(Written({"v": concat(Ref(p, 0, 0, 4), Ref(q, 0, 0, 4))}), [p, q])
    assert e.value.code == "inputs"


def test_seal_refuses_a_placement_of_no_bytes_entry(tmp_path):
    with read(_npy(tmp_path)) as p:
        w = Written({"v": Ref(p, 0, 0, 4)})
        w.at_open["v"] = True  # placed after it was set: a placement of no bytes entry
        with pytest.raises(SealError) as e:
            seal(w, [p])
    assert e.value.code == "open"


def test_seal_refuses_a_payload_past_the_limit_with_its_checksums(tmp_path):
    with read(_npy(tmp_path, n=20000), checksums=True) as p, pytest.raises(SealError) as e:
        seal(Written({"v": concat(*(Ref(p, 0, 2 * k, 1) for k in range(7000)))}), [p])
    assert e.value.code == "checksums"


def test_seal_refuses_a_check_it_did_not_issue(tmp_path):
    """A caller's Check, however it is bound, lifts no read rule."""
    with read(_npy(tmp_path, n=1100 * (1 << 17))) as p:
        doc = {"shape": [4, 6], "data_type": "uint8", "codecs": SHARDED, "fill_value": 0,
               "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": [4, 6]}}}
        far = concat(*(Ref(p, 0, k << 17, 1) for k in range(1025)))
        forged = Value(far.parts, Check("builtin", Binding.of(doc, (0, 0))))
        out = Array([4, 6], "uint8", [4, 6], SHARDED, {(0, 0): forged}).entries(to="a")
        with pytest.raises(SealError) as e:
            seal(out, [p])
    assert e.value.code == "reads"


def test_seal_refuses_inputs_read_under_different_policies(tmp_path):
    with read(_npy(tmp_path)) as p, read(_npy(tmp_path, "y.npy"), policy=Policy(max_reads=10)) as q, \
            pytest.raises(SealError) as e:
        seal(Written({"v": Ref(p, 0, 0, 4)}), [p, q])
    assert e.value.code == "inputs"


def test_seal_refuses_inputs_read_with_different_registries(tmp_path):
    with read(_npy(tmp_path)) as p, read(_npy(tmp_path, "y.npy"), registry={}) as q, \
            pytest.raises(SealError) as e:
        seal(Written({"v": Ref(p, 0, 0, 4)}), [p, q])
    assert e.value.code == "inputs"


def test_seal_refuses_two_files_named_by_one_url(tmp_path):
    url = "https://example.org/f.npy"
    with read(_npy(tmp_path), url) as p, read(_npy(tmp_path, "y.npy"), url) as q, pytest.raises(SealError) as e:
        seal(Written({"v": Ref(p, 0, 0, 4), "w": Ref(q, 0, 0, 4)}), [p, q])
    assert e.value.code == "inputs"


def test_seal_refuses_a_closed_input_read_with_checksums(tmp_path):
    p = read(_npy(tmp_path), checksums=True)
    p.close()
    with pytest.raises(SealError) as e:
        seal(Written({"v": Ref(p, 0, 0, 4)}), [p])
    assert e.value.code == "inputs"


def test_seal_refuses_a_malformed_part(tmp_path):
    with read(_npy(tmp_path)) as p, pytest.raises(SealError) as e:
        seal(Written({"v": Zeros(-1)}), [p])
    assert e.value.code == "value"


def test_seal_refuses_a_copy_of_an_issued_check(tmp_path):
    """A check refkv issued, copied with dataclasses.replace onto other parts, lifts no read rule."""
    with read(_npy(tmp_path, n=1100 * (1 << 17))) as p:
        issued = next(v.check for v in project(p).written.values() if isinstance(v, Value) and v.check)
        doc = {"shape": [4, 6], "data_type": "uint8", "codecs": SHARDED, "fill_value": 0,
               "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": [4, 6]}}}
        far = concat(*(Ref(p, 0, k << 17, 1) for k in range(1025)))
        moved = Value(far.parts, dataclasses.replace(issued, binding=Binding.of(doc, (0, 0)), parts=far.parts))
        out = Array([4, 6], "uint8", [4, 6], SHARDED, {(0, 0): moved}).entries(to="a")
        with pytest.raises(SealError) as e:
            seal(out, [p])
    assert e.value.code == "reads"


def test_seal_refuses_an_entry_that_is_not_bytes_or_a_value(tmp_path):
    with read(_npy(tmp_path)) as p, pytest.raises(SealError) as e:
        seal({"v": 5}, [p])
    assert e.value.code == "value"


def test_seal_refuses_a_source_the_input_does_not_have(tmp_path):
    with read(_npy(tmp_path)) as p, pytest.raises(SealError) as e:
        seal(Written({"v": Ref(p, 1, 0, 4)}), [p])
    assert e.value.code == "range"


def test_seal_refuses_a_data_part_outside_its_data(tmp_path):
    with read(_npy(tmp_path)) as p, pytest.raises(SealError) as e:
        seal(Written({"v": Data(b"abc", 10)}), [p])
    assert e.value.code == "range"


def test_seal_refuses_data_that_is_not_bytes(tmp_path):
    with read(_npy(tmp_path)) as p, pytest.raises(SealError) as e:
        seal(Written({"v": Literal("abc")}), [p])  # type: ignore[arg-type]
    assert e.value.code == "value"


def test_seal_refuses_a_source_of_unknown_size(tmp_path):
    with read(_npy(tmp_path)) as p:
        p.sources = (dataclasses.replace(p.sources[0], size=None),)
        with pytest.raises(SealError) as e:
            seal(Written({"v": Ref(p, 0, 0, 4)}), [p])
    assert e.value.code == "range"


def test_seal_refuses_an_input_that_is_not_parsed():
    with pytest.raises(SealError) as e:
        seal(Written({"v": b"x"}), [object()])  # type: ignore[list-item]
    assert e.value.code == "inputs"


def test_seal_refuses_an_input_given_twice(tmp_path):
    with read(_npy(tmp_path)) as p, pytest.raises(SealError) as e:
        seal(Written({"v": Ref(p, 0, 0, 4)}), [p, p])
    assert e.value.code == "inputs"


def test_seal_refuses_more_than_a_reader_reads_at_open(tmp_path, monkeypatch):
    monkeypatch.setattr(seal_module, "MAX_OPEN", 8)
    with read(_npy(tmp_path)) as p, pytest.raises(SealError) as e:
        seal(Written({"zarr.json": b"{" + b" " * 16 + b"}"}), [p])
    assert e.value.code == "open"


def test_seal_refuses_a_zero_source_past_a_source_table(tmp_path):
    with read(_npy(tmp_path)) as p, pytest.raises(SealError) as e:
        seal(Written({"v": Zeros((256 << 20) + 1)}), [p])
    assert e.value.code == "archive"


def test_seal_refuses_a_range_it_cannot_read_to_checksum(tmp_path):
    path = _npy(tmp_path)
    with read(path, checksums=True) as p:
        path.write_bytes(path.read_bytes()[:64])  # the file shrinks after it was read
        with pytest.raises(SealError) as e:
            seal(Written({"v": Ref(p, 0, 100, 4)}), [p])
    assert e.value.code == "checksums"


def test_a_store_named_by_a_url_is_read_where_it_was_opened(tmp_path):
    """A file read with its siblings, named under another URL, reads them where `read` opened them."""
    for f in (FIXTURES / "nrrd").glob("nrrd_detached_raw.*"):
        (tmp_path / f.name).write_bytes(f.read_bytes())
    nhdr = tmp_path / "nrrd_detached_raw.nhdr"
    with read(nhdr, "https://example.invalid/data/nrrd_detached_raw.nhdr") as p:
        assert (p.sources[0].url or "").startswith("https://example.invalid/")  # how the output names them
        k = next(k for k, s in enumerate(p.sources) if (s.url or "").endswith(".raw"))
        assert p.bytes((k, 0, 16)) == (tmp_path / "nrrd_detached_raw.raw").read_bytes()[:16]


def test_seal_refuses_a_value_of_more_reads_than_its_policy_allows(tmp_path):
    with read(_npy(tmp_path, n=40 << 17), policy=Policy(max_reads=10)) as p, pytest.raises(SealError) as e:
        seal(Written({"v": concat(*(Ref(p, 0, k << 17, 1) for k in range(20)))}), [p])
    assert e.value.code == "reads"


def test_seal_refuses_an_output_that_is_not_a_mapping(tmp_path):
    with read(_npy(tmp_path)) as p, pytest.raises(SealError) as e:
        seal([("v", b"x")], [p])  # type: ignore[arg-type]
    assert e.value.code == "value"


def test_seal_refuses_a_policy_that_is_not_a_policy(tmp_path):
    with read(_npy(tmp_path)) as p, pytest.raises(SealError) as e:
        seal(Written({"v": b"x"}), [p], policy="x")  # type: ignore[arg-type]
    assert e.value.code == "inputs"


def test_seal_refuses_info_that_is_not_a_mapping(tmp_path):
    with read(_npy(tmp_path)) as p, pytest.raises(SealError) as e:
        seal(Written({"v": b"x"}), [p], info=[1])  # type: ignore[arg-type]
    assert e.value.code == "inputs"


def test_seal_refuses_a_ref_of_no_natural_offset(tmp_path):
    with read(_npy(tmp_path)) as p, pytest.raises(SealError) as e:
        seal(Written({"v": Ref(p, 0, -1, 4)}), [p])
    assert e.value.code == "value"


def test_seal_refuses_a_source_table_past_what_a_reader_reads(tmp_path, monkeypatch):
    monkeypatch.setattr(seal_module, "MAX_OPEN", 8)
    with read(_npy(tmp_path)) as p, pytest.raises(SealError) as e:
        seal(Written({"v": Ref(p, 0, 0, 1)}), [p])
    assert e.value.code == "archive"


def test_a_relative_input_reads_the_file_read_opened(tmp_path, monkeypatch):
    """An input read by a relative path reads that file after the working directory changes, and
    two inputs of one relative path in two directories are two sources."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    np.save(tmp_path / "a" / "x.npy", np.full(64, 1, dtype=np.uint8))
    np.save(tmp_path / "b" / "x.npy", np.full(64, 2, dtype=np.uint8))
    monkeypatch.chdir(tmp_path / "a")
    p = read("x.npy")
    monkeypatch.chdir(tmp_path / "b")
    q = read("x.npy")
    assert p.bytes((0, 130, 1)) == b"\x01"
    with pytest.raises(SealError) as e:  # one URL naming two files
        seal(Written({"a": Ref(p, 0, 130, 1), "b": Ref(q, 0, 130, 1)}), [p, q])
    assert e.value.code == "inputs"
    p.close()
    q.close()


def test_seal_refuses_a_registry_the_store_cannot_take(tmp_path):
    with read(_npy(tmp_path)) as p, pytest.raises(SealError) as e:
        seal(Written({"v": b"x"}), [p], registry="x")  # type: ignore[arg-type]
    assert e.value.code == "inputs"


def test_seal_refuses_a_data_part_of_no_natural_offset(tmp_path):
    with read(_npy(tmp_path)) as p, pytest.raises(SealError) as e:
        seal(Written({"v": Data(b"abc", -1, 1)}), [p])
    assert e.value.code == "value"


def test_seal_refuses_shared_data_that_is_not_bytes(tmp_path):
    with read(_npy(tmp_path)) as p, pytest.raises(SealError) as e:
        seal(Written({"v": Shared("abc")}), [p])  # type: ignore[arg-type]
    assert e.value.code == "value"


def test_seal_refuses_keys_past_what_a_reader_reads_at_open(monkeypatch):
    monkeypatch.setattr(seal_module, "MAX_OPEN", 8)
    w = Written({"a-long-key": b"x"}, at_open={"a-long-key": False})
    with pytest.raises(SealError) as e:
        seal(w, [])
    assert e.value.code == "archive"


def test_seal_refuses_more_sources_than_its_policy_allows(tmp_path):
    with read(_npy(tmp_path), policy=Policy(max_sources=2)) as p, pytest.raises(SealError) as e:
        seal(Written({"v": concat(Ref(p, 0, 0, 1), Shared(b"a"), Shared(b"b"))}), [p])
    assert e.value.code == "archive"


def test_seal_refuses_a_source_table_past_its_policys_format_entry(tmp_path):
    with read(_npy(tmp_path), policy=Policy(max_format_entry=4096)) as p, pytest.raises(SealError) as e:
        seal(Written({"v": Shared(bytes(range(256)) * 64)}), [p])
    assert e.value.code == "archive"


def test_seal_refuses_a_source_its_policy_refuses(tmp_path):
    with read(_npy(tmp_path)) as p, pytest.raises(SealError) as e:
        seal(Written({"v": Ref(p, 0, 0, 4)}), [p], policy=Policy(prefixes=("https://data.example/",)))
    assert e.value.code == "inputs"


def test_seal_refuses_inputs_that_are_not_a_sequence():
    with pytest.raises(SealError) as e:
        seal(Written({"v": b"x"}), 5)  # type: ignore[arg-type]
    assert e.value.code == "inputs"


def test_seal_holds_a_built_in_value_to_a_stricter_policy():
    """Under a policy that allows fewer reads than refkv's default, no check lifts the read rule."""
    with read(FIXTURES / "tiff/strips_uint8.tif", policy=Policy(max_reads=0)) as p, pytest.raises(SealError) as e:
        seal(project(p).written, [p])
    assert e.value.code == "reads"


def test_seal_refuses_a_checksum_it_cannot_read_on_refkvs_own_loop(tmp_path):
    """seal blocks to checksum; on refkv's own event loop that is a SealError, not a hang."""
    with read(_npy(tmp_path), checksums=True) as p:
        async def sealing():
            return seal(Written({"v": Ref(p, 0, 0, 4)}), [p])
        with pytest.raises(SealError) as e:
            _sync.run(sealing())
    assert e.value.code == "checksums"


# ---- what the other tools refuse: one test each

def test_element_at_refuses_a_source_the_input_does_not_have(tmp_path):
    with read(_npy(tmp_path)) as p, pytest.raises(KeyError):
        p.element_at(5, 0)


def test_bytes_of_nothing_is_a_type_error(tmp_path):
    with read(_npy(tmp_path)) as p, pytest.raises(TypeError):
        p.bytes()  # type: ignore[call-overload]


def test_a_value_has_parts():
    with pytest.raises(ValueError, match="at least one part"):
        Value(())


def test_a_value_is_made_of_parts():
    with pytest.raises(TypeError, match="parts"):
        value(5)  # type: ignore[arg-type]


def test_reference_refuses_an_alias_of_a_struct():
    with read(FIXTURES / "tiff/edge_shared_tables.tif") as p, pytest.raises(ValueError, match="no bytes of its own"):
        p.reference(p.ir.index("ifds/2/strips"))


def test_reference_refuses_a_chunk_whose_form_skips_filters():
    with read(FIXTURES / "hdf5/hdf5_masks.h5") as p, pytest.raises(ValueError, match="skips filters"):
        p.reference(p.ir.index("objects/2/chunks/0"))


def test_bytes_refuses_an_element_in_a_derived_space():
    with read(FIXTURES / "fits/fits_gzip_image.fits.gz") as p, pytest.raises(ValueError, match="derived space"):
        p.bytes(p.ir.index("stream/hdus/0/data"))  # in the inflated stream, its bytes not kept


def test_bytes_counts_what_the_reader_fetches(tmp_path):
    """Ranges read together count against max_bytes as the reader merges them, the bytes between
    them included."""
    with read(_npy(tmp_path, n=1 << 17), max_bytes=1000) as p, pytest.raises(ValueError, match="max_bytes"):
        p.bytes((0, 200, 1), (0, 60000, 1))


def test_bytes_refuses_a_malformed_range(tmp_path):
    with read(_npy(tmp_path)) as p, pytest.raises(TypeError):
        p.bytes((0, 1.5, 2))  # type: ignore[arg-type]


def test_bytes_refuses_a_range_past_its_source(tmp_path):
    with read(_npy(tmp_path), max_bytes=1 << 31) as p, pytest.raises(ValueError, match="past its"):
        p.bytes((0, 0, 1 << 30))


def test_reference_refuses_a_member_the_element_does_not_have():
    with read(FIXTURES / "tiff/strips_uint8.tif") as p, pytest.raises(KeyError):
        p.reference(p.ir.index("ifds/0/strips"), member=10**6)


def test_an_array_of_a_type_whose_zero_is_unknown_needs_its_fill_value():
    with pytest.raises(ValueError, match="no zero"):
        Array([2], "string", [2], [{"name": "vlen-utf8"}], {}).document()


def test_bytes_refuses_past_max_bytes(tmp_path):
    with read(_npy(tmp_path), max_bytes=10) as p, pytest.raises(ValueError, match="max_bytes"):
        p.bytes((0, 0, 11))


def test_span_refuses_a_node(tmp_path):
    with read(_npy(tmp_path)) as p, pytest.raises(ValueError, match="no bytes of its own"):
        p.span(0)


def test_an_array_of_a_data_type_that_is_neither_a_name_nor_an_object():
    with pytest.raises(TypeError, match="data type"):
        Array([2], 5, [2], [{"name": "bytes"}], {}).document()  # type: ignore[arg-type]


def test_read_refuses_an_input_whose_run_keeps_no_ir():
    with pytest.raises(ValueError, match="keeps no IR"):
        read(str(min(p for p in (FIXTURES / "dicom_series").iterdir() if p.is_dir())) + "/",
             "https://example.invalid/series/")


def test_bytes_refuses_a_decoded_value_in_a_derived_space():
    """An element whose kept bytes are a decoded value (unescaped XML text), not its own, has no bytes."""
    with read(FIXTURES / "nd2/nd2_streams.nd2") as p, pytest.raises(ValueError, match="derived space"):
        p.bytes(p.ir.index("chunks/CustomDataVar|AppInfo_V1_0!/xml/variant/no_name/Version"))


def test_bytes_refuses_a_range_of_no_source(tmp_path):
    with read(_npy(tmp_path)) as p, pytest.raises(KeyError):
        p.bytes((5, 0, 1))


def test_source_of_refuses_an_element_in_no_object():
    url = "https://example.invalid/zarr2_codecs/"
    with read(str(FIXTURES / "zarr2/zarr2_codecs") + "/", url) as p, pytest.raises(ValueError, match="no object"):
        p.source_of(0)


def test_an_array_of_an_object_data_type_whose_zero_is_unknown_needs_its_fill_value():
    with pytest.raises(ValueError, match="no zero"):
        Array([2], {"name": "foo"}, [2], [{"name": "bytes"}], {}).document()


def test_reference_refuses_a_node(tmp_path):
    with read(_npy(tmp_path)) as p, pytest.raises(ValueError, match="no bytes of its own"):
        p.reference(0)


def test_an_element_the_ir_does_not_have_is_a_key_error(tmp_path):
    with read(_npy(tmp_path)) as p, pytest.raises(KeyError):
        p.span(len(p.ir))


def test_a_closed_input_reads_nothing(tmp_path):
    p = read(_npy(tmp_path))
    p.close()
    with pytest.raises(ValueError, match="closed"):
        p.bytes((0, 0, 1))


def test_cut_refuses_bounds_outside_the_value():
    with pytest.raises(ValueError, match="not within"):
        cut(Literal(b"abc"), 1, 4)


def test_concat_refuses_no_values():
    with pytest.raises(ValueError, match="no values"):
        concat()


def test_a_value_holds_parts_only():
    with pytest.raises(TypeError, match="parts"):
        Value((b"bytes",))  # type: ignore[arg-type]


@pytest.mark.parametrize(("fixture", "path", "attributes", "separator"), [
    ("zarr3/zarr3_hierarchy.zip", "a", {"units": "m"}, "/"),
    ("zarr3/zarr3_hierarchy.zip", "g/dot", None, "."),
    ("zarr2/zarr2_root_array_objects/", "", None, "."),  # a root array, the IR mirror below it
])
def test_project_arrays_keep_their_documents(fixture, path, attributes, separator):
    """An array of the built-in output, written again from `Projected.array`, keeps its attributes
    and its chunk key encoding, so its chunks keep their keys; a root array's chunks are its own."""
    url = f"https://example.invalid/{fixture}" if fixture.endswith("/") else None
    with read(str(FIXTURES / fixture) + ("/" if url else ""), url) as p:
        pr = project(p)
        doc_key = f"{path}/zarr.json" if path else "zarr.json"
        original = json.loads(pr.written[doc_key])  # type: ignore[arg-type]
        again = pr.array(path).entries(to=path)
        doc = json.loads(again[doc_key])  # type: ignore[arg-type]
        assert doc["attributes"] == original["attributes"]
        if attributes is not None:
            assert attributes.items() <= doc["attributes"].items()
        assert doc["chunk_key_encoding"]["configuration"]["separator"] == separator
        assert {k for k in again if k != doc_key} <= set(pr.written)
        assert not any(k.startswith("refkv_source/") for k in again)
        assert {k: v for k, v in doc.items() if k != "attributes"} == {
            k: v for k, v in original.items() if k != "attributes"}  # every member, storage_transformers too


@pytest.mark.parametrize(("data_type", "zero"), [("uint8", 0), ("float32", 0.0), ("complex64", [0.0, 0.0]),
                                                 ("bool", False), ("r16", [0, 0])])
def test_array_documents(data_type, zero):
    """`Array.document` and `entries` write a Zarr v3 document a reader parses, its fill value the
    data type's zero when none is given, and its chunks at their keys."""
    a = Array([2], data_type, [2], [{"name": "bytes"}], {(0,): Literal(b"..")})
    doc = json.loads(a.entries(to="x")["x/zarr.json"])  # type: ignore[arg-type]
    assert doc["node_type"] == "array"
    assert doc["chunk_grid"]["configuration"]["chunk_shape"] == [2]
    assert doc["fill_value"] == zero
    assert "x/c/0" in a.entries(to="x")
