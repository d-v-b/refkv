# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "refkv[virtualizarr]>=0.1.0",
#     "obstore>=0.11.1",
#     "virtualizarr>=2.7.3",
#     "zarr>=3.4.0",
#     "cast-value-rs>=0.4.2",
#     "xarray>=2026.9.0",
#     "h5py>=3.16.0",
#     "h5netcdf>=1.8.1",
#     "matplotlib>=3.10",
# ]
# ///
"""The many-file cases of zarr-developers/zarr-specs#399 ("Varying codec parameters
per-chunk"), and a sketch of one way to express them as a single Zarr array.

Each case writes a few small netCDF4 files, each valid and consistent on its own,
holding one smooth field `v(time, x)` that continues across the files. Every file is
virtualized with VirtualiZarr, which parses all of them without trouble. The question
is the next step, one logical array along time:

- "one Zarr array today": `xr.concat` of the virtual datasets, as
  `open_virtual_mfdataset` does. A Zarr array has one codec chain, one dtype, one
  regular chunk grid and one set of attributes, so VirtualiZarr refuses combinations
  that would need more, and says what is missing. That refusal protects users; it is
  the gap the issue describes.
- "switch sketch": the same per-file manifests, combined by switch.py into one array
  whose chunks each select their own codec chain (design 2.1 of the issue), saved as a
  vzip archive with refkv and read back through Zarr.

Both are compared with the files read one by one:

- the reference is each file decoded by xarray + h5netcdf (HDF5's own filters, CF
  scaling and _FillValue), concatenated;
- "identical" means equal bit for bit (NaN equal to NaN); otherwise the largest
  absolute difference is reported;
- figures/ gets one plot per case: the files and the combined arrays along time at
  x = 0, with the files' boundaries, and the sketch's whole field over (time, x).

    uv run demo.py

The dependencies are declared inline (PEP 723), so `uv run` builds an environment for
this script alone; switch.py, next to it, is the only other file it needs.

Cases (the use cases of the issue):

1. codec eras: the compression changes partway through the record (GOES-16)
2. rectilinear: files of different lengths along time (monthly files)
3. ragged inner chunks: each file is chunked internally and its last chunk is short
4. per-file scale_factor / add_offset (BOM BARPA)
5. varying dtype: the same variable stored as int16 in some files and float32 in others
"""

from __future__ import annotations

import json
import tempfile
import textwrap
import warnings
from dataclasses import dataclass, field
from pathlib import Path

import h5py
import matplotlib
import numpy as np
import virtualizarr as vz
import xarray as xr
from obspec_utils.registry import ObjectStoreRegistry
from obstore.store import LocalStore
from virtualizarr.parsers import HDFParser

import refkv
import refkv.zarr
import switch  # next to this script, which Python puts on sys.path

matplotlib.use("Agg")  # files only, no display
import matplotlib.pyplot as plt  # noqa: E402 - after choosing the backend

warnings.filterwarnings("ignore")
HERE = Path(__file__).parent
NX = 6  # a small spatial axis, so every case runs in a second


def smooth(t: np.ndarray, base: float = 285.0, amp: float = 4.0) -> np.ndarray:
    """One smooth field over (time, x), continuous across files: a misdecoded file
    shows as a jump at its boundary."""
    x = np.arange(NX)
    return base + amp * np.sin(2 * np.pi * t[:, None] / 37.0) + 0.6 * np.cos(2 * np.pi * x[None, :] / NX)


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


def truth(paths: list[Path]) -> np.ndarray:
    """What a user expects: every file decoded on its own, then stacked."""
    return np.concatenate([xr.open_dataset(p, engine="h5netcdf")["v"].values for p in paths])


@dataclass
class Result:
    title: str
    files: list[str]
    boundaries: list[int]  # the time index where each file starts
    want: np.ndarray
    vz: np.ndarray | None = None
    vz_note: str = ""
    rk: np.ndarray | None = None
    rk_note: str = ""
    extra: dict = field(default_factory=dict)


def compare(got: np.ndarray, want: np.ndarray) -> str:
    if got.shape != want.shape:
        return f"shape {got.shape}, not {want.shape}"
    if np.array_equal(got, want, equal_nan=True):
        return "identical to the files (bit for bit)"
    d = np.abs(got.astype("float64") - want.astype("float64"))
    bad = int(np.sum(~np.isclose(got, want, equal_nan=True, rtol=0, atol=1e-3)))
    return (f"max |difference| {np.nanmax(d):.3g}; {bad} of {want.size} values off by more than 0.001"
            if bad else f"max |difference| {np.nanmax(d):.3g} (float32 rounding)")


def virtualizarr_result(r: Result, vdss: list[xr.Dataset]) -> None:
    """One Zarr array today: concatenate the virtual datasets (xarray's default
    combine_attrs="override", as open_virtual_mfdataset uses), then read through Zarr."""
    try:
        combined = xr.concat(vdss, dim="time", coords="minimal", compat="override", combine_attrs="override")
    except Exception as e:  # what one Zarr array can't express, as VirtualiZarr reports it
        r.vz_note = f"not one array: {type(e).__name__}: {str(e).splitlines()[0][:160]}"
        return
    store = refkv.ReferenceStore.from_virtualizarr(combined)
    r.vz = xr.open_zarr(refkv.zarr.store(store), consolidated=False)["v"].values
    r.vz_note = ("combined, with the first file's attributes for every file (combine_attrs='override'; "
                 "'no_conflicts' refuses instead): " + compare(r.vz, r.want))


def refkv_result(r: Result, vdss: list[xr.Dataset], root: Path) -> None:
    """refkv: one array from the same per-file manifests (switch.py), saved as a vzip
    archive, reopened, and read through Zarr."""
    store = switch.combine(vdss)
    archive = root / "combined.vzip"
    store.save(str(archive))
    r.rk = switch.read(refkv.open(str(archive)))
    n = len(json.loads(store.get("v/zarr.json"))["codecs"][0]["configuration"]["cases"])
    data = sum(p.stat().st_size for p in root.glob("*.nc"))
    r.rk_note = f"one array, {n} switch case{'s' * (n != 1)}: " + compare(r.rk, r.want)
    r.extra["archive"] = (f"the archive is {archive.stat().st_size:,} bytes; the chunks stay in "
                          f"the {len(store.sources)} files ({data:,} bytes), read in place")


def run(title: str, make) -> Result:
    print(f"\n{'=' * 78}\n{title}\n{'-' * 78}")
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        paths = make(root)
        files, starts, t = [], [], 0
        for p in paths:
            with h5py.File(p) as f:
                v = f["v"]
                filters = [s for s in (v.compression and f"{v.compression}", v.shuffle and "shuffle") if s]
                cf = ""
                if "scale_factor" in v.attrs:
                    cf = f"\n×{float(v.attrs['scale_factor']):g} +{float(v.attrs['add_offset']):g}"
                label = f"{v.dtype}\n" + ("+".join(filters) or "uncompressed") + cf
                print(f"  {p.name}: shape={v.shape} chunks={v.chunks} {label.replace(chr(10), ' ')}")
                files.append(label)
                starts.append(t)
                t += v.shape[0]
        r = Result(title, files, starts, truth(paths))
        vdss = virtualize(paths, root)  # each file alone virtualizes fine
        virtualizarr_result(r, vdss)
        refkv_result(r, vdss, root)
    print(f"  one Zarr array today: {r.vz_note}\n  switch sketch:        {r.rk_note}\n"
          f"                        {r.extra['archive']}")
    return r


def plot(r: Result, path: Path) -> None:
    fig, (ax, ax2) = plt.subplots(2, 1, figsize=(9, 6.5), height_ratios=[3, 2], constrained_layout=True)
    fig.suptitle(r.title, fontsize=10, x=0.01, ha="left")
    n = r.want.shape[0]
    t = np.arange(n)
    ax.plot(t, r.want[:, 0], color="0.75", lw=6, label="the files, each read on its own")
    if r.vz is not None:
        ax.plot(t, r.vz[:, 0], color="tab:orange", lw=1.5, marker="x", ms=4,
                label="one Zarr array today (first file's attributes)")
    if r.rk is not None:
        ax.plot(t, r.rk[:, 0], color="k", lw=1.2, ls="--", label="switch sketch")
    ends = r.boundaries[1:] + [n]
    for b in r.boundaries[1:]:
        ax.axvline(b - 0.5, color="0.5", lw=0.8, ls=":")
    top = ax.secondary_xaxis("top")  # each file's encoding, over its span
    top.set_xticks([(b + e - 1) / 2 for b, e in zip(r.boundaries, ends)], r.files, fontsize=7)
    top.tick_params(length=0)
    ax.set_xlim(-0.5, n - 0.5)
    ax.set_xlabel("time index")
    ax.set_ylabel("v at x = 0")
    vz = r.vz_note if r.vz is not None else "\n".join(textwrap.wrap(r.vz_note, 120))
    ax.set_title(vz if r.title.startswith("6.") else f"one Zarr array today: {vz}", fontsize=8, loc="left",
                 color="0.3")
    ax.legend(fontsize=8, loc="best")
    im = ax2.imshow(r.rk.T, aspect="auto", cmap="viridis", interpolation="nearest",
                    extent=(-0.5, n - 0.5, NX - 0.5, -0.5))
    for b in r.boundaries[1:]:
        ax2.axvline(b - 0.5, color="w", lw=0.8, ls=":")
    ax2.set_xlabel("time index")
    ax2.set_ylabel("x")
    ax2.set_title(f"switch sketch, all of v(time, x): {r.rk_note}", fontsize=8, loc="left")
    fig.colorbar(im, ax=ax2, label="v")
    fig.savefig(path, dpi=110)
    plt.close(fig)


rng = np.random.default_rng(0)


def era_files(root: Path) -> list[Path]:
    paths = []
    # gzip, gzip, then gzip behind a shuffle filter, then no compression at all
    for i, (comp, shuf) in enumerate([("gzip", False), ("gzip", False), ("gzip", True), (None, False)]):
        p = root / f"era_{i}.nc"
        write(p, smooth(np.arange(8 * i, 8 * i + 8)).astype("float32"), t0=8 * i, chunks=(4, NX),
              compression=comp, shuffle=shuf)
        paths.append(p)
    return paths


def rectilinear_files(root: Path) -> list[Path]:
    paths, t0 = [], 0
    for i, nt in enumerate([31, 28, 31]):  # January, February, March
        p = root / f"month_{i}.nc"
        write(p, smooth(np.arange(t0, t0 + nt)).astype("float32"), t0=t0, chunks=(nt, NX), compression="gzip")
        paths.append(p)
        t0 += nt
    return paths


def ragged_files(root: Path) -> list[Path]:
    paths = []
    for i in range(3):  # 10 steps per file, chunked by 4: chunks of 4, 4, 2
        p = root / f"ragged_{i}.nc"
        write(p, smooth(np.arange(10 * i, 10 * i + 10)).astype("float32"), t0=10 * i, chunks=(4, NX),
              compression="gzip")
        paths.append(p)
    return paths


def scaled_files(root: Path) -> list[Path]:
    paths = []
    for i, (scale, offset) in enumerate([(0.01, 280.0), (0.02, 270.0), (0.005, 290.0)]):
        p = root / f"scaled_{i}.nc"
        physical = smooth(np.arange(10 * i, 10 * i + 10))
        packed = np.round((physical - offset) / scale).astype("int16")
        write(p, packed, t0=10 * i, chunks=(5, NX), compression="gzip",
              attrs={"scale_factor": np.float32(scale), "add_offset": np.float32(offset),
                     "_FillValue": np.int16(-32767)})
        paths.append(p)
    return paths


def dtype_files(root: Path) -> list[Path]:
    paths = []
    for i, dtype in enumerate(["int16", "int16", "float32"]):
        p = root / f"dtype_{i}.nc"
        values = smooth(np.arange(10 * i, 10 * i + 10), base=50, amp=40)
        write(p, (np.round(values) if dtype == "int16" else values).astype(dtype), t0=10 * i, chunks=(5, NX),
              compression="gzip", attrs={"_FillValue": np.int16(-32767)} if dtype == "int16" else None)
        paths.append(p)
    return paths


CASES = [
    ("1. codec eras: gzip, gzip, shuffle + gzip, uncompressed (GOES-16)", era_files),
    ("2. rectilinear: monthly files of 31, 28 and 31 days, one chunk each", rectilinear_files),
    ("3. ragged inner chunks: 10 steps per file chunked by 4 (4, 4, 2)", ragged_files),
    ("4. per-file scale_factor / add_offset on int16 (BOM BARPA)", scaled_files),
    ("5. varying dtype: int16, int16, float32", dtype_files),
]

def concatenation(figures: Path) -> None:
    """Two arrays combined separately, then concatenated as manifests: the case tables
    merge by union of their content keys, and no chunk's header is rewritten."""
    title = "6. concatenating two combined arrays (gzip, shuffle + gzip | gzip, uncompressed)"
    print(f"\n{'=' * 78}\n{title}\n{'-' * 78}")
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        paths = []
        for i, (comp, shuf) in enumerate([("gzip", False), ("gzip", True), ("gzip", False), (None, False)]):
            p = root / f"part_{i}.nc"
            write(p, smooth(np.arange(8 * i, 8 * i + 8)).astype("float32"), t0=8 * i, chunks=(4, NX),
                  compression=comp, shuffle=shuf)
            paths.append(p)
        vdss = virtualize(paths, root)
        a, b = switch.build(vdss[:2]), switch.build(vdss[2:])
        mapping, sources = switch.concatenate(a, b)
        keys = [set(json.loads(m["v/zarr.json"])["codecs"][0]["configuration"]["cases"]) for m in (a[0], b[0], mapping)]
        moved = all(mapping[f"v/c/{int(k.split('/')[2]) + len(json.loads(a[0]['v/zarr.json'])['chunk_grid']['configuration']['chunk_shapes'][0])}/{k.split('/')[3]}"].parts[0].data
                    == ref.parts[0].data for k, ref in b[0].items() if k.startswith("v/c/"))
        direct = json.loads(switch.build(vdss)[0]["v/zarr.json"])["codecs"]
        same = json.loads(mapping["v/zarr.json"])["codecs"] == direct
        archive = root / "concatenated.vzip"
        refkv.ReferenceStore(mapping, sources).save(str(archive))
        got, want = switch.read(refkv.open(str(archive))), truth(paths)
    print(f"  array A: {len(keys[0])} cases {sorted(keys[0])}\n  array B: {len(keys[1])} cases {sorted(keys[1])}")
    print(f"  A + B:   {len(keys[2])} cases (the shared gzip case once); every chunk header copied unchanged: {moved};"
          f" codecs equal to combining all four files at once: {same}")
    note = "concatenated, saved and reopened: " + compare(got, want)
    print(f"  {note}")
    r = Result(title, ["float32\ngzip", "float32\ngzip+shuffle", "float32\ngzip", "float32\nuncompressed"],
               [0, 8, 16, 24], want, vz_note="array A = files 1-2 and array B = files 3-4, each combined with switch, then concatenated", rk=got,
               rk_note=f"A ({len(keys[0])} cases) + B ({len(keys[1])} cases) = {len(keys[2])} cases: " + compare(got, want))
    plot(r, figures / "case6.png")


if __name__ == "__main__":
    figures = HERE / "figures"
    figures.mkdir(exist_ok=True)
    for i, (title, make) in enumerate(CASES, 1):
        r = run(title, make)
        plot(r, figures / f"case{i}.png")
    concatenation(figures)
    print(f"\nfigures: {figures}")
