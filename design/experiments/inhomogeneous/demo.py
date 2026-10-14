"""Why many-file datasets fall off the virtualization "complexity cliff".

An executable companion to zarr-developers/zarr-specs#399 ("Varying codec parameters
per-chunk"). Each case writes a few small netCDF4 files that are each perfectly valid,
virtualizes every file with VirtualiZarr, and then tries to combine them into one
logical Zarr array along time, the way you would a real archive. Each case reports
what happens: an error, or a combined array whose values are compared with the
truth read straight from the files.

    uv run python design/experiments/inhomogeneous/demo.py

Cases (the use cases of the issue):

1. codec eras: the compression changes partway through the record (GOES-16)
2. rectilinear: files of different lengths along time (monthly files)
3. ragged inner chunks: each file is chunked internally and its last chunk is short
4. per-file scale_factor / add_offset (BOM BARPA)
5. varying dtype: the same variable stored as int16 in some files and float32 in others
"""

from __future__ import annotations

import json
import sys
import tempfile
import warnings
from pathlib import Path

import h5py
import numpy as np
import virtualizarr as vz
import xarray as xr
from obspec_utils.registry import ObjectStoreRegistry
from obstore.store import LocalStore
from virtualizarr.parsers import HDFParser

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).parent))
NX = 6  # a small spatial axis, so every case runs in a second


def write(path: Path, data: np.ndarray, *, t0: int, chunks=None, compression=None,
          compression_opts=None, shuffle=False, attrs=None) -> None:
    """One netCDF4 file: a variable `v(time, x)`, its `time` coordinate starting at t0."""
    with h5py.File(path, "w") as f:
        nt = data.shape[0]
        t = f.create_dataset("time", data=np.arange(t0, t0 + nt, dtype="int64"))
        t.make_scale("time")
        t.attrs["units"] = "days since 2000-01-01"
        x = f.create_dataset("x", data=np.arange(NX, dtype="int64"))
        x.make_scale("x")
        v = f.create_dataset("v", data=data, chunks=chunks, compression=compression,
                             compression_opts=compression_opts, shuffle=shuffle)
        v.dims[0].attach_scale(t)
        v.dims[1].attach_scale(x)
        for k, val in (attrs or {}).items():
            v.attrs[k] = val


def virtualize(paths: list[Path], root: Path) -> list[xr.Dataset]:
    """Each file as a virtual dataset (only its metadata is read)."""
    registry = ObjectStoreRegistry({f"file://{root}": LocalStore(prefix=root)})
    return [vz.open_virtual_dataset(f"file://{p}", registry=registry, parser=HDFParser(),
                                    loadable_variables=["time", "x"]) for p in paths]


def combine(vdss: list[xr.Dataset]) -> xr.Dataset:
    """Concatenate along time, as open_virtual_mfdataset would."""
    return xr.concat(vdss, dim="time", coords="minimal", compat="override", combine_attrs="override")


def truth(paths: list[Path]) -> np.ndarray:
    """What a user expects: every file decoded on its own (CF scaling applied), then stacked."""
    return np.concatenate([xr.open_dataset(p, engine="h5netcdf")["v"].values for p in paths])


def read_back(vds: xr.Dataset) -> np.ndarray:
    """Read the combined virtual array through Zarr, as a user of the virtual store would:
    via refkv's in-memory reference store, decoding CF attributes the way xarray does."""
    import refkv
    import refkv.zarr

    store = refkv.ReferenceStore.from_virtualizarr(vds)
    return xr.open_zarr(refkv.zarr.store(store), consolidated=False)["v"].values


def run(title: str, issue: str, make) -> None:
    print(f"\n{'=' * 78}\n{title}\n  {issue}\n{'-' * 78}")
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        paths = make(root)
        for p in paths:
            with h5py.File(p) as f:
                v = f["v"]
                filters = {k: v for k, v in (("compression", v.compression), ("opts", v.compression_opts),
                                             ("shuffle", v.shuffle)) if v}
                cf = {k: float(v.attrs[k]) for k in ("scale_factor", "add_offset") if k in v.attrs}
                print(f"  {p.name}: shape={v.shape} chunks={v.chunks} dtype={v.dtype} "
                      f"filters={filters or 'none'}" + (f" {cf}" if cf else ""))
        vdss = virtualize(paths, root)  # each file alone virtualizes fine
        print(f"  -> each file virtualizes on its own: {len(vdss)} virtual datasets")
        want = truth(paths)
        virtualizarr_result(vdss, want)
        refkv_result(vdss, want, root)


def virtualizarr_result(vdss: list[xr.Dataset], want: np.ndarray) -> None:
    """VirtualiZarr: concatenate the ManifestArrays, then read through Zarr."""
    try:
        combined = combine(vdss)
    except Exception as e:  # the cliff: no single Zarr array can describe these files
        print(f"  VirtualiZarr: combining fails: {type(e).__name__}: {str(e).splitlines()[0][:200]}")
        return
    try:
        got = read_back(combined)
    except Exception as e:
        print(f"  VirtualiZarr: combined, but reading fails: {type(e).__name__}: {str(e).splitlines()[0][:200]}")
        return
    report("VirtualiZarr: combined (one codec chain and the first file's attributes for all)", got, want)

def report(label: str, got: np.ndarray, want: np.ndarray) -> None:
    if got.shape == want.shape and np.allclose(got, want, equal_nan=True):
        print(f"  {label}: v{got.shape} {got.dtype}, every value matches the files")
    else:
        bad = int(np.sum(~np.isclose(got, want, equal_nan=True))) if got.shape == want.shape else "all"
        print(f"  {label}: SILENTLY WRONG, {bad} of {want.size} values differ from the files")
        print(f"     expected {np.round(want[:, 0], 2)}\n     got      {np.round(got[:, 0], 2)}")


def refkv_result(vdss: list[xr.Dataset], want: np.ndarray, root: Path) -> None:
    """refkv: one array from the same per-file manifests (variant.py), saved as a vzip
    archive and read back through Zarr."""
    import refkv
    import variant

    try:
        store = variant.combine(vdss)
        archive = root / "combined.vzip"
        store.save(str(archive))
        got = variant.read(refkv.open(str(archive)))
    except Exception as e:
        print(f"  refkv: fails: {type(e).__name__}: {str(e).splitlines()[0][:200]}")
        return
    n = len(json.loads(store.get("v/zarr.json"))["codecs"][0]["configuration"]["variants"])
    report(f"refkv: one array, {n} chunk variant{'s' * (n != 1)}, saved and reopened", got, want)
    data = sum(p.stat().st_size for p in root.glob("*.nc"))
    print(f"     the archive is {archive.stat().st_size:,} bytes; the chunks stay in the "
          f"{len(store.sources)} files ({data:,} bytes), read in place")


rng = np.random.default_rng(0)


def era_files(root: Path) -> list[Path]:
    paths = []
    # gzip, then gzip behind a shuffle filter, then no compression at all
    for i, (comp, opts, shuf) in enumerate([("gzip", 4, False), ("gzip", 4, False),
                                            ("gzip", 4, True), (None, None, False)]):
        p = root / f"era_{i}.nc"
        write(p, rng.random((4, NX)).astype("float32"), t0=4 * i, chunks=(4, NX),
              compression=comp, compression_opts=opts, shuffle=shuf)
        paths.append(p)
    return paths


def rectilinear_files(root: Path) -> list[Path]:
    paths, t0 = [], 0
    for i, nt in enumerate([31, 28, 31]):  # January, February, March
        p = root / f"month_{i}.nc"
        write(p, rng.random((nt, NX)).astype("float32"), t0=t0, chunks=(nt, NX), compression="gzip")
        paths.append(p)
        t0 += nt
    return paths


def ragged_files(root: Path) -> list[Path]:
    paths = []
    for i in range(3):  # 10 steps per file, chunked by 4: chunks of 4, 4, 2
        p = root / f"ragged_{i}.nc"
        write(p, rng.random((10, NX)).astype("float32"), t0=10 * i, chunks=(4, NX), compression="gzip")
        paths.append(p)
    return paths


def scaled_files(root: Path) -> list[Path]:
    paths = []
    for i, (scale, offset) in enumerate([(0.01, 280.0), (0.02, 270.0), (0.005, 290.0)]):
        p = root / f"scaled_{i}.nc"
        physical = 285.0 + 5 * rng.random((4, NX))
        packed = np.round((physical - offset) / scale).astype("int16")
        write(p, packed, t0=4 * i, chunks=(4, NX), compression="gzip",
              attrs={"scale_factor": np.float32(scale), "add_offset": np.float32(offset)})
        paths.append(p)
    return paths


def dtype_files(root: Path) -> list[Path]:
    paths = []
    for i, dtype in enumerate(["int16", "int16", "float32"]):
        p = root / f"dtype_{i}.nc"
        write(p, (100 * rng.random((4, NX))).astype(dtype), t0=4 * i, chunks=(4, NX), compression="gzip")
        paths.append(p)
    return paths


if __name__ == "__main__":
    run("1. codec eras", "the compression changes partway through the record (GOES-16)", era_files)
    run("2. rectilinear chunks", "monthly files: 31, 28 and 31 days, one chunk each", rectilinear_files)
    run("3. ragged inner chunks", "each file chunked by 4 along time, 10 steps: chunks 4, 4, 2", ragged_files)
    run("4. per-file scale_factor / add_offset", "each file packs int16 with its own CF scaling (BOM BARPA)",
        scaled_files)
    run("5. varying dtype", "the same variable is int16 in two files and float32 in the third", dtype_files)
