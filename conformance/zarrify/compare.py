"""Compares zarrifiers (spec/zarrify.md §1.1
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#11-output-and-equivalence>, Appendix A
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#appendix-a-conformance>; HARNESS.md).

Every implementation runs on every input of the corpus, which the caching
proxy (proxy.py) serves over local HTTP: the synthetic files in
fixtures/ (one directory per format), the synthetic stores in
fixtures/n5/, zarr2/ and ome-zarr/ (one directory per store,
served with the S3 listing of spec/zarrify.md §1.5
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>), the synthetic SAFE
products in fixtures/safe/ (directories, and .SAFE.zip files), the
synthetic CZI files in fixtures/czi/, the 205 OME-TIFFs of IDR
idr0096, and the public files and stores in corpus_*.txt. For each input, all
implementations must either reject it (exit status 3) or produce equivalent
outputs; outputs are compared with the reference implementation's (the first
one).

Corpus lines are `url|name`, or `url|name|py-only` for an input larger than
the browser implementation's limit (spec/zarrify.md Appendix A
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#appendix-a-conformance>): such inputs are
skipped unless `--large` is given, and then every implementation but `web` runs on them.
Store URLs end in `/`. A line may end with the input's fingerprint, which
corpus_hash.py records and checks.

Implementations are `name=command`, where the command is run as
`command <url> <out>`. The built-in ones write vzip archives: `ref` (the
reference, the frozen Python profiles of conformance/zarrify/reference/ for
TIFF, ND2, CZI, DICOM, NIfTI, IMS, NDPI, SAFE, Zarr v2, N5 and OME-Zarr and the shipped ones
for the others; a profile added since, whose registry entry's `reference` is null, has no
reference: its inputs compare `py` with `web` alone), `py` (`python -m refkv.zarrify`) and `web` (the browser code
under Node).
Others write HARNESS.md's JSON description; give them as `--impl name=command`.

`py` and `web` zarrify every profile through one Rust core (rust/refkv-ir, natively and as
wasm32), and write its mirror under `refkv_source`, which the frozen reference does not: an
implementation is compared with the reference outside
`refkv_source`, and `py` and `web` with each other entirely, entry for entry (they must give the
same archive contents, `refkv_source` included).
The frozen reference records the revision it implements (20 for TIFF, ND2 and CZI, 25
for DICOM, NIfTI, IMS, NDPI, SAFE, Zarr v2, N5 and OME-Zarr) in its root, where the shipped
code records the current one, and writes the names those revisions used (`vzip_source`,
`vzip_virtualized`, the convention's URLs under d-v-b/vzip), which revision 27 renamed:
compared with it, the reference's output is read with the current names, at the current
revision, and without the members of its source metadata that the mirror holds instead
(NIfTI's extensions and raw bytes; `at_revision`), and the shipped code's without the members
of its root image added since (TIFF's and NDPI's `omero`; `without_added`) and with NIfTI's
zero padding, a range of its zero data source since the freeze, as the literal ranges the
reference writes (`literal_padding`). Each mirror is also checked on its own
(`mirror_problem`): it reads, it rebuilds a source of source 0's size (a store's: its listing,
the sizes its sources pin, and every held object byte for byte, read from the store), it is
canonical, and its view is the one its table and the source give.

Usage: uv run python conformance/zarrify/compare.py <out dir>
           [--impl name=command ...] [--no-builtin web] [--quick] [--only <substring>]
           [--fixtures <dir>]  (only the fixture files and stores under <dir>, e.g. from mutate.py)
           [--large]  (also the py-only corpus inputs, without web)
           [--shard k/n]  (only every n-th input, from the k-th: one of n parallel runs)
           [--idr-listing <file>]  (idr0096's listing, saved, instead of fetching it)

Every archive is also read by spec/bindings/vzip.md's strict reading (`strict`): an archive the
independent reader refuses, or reads differently, or that breaks a writer requirement,
counts as a crash of the implementation that wrote it.
"""

from __future__ import annotations

import base64
import bisect
import hashlib
import json
import re
import shlex
import struct
import subprocess
import sys
import urllib.request
import zipfile
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "reference"))
sys.path.insert(0, str(ROOT / "conformance" / "vzip"))
sys.path.insert(0, str(ROOT / "conformance" / "impls" / "python"))
from corpus_hash import entries as corpus_entries  # noqa: E402
from proxy import Proxy  # noqa: E402
from refkv_reference import legacy  # noqa: E402
from validate import validate  # noqa: E402
from vzip_impl import reader as strict_reader  # noqa: E402
from vzip_impl.errors import VzError  # noqa: E402

from refkv import _core  # noqa: E402
from refkv._narrow import mapping, sequence, string  # noqa: E402
from refkv.ir.cmirror import Archive, canonical_problem, load, view_problem  # noqa: E402
from refkv.pb import Concat, Range, Source, decode_source_table  # noqa: E402
from refkv.zarrify.common import CONVENTION_KEY, SOURCE_NODE, same  # noqa: E402
from refkv.zarrify.profiles import REGISTRY  # noqa: E402
from refkv.zarrify.store import object_url  # noqa: E402

IDR = "https://ftp.ebi.ac.uk/pub/databases/IDR/idr0096-tratwal-marrowquant/20210609-ftp-ome-tiffs/"
BUILTIN = {
    # the inputs are served from 127.0.0.1, which the reader policy refuses by default
    # (spec/refkv.md §6.7
    # <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>)
    "ref": ("vzip", ["uv", "run", "python", str(HERE / "reference" / "cli.py"), "--allow-private-hosts"]),
    "py": ("vzip", ["uv", "run", "python", "-m", "refkv.zarrify", "--allow-private-hosts"]),
    "web": ("vzip", ["node", str(ROOT / "js" / "conformance" / "zarrify.ts"), "--allow-private-hosts"]),
}
# The profiles whose shipped implementations write the IR's mirror under refkv_source: every
# profile of the registry (spec/zarrify/profiles/; a dataset layout is no profile).
IR_PROFILES = tuple(e["name"] for e in REGISTRY if e["kind"] != "layout")
SOURCE = SOURCE_NODE + "/"
# The names the frozen reference writes, of the revisions it implements, and their translation.
LEGACY_SOURCE = legacy.SOURCE_NODE + "/"
LEGACY_KEY = legacy.CONVENTION_KEY
current_names = legacy.current_names
# The revision each frozen profile's reference roots record (HARNESS.md): the last before
# the profile moved to the IR (the registry's `reference`; a profile the frozen reference never
# had, such as MRC, has none).
REFERENCE_REVISIONS = {e["name"]: e["reference"] for e in REGISTRY if e["reference"] is not None}
# The members of a frozen reference root's source metadata that the IR's mirror holds
# instead (they named arrays of the reference's vzip_source, or held raw bytes).
MIRRORED = {"nifti": ("header_rest", "extender", "extensions", "extensions_truncated", "unparsed", "trailing")}
# The members of the root image's `ome` the shipped code writes and the frozen reference does
# not, by profile: TIFF's and NDPI's `omero`, added after the freeze (spec/zarrify/tiff.md §4.4
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#44-arrays>, spec/zarrify/ndpi.md §4
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#4-the-image>).
ADDED = {"tiff": ("omero",), "ndpi": ("omero",)}
# The fixtures (by file name) whose layout the profile changed after the freeze, which the frozen
# reference does not give: they compare py with web alone. NDPI's strips without McuStarts whose frame header is
# not their level's size (spec/zarrify/ndpi.md §7
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#7-ndpi-profile>), and its JPEG XR levels
# (spec/zarrify/ndpi.md §3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#3-levels>).
REVISED = {"edge_ndpi_frames.ndpi", "edge_reject_ndpi_frame.ndpi", "ndpi_jpegxr_gray16.ndpi",
           "ndpi_jpegxr_rgb.ndpi", "ndpi_jpegxr_bgr.ndpi"}
# The profiles whose edge chunks' zero padding is a range of the output's zero data source since
# the freeze, where the frozen reference pads with a literal range (spec/zarrify/nifti.md §7.2
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nifti.md#72-chunks>): compared with it,
# the shipped code's padding reads as literal ranges (`literal_padding`).
ZERO_PADDED = ("nifti",)


class UnreadableError(Exception):
    """An archive that spec/bindings/vzip.md's strict reading refuses, or that breaks a writer requirement."""


def _no_duplicates(pairs: list) -> dict:
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        dups = sorted(k for k in keys if keys.count(k) > 1)[:3]
        raise UnreadableError(f"a JSON document has a duplicate member: {dups}")
    return dict(pairs)


def from_vzip(path: Path) -> dict:
    """An archive's output (§1.1
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#11-output-and-equivalence>), once it
    has passed `strict`."""
    try:  # first, so that zipfile never reads what spec/bindings/vzip.md does not
        strict_reader.Archive(str(path)).close()
    except VzError as e:
        raise UnreadableError(f"strict reader: {e.cls} error: {e}") from e
    view = _zip_view(path)
    problems = strict(path, view)
    if problems:
        raise UnreadableError("; ".join(problems[:5]))
    return view


def _zip_view(path: Path) -> dict:
    """An archive's output as zipfile reads it."""
    z = zipfile.ZipFile(path)
    sources = [_source(s) for s in decode_source_table(z.read("__vz__/sources"))]
    entries: dict[str, tuple[str, object]] = {}
    for info in z.infolist():
        if info.filename.startswith("__vz__/"):
            continue
        ex, p, ref = info.extra, 0, None
        while p < len(ex):
            hid, n = struct.unpack_from("<HH", ex, p)
            if hid == 0x7A76:
                ref = [_range(Range.decode(ex[p + 4 : p + 4 + n]))]
            elif hid == 0x7A77:
                ref = [_range(r) for r in Concat.decode(ex[p + 4 : p + 4 + n]).parts]
            p += 4 + n
        if ref is not None:
            entries[info.filename] = ("ranges", ref)
        elif info.filename.endswith("zarr.json"):
            entries[info.filename] = ("json", json.loads(z.read(info), object_pairs_hook=_no_duplicates))
        else:
            entries[info.filename] = ("bytes", hashlib.sha256(z.read(info)).hexdigest())
    return {"sources": sources, "entries": entries}


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def strict(path: Path, view: dict) -> list[str]:
    """The problems spec/bindings/vzip.md finds in an archive, by code that shares nothing with the
    writers under test: the independent reader (conformance/impls/python, written from spec/bindings/vzip.md
    alone) must open it and see exactly the output `view` holds, and the archive must
    meet the writer requirements (spec/bindings/vzip.md §10
    <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#10-zip-container>, §11
    <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#11-entries>, §12
    <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#12-page-index-optional>, §14
    <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#14-writing-a-vzip-archive>) as
    conformance/vzip/validate.py,
    with its own ZIP parser, checks them: canonical payloads, reference bodies,
    duplicate names, hidden entries, CRC-32s, local headers and the page index."""
    try:
        archive = strict_reader.Archive(str(path))
    except VzError as e:
        return [f"strict reader: {e.cls} error: {e}"]
    problems: list[str] = []
    with archive:
        sources = [s.value if s.kind == "url" else {s.kind: _b64(s.value) if s.kind == "data" else s.value}
                   for s in archive.sources]
        if sources != view["sources"]:
            problems.append(f"strict reader: sources {sources[:3]} != {view['sources'][:3]}")
        keys = archive.list("")
        if set(keys) != set(view["entries"]):
            problems.append(f"strict reader: keys only it lists {sorted(set(keys) - set(view['entries']))[:5]}, "
                            f"keys only zipfile lists {sorted(set(view['entries']) - set(keys))[:5]}")
        desc_sources = [{s.kind: s.value.hex() if s.kind == "data" else s.value,
                         **{k: getattr(s, k) for k in ("size", "etag") if getattr(s, k) is not None},
                         **({"modified_not_after": s.mnf} if s.mnf is not None else {})} for s in archive.sources]
        entries, mirrored = [], set()
        for key in keys:
            try:
                e = archive._lookup(key.encode())
                assert e is not None  # a listed key
                if e.kind == "reference":
                    parts, _ = strict_reader.decode_payload(e.ref_id, e.payload, len(archive.sources))
                    ranges = [["data", _b64(p.data)] if p.data is not None else [p.source, p.offset, p.length]
                              for p in parts]
                    if view["entries"].get(key) != ("ranges", ranges):
                        problems.append(f"strict reader: {key}: ranges {ranges[:3]} differ from zipfile's")
                    mirrored.add(archive.raw(key) != b"")
                    entries.append({"key": key, "ranges": [
                        {"data": p.data.hex()} if p.data is not None else
                        {"source": p.source, "offset": p.offset, "length": p.length} for p in parts]})
                else:
                    value = archive.get(key)
                    assert value is not None  # a listed key's bytes
                    kind, seen = view["entries"].get(key, (None, None))
                    if (kind == "json" and json.loads(value) != seen) or (kind == "bytes" and (
                            hashlib.sha256(value).hexdigest() != seen)) or kind not in ("json", "bytes"):
                        problems.append(f"strict reader: {key}: bytes differ from zipfile's")
                    entries.append({"key": key, "bytes": value.hex(), "compress": e.method == 8,
                                    "pinned": archive.paged and key.encode() in archive.pinned})
            except VzError as e:
                problems.append(f"strict reader: {key}: {e.cls} error: {e}")
        if len(mirrored) > 1:
            problems.append("some reference bodies are empty and others are not")
        desc = {"sources": desc_sources, "entries": entries, "mirror": mirrored != {False},
                "page_size": 1 if archive.paged else None}
    problems += [f"writer requirement: {p}" for p in validate(path, desc)]
    return problems


def _source(s: Source) -> str | dict:
    """A source as compared: its URL, or {"data": base64} for a data source."""
    if s.url is not None:
        return s.url
    if s.data is not None:
        return {"data": base64.b64encode(s.data).decode()}
    return {"key": s.key}


def _range(r: Range) -> list:
    """A range as compared: [source, offset, length], or ["data", base64] for a literal."""
    if r.data is not None:
        return ["data", base64.b64encode(r.data).decode()]
    return [r.source, r.offset, r.length]


def from_json(path: Path) -> dict:
    """A HARNESS.md JSON description's output."""
    d = json.loads(path.read_text())
    entries: dict[str, tuple[str, object]] = {}
    for key, v in d["entries"].items():
        if "ranges" in v:
            entries[key] = ("ranges", [["data", r["data"]] if isinstance(r, dict) else list(r) for r in v["ranges"]])
        elif "json" in v:
            entries[key] = ("json", v["json"])
        else:
            entries[key] = ("bytes", hashlib.sha256(base64.b64decode(v["base64"])).hexdigest())
    return {"sources": d["sources"], "entries": entries}


def differences(a: Mapping[str, Any], b: Mapping[str, Any]) -> list[str]:
    out = []
    if not same(a["sources"], b["sources"]):
        out.append(f"sources {a['sources']} != {b['sources']}")
    ka, kb = set(a["entries"]), set(b["entries"])
    if ka != kb:
        out.append(f"keys only in reference: {sorted(ka - kb)[:5]}, only in this one: {sorted(kb - ka)[:5]}")
    for k in sorted(ka & kb):
        # §1.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#11-output-and-equivalence>: integers exactly
        if not same(a["entries"][k], b["entries"][k]):
            out.append(f"{k}: reference {json.dumps(a['entries'][k], default=str)[:400]} != "
                       f"{json.dumps(b['entries'][k], default=str)[:400]}")
            if len(out) > 6:
                break
    return out


def run(impl: tuple[str, list[str]], url: str, out: Path) -> tuple[str, object]:
    kind, cmd = impl
    try:
        p = subprocess.run([*cmd, url, str(out)], capture_output=True, text=True, timeout=1800,
                           check=False)
    except subprocess.TimeoutExpired:
        return "crashed", "timeout"
    try:
        if p.returncode == 3:
            return "rejected", (p.stderr.strip().splitlines() or [""])[-1][:300]
        if p.returncode or not out.exists():
            return "crashed", (p.stderr or p.stdout).strip()[-600:]
        view = from_vzip(out) if kind == "vzip" else from_json(out)
        if kind == "vzip":
            problem = mirror_problem(out, view)
            if problem:
                return "crashed", f"invalid IR mirror: {problem}"
    except Exception as e:  # noqa: BLE001
        return "crashed", f"unreadable output: {type(e).__name__}: {e}"
    else:
        return "ok", view
    finally:
        out.unlink(missing_ok=True)


def mirror_problem(path: Path, view: dict, read=None, fetch=None) -> str | None:
    """For an archive whose refkv_source is an IR mirror (conventions §8
    <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#8-the-ir-mirror>): None when its
    table reads, its invariants hold, it describes a source of source 0's size, it is
    canonical (§8.8 <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#88-canonical-form>,
    rebuilt from what it loads, it is the same table) and its view is
    the one the table and the source give (§8.7
    <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#87-the-view>); else what is wrong.
    The array columns' bytes and the view's values are read from source 0 (small ranges;
    `read(offset, length)`, by default its URL). A store IR's mirror is checked by
    `store_mirror_problem`, which reads no source and fetches the held objects with
    `fetch(url, size)` (by default over HTTP); `read` applies to a file's mirror only. These are
    checks in addition to comparing mirrors entry for entry (spec/zarrify.md §1.1
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#11-output-and-equivalence>)."""
    doc = view["entries"].get("refkv_source/zarr.json")
    own = (doc[1].get("attributes", {}).get("refkv_zarrified", {}) if doc and doc[0] == "json" else {})
    if not any(isinstance(v, dict) and "ir" in v for v in own.values()):
        return None
    if any(isinstance(v, dict) and isinstance(v.get("ir"), dict) and "objects" in v["ir"] for v in own.values()):
        return store_mirror_problem(path, view, fetch)

    url = view["sources"][0]

    def read_source(o: int, n: int) -> bytes:
        req = urllib.request.Request(url, headers={"Range": f"bytes={o}-{o + n - 1}"})  # noqa: S310 (an http(s) corpus URL)
        with urllib.request.urlopen(req, timeout=120) as r:  # noqa: S310 (as above)
            return r.read()

    read = read or read_source

    try:
        ir = load(Archive(str(path), read))
        ir.check()
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {e}"
    pinned = _source_size(path)
    if pinned is not None and ir.size != pinned:
        return f"the mirror describes {ir.size} bytes, source 0 pins {pinned}"
    problem = canonical_problem(Archive(str(path), read))
    if problem:
        return f"not canonical: {problem}"
    return view_problem(Archive(str(path), read))


# A SAFE folder marker's key ends in it (spec/zarrify/safe.md §2.1
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safe.md#21-objects>).
SAFE_MARKER = "_$folder$"


def store_mirror_problem(path: Path, view: dict, fetch=None) -> str | None:
    """For an archive whose refkv_source is a store IR's mirror (conventions §8.9
    <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#89-stores>): None when its table
    reads and its invariants hold, it rebuilds the store's listing (every url source names an
    object of it and pins its size, and every object the archive references has a source) and
    every held object byte for byte, it is canonical and its view is the one its table and the
    store give; else what is wrong. The store's objects are read at its URL (the root's
    `source.url`) by `fetch(url, offset, length)`, by default over HTTP: the held objects whole,
    and the values the view shows of an object described in place (a SAFE band file) exactly,
    each its own range, at the object's virtual offsets. No source 0 is read."""
    def never(o: int, n: int) -> bytes:
        raise AssertionError(f"a store's mirror read [{o}, {o + n}) of a source 0")

    def get_url(url: str, o: int, n: int) -> bytes:
        req = urllib.request.Request(url, headers={"Range": f"bytes={o}-{o + n - 1}"})  # noqa: S310 (an http(s) corpus URL)
        with urllib.request.urlopen(req, timeout=120) as r:  # noqa: S310 (as above)
            return r.read()

    fetch = fetch or get_url
    try:
        archive = Archive(str(path), never)
        ir = load(archive)
        ir.check()
        rebuilt = _core.rebuild_store(archive.get)
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {e}"
    root = view["entries"].get("zarr.json")
    prop = root[1].get("attributes", {}).get("refkv_zarrified") if root and root[0] == "json" else None
    url = prop.get("source", {}).get("url") if isinstance(prop, dict) else None
    if not isinstance(prop, dict) or not isinstance(url, str):
        return "no store URL on the root"
    if prop.get("profile") in ("vsi", "nrrd"):
        # a VSI root names the .vsi file, and a NRRD root the header file; the store's URL is its
        # directory (spec/zarrify/vsi.md §9
        # <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#9-vsi-profile>,
        # spec/zarrify/nrrd.md §8 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nrrd.md#8-nrrd-profile>)
        url = url[:url.rfind("/") + 1]
    pins = {s.url: s.size for s in decode_source_table(zipfile.ZipFile(path).read("__vz__/sources"))
            if s.url is not None}
    listed = {object_url(url, key): (key, size) for key, size, _ in rebuilt}
    for u in pins:
        if u not in listed:
            return f"the url source {u} names no object of the listing"
    for key, size, held in rebuilt:
        u = object_url(url, key)
        if u in pins and pins[u] is not None and pins[u] != size:
            return f"{key}: the mirror lists {size} bytes, its source pins {pins[u]}"
        # every object the archive references has a source: every object neither held nor
        # empty, but a SAFE product's folder markers, which it drops (spec/zarrify/safe.md
        # §2.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safe.md#21-objects>)
        marker = prop.get("profile") == "safe" and key.endswith(SAFE_MARKER) and len(key) > len(SAFE_MARKER)
        if held is None and size > 0 and u not in pins and not marker:
            return f"{key}: a referenced object of {size} bytes that no source names"
        if held is not None:
            try:
                same_bytes = fetch(u, 0, size) == held
            except Exception as e:  # noqa: BLE001
                return f"{key}: {type(e).__name__}: {e}"
            if not same_bytes:
                return f"{key}: the mirror's bytes are not the object's"
    problem = canonical_problem(archive)
    if problem:
        return f"not canonical: {problem}"
    # the view's values of an object described in place, by virtual offset: object `o`'s byte
    # `x` at the sum of the sizes of the objects before it, in id order, plus `x`
    bases = ir.object_bases()
    starts = [b for _, b, _ in bases]

    def vread(v: int, n: int) -> bytes:
        k = bisect.bisect_right(starts, v) - 1
        while k > 0 and bases[k][2] == 0:
            k -= 1
        if k < 0:
            raise ValueError(f"the virtual offset {v} is before every object")
        key, base, size = bases[k]
        if v - base + n > size:
            raise ValueError(f"[{v - base}, {v - base + n}) runs past the object {key!r} of {size} bytes")
        return fetch(object_url(url, key), v - base, n)

    return view_problem(Archive(str(path), vread))


def _source_size(path: Path) -> int | None:
    z = zipfile.ZipFile(path)
    sources = decode_source_table(z.read("__vz__/sources"))
    return sources[0].size if sources else None


def check(item: tuple, impls: dict, out_dir: Path) -> dict:
    name, url = item[:2]
    if len(item) > 2 and item[2] == "py-only":  # past the browser's limit: every implementation but web
        impls = {k: v for k, v in impls.items() if k != "web"}
    # a profile the frozen reference never had (its `reference` is null), or REVISED: py and web alone
    if len(item) > 2 and item[2] == "no-ref":
        impls = {k: v for k, v in impls.items() if k != "ref"}
    results = {n: run(impl, url, out_dir / f"{name}.{n}.out") for n, impl in impls.items()}
    ref_name = next(iter(impls))
    ref = results[ref_name]
    mirror = ref[0] == "ok" and profile(mapping(ref[1])) in IR_PROFILES
    verdicts = {}
    for n, (status, value) in results.items():
        if status == "crashed":
            verdicts[n] = ("CRASHED", [str(value)])
        elif status != ref[0]:
            why = value if status == "rejected" else ref[1] if ref[0] == "rejected" else ""
            verdicts[n] = ("DIVERGES", [f"{ref_name} {ref[0]}, this one {status}: {why}"])
        elif status == "rejected":
            verdicts[n] = ("rejects", [])
        else:
            # against the frozen reference, outside the mirror and without what it does not write;
            # without it (no-ref), py and web entry for entry
            a, b = mapping(ref[1]), mapping(value)  # both ok: views
            diffs = differences(without_source(at_revision(a)), without_source(literal_padding(without_added(b)))) \
                if mirror and ref_name == "ref" and n != ref_name else differences(a, b)
            verdicts[n] = ("DIVERGES", diffs) if diffs else ("equivalent", [])
    # one core, two hosts: py and web give the same archive contents, the mirror included
    if mirror and all(results.get(n, ("",))[0] == "ok" for n in ("py", "web")) and \
            verdicts["web"][0] == "equivalent":
        diffs = differences(mapping(results["py"][1]), mapping(results["web"][1]))
        if diffs:
            verdicts["web"] = ("DIVERGES", [f"from py: {d}" for d in diffs])
    return {"name": name, "url": url, "verdicts": verdicts}


def profile(output: Mapping[str, Any]) -> str | None:
    """The profile an output's root declares."""
    root = output["entries"].get("zarr.json")
    if not root or root[0] != "json":
        return None
    attrs = root[1].get("attributes", {})
    return (attrs.get(CONVENTION_KEY) or attrs.get(LEGACY_KEY) or {}).get("profile")


def at_revision(output: Mapping[str, Any]) -> Mapping[str, Any]:
    """The frozen reference's output as the shipped code names it, for comparing the
    two: when its root has the frozen reference's names (LEGACY_KEY), its source metadata
    node's keys under `refkv_source/`, and every document outside it with the current
    names (`current_names`); and when its root records the revision the reference
    implements (REFERENCE_REVISIONS), without that revision (the shipped code records none)
    and without the members of its source metadata that the IR's mirror holds instead
    (MIRRORED). Any other output is unchanged."""
    root = output["entries"].get("zarr.json")
    attrs = root[1].get("attributes", {}) if root and root[0] == "json" else {}
    if LEGACY_KEY in attrs:
        output = {**output, "entries": {
            (SOURCE + k[len(LEGACY_SOURCE):] if k.startswith(LEGACY_SOURCE) else k):
                (kind, current_names(v)) if kind == "json" and not k.startswith(LEGACY_SOURCE) else (kind, v)
            for k, (kind, v) in output["entries"].items()}}
        root = output["entries"]["zarr.json"]
    prop = root[1].get("attributes", {}).get(CONVENTION_KEY) if root and root[0] == "json" else None
    p = prop.get("profile") if isinstance(prop, dict) else None
    if not isinstance(prop, dict) or p not in REFERENCE_REVISIONS or prop.get("revision") != REFERENCE_REVISIONS[p]:
        return output
    prop = {k: v for k, v in prop.items() if k != "revision"}
    if isinstance(prop.get(p), dict) and p in MIRRORED:
        prop[p] = {k: v for k, v in prop[p].items() if k not in MIRRORED[p]}
    attrs = {**root[1]["attributes"], CONVENTION_KEY: prop}
    return {**output, "entries": {**output["entries"], "zarr.json": (root[0], {**root[1], "attributes": attrs})}}


def without_added(output: Mapping[str, Any]) -> Mapping[str, Any]:
    """An output of the shipped code without the members of its root image that the frozen
    reference does not write (ADDED), for comparing the two."""
    root = output["entries"].get("zarr.json")
    drop = ADDED.get(profile(output) or "", ())
    if not drop or root is None or root[0] != "json" or not isinstance(root[1].get("attributes", {}).get("ome"), dict):
        return output
    attrs = root[1]["attributes"]
    ome = {k: v for k, v in attrs["ome"].items() if k not in drop}
    root = (root[0], {**root[1], "attributes": {**attrs, "ome": ome}})
    return {**output, "entries": {**output["entries"], "zarr.json": root}}


def literal_padding(output: Mapping[str, Any]) -> Mapping[str, Any]:
    """An output of the shipped code with each range of a data source of zero bytes read as the
    literal range of its bytes, when its profile is ZERO_PADDED, for comparing it with the frozen
    reference, which pads with literal ranges: the padding then compares by its bytes, and the
    zero data source, which no entry uses any more, drops out of `without_source`. Any other
    output is unchanged."""
    if profile(output) not in ZERO_PADDED:
        return output
    zeros = {i for i, s in enumerate(output["sources"])
             if isinstance(s, dict) and "data" in s and not any(base64.b64decode(s["data"]))}

    def literal(r: list) -> list:
        return ["data", _b64(bytes(r[2]))] if r[0] in zeros else r

    return {**output, "entries": {k: (kind, [literal(r) for r in v]) if kind == "ranges" else (kind, v)
                                  for k, (kind, v) in output["entries"].items()}}


def without_source(output: Mapping[str, Any]) -> dict:
    """An output without the entries under refkv_source, its sources those the other entries
    use, in order of first use (the entries in order of their keys' UTF-8 bytes, each one's
    ranges in order), renumbered: so that sources only refkv_source used (a store's objects
    the mirror holds, a profile's own source metadata arrays) and the order a profile gives
    its url sources do not count, and the hierarchy's references still compare by source."""
    sources = output["sources"]
    used: list = []
    index: dict[str, int] = {}
    entries = {}
    for k in sorted((k for k in output["entries"] if not k.startswith(SOURCE)), key=lambda k: k.encode()):
        kind, value = output["entries"][k]
        if kind == "ranges":
            ranges = []
            for r in value:
                if r[0] == "data":
                    ranges.append(r)
                    continue
                ident = json.dumps(sources[r[0]], sort_keys=True)
                if ident not in index:
                    index[ident] = len(used)
                    used.append(sources[r[0]])
                ranges.append([index[ident], r[1], r[2]])
            value = ranges
        entries[k] = (kind, value)
    return {"sources": used, "entries": entries}


STORE_FORMATS = tuple(string(e["name"]) for e in REGISTRY if e["kind"] in ("store", "both"))
# The fixture files, by suffix: their profile's registry entry.
FILE_SUFFIXES = {string(s): e for e in REGISTRY for s in sequence(e["suffixes"])}


def store_fixtures(fixtures: Path) -> list[Path]:
    """The synthetic stores: each directory directly under a store profile's <fixtures>/<p>."""
    return sorted(d for f in STORE_FORMATS if (fixtures / f).is_dir() for d in (fixtures / f).iterdir() if d.is_dir())


def corpus(proxy: Proxy, fixtures: Path, quick: bool, local_only: bool, large: bool = False,
           idr_listing: str | None = None) -> list[tuple]:
    stores = store_fixtures(fixtures)
    files = [p for p in sorted(p for s in FILE_SUFFIXES for p in fixtures.rglob(f"*{s}"))
             if not any(p.is_relative_to(s) for s in stores)]
    # a profile the frozen reference never had (its `reference` is null), and a fixture whose layout
    # changed after the freeze (REVISED): its files compare py with web alone
    items = [(f"fixture-{p.stem}", proxy.local(p.relative_to(fixtures).as_posix()),
              *(["no-ref"] if FILE_SUFFIXES[p.suffix]["reference"] is None
                or p.name in REVISED else []))
             for p in files]
    items += [(f"fixture-{s.name}", proxy.local_store(s.relative_to(fixtures).as_posix())) for s in stores]
    if local_only:
        return items
    listing = (Path(idr_listing).read_text() if idr_listing else
               urllib.request.urlopen(IDR, timeout=60).read().decode())
    tiffs = sorted(set(re.findall(r'href="([^"?/][^"]*\.ome\.tiff)"', listing)))
    items += [(f"idr-{i:03d}", proxy.remote(IDR + n)) for i, n in enumerate(tiffs[:3] if quick else tiffs)]
    # each profile's public inputs, corpus_<p>.txt (NDPI's are in TIFF's), named <p>-<name>
    for e in REGISTRY:
        corpus_file = HERE / f"corpus_{string(e['name']).replace('-', '_')}.txt"
        if not corpus_file.is_file():
            continue
        prefix = f"{e['name']}-"
        listed = [(url, name, *flags) for url, name, flags, _ in corpus_entries(corpus_file)]
        listed = [x for x in listed if large or x[2:] != ("py-only",)]
        for url, name, *flags in (listed[:3] if quick else listed):
            target = proxy.remote_store(url) if url.endswith("/") else proxy.remote(url)
            items.append((f"{prefix}{name}", target, *flags))
    return items


def main(argv: list[str]) -> int:
    out = Path(argv[0])
    out.mkdir(parents=True, exist_ok=True)
    opts = argv[1:]
    impls = dict(BUILTIN)
    for i, a in enumerate(opts):
        if a == "--impl":
            name, _, cmd = opts[i + 1].partition("=")
            impls[name] = ("json", shlex.split(cmd))
        if a == "--no-builtin":
            impls.pop(opts[i + 1], None)
    only = opts[opts.index("--only") + 1] if "--only" in opts else None
    fixtures = Path(opts[opts.index("--fixtures") + 1]) if "--fixtures" in opts else ROOT / "fixtures"
    proxy = Proxy(fixtures, Path("/tmp/vzip-proxy-cache"))  # noqa: S108 (the cache the runs share, by design)
    listing = opts[opts.index("--idr-listing") + 1] if "--idr-listing" in opts else None
    items = corpus(proxy, fixtures, "--quick" in opts, "--fixtures" in opts, "--large" in opts, listing)
    if only:
        items = [c for c in items if only in c[0]]
    if "--shard" in opts:  # k/n: every n-th input from the k-th (1-based), for parallel CI jobs
        k, n = map(int, opts[opts.index("--shard") + 1].split("/"))
        if not 1 <= k <= n:
            raise SystemExit(f"--shard {k}/{n}: k must be from 1 to n")
        items = items[k - 1 :: n]
    print(f"{len(items)} inputs, implementations: {', '.join(impls)} (reference: {next(iter(impls))})", flush=True)
    results = []
    with ThreadPoolExecutor(3) as pool:
        for r in pool.map(lambda item: check(item, impls, out), items):
            results.append(r)
            line = "  ".join(f"{n}:{v[0]}" for n, v in r["verdicts"].items())
            print(f"{r['name'][:44]:44s} {line}", flush=True)
            for n, (_verdict, details) in r["verdicts"].items():
                for d in details[:4]:
                    print(f"      {n}: {d}", flush=True)
    (out / "results.json").write_text(json.dumps(results, indent=1, default=str))
    print()
    for n in impls:
        counts: dict[str, int] = {}
        for r in results:
            if n in r["verdicts"]:  # an input this implementation did not run (py-only, no-ref)
                counts[r["verdicts"][n][0]] = counts.get(r["verdicts"][n][0], 0) + 1
        print(f"{n:10s} " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())))
    bad = [r for r in results if any(v[0] not in ("equivalent", "rejects") for v in r["verdicts"].values())]
    print(f"\n{len(results)} inputs, {len(bad)} with a divergence or crash")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
