"""A digest of what `refkv.zarrify.zarrify` makes of every input of the fixture corpus, checked
against a committed manifest, so that a change to an output shows in the change that makes it.

The Python and the browser implementations run one core, and most profiles have no independent
reference implementation (conformance/zarrify/HARNESS.md), so neither comparison sees a change to
how the core builds an output. This one does: `manifest.jsonl` holds, for each input, with and
without checksums, digests of the output's parts, or the error that refuses the input. A change
that alters an output alters the manifest, and its diff names the inputs and the parts.

The corpus is rust/refkv-cli/tests/identical.py's: every file under fixtures/ with the suffix of
a profile of the registry, and every store directory under a store profile's fixtures/<p>. A file
is zarrified from its path relative to the repository's root, which is then its source's URL; a
store with `url=https://data.example/<path>/`. So a manifest does not depend on where the
repository is.

A line of the manifest is one input's record, a JSON object:

- `input`: its path under fixtures/, a store's ending in `/`;
- `plain` and `checksums`: the result of each mode, either `{"error": <exception's name>,
  "message": ...}` or the digests of an output:
  - `profile`: the output's `format`;
  - `entries`: how many entries it has;
  - `layout`: its entries outside `refkv_source/`, each key with its value (bytes, or a
    reference's parts with their checksums);
  - `mirror`: its entries under `refkv_source/`;
  - `sources`: its source table;
  - `placement`: which entries a reader is given at open, and which are compressed;
  - `summary`: its summary, without the read planner's counts, which depend on timing.

  `checksums` holds only the members that differ from `plain`'s, where both are outputs, and is
  the string `"plain"` where the two results are equal (a refused input's).

    uv run python conformance/outputs/digest.py            # check: exit 1 if any record differs
    uv run python conformance/outputs/digest.py --update   # rewrite the manifest
    uv run python conformance/outputs/digest.py --keys fixtures/npy/x.npy
                                                           # one input's entries, a line each, to
                                                           # diff between two checkouts

`just outputs-check` and `just outputs-update` run the first two, and CI's `outputs` job the check.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = Path(__file__).resolve().parent / "manifest.jsonl"
sys.path.insert(0, str(ROOT / "rust" / "refkv-cli" / "tests"))

from identical import corpus  # noqa: E402

from refkv import ReferenceStore  # noqa: E402
from refkv.pb import Reference, parts  # noqa: E402
from refkv.zarrify import zarrify  # noqa: E402

MIRROR = "refkv_source/"
MODES = ("plain", "checksums")
HEX = 12  # hex digits kept of each SHA-256


def digest(lines: list[str]) -> str:
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()[:HEX]


def of_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:HEX]


def entry(key: str, value: bytes | Reference) -> str:
    """One entry as a line: its key, then `bytes` with their length and digest, or each part of
    its reference."""
    if isinstance(value, bytes):
        return f"{key}\tbytes {len(value)} {of_bytes(value)}"
    shown = []
    for p in parts(value):
        data = "-" if p.data is None else of_bytes(p.data)
        crc = "-" if p.crc32c is None else str(p.crc32c)
        shown.append(f"{p.source}:{p.offset}+{p.length} data={data} crc={crc}")
    return f"{key}\tref {' | '.join(shown)}"


def entries(store: ReferenceStore) -> list[str]:
    return [entry(k, store.mapping[k]) for k in sorted(store.mapping)]


def sources(store: ReferenceStore) -> list[str]:
    return [f"{i}\turl={s.url} key={s.key} data={'-' if s.data is None else of_bytes(s.data)} "
            f"size={s.size} etag={s.etag} modified_not_after={s.modified_not_after}"
            for i, s in enumerate(store.sources)]


def location(name: str) -> tuple[str, str | None]:
    """The location and `url=` an input of the corpus is zarrified with."""
    if name.endswith("/"):
        return f"fixtures/{name}", f"https://data.example/{name}"
    return f"fixtures/{name}", None


def output(name: str, checksums: bool) -> ReferenceStore:
    where, url = location(name)
    return zarrify(where, url=url, checksums=checksums)


def result(name: str, checksums: bool) -> dict[str, Any]:
    try:
        store = output(name, checksums)
    except Exception as e:  # noqa: BLE001 (whatever refuses the input is its result)
        return {"error": type(e).__name__, "message": str(e)}
    lines = entries(store)
    summary = {k: v for k, v in store.info.items() if k != "planner"}
    return {
        "profile": store.info.get("format"),
        "entries": len(lines),
        "layout": digest([x for x in lines if not x.startswith(MIRROR)]),
        "mirror": digest([x for x in lines if x.startswith(MIRROR)]),
        "sources": digest(sources(store)),
        "placement": digest([f"open {k}" for k in sorted(store.read_at_open)]
                            + [f"compressed {k}" for k in sorted(store.compressed)]),
        "summary": digest([json.dumps(summary, sort_keys=True, ensure_ascii=False)]),
    }


def record(name: str) -> dict[str, Any]:
    plain, checked = result(name, False), result(name, True)
    if checked == plain:
        return {"input": name, "plain": plain, "checksums": "plain"}
    if "error" not in plain and "error" not in checked:
        checked = {k: v for k, v in checked.items() if plain.get(k) != v}
    return {"input": name, "plain": plain, "checksums": checked}


def expanded(rec: dict[str, Any], mode: str) -> dict[str, Any]:
    """A record's result for `mode`, whole."""
    plain, value = rec["plain"], rec[mode]
    if value == "plain":
        return plain
    if "error" in plain or "error" in value:
        return value
    return {**plain, **value}


def records(names: list[str], jobs: int) -> list[dict[str, Any]]:
    with ThreadPoolExecutor(jobs) as pool:
        return list(pool.map(record, names))


def inputs() -> list[str]:
    files, stores = corpus()
    return sorted(files + [f"{s}/" for s in stores])


def differences(was: dict[str, Any] | None, now: dict[str, Any] | None) -> list[str]:
    """What differs between an input's committed record and its record now, mode by mode."""
    if was is None:
        return ["not in the manifest"]
    if now is None:
        return ["in the manifest, but no longer an input of the corpus"]
    out = []
    for mode in MODES:
        a, b = expanded(was, mode), expanded(now, mode)
        if a == b:
            continue
        if "error" in a or "error" in b:
            out.append(f"{mode}: was {json.dumps(a, ensure_ascii=False)}, is {json.dumps(b, ensure_ascii=False)}")
        else:
            out.append(f"{mode}: {', '.join(k for k in sorted(a.keys() | b.keys()) if a.get(k) != b.get(k))}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--update", action="store_true", help="rewrite the manifest")
    ap.add_argument("--keys", metavar="INPUT", help="print one input's entries and sources, in both modes")
    ap.add_argument("--jobs", type=int, default=8)
    args = ap.parse_args()
    os.chdir(ROOT)  # a file's source URL is its path as given: relative to the repository's root

    if args.keys:
        name = args.keys.removeprefix("fixtures/")
        for mode in MODES:
            store = output(name, mode == "checksums")
            print("\n".join(f"{mode}\t{x}" for x in sources(store) + entries(store)))
        return 0

    now = records(inputs(), args.jobs)
    text = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in now)
    if args.update:
        MANIFEST.write_text(text, encoding="utf-8")
        print(f"wrote {len(now)} records to {MANIFEST.relative_to(ROOT)}")
        return 0

    committed = MANIFEST.read_text(encoding="utf-8")
    if committed == text:
        print(f"{len(now)} inputs: every output is the manifest's")
        return 0
    was = {r["input"]: r for r in map(json.loads, committed.splitlines())}
    by_name = {r["input"]: r for r in now}
    changed = 0
    for name in sorted(was.keys() | by_name.keys()):
        found = differences(was.get(name), by_name.get(name))
        if found:
            changed += 1
            print(f"{name}\n  " + "\n  ".join(found))
    print(f"\n{changed} of {len(now)} inputs differ from {MANIFEST.relative_to(ROOT)}. If the change is "
          "meant, run `just outputs-update` and commit the manifest; `--keys INPUT` on two checkouts "
          "shows which entries changed.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
