"""Checks the SAFE zarrifiers' outputs against an independent reader (GDAL).

Each product in fixtures/safe/ (a directory, served by the harness
proxy with its S3 listing, or a `.SAFE.zip` file) is zarrified by the
shipped Python code (`python -m refkv.zarrify`, the Rust core natively) and
by the browser code (js/conformance/zarrify.ts, under Node, the core as
wasm32). Each archive is opened through
refkv.zarr and zarr-python (with refkv.zarr.codecs' imagecodecs_jpeg2k), and:

- every band array, read whole, must equal its band file as rasterio's
  JP2OpenJPEG driver (GDAL, OpenJPEG) decodes it;
- each resolution group's `spatial:transform`, `spatial:shape` and
  `proj:code` must be GDAL's geotransform, size and CRS of each of its band
  files (from their GML-in-JP2), and `proj:wkt2` must be the same CRS as
  pyproj's EPSG definition;
- the product must be rebuilt from the hierarchy (spec/zarrify/safe.md
  §6.4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safe.md#64-reconstruction>): a
  zip file byte for byte from its IR mirror; a directory's held XML documents
  from its mirror alone, every other object from `refkv_source/objects/`, the
  empty objects from their keys, and every band file, described in place,
  from its own source up to its codestream; and, in both forms, every band
  file's codestream from its `siz` and its chunks, byte for byte, with
  nothing left over (folder markers excepted);
- `safe_reject_*` products must be rejected by both.

With `--remote <product URL> [<band path> ...]`, a public product (a `.SAFE/`
directory, or a zip file, read through the harness proxy) is zarrified by
both implementations,
and the listed band arrays (default: the 60 m group) are compared with GDAL
reading the band files over HTTP (/vsicurl/), and, for a directory, with
GDAL's SENTINEL2 driver reading the product metadata; the rebuild check
covers the listed bands' codestreams, rebuilt from their chunks.

Usage: uv run python js/test/safe/verify.py [<fixture> ...] [--py-only]
       uv run python js/test/safe/verify.py --remote <url> [<band path> ...] [--py-only]
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

import numpy as np
import rasterio
import zarr
from pyproj import CRS
from zarr.core.buffer import default_buffer_prototype

HERE = Path(__file__).parent
ROOT = HERE.parents[2]
FIXTURES = ROOT / "fixtures"
sys.path.insert(0, str(ROOT / "conformance" / "zarrify"))
# the frozen SAFE twin's zip reader, for reading remote zip files' entries
sys.path.insert(0, str(ROOT / "conformance" / "zarrify" / "reference"))
from proxy import Proxy  # noqa: E402
from refkv_reference.safe import zipdir  # noqa: E402

import refkv.zarr  # noqa: E402  (a vzip archive as a zarr store; registers imagecodecs_jpeg2k)
from refkv import _core  # noqa: E402
from refkv.ir.cmirror import Archive, canonical_problem, rebuild_from_archive, view_problem  # noqa: E402
from refkv.pb import decode_source_table  # noqa: E402
from refkv.zarrify.common import http_reader  # noqa: E402
from refkv.zarrify.store import object_url, open_store  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # js/test, for verify_common
from verify_common import LOOPBACK_SOURCES  # noqa: E402

ESCAPED = re.compile(r"(?:zarr\.json|\.zarray|\.zgroup)~+\Z")


def run(impl: str, url: str, out: Path) -> subprocess.CompletedProcess:
    cmd = (["uv", "run", "python", "-m", "refkv.zarrify", "--allow-private-hosts"] if impl == "py"
           else ["node", str(ROOT / "js" / "conformance" / "zarrify.ts"), "--allow-private-hosts"])
    return subprocess.run([*cmd, url, str(out)], capture_output=True, text=True, cwd=ROOT, check=False)


def product_files(path: Path) -> dict[str, bytes]:
    """The product's objects: a directory's files, or a zip file's entries under its root."""
    if path.is_dir():
        return {p.relative_to(path).as_posix(): p.read_bytes() for p in path.rglob("*") if p.is_file()}
    z = zipfile.ZipFile(path)
    return {i.filename.partition("/")[2]: z.read(i) for i in z.infolist() if not i.filename.endswith("/")}


def product_dirs(path: Path) -> set[str]:
    """The directories the product names: by its folder markers (`d_$folder$`), or by its zip
    file's directory entries."""
    if path.is_dir():
        return {p.relative_to(path).as_posix()[: -len("_$folder$")] for p in path.rglob("*_$folder$")
                if p.is_file() and p.name != "_$folder$"}
    return {n.partition("/")[2][:-1] for n in zipfile.ZipFile(path).namelist()
            if n.endswith("/") and n.partition("/")[2]}


def parents(keys) -> set[str]:
    return {k[:i] for k in keys for i in range(len(k)) if k[i] == "/"}


def get_all(vz: refkv.zarr.ZarrStore, keys: list[str]) -> list[bytes]:
    async def go():
        return [(await vz.get(k, default_buffer_prototype())).to_bytes() for k in keys]

    return asyncio.run(go())


def documents(out: Path) -> dict[str, dict]:
    z = zipfile.ZipFile(out)
    return {n: json.loads(z.read(n)) for n in z.namelist() if n.endswith("zarr.json")}


def gdal_read(data: bytes | str):
    """(pixels [c, h, w], transform, crs, width, height) of a band file, by GDAL's JP2OpenJPEG driver."""
    with rasterio.Env(GDAL_SKIP="JP2KAK JP2ECW JP2MrSID JP2Lura"):
        if isinstance(data, str):
            with rasterio.open(data, driver="JP2OpenJPEG") as ds:
                return ds.read(), ds.transform, ds.crs, ds.width, ds.height
        with rasterio.MemoryFile(data) as m, m.open(driver="JP2OpenJPEG") as ds:
            return ds.read(), ds.transform, ds.crs, ds.width, ds.height


def check_bands(out: Path, docs: dict, read_band, only: list[str] | None = None) -> tuple[list[str], int]:
    """Every band array (or those of `only`) against GDAL, and each group's georeferencing."""
    problems = []
    vz = refkv.zarr.open(str(out), policy=LOOPBACK_SOURCES)
    arrays = sorted(k[: -len("/zarr.json")] for k, d in docs.items()
                    if d.get("node_type") == "array" and re.match(r"r[0-9]+m/", k))
    if only is not None:
        arrays = [a for a in arrays if a in only or a.split("/")[0] in only]
    for path in arrays:
        group = docs[path.split("/")[0] + "/zarr.json"]["attributes"]
        s = docs[f"{path}/zarr.json"]["attributes"]["refkv_zarrified"]["safe"]
        want, transform, crs, w, h = read_band(s["IMAGE_FILE"] + ".jp2")
        got = zarr.open_array(vz, path=path, mode="r", zarr_format=3)[...]
        if got.ndim == 2:
            got = got[None]
        if got.shape != want.shape or got.dtype != want.dtype or not np.array_equal(got, want):
            problems.append(f"{path}: {got.dtype}{got.shape} differs from GDAL's {want.dtype}{want.shape}")
        if group["spatial:transform"] != list(transform)[:6]:
            problems.append(f"{path}: spatial:transform {group['spatial:transform']} "
                            f"is not GDAL's {list(transform)[:6]}")
        if group["spatial:shape"] != [h, w]:
            problems.append(f"{path}: spatial:shape {group['spatial:shape']} is not GDAL's {[h, w]}")
        if crs is None or group["proj:code"] != f"EPSG:{crs.to_epsg()}":
            problems.append(f"{path}: proj:code {group['proj:code']} is not GDAL's {crs}")
    for key, d in docs.items():
        a = d.get("attributes", {})
        if "proj:wkt2" in a:
            c = CRS.from_wkt(a["proj:wkt2"])
            if not c.equals(CRS.from_user_input(a["proj:code"])):
                problems.append(f"{key}: proj:wkt2 is not {a['proj:code']}")
    return problems, len(arrays)


def band_codestreams(out: Path, docs: dict, bands_only: list[str] | None = None) -> dict[str, bytes]:
    """Each band file's codestream, rebuilt from its array's chunks and `siz` (spec/zarrify/safe.md
    §6.4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safe.md#64-reconstruction>):
    `FF 4F`, the SIZ segment, a chunk's main header after its SIZ, each tile's tile-part with
    its `Isot` restored, and `FF D9`. A band file ends with its codestream."""
    vz = refkv.zarr.open(str(out), policy=LOOPBACK_SOURCES)
    names = zipfile.ZipFile(out).namelist()
    files: dict[str, bytes] = {}
    for k, d in docs.items():
        if d.get("node_type") != "array" or not re.match(r"r[0-9]+m/", k):
            continue
        path = k[: -len("/zarr.json")]
        if bands_only is not None and path not in bands_only and path.split("/")[0] not in bands_only:
            continue
        band = d["attributes"]["refkv_zarrified"]["safe"]
        siz = base64.b64decode(band["siz"])
        chunk_keys = sorted((n for n in names if n.startswith(path + "/c/")),
                            key=lambda n: tuple(int(x) for x in n.split("/c/")[1].split("/")))
        cols = -(-d["shape"][-1] // d["chunk_grid"]["configuration"]["chunk_shape"][-1])
        chunks = get_all(vz, chunk_keys)
        parts = [b"\xff\x4f", siz]
        rest = None
        for key, c in zip(chunk_keys, chunks, strict=True):
            *_, v, u = (int(x) for x in key.split("/c/")[1].split("/"))
            lsiz = struct.unpack_from(">H", c, 4)[0]
            sot = c.index(b"\xff\x90\x00\x0a", 4 + lsiz)
            if rest is None:
                rest = c[4 + lsiz : sot]
                parts.append(rest)
            elif c[4 + lsiz : sot] != rest:
                raise AssertionError(f"{key}: the main header differs from the band's other chunks")
            psot = struct.unpack_from(">I", c, sot + 6)[0]
            parts.append(c[sot : sot + 4] + struct.pack(">H", v * cols + u) + c[sot + 6 : sot + psot])
        parts.append(b"\xff\xd9")
        files[band["IMAGE_FILE"] + ".jp2"] = b"".join(parts)
    return files


def _get(out: Path):
    """The archive's plain entries (the mirror's arrays and documents), by key."""
    z = zipfile.ZipFile(out)
    names = set(z.namelist())
    return lambda k: z.read(k) if k in names else None


def rebuild_store(out: Path, docs: dict, read) -> dict[str, bytes]:
    """The directory form's objects, rebuilt from the hierarchy (spec/zarrify/safe.md §6.4
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safe.md#64-reconstruction>): the
    held ones from the mirror alone, the empty ones from their keys, the others from
    `refkv_source/objects/`, and each band file, described in place, from its own source
    (`read(key, offset, length)`) up to its codestream, then its codestream from its chunks.
    The folder markers are not rebuilt."""
    vz = refkv.zarr.open(str(out), policy=LOOPBACK_SOURCES)
    files: dict[str, bytes] = {}
    objects = _core.rebuild_store(_get(out))
    kept = [n for n in zipfile.ZipFile(out).namelist() if n.startswith("refkv_source/objects/")]
    whole = {}
    for n, data in zip(kept, get_all(vz, kept), strict=True):
        key = n.removeprefix("refkv_source/objects/")
        whole[key[:-1] if ESCAPED.match(key.rpartition("/")[2]) else key] = data
    bands = band_codestreams(out, docs)
    for key, size, data in objects:
        if data is not None:
            files[key] = data
        elif size == 0:
            files[key] = b""
        elif key in whole:
            files[key] = whole[key]
        elif key in bands:
            cs = bands[key]
            files[key] = read(key, 0, size - len(cs)) + cs
    return files


def check_rebuild(item: Path, product: dict[str, bytes], out: Path, docs: dict, url: str,
                  dirs: set[str] = frozenset()) -> list[str]:
    """The product rebuilt from the hierarchy against the product: a zip file byte for byte from
    its mirror, and each band file's codestream from its chunks; or a directory's objects
    (`rebuild_store`), and its directories (`dirs`, those its folder markers name): each must be
    a directory of a rebuilt object, or one of the empty directories rebuilt from `empty_dirs`.
    The folder markers are layout of the mirror, and are not rebuilt as objects."""
    def ranged(u: str, o: int, n: int) -> bytes:
        if n == 0:
            return b""
        # an http URL: the proxy's, or the product's
        req = urllib.request.Request(u, headers={"Range": f"bytes={o}-{o + n - 1}"})  # noqa: S310
        with urllib.request.urlopen(req, timeout=120) as r:  # noqa: S310 (as above)
            return r.read()

    if not item.is_dir():
        # the leaves partition the zip file (read from source 0); the values the mirror shows
        # (the end records, the central directory's and local headers, the band files' SIZ,
        # marker and SOT segments) are the source's bytes
        rebuilt = bytearray()
        rebuild_from_archive(str(out), lambda o, n: ranged(url, o, n), rebuilt.extend)
        problems = [] if bytes(rebuilt) == item.read_bytes() else ["the zip file is not rebuilt byte for byte"]
        archive = Archive(str(out), lambda o, n: ranged(url, o, n))
        problems += [p for p in (canonical_problem(archive), view_problem(archive)) if p]
        for k, cs in band_codestreams(out, docs).items():
            if not product[k].endswith(cs):
                problems.append(f"{k}: its codestream is not rebuilt from its chunks")
        return problems
    # a band file is read through the url source the archive records for it, whose size pin must
    # be the product's object's
    with zipfile.ZipFile(out) as z:
        pins = {s.url: s.size for s in decode_source_table(z.read("__vz__/sources")) if s.url is not None}
    problems = [f"{k}: its url source pins {pins.get(object_url(url, k))} bytes, not {len(v)}"
                for k, v in product.items() if object_url(url, k) in pins and pins[object_url(url, k)] != len(v)]

    def read(k: str, o: int, n: int) -> bytes:
        u = object_url(url, k)
        if u not in pins:
            raise AssertionError(f"{k}: the archive has no url source for it")
        return ranged(u, o, n)

    files = rebuild_store(out, docs, read)
    want = {k: v for k, v in product.items() if not (k.endswith("_$folder$") and k[: -len("_$folder$")] in dirs)}
    problems += [f"{k}: not rebuilt" for k in sorted(set(want) - set(files))]
    problems += [f"{k}: rebuilt, but not in the product" for k in sorted(set(files) - set(want))]
    problems += [f"{k}: rebuilt with other bytes" for k in sorted(set(want) & set(files)) if want[k] != files[k]]
    empty = docs["refkv_source/zarr.json"]["attributes"]["refkv_zarrified"]["safe"].get("empty_dirs", [])
    kept = parents(files) | set(empty) | parents(empty)
    problems += [f"{d}/: a directory not rebuilt" for d in sorted(dirs - kept)]
    problems += [f"{d}/: rebuilt as an empty directory, but not one of the product" for d in empty
                 if d not in dirs or d in parents(files)]
    return problems


def fixtures(argv: list[str], impls: list[str]) -> int:
    items = [Path(a) for a in argv] or sorted(p for p in (FIXTURES / "safe").iterdir())
    proxy = Proxy(FIXTURES, Path(tempfile.mkdtemp()))
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        for item in items:
            rel = item.resolve().relative_to(FIXTURES.resolve()).as_posix()
            url = proxy.local_store(rel) if item.is_dir() else proxy.local(rel)
            for impl in impls:
                out = Path(tmp) / f"{item.name}.{impl}.vzip"
                p = run(impl, url, out)
                label = f"{item.name[:44]:44s} {impl:3s}"
                if item.name.startswith("safe_reject"):
                    ok = p.returncode == 3 and not out.exists()
                    failures += not ok
                    print(f"{label} "
                          f"{'rejected: ' + p.stderr.strip()[-80:] if ok else 'NOT REJECTED ' + p.stderr[-200:]}")
                    continue
                if p.returncode:
                    failures += 1
                    print(f"{label} FAILED: {p.stderr.strip()[-300:]}")
                    continue
                product = product_files(item)
                docs = documents(out)
                problems, n = check_bands(out, docs, lambda key, product=product: gdal_read(product[key]))
                problems += check_rebuild(item, product, out, docs, url, product_dirs(item))
                failures += bool(problems)
                print(f"{label} {problems[:4] if problems else 'ok'} ({n} bands, {len(product)} objects rebuilt)")
    print(f"\n{failures} failures")
    return 1 if failures else 0


def sentinel2(url: str, docs: dict, out: Path, groups: list[str]) -> list[str]:
    """The bands of GDAL's SENTINEL2 driver at the groups' resolutions against the arrays, and
    its band metadata against the band arrays' source metadata."""
    problems = []
    s = docs["zarr.json"]["attributes"]["refkv_zarrified"]["safe"]
    level = "L2A" if s.get("PROCESSING_LEVEL", "").startswith("Level-2A") else "L1C"  # Level-2Ap: PSD 14.2
    code = docs["zarr.json"]["attributes"]["proj:code"].replace(":", "_")
    vz = refkv.zarr.open(str(out), policy=LOOPBACK_SOURCES)
    for g in groups:
        sub = f"SENTINEL2_{level}:/vsicurl/{url}MTD_MSI{level}.xml:{g[1:]}:{code}"
        try:
            ds = rasterio.open(sub)
        except Exception as e:  # noqa: BLE001
            problems.append(f"SENTINEL2 {g}: {e}")
            continue
        with ds:
            for i in range(1, ds.count + 1):
                tags = ds.tags(i)
                name = tags.get("BANDNAME", "")
                m = re.fullmatch(r"B([0-9]+|8A)", name)
                array = f"{g}/B{int(m.group(1)):02d}" if m and m.group(1) != "8A" else f"{g}/{name}"
                if f"{array}/zarr.json" not in docs:
                    continue
                want = ds.read(i)
                got = zarr.open_array(vz, path=array, mode="r", zarr_format=3)[...]
                if got.shape != want.shape or not np.array_equal(got, want):
                    problems.append(f"SENTINEL2 {array}: differs ({got.shape} vs {want.shape})")
                band = docs[f"{array}/zarr.json"]["attributes"]["refkv_zarrified"]["safe"]
                for tag, member in (("SOLAR_IRRADIANCE", "SOLAR_IRRADIANCE"), ("BOA_ADD_OFFSET", "BOA_ADD_OFFSET"),
                                    ("RADIO_ADD_OFFSET", "RADIO_ADD_OFFSET")):
                    if tag in tags and member in band:
                        v = band[member]["value"] if isinstance(band[member], dict) else band[member]
                        if float(tags[tag]) != float(v):
                            problems.append(f"SENTINEL2 {array}: {tag} {tags[tag]} != {v}")
                compared = set(tags) & {"SOLAR_IRRADIANCE", "BOA_ADD_OFFSET", "RADIO_ADD_OFFSET", "WAVELENGTH"}
                print(f"    SENTINEL2 {array}: compared, metadata {sorted(compared)}")
    return problems


def remote(url: str, only: list[str], impls: list[str]) -> int:
    failures = 0
    if not url.endswith("/"):
        # A zip file is read through the harness proxy, which retries and caches its blocks: the
        # many range requests of the reads below otherwise meet dropped connections at some hosts.
        url = Proxy(FIXTURES, Path("/tmp/vzip-proxy-cache")).remote(url)  # noqa: S108 (the proxy cache, kept between runs)
    with tempfile.TemporaryDirectory() as tmp:
        for impl in impls:
            out = Path(tmp) / f"remote.{impl}.vzip"
            t0 = time.time()
            p = run(impl, url, out)
            if p.returncode:
                print(f"{impl}: FAILED {p.stderr[-400:]}")
                failures += 1
                continue
            print(f"{impl}: {p.stdout.strip()[:600]} ({time.time() - t0:.0f} s, archive {out.stat().st_size} bytes)")
            docs = documents(out)
            groups = only or [min((k.split("/")[0] for k in docs if re.match(r"r[0-9]+m/zarr.json", k)),
                                  key=lambda g: -int(g[1:-1]))]
            if url.endswith("/"):
                problems, n = check_bands(out, docs, lambda key: gdal_read(f"/vsicurl/{url}{key}"), groups)
                problems += sentinel2(url, docs, out, [g for g in groups if "/" not in g])
            else:
                # A zip file: GDAL reads its band files through /vsizip/ over /vsicurl/.
                root = zip_entries(url)[0]
                problems, n = check_bands(
                    out, docs, lambda key, root=root: gdal_read(f"/vsizip/{{/vsicurl/{url}}}/{root}/{key}"), groups)
            files = band_codestreams(out, docs, groups)
            problems += check_dirs(url, out, docs)
            bad = [k for k, data in files.items() if not fetch(url, k).endswith(data)]
            problems += [f"{k}: its codestream is not rebuilt from its chunks" for k in bad]
            failures += bool(problems)
            print(f"{impl}: {problems[:6] if problems else 'ok'} ({n} bands compared with GDAL, "
                  f"{len(files)} band codestreams rebuilt and compared)")
    return 1 if failures else 0


def described_keys(out: Path, docs: dict, url: str) -> set[str]:
    """The keys of every object the mirror describes (spec/zarrify/safe.md §6.3
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safe.md#63-the-source-metadata-node>):
    a directory's listing, from the mirror alone; or a zip file's objects, which its file IR
    covers whole (its leaves partition the file), read from its central directory."""
    desc = docs["refkv_source/zarr.json"]["attributes"]["refkv_zarrified"]["safe"]["ir"]
    if "objects" in desc:
        return {k for k, _, _ in _core.rebuild_store(_get(out))}
    return set(zip_entries(url)[1])


def check_dirs(url: str, out: Path, docs: dict) -> list[str]:
    """Each directory a remote product names (by its folder markers, or its zip directory entries)
    is a directory of a rebuilt object or a rebuilt empty directory."""
    if url.endswith("/"):
        dirs = {k[: -len("_$folder$")] for k in open_store(url).objects if k.endswith("_$folder$") and len(k) > 9}
    else:
        read, size = http_reader(url)
        dirs = {k[:-1] for k in zipdir.product_entries(zipdir.central_directory(read, size))[3]}
    empty = docs["refkv_source/zarr.json"]["attributes"]["refkv_zarrified"]["safe"].get("empty_dirs", [])
    kept = parents(described_keys(out, docs, url)) | set(empty) | parents(empty)
    return [f"{d}/: a directory not rebuilt" for d in sorted(dirs - kept)]


_ZIPS: dict[str, tuple] = {}


def zip_entries(url: str) -> tuple:
    """(root, entries by key, reader) of a remote zip file, read once."""
    if url not in _ZIPS:
        read, size = http_reader(url)
        d = zipdir.central_directory(read, size)
        root, entries, _, _ = zipdir.product_entries(d)
        zipdir.locate(read, d, entries)
        _ZIPS[url] = (root, entries, read)
    return _ZIPS[url]


def fetch(url: str, key: str) -> bytes:
    """An object of a remote product: by its URL (directory form), or by its entry's range."""
    if url.endswith("/"):
        with urllib.request.urlopen(object_url(url, key), timeout=300) as r:  # noqa: S310 (the product's URL)
            return r.read()
    _, entries, read = zip_entries(url)
    e = entries[key]
    data = read(e.ds, e.cs)
    return zipdir.inflate(data, e) if e.method == 8 else data


def main(argv: list[str]) -> int:
    impls = ["py"] if "--py-only" in argv else ["py", "web"]
    argv = [a for a in argv if a != "--py-only"]
    if argv[:1] == ["--remote"]:
        return remote(argv[1], argv[2:], impls)
    return fixtures(argv, impls)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
