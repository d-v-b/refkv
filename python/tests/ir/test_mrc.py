"""The MRC IR: the Rust parser and its schema, the image projection (spec/zarrify/mrc.md)
against mrcfile's reading of every synthetic file, the mirror's rebuild, the inputs the
profile rejects, and the edge chunks it copies. MRC has no frozen Python reference: mrcfile
is the independent reader."""

import base64
import gzip
import json
import math
import struct
import warnings
from typing import cast

import mrcfile
import numpy as np
import pytest
import xarray as xr
from ir_helpers import FIXTURES
from ir_helpers import zarrified as shared_zarrified

import refkv.zarr
from refkv import _core
from refkv._narrow import at, mapping, ndarray

MRCS = sorted((FIXTURES / "mrc").glob("*.mrc"))
A = "angstrom"
CANONICAL_NAN = 0x7FC00000


def as_json(v):
    """A header value as spec/zarrify/mrc.md §5
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mrc.md#5-source-metadata> writes it,
    from mrcfile's numpy value: a negative zero, or a NaN other than the canonical one, as
    {"bits": hex}; opaque bytes as base64; text up to its first NUL."""
    if isinstance(v, np.ndarray) and v.ndim == 0:
        return as_json(v[()])
    if isinstance(v, np.void) and v.dtype.names:
        return [as_json(v[name]) for name in v.dtype.names]
    if isinstance(v, np.void):
        return base64.b64encode(bytes(v)).decode()
    if isinstance(v, np.ndarray):
        return [as_json(x) for x in v]
    if isinstance(v, np.floating):
        bits = int(np.asarray(v).astype(v.dtype.newbyteorder("=")).view("=u4"))
        if bits == 1 << 31 or (np.isnan(v) and bits != CANONICAL_NAN):
            return {"bits": f"{bits:08x}"}
    if isinstance(v, (bytes, np.bytes_)):
        b = bytes(v).split(b"\0", 1)[0]
        try:
            return b.decode("utf-8")
        except UnicodeDecodeError:
            return {"latin1": b.decode("latin-1")}
    v = v.item() if hasattr(v, "item") else v
    if isinstance(v, float):
        if math.isnan(v):
            return "NaN"
        return "Infinity" if v == float("inf") else "-Infinity" if v == float("-inf") else v
    return v


IMOD = 1146047817


def load(path):
    """mrcfile's reading (its warnings, such as a zero grid size, are not failures). mrcfile
    does not read IMOD's unsigned-byte flag (spec/zarrify/mrc.md §3
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mrc.md#3-data-types>): a mode 0
    file with IMOD's stamp and bit 0 of its flags is its bytes as uint8."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with mrcfile.open(path, permissive=False) as f:
            # mrcfile is unannotated: pyrefly takes its header for None, as it is before a read
            header, data = cast("np.recarray", f.header).copy(), np.array(f.data)
    raw = path.read_bytes()
    order = "<" if header.mode.dtype.byteorder in "<=" else ">"
    stamp, flags = struct.unpack(order + "ii", raw[152:160])
    if int(header.mode) == 0 and stamp == IMOD and flags & 1:
        data = data.view(np.uint8)
    return header, data


def zarrified(path, tmp_path):
    """(the output, the hierarchy's root group); the archive rebuilds the file
    (ir_helpers.zarrified)."""
    out, root, _ = shared_zarrified(path, tmp_path, "mrc")
    return out, root

@pytest.mark.parametrize("path", [p for p in MRCS if not p.name.startswith("mrc_reject")], ids=lambda p: p.name)
def test_matches_mrcfile_and_rebuilds(path, tmp_path):
    """The array is mrcfile's data, in its shape and type; every header field is mrcfile's
    reading of it (spec/zarrify/mrc.md §5.1
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mrc.md#51-the-root>); the file is
    rebuilt from the mirror."""
    header, want = load(path)
    out, root = zarrified(path, tmp_path)
    arr = root.get_array("0")
    got = ndarray(arr[...])
    assert got.shape == want.shape
    assert got.dtype == want.dtype.newbyteorder("=")
    assert np.array_equal(got, want, equal_nan=got.dtype.kind in "fc")
    meta = mapping(at(root.attrs, "refkv_zarrified", "mrc"))
    names = header.dtype.names
    assert names is not None  # a record
    assert list(mapping(meta["header"])) == list(names)
    for name in names:
        assert at(meta, "header", name) == as_json(header[name]), name
    assert meta["byte_order"] == ("little" if header.mode.dtype.byteorder in "<=" else "big")
    assert meta["data_type"] == str(want.dtype.newbyteorder("="))
    kind = {2: "image", 4: "volume_stack"}.get(want.ndim, "image_stack" if header.ispg == 0 else "volume")
    assert meta["kind"] == kind
    assert out.summary["kind"] == kind
    assert out.summary["extendedHeader"] == int(header.nsymbt)
    # the extended header is never read: the run reads the header alone
    assert at(out.summary, "planner", "bytes") == 1024
    dataset_is_the_file(out, root, header, want, kind, meta)


def dataset_is_the_file(out, root, header, want, kind, meta):
    """The dataset layout (spec/zarrify/mrc-dataset.md): the root names it, its group
    declares the mrc-dataset convention with the root's source metadata, xarray opens it with
    the kind's dimensions and the file's values, and each spatial dimension with a voxel size
    has the coordinate o + i * s in angstroms, o the origin of a placed volume."""
    assert root.attrs["refkv_zarrified"]["layouts"] == {"dataset": "dataset"}
    group = json.loads(out.bytes_entries["dataset/zarr.json"])["attributes"]
    assert group["refkv_zarrified"]["profile"] == "mrc-dataset"
    assert group["refkv_zarrified"]["mrc-dataset"] == meta
    assert set(group) == {"zarr_conventions", "refkv_zarrified"}
    ds = xr.open_zarr(refkv.zarr.store(out.to_store()), group="dataset", consolidated=False)
    names = {1: "x", 2: "y", 3: "z"}
    m = [int(header.maps), int(header.mapr), int(header.mapc)]
    spatial = [names[v] for v in m] if sorted(m) == [1, 2, 3] else ["z", "y", "x"]
    dims = {"image": spatial[1:], "image_stack": ["section", *spatial[1:]], "volume": spatial,
            "volume_stack": ["volume", *spatial]}[kind]
    assert list(ds["data"].dims) == dims
    assert np.array_equal(ds["data"].values, want, equal_nan=want.dtype.kind in "fc")
    first = "0/" * (want.ndim - 1) + "0"
    assert out.refs.get("dataset/data/c/" + first) == out.refs.get("0/c/" + first)
    for axis in "xyz":
        cella = float(header.cella[axis])
        grid = int(header[f"m{axis}"])
        voxel = cella / grid if grid >= 1 and np.isfinite(cella) and cella > 0 else None
        if voxel is not None and not (np.isfinite(voxel) and voxel > 0):
            voxel = None
        if axis not in dims or voxel is None:
            assert axis not in ds.coords, axis
            continue
        origin = [float(header.origin[a]) for a in "xyz"]
        placed = int(header.ispg) != 0 and all(np.isfinite(origin)) and any(o != 0 for o in origin)
        o = origin["xyz".index(axis)] if placed else 0.0
        n = ds.sizes[axis]
        assert np.array_equal(ds[axis].values, o + np.arange(n, dtype=np.float64) * voxel), axis
        assert ds[axis].attrs == {"units": "angstrom"}, axis
        assert ds[axis].dtype == np.float64
        # stored as the delta-coded, deflated index with CF scale and offset (conventions §9
        # <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#9-dataset-layouts>)
        doc = json.loads(out.bytes_entries[f"dataset/{axis}/zarr.json"])
        assert doc["data_type"] == "uint32", axis
        assert doc["attributes"] == {"units": "angstrom", "scale_factor": voxel, "add_offset": o}, axis
        assert [c["name"] for c in doc["codecs"]] == ["numcodecs.delta", "bytes", "gzip"], axis
        c = min(n, 1 << 17)
        for q in range(-(-n // c)):
            raw = gzip.decompress(out.bytes_entries[f"dataset/{axis}/c/{q}"])
            assert np.array_equal(np.frombuffer(raw, "<u4"), [q * c] + [1] * (c - 1)), (axis, q)
        assert len(out.bytes_entries[f"dataset/{axis}/c/0"]) < 64 + c // 200, axis  # a few bytes per thousand
    omero = root.attrs["ome"].get("omero")
    assert ds["data"].attrs == ({"actual_range": [float(header.dmin), float(header.dmax)]} if omero else {})


# fixture: (summary members, [(axis, unit)], scale, translation, members of the `mrc` attribute, the
#           display window (... for one from mrcfile's statistics), (key, ranges) of one chunk)
CASES = {
    "mrc_volume_float32_le": (
        {"kind": "volume", "byteOrder": "little", "mode": 2, "dataType": "float32", "sizes": {"z": 2, "y": 3, "x": 4},
         "voxelSize": [1.5, 1.5, 1.5], "translation": True, "axisMap": True, "extendedHeader": 0, "extendedType": ""},
        [("z", A), ("y", A), ("x", A)], [1.5, 1.5, 1.5], [7.25, 2, -1.5],
        {"voxel_size": {"x": 1.5, "y": 1.5, "z": 1.5}, "axis_map": {"applied": True}},
        {"min": -100, "max": 150, "start": -100, "end": 150}, ("0/c/0/0/0", [(1024, 96)])),
    "mrc_volume_int16_be": ({"byteOrder": "big", "dataType": "int16", "voxelSize": [0.5, 0.5, 0.5]}, None,
                            [0.5, 0.5, 0.5], None, None, {"min": -100, "max": 100, "start": -100, "end": 100},
                            ("0/c/0/0/0", [(1024, 48)])),
    "mrc_image_int8": ({"kind": "image", "sizes": {"y": 3, "x": 5}, "chunkShape": [3, 5]}, [("y", A), ("x", A)],
                       [2, 2], None, {"kind": "image"}, {"min": -100, "max": 100, "start": -100, "end": 100},
                       ("0/c/0/0", [(1024, 15)])),
    "mrc_image_stack_uint16": ({"kind": "image_stack", "sizes": {"z": 5, "y": 3, "x": 4}, "dataType": "uint16"},
                               [("z", A), ("y", A), ("x", A)], [1, 1, 1], None, None, None, None),
    "mrc_volume_stack_float16": ({"kind": "volume_stack", "sizes": {"t": 2, "z": 3, "y": 2, "x": 3},
                                  "dataType": "float16", "chunkShape": [2, 3, 2, 3]},
                                 [("t", None), ("z", A), ("y", A), ("x", A)], [1, 1, 1, 1], None,
                                 None, {"min": -100, "max": 100, "start": -100, "end": 100},
                                 ("0/c/0/0/0/0", [(1024, 72)])),
    # a complex array has no display window
    "mrc_volume_complex64": ({"dataType": "complex64", "mode": 4}, None, None, None, None, None,
                             ("0/c/0/0/0", [(1024, 96)])),
    # the axis map: sections along x, columns along z
    "mrc_axis_map_permuted": ({"sizes": {"x": 2, "y": 3, "z": 4}, "axisMap": True}, [("x", A), ("y", A), ("z", A)],
                              [2, 2, 2], [1, 2, 3], {"axis_map": {"applied": True}}, None, None),
    "mrc_axis_map_permuted_image": ({"sizes": {"x": 3, "y": 4}}, [("x", A), ("y", A)], [2, 2], None, None, None, None),
    "mrc_axis_map_zero": ({"sizes": {"z": 2, "y": 3, "x": 4}, "axisMap": False}, [("z", A), ("y", A), ("x", A)],
                          None, None, {"axis_map": {"applied": False}}, None, None),
    "mrc_axis_map_duplicate": ({"axisMap": False}, [("z", A), ("y", A), ("x", A)], None, None, None, None, None),
    "mrc_axis_map_out_of_range": ({"axisMap": False}, [("z", A), ("y", A), ("x", A)], None, None, None, None, None),
    # windows, cell sizes and origins that give no window, scale or translation
    "mrc_edge_window_reversed": ({}, None, None, None, None, None, None),
    "mrc_edge_window_nan": ({}, None, None, None, None, None, None),
    "mrc_edge_window_inf": ({}, None, None, None, None, None, None),
    "mrc_edge_cella_zero": ({"voxelSize": None}, [("z", A), ("y", A), ("x", None)], [1, 1, 1], None,
                            {"voxel_size": None}, None, None),
    "mrc_edge_cella_negative": ({"voxelSize": None}, [("z", A), ("y", None), ("x", A)], [1, 1, 1], None,
                                None, None, None),
    "mrc_edge_cella_nan": ({"voxelSize": None}, [("z", None), ("y", A), ("x", A)], [1, 1, 1], None, None, None, None),
    "mrc_edge_m_zero": ({"voxelSize": None}, [("z", A), ("y", A), ("x", None)], [1, 1, 1], None, None, None, None),
    "mrc_edge_m_negative": ({"voxelSize": None}, [("z", A), ("y", None), ("x", A)], [1, 1, 1], None, None, None, None),
    "mrc_edge_m_larger": ({"voxelSize": [0.5, 0.5, 0.5]}, None, [0.5, 0.5, 0.5], None, None, None, None),
    "mrc_edge_origin_nan": ({"translation": False}, None, None, None, None, None, None),
    "mrc_edge_origin_inf": ({"translation": False}, None, None, None, None, None, None),
    "mrc_edge_origin_one": ({"translation": True}, None, None, [-2.5, 0, 0], None, None, None),
    # an image's origin is a phase origin in pixels: no translation
    "mrc_edge_origin_image_stack": ({"kind": "image_stack", "translation": False}, None, None, None, None, None, None),
    "mrc_edge_origin_image": ({"kind": "image", "translation": False}, None, None, None, None, None, None),
    # IMOD's unsigned-byte flag: mode 0 as uint8, with the stamp and bit 0 of the flags alone
    "mrc_imod_uint8": ({"mode": 0, "dataType": "uint8"}, None, None, None, {"data_type": "uint8"},
                       {"min": 0, "max": 255, "start": 0, "end": 255}, ("0/c/0/0/0", [(1024, 24)])),
    "mrc_imod_uint8_other_flags": ({"dataType": "uint8"}, None, None, None, None, None, None),
    "mrc_imod_uint8_be": ({"dataType": "uint8", "byteOrder": "big"}, None, None, None, None, None, None),
    "mrc_imod_int8": ({"dataType": "int8"}, None, None, None, None, None, None),
    "mrc_imod_flag_no_stamp": ({"dataType": "int8"}, None, None, None, None, None, None),
    "mrc_edge_volume_stack_one": ({"kind": "volume_stack", "sizes": {"t": 1, "z": 3, "y": 2, "x": 3}},
                                  None, None, None, None, None, None),
    "mrc_edge_ispg_230": ({"kind": "volume"}, None, None, None, None, None, None),
    "mrc_edge_ispg_negative": ({"kind": "volume"}, None, None, None, None, None, None),
    "mrc_edge_nz_one_volume": ({"kind": "volume", "sizes": {"z": 1, "y": 2, "x": 3}},
                               None, None, None, None, None, None),
    # extended headers: described, never read
    "mrc_extended_ccp4": ({"extendedHeader": 160, "extendedType": "CCP4"}, None, None, None, None, None,
                          ("0/c/0/0/0", [(1184, 48)])),
    "mrc_extended_fei1": ({"extendedHeader": 768, "extendedType": "FEI1"}, None, None, None, None, None,
                          ("0/c/0/0/0", [(1792, 48)])),
    "mrc_extended_large": ({"extendedHeader": 5000, "extendedType": "SERI"}, None, None, None, None, None, None),
    # chunks (spec/zarrify/mrc.md §4.1
    # <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mrc.md#41-the-array>): blocks of sections,
    # blocks of rows, and an edge chunk padded with a zero byte, of the zero data source
    "mrc_chunk_sections": ({"chunkShape": [10, 128, 256], "chunks": 2}, None, None, None, None, None,
                           ("0/c/1/0/0", [(1024 + 655360, 655360)])),
    "mrc_chunk_rows": ({"chunkShape": [550, 1000], "chunks": 2}, None, None, None, None, None,
                       ("0/c/1/0", [(1024 + 550000, 550000)])),
    "mrc_chunk_edge_literal": ({"chunkShape": [1, 524289], "chunks": 2}, None, None, None, None, None,
                               ("0/c/0/1", [(1024 + 524289, 524288), (1, 0, 1)])),
    "mrc_mrcfile_volume": ({"kind": "volume", "voxelSize": [1.25, 1.25, 1.25], "translation": True}, None,
                           [1.25, 1.25, 1.25], [10, 2.5, -3], None, ..., None),
    "mrc_mrcfile_stack": ({"kind": "image_stack", "voxelSize": [0.5, 0.5, 1]}, None, [1, 0.5, 0.5], None, None, ...,
                          None),
    "mrc_mrcfile_image": ({"kind": "image", "dataType": "uint16"}, None, [2, 2], None, None, ..., None),
}


def test_zarrifies_the_synthetic_files(tmp_path):
    for name, (summary, axes, scale, translation, members, window, chunk) in CASES.items():
        out, _root = zarrified(FIXTURES / "mrc" / f"{name}.mrc", tmp_path)
        assert {**out.summary, **summary} == out.summary, name
        attributes = json.loads(out.bytes_entries["zarr.json"])["attributes"]
        ms = attributes["ome"]["multiscales"][0]
        transforms = ms["datasets"][0]["coordinateTransformations"]
        if axes is not None:
            assert [(a["name"], a.get("unit")) for a in ms["axes"]] == axes, name
        if scale is not None:
            assert transforms[0]["scale"] == scale, name
        assert (transforms[1]["translation"] if len(transforms) > 1 else None) == translation, name
        assert set(attributes) == {"ome", "zarr_conventions", "refkv_zarrified"}, name
        meta = attributes["refkv_zarrified"]["mrc"]
        assert list(meta) == ["byte_order", "kind", "data_type", "header", "axis_map"] + (
            ["voxel_size"] if "voxel_size" in meta else []), name
        for k, v in (members or {}).items():
            assert meta.get(k) == v, (name, k)
        omero = attributes["ome"].get("omero")
        if window is not ...:
            assert (omero["channels"][0]["window"] if omero else None) == window, name
        if omero:
            assert len(omero["channels"]) == 1, name
        if chunk is not None:
            assert out.refs[chunk[0]] == chunk[1], name
        assert out.data == ({b"\0": 1} if name == "mrc_chunk_edge_literal" else {}), name  # the zero data source
        codecs = json.loads(out.bytes_entries["0/zarr.json"])["codecs"]
        assert [c["name"] for c in codecs] == ["bytes"], name


def test_edge_chunks_past_the_payload_are_copied(tmp_path):
    """An edge chunk whose reference (its range and its zero padding) does not fit in a
    payload is copied into the archive, padding included (spec/zarrify/mrc.md §8.2
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mrc.md#82-chunks>). With the
    chunk limit no file reaches it in practice, so the core takes a payload limit of 8
    bytes here: the padded edge chunk is copied, by the run's last batch, and the array
    is still mrcfile's."""
    path = FIXTURES / "mrc" / "mrc_chunk_edge_literal.mrc"
    _core._set_copy_threshold(8)
    try:
        out, root = zarrified(path, tmp_path)
    finally:
        _core._set_copy_threshold(None)
    assert "0/c/0/1" in out.bytes_entries
    assert "0/c/0/1" not in out.refs
    assert len(out.bytes_entries["0/c/0/1"]) == 524289
    assert out.bytes_entries["dataset/data/c/0/1"] == out.bytes_entries["0/c/0/1"]
    _, want = load(path)
    assert np.array_equal(ndarray(root.get_array("0")[...]), want)
