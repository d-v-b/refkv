"""A file's IR with its stored arrays, for the tests that compare the descriptions with a reference
reader (spec/stored-array.md <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md>)."""

import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import jsonschema
import pytest
from ir_helpers import ROOT

from refkv.ir import Ir
from refkv.ir.kind import Kind
from refkv.ir.store import StoreIrOutput
from refkv.ir.stored import ElementPlace, Place, Stored
from refkv.zarrify import _zarrify
from refkv.zarrify.common import Output

pa = pytest.importorskip("pyarrow")

SCHEMA = json.loads((ROOT / "spec" / "stored-array.json").read_text())


@dataclass(frozen=True)
class Described:
    """A file's IR, its stored arrays, its chunk map's rows by array, its objects' bytes by key
    (a file IR's one object under the key None), the output its profile wrote of it, and each
    object's key by its element, as the IR gives its objects' keys (none in a file's IR)."""

    ir: Ir
    stored: Stored
    rows: dict[int, list[dict]]
    objects: dict[str | None, bytes]
    out: Output | StoreIrOutput
    keys: dict[int, str]

    def key(self, element: int) -> str | None:
        """The key of the object `element` is in, as the IR gives it (`object_keys`)."""
        while element and self.ir.kind(element) != Kind.OBJECT:
            element = self.ir.parent(element)
        return self.keys[element] if element else None

    def at(self, element: int, within: tuple[int, int] | None = None) -> tuple[str | None, int, int]:
        """(object key, offset, length) of an element's extent, or of a range of it."""
        start, length = self.ir.extent(element)
        if within is not None:
            start, length = start + within[0], within[1]
        return self.key(element), start, length

    def place(self, p: Place) -> bytes:
        """The bytes at a place."""
        if isinstance(p, ElementPlace):
            key, start, length = self.at(p.element, p.range)
        else:
            key, start, length = p.object, p.offset, p.length
        return self.objects[key][start:start + length]

    def held(self, at: tuple[str | None, int, int]) -> bytes:
        """The bytes of (object key, offset, length)."""
        key, start, length = at
        return self.objects[key][start:start + length]

    def parts(self, row: dict) -> list[tuple[str | None, int, int]]:
        """A row's unit: the ranges of objects that, joined, are its stored bytes."""
        if row["element"] is None:
            return [(r["object"], r["offset"], r["length"]) for r in row["ranges"]]
        r = row["range"]
        return [self.at(row["element"], None if r is None else (r["offset"], r["length"]))]

    def unit(self, row: dict) -> bytes:
        return b"".join(self.objects[k][o:o + n] for k, o, n in self.parts(row))


def object_keys(ir: Ir) -> dict[int, str]:
    """Each object's key by its element, as the IR lists its objects' keys, in their elements'
    order; none in a file's IR. A store's directories are elements above its objects, so a key
    is its object's path, but where a name needs quoting."""
    bases = ir.object_bases()
    elements = [i for i in range(len(ir)) if ir.kind(i) == Kind.OBJECT] if bases else []
    return {i: key for i, (key, _, _) in zip(elements, bases, strict=True)}


@cache
def described(path: Path) -> Described:
    """The IR of the file at `path`, read with the files beside it or below its directory that it
    names (an external file, an external link's target, a detached data file), its descriptions'
    document checked against the schema."""
    _, out = _zarrify(str(path), scope=str(path.parent))
    core = out.ir
    assert core is not None
    jsonschema.validate(json.loads(core.stored_arrays()), SCHEMA)
    ir = Ir(core)
    rows: dict[int, list[dict]] = {}
    for r in pa.record_batch(core.chunk_map()).to_pylist():
        rows.setdefault(r["array"], []).append(r)
    # a store's keys are relative to the file's directory
    keys = object_keys(ir)
    objects: dict[str | None, bytes] = {k: (path.parent / k).read_bytes() for k in keys.values()} or {
        None: path.read_bytes()}
    return Described(ir, ir.stored_arrays(), rows, objects, out, keys)
