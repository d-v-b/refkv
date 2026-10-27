"""Writes small synthetic HDF5 and NWB files to fixtures/hdf5/, covering the HDF5 convention and
profile (spec/zarrify/hdf5.md <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md>):
with h5py, every layout (contiguous, compact, chunked, unallocated), data type (integers and
floats of each width and byte order, h5py's bool, enumerations, packed and unpacked compounds,
fixed-length and variable-length strings, object and region references), filter, link (soft,
second hard, external), both file formats (h5py's default, the earliest, and libver="latest"),
names the hierarchy cannot take, and attributes of every type; with pynwb, NWB files of
extracellular and optical physiology; and the inputs the profile rejects (`hdf5_reject_*`),
patched byte by byte from accepted ones.

python/tests/ir/test_hdf5.py and js/test/hdf5/verify.py check the zarrified files against h5py,
and the NWB files against pynwb's NWBHDF5IO through hdmf-zarr's NWBZarrIO.

Usage: uv run python fixtures/generators/hdf5/write_fixtures.py
"""

from __future__ import annotations

import struct
import uuid
from datetime import UTC, datetime
from pathlib import Path

import h5netcdf
import h5py
import hdmf.container
import numpy as np
from hdmf.backends.hdf5 import H5DataIO
from pynwb import NWBHDF5IO, NWBFile, TimeSeries
from pynwb.ecephys import ElectricalSeries
from pynwb.file import Subject
from pynwb.ophys import Fluorescence, ImageSegmentation, OpticalChannel, RoiResponseSeries

OUT = Path(__file__).parents[2] / "hdf5"

# Reproducible files: HDF5 stamps an object's header with its creation time unless its creation
# property list says not to; every group and dataset creation list made through h5py.h5p.create
# is made untimed (as fixtures/generators/ims/write_fixtures.py does).
_create = h5py.h5p.create


def _untimed(cls):
    plist = _create(cls)
    if isinstance(plist, (h5py.h5p.PropDCID, h5py.h5p.PropGCID)):
        plist.set_obj_track_times(False)
    return plist


h5py.h5p.create = _untimed


def ramp(shape, dtype) -> np.ndarray:
    """Distinct values, seeded by position."""
    n = int(np.prod(shape))
    dt = np.dtype(dtype)
    if dt.kind == "f":
        return (np.arange(n, dtype="f8") * 0.25 - 3).astype(dt).reshape(shape)
    if dt.kind == "u":
        return (np.arange(n) % (1 << min(8 * dt.itemsize, 16))).astype(dt).reshape(shape)
    return (np.arange(n) % (1 << min(8 * dt.itemsize - 1, 15)) - 7).astype(dt).reshape(shape)


def open_file(name: str, libver: str = "earliest", **kw) -> h5py.File:
    return h5py.File(OUT / name, "w", libver=libver, track_order=False, **kw)


def numbers():
    """Contiguous datasets of every numeric type, both byte orders, ranks 0 to 3; a dataset cut
    into two row bands whose last is padded."""
    with open_file("hdf5_numbers.h5") as f:
        for t in ["i1", "u1", "<i2", ">i2", "<u2", "<i4", ">u4", "<i8", ">u8", "<f2", "<f4", ">f4", "<f8", ">f8"]:
            name = t.replace("<", "le_").replace(">", "be_")
            f.create_dataset(f"d1/{name}", data=ramp((7,), t))
        f.create_dataset("d2", data=ramp((5, 6), "<i4"))
        f.create_dataset("d3", data=ramp((2, 3, 4), "<f8"))
        f.create_dataset("rank0", data=np.float64(2.5))
        f.create_dataset("empty", shape=(0, 3), dtype="<i4")
        # 301 rows of 4400 bytes: two bands of 151 rows within 2^20 bytes, the last padded
        f.create_dataset("bands", data=ramp((301, 1100), "<f4"))
        f.create_dataset("nan", data=np.array([np.nan, np.inf, -np.inf, 0.0]))


def scalars_and_strings():
    """Scalars of each kind; variable-length strings (ASCII and UTF-8, empty, more than one chunk
    of 2048, two-dimensional, chunked and deflated); fixed-length strings."""
    utf8 = h5py.string_dtype("utf-8")
    ascii_ = h5py.string_dtype("ascii")
    with open_file("hdf5_strings.h5") as f:
        f.create_dataset("scalar_int", data=np.int32(-5))
        f.create_dataset("scalar_text", data="a session description", dtype=utf8)
        f.create_dataset("scalar_empty", data="", dtype=utf8)
        f.create_dataset("scalar_fixed", data=np.bytes_(b"abc"))
        f.create_dataset("ascii", data=np.array([b"CA1", b"", b"DG", b"x" * 300], dtype=object), dtype=ascii_)
        f.create_dataset("utf8", data=np.array(["été", "日本", "", "plain"], dtype=object), dtype=utf8)
        f.create_dataset("many", data=np.array([f"s{k}" * (k % 5) for k in range(5000)], dtype=object), dtype=utf8)
        f.create_dataset("grid", data=np.array([["a", "bb", "ccc"], ["", "e", "ff"]], dtype=object), dtype=utf8)
        f.create_dataset("chunked", data=np.array([f"c{k}" for k in range(100)], dtype=object), dtype=utf8,
                         chunks=(30,), compression="gzip", shuffle=True)
        f.create_dataset("fixed", data=np.array([b"ab", b"abcde", b""], dtype="S5"))
        f.create_dataset("unwritten", shape=(3,), dtype=utf8)


def compact_and_layouts():
    """Compact datasets; unallocated contiguous storage; a nonzero fill value; external storage,
    a virtual dataset and a null dataspace (not presented)."""
    with open_file("hdf5_layouts.h5") as f:
        for name, data in [("compact_i2", ramp((3, 4), "<i2")), ("compact_scalar", np.float32(1.5))]:
            dcpl = h5py.h5p.create(h5py.h5p.DATASET_CREATE)
            dcpl.set_layout(h5py.h5d.COMPACT)
            space = h5py.h5s.create_simple(data.shape) if data.shape else h5py.h5s.create(h5py.h5s.SCALAR)
            d = h5py.h5d.create(f.id, name.encode(), h5py.h5t.py_create(data.dtype), space, dcpl=dcpl)
            d.write(h5py.h5s.ALL, h5py.h5s.ALL, np.ascontiguousarray(data))
        dcpl = h5py.h5p.create(h5py.h5p.DATASET_CREATE)
        dcpl.set_layout(h5py.h5d.COMPACT)
        strings = np.array(["one", "two", ""], dtype=object)
        vlen = h5py.h5t.py_create(h5py.string_dtype("utf-8"), logical=True)
        d = h5py.h5d.create(f.id, b"compact_strings", vlen, h5py.h5s.create_simple((3,)), dcpl=dcpl)
        h5py.Dataset(d)[...] = strings
        f.create_dataset("unallocated", shape=(10,), dtype="<f4")
        f.create_dataset("fill7", shape=(4, 4), dtype="<i4", chunks=(2, 2), fillvalue=7)
        f["fill7"][0:2, 0:2] = 1
        f.create_dataset("fill_nan", shape=(6,), dtype="<f8", chunks=(3,), fillvalue=np.nan)
        f["fill_nan"][0:3] = [1, 2, 3]
        f.create_dataset("external", shape=(4,), dtype="<i4", external=[("external.bin", 0, 16)])
        f.create_dataset("null", data=h5py.Empty("f4"))
        layout = h5py.VirtualLayout(shape=(2, 3), dtype="<i2")
        layout[0] = h5py.VirtualSource(".", "compact_i2", shape=(3, 4))[0, :3]
        f.create_virtual_dataset("virtual", layout)


def enums_and_compounds():
    """h5py's bool, an enumeration, and compounds: packed (presented as structs), nested, and
    with a gap, a string member, a reference member, or two byte orders (not presented)."""
    with open_file("hdf5_compounds.h5") as f:
        f.create_dataset("bool", data=np.array([True, False, True]))
        enum = h5py.enum_dtype({"a": 0, "b": 1, "c": 2}, basetype="u1")
        f.create_dataset("enum", data=np.array([0, 2, 1], dtype="u1"), dtype=enum)
        mask = np.dtype([("x", "<u4"), ("y", "<u4"), ("weight", "<f4")])
        f.create_dataset("pixel_mask", data=np.array([(k, 2 * k, k / 4) for k in range(9)], dtype=mask))
        nested = np.dtype([("t", "<f8"), ("p", [("a", "<i2"), ("b", "<i2")]), ("ok", "?")])
        f.create_dataset("nested", data=np.array([(0.5, (1, 2), True), (1.5, (3, 4), False)], dtype=nested))
        f.create_dataset("chunked_mask", data=np.array([(k, k, 1.0) for k in range(10)], dtype=mask), chunks=(4,),
                         compression="gzip")
        aligned = np.dtype({"names": ["a", "b"], "formats": ["u1", "<f8"], "offsets": [0, 8], "itemsize": 16})
        f.create_dataset("gap", data=np.zeros(2, dtype=aligned))
        f.create_dataset("with_string", data=np.array([(1, "x")], dtype=[("i", "<i4"), ("s", h5py.string_dtype())]))
        f.create_dataset("mixed_order", data=np.zeros(2, dtype=[("a", "<i4"), ("b", ">i4")]))
        refs = np.zeros(1, dtype=[("start", "<i4"), ("count", "<i4"), ("ts", h5py.ref_dtype)])
        refs[0] = (0, 3, f["bool"].ref)
        f.create_dataset("with_reference", data=refs)


def references_and_links(name: str, libver: str):
    """Object references (datasets and attributes) to groups and datasets, a null one, a target
    with two hard links; a region reference (not presented); soft links (absolute, relative,
    dangling), a second hard link, and an external link."""
    with open_file(name, libver=libver) as f:
        g = f.create_group("general/devices")
        dev = g.create_group("probe")
        dev.attrs["description"] = "a probe"
        data = f.create_dataset("acquisition/data", data=ramp((4, 3), "<i2"))
        f["acquisition/alias"] = data  # a second hard link
        refs = f.create_dataset("refs", shape=(4,), dtype=h5py.ref_dtype)
        refs[0] = dev.ref
        refs[1] = data.ref
        refs[3] = f["general"].ref
        f.create_dataset("scalar_ref", data=data.ref, dtype=h5py.ref_dtype)
        f.attrs.create("main", data=data.ref, dtype=h5py.ref_dtype)
        f.attrs.create("pair", data=np.array([dev.ref, data.ref]), dtype=h5py.ref_dtype)
        f.create_dataset("region", data=[data.regionref[0:2, 1]], dtype=h5py.regionref_dtype)
        f.attrs["region_attribute"] = data.regionref[1:3]
        e = f.create_group("acquisition/es")
        e["device"] = h5py.SoftLink("/general/devices/probe")
        e["relative"] = h5py.SoftLink("data")
        e["dangling"] = h5py.SoftLink("/nowhere")
        e["external"] = h5py.ExternalLink("other.h5", "/a/b")


def latest():
    """The new file format: superblock 3, version 2 object headers, dense groups and attributes,
    and the single chunk, fixed array, extensible array and version 2 B-tree chunk indexes."""
    with open_file("hdf5_latest.h5", libver="latest") as f:
        g = f.create_group("dense")
        for k in range(20):
            g.create_dataset(f"d{k:02}", data=np.arange(k + 1, dtype="<i4"))
            g.attrs[f"a{k:02}"] = k
        f.create_dataset("single", data=ramp((4, 4), "<u2"), chunks=(4, 4))
        f.create_dataset("fixed_array", data=ramp((40, 6), "<f4"), chunks=(7, 6), compression="gzip")
        f.create_dataset("extensible", data=ramp((25,), "<i8"), chunks=(4,), maxshape=(None,))
        f.create_dataset("btree2", data=ramp((9, 10), "<i2"), chunks=(4, 4), maxshape=(None, None), shuffle=True,
                         compression="gzip")
        f.create_dataset("strings", data=np.array(["p", "q", "r"], dtype=object), dtype=h5py.string_dtype())


def filters_and_chunks():
    """Chunked datasets: partial edge chunks, unallocated chunks, deflate, shuffle and deflate,
    big-endian data; and fletcher32 and lzf (not presented)."""
    with open_file("hdf5_filters.h5") as f:
        f.create_dataset("plain", data=ramp((10, 7), "<i4"), chunks=(4, 3))
        sparse = f.create_dataset("sparse", shape=(8, 8), dtype="<f4", chunks=(4, 4))
        sparse[0:4, 4:8] = 1.0
        f.create_dataset("deflate", data=ramp((50,), "<f8"), chunks=(16,), compression="gzip", compression_opts=6)
        f.create_dataset("shuffle", data=ramp((6, 9), ">i2"), chunks=(3, 4), shuffle=True, compression="gzip")
        f.create_dataset("unlimited", data=ramp((5,), "<u4"), chunks=(2,), maxshape=(None,))
        f.create_dataset("fletcher32", data=ramp((8,), "<i4"), chunks=(4,), fletcher32=True)
        f.create_dataset("lzf", data=ramp((8,), "<i4"), chunks=(4,), compression="lzf")


def names_and_attributes():
    """Names the hierarchy cannot take (not UTF-8, starting with `__`, a root link named
    refkv_source, reserved attribute names), a named datatype, and attributes of every type,
    a null one, and a node over the attribute budget."""
    with open_file("hdf5_attributes.h5") as f:
        f.create_group("café")
        f.create_group("__reserved")
        f.create_group("refkv_source")
        f.create_dataset(b"bad\xffname", data=[1])
        f["named_type"] = np.dtype("<i4")
        f.create_dataset("typed", data=[1, 2], dtype=f["named_type"])
        a = f.create_group("attrs").attrs
        a["int"] = np.int64(-3)
        a["uint64_big"] = np.uint64(2**63 + 5)
        a["float"] = 0.1
        a["nan"] = np.nan
        a["array"] = np.arange(6, dtype="<i2").reshape(2, 3)
        a["text"] = "vlen text"
        a["texts"] = np.array(["a", "é", ""], dtype=h5py.string_dtype())
        a["fixed"] = np.bytes_(b"fixed")
        a["latin1"] = np.bytes_(b"caf\xe9")
        a["bool"] = np.bool_(True)
        a["empty"] = h5py.Empty("f4")
        a["compound"] = np.array((1, 2.5), dtype=[("i", "<i4"), ("f", "<f8")])
        a["_LINKS"] = "taken"
        a["refkv_zarrified"] = "taken"
        sequences = np.array([np.array([1, 2], "i4"), np.array([3], "i4")], dtype=object)
        a.create("sequence", data=sequences, dtype=h5py.vlen_dtype("i4"))
        f.create_group("big").attrs["big"] = "x" * 70000
        f.create_group("big_and_small").attrs.update({"big": "y" * 70000, "small": 1})


def netcdf4():
    """A netCDF-4 file (h5netcdf): dimension scales, whose DIMENSION_LIST and REFERENCE_LIST
    attributes are sequences of references and compounds with a reference member (not presented)."""
    path = OUT / "hdf5_netcdf4.h5"
    with h5netcdf.File(path, "w", track_order=False) as f:
        f.dimensions = {"time": 4, "x": 3}
        t = f.create_variable("time", ("time",), "f8")
        t[:] = np.arange(4.0)
        t.attrs["units"] = "days since 2000-01-01"
        v = f.create_variable("sst", ("time", "x"), "f4", chunks=(2, 3), compression="gzip", shuffle=True)
        v[:] = ramp((4, 3), "<f4")
        v.attrs["long_name"] = "sea surface temperature"


def deep():
    """66 nested groups: deeper than the walk's 64 levels."""
    with open_file("hdf5_reject_too_deep.h5") as f:
        g = f
        for k in range(66):
            g = g.create_group(f"g{k}")


# ---- NWB, with pynwb: object IDs and dates fixed, so that the files are the same on every run


def nwb_files():
    counter = iter(range(1 << 30))

    def fixed_uuid() -> uuid.UUID:
        return uuid.UUID(int=next(counter))

    hdmf.container.uuid4 = fixed_uuid  # every container's object_id
    when = datetime(2020, 1, 2, 3, 4, 5, tzinfo=UTC)

    def new(identifier: str) -> NWBFile:
        return NWBFile(session_description="a synthetic session", identifier=identifier, session_start_time=when,
                       file_create_date=[when], subject=Subject(subject_id="m1", species="Mus musculus"))

    f = new("ecephys")
    dev = f.create_device("probe", description="a probe")
    for s in range(2):
        grp = f.create_electrode_group(f"shank{s}", description=f"shank {s}", location="CA1", device=dev)
        for k in range(4):
            f.add_electrode(group=grp, location=["CA1", "DG"][k % 2], x=float(k), y=float(s), z=0.0)
    region = f.create_electrode_table_region(list(range(8)), "all electrodes")
    f.add_acquisition(ElectricalSeries(name="raw", data=ramp((200, 8), "<i2"), electrodes=region, rate=30000.0,
                                       conversion=0.195e-6))
    lfp = H5DataIO(ramp((100, 8), "<f4"), chunks=(32, 8), compression="gzip", maxshape=(None, 8))
    f.add_acquisition(ElectricalSeries(name="lfp", data=lfp, electrodes=region, timestamps=np.arange(100) / 1000.0))
    f.add_unit(spike_times=[0.1, 0.2, 0.3], electrodes=[0, 1], obs_intervals=[[0.0, 1.0]])
    f.add_unit(spike_times=[0.15], electrodes=[2], obs_intervals=[[0.0, 1.0]])
    # a trials table whose `timeseries` column is a compound with a reference member (records)
    f.add_trial(start_time=0.0, stop_time=0.5, timeseries=[f.acquisition["raw"]])
    f.add_trial(start_time=0.5, stop_time=1.0, timeseries=[f.acquisition["raw"]])
    with NWBHDF5IO(OUT / "nwb_ecephys.nwb", "w") as io:
        io.write(f)

    f = new("ophys")
    dev = f.create_device("microscope", description="2p")
    channel = OpticalChannel(name="green", description="GFP", emission_lambda=510.0)
    plane = f.create_imaging_plane(name="plane", optical_channel=channel, description="a plane", device=dev,
                                   excitation_lambda=920.0, imaging_rate=30.0, indicator="GCaMP6s", location="V1")
    seg = ImageSegmentation()
    ps = seg.create_plane_segmentation("cells", plane, "PlaneSegmentation")
    for k in range(3):
        ps.add_roi(pixel_mask=[(k, k, 1.0), (k + 1, k, 0.5)])
    mod = f.create_processing_module("ophys", "optical physiology")
    mod.add(seg)
    rois = ps.create_roi_table_region("all", region=[0, 1, 2])
    fl = Fluorescence()
    fl.add_roi_response_series(RoiResponseSeries(name="dff", data=ramp((50, 3), "<f4"), rois=rois, unit="1", rate=30.0))
    mod.add(fl)
    f.add_acquisition(TimeSeries(name="speed", data=ramp((20,), "<f8"), unit="m/s", rate=10.0))
    with NWBHDF5IO(OUT / "nwb_ophys.nwb", "w") as io:
        io.write(f)


# ---- rejections, patched from accepted files


def patched(source: str, name: str, patches: list[tuple[int, bytes]], cut: int | None = None):
    b = bytearray((OUT / source).read_bytes())
    for at, value in patches:
        b[at:at + len(value)] = value
    if cut is not None:
        b = b[:cut]
    (OUT / name).write_bytes(bytes(b))


def rejects():
    with open_file("_strings.h5") as f:
        f.create_dataset("scalar_int", data=np.int32(-5))
        f.create_dataset("ascii", data=np.array([b"CA1", b"DG"], dtype=object), dtype=h5py.string_dtype("ascii"))
    with h5py.File(OUT / "_strings.h5", "r") as f:
        ascii_at = f["ascii"].id.get_offset()
        plain = f["scalar_int"].id.get_offset()
    with h5py.File(OUT / "hdf5_filters.h5", "r") as f:
        chunk = f["plain"].id.get_chunk_info(0).byte_offset
    size = (OUT / "hdf5_links_earliest.h5").stat().st_size
    patched("hdf5_links_earliest.h5", "hdf5_reject_offsets_4.h5", [(13, b"\x04")])
    patched("hdf5_links_earliest.h5", "hdf5_reject_superblock_version.h5", [(8, b"\x09")])
    patched("hdf5_links_earliest.h5", "hdf5_reject_truncated.h5", [], cut=size // 3)
    # the first element of `ascii`: a collection at the address of `scalar_int`'s data, an
    # object index no collection has, a length its object does not have
    patched("_strings.h5", "hdf5_reject_vlen_collection.h5", [(ascii_at + 4, struct.pack("<Q", plain))])
    patched("_strings.h5", "hdf5_reject_vlen_index.h5", [(ascii_at + 12, struct.pack("<I", 60000))])
    patched("_strings.h5", "hdf5_reject_vlen_length.h5", [(ascii_at, struct.pack("<I", 2))])
    (OUT / "_strings.h5").unlink()
    # the B-tree's first chunk address of `plain` past the end of the file
    data = (OUT / "hdf5_filters.h5").read_bytes()
    key = data.find(struct.pack("<Q", chunk))
    patched("hdf5_filters.h5", "hdf5_reject_chunk_outside.h5", [(key, struct.pack("<Q", len(data) + 4096))])
    deep()


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for p in OUT.glob("*"):
        if p.suffix in (".h5", ".nwb", ".bin"):
            p.unlink()
    numbers()
    scalars_and_strings()
    compact_and_layouts()
    enums_and_compounds()
    references_and_links("hdf5_links_earliest.h5", "earliest")
    references_and_links("hdf5_links_latest.h5", "latest")
    latest()
    filters_and_chunks()
    names_and_attributes()
    netcdf4()
    nwb_files()
    rejects()
    for p in sorted(OUT.glob("*")):
        print(p.name, p.stat().st_size)


if __name__ == "__main__":
    main()
