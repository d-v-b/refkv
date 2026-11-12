"""Oracle for the phkv suite: the expected results of harness queries against a valid description.

The vzip suite's model (conformance/vzip/model.py) computes what a reader gets from sources
(files, HTTP, pins, key and data sources); a phkv store resolves its sources the same way, so this
model reuses it and changes only what the bindings do differently
(spec/bindings/phkv.md §22.2 <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#222-operations>):

* a phkv store reserves no names, so every key of the description is an ordinary key
  (`__vz__/hdr` included), and `__vz__/sources` is just a key the store does not have;
* `raw(key)` of a bytes value is its *stored* bytes: the value itself, or, for a `compress` entry,
  whatever the writer stored (a DEFLATE stream of it, with the codec a writer may choose);
* `raw(key)` of a reference is its payload, and there is no mirror.
"""

from __future__ import annotations

import builtins
import sys
import zlib
from pathlib import Path
from typing import override

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "vzip"))

import model as vzip_model
import pbref

_utf8_key = vzip_model._utf8_key


class PhkvModel(vzip_model.Model):
    def __init__(self, desc: dict, store_path: Path, http: tuple[str, Path] | None = None):
        super().__init__(desc, store_path, http)
        self.revision = None

    @override
    def _source_bytes(self, idx: int) -> bytes:
        """As the vzip model's, but a `key` source may name any bytes entry: `__vz__/sources` and
        `__vz__/index` are ordinary keys here."""
        if idx < len(self.sources) and "key" in self.sources[idx]:
            e = self.entries.get(self.sources[idx]["key"])
            if e is None or "ranges" in e:
                raise vzip_model.ModelError("bad key source")
            return bytes.fromhex(e["bytes"])
        return super()._source_bytes(idx)

    @override
    def kind(self, key: str) -> str:
        if key not in self.entries:
            return "missing"
        return "reference" if "ranges" in self.entries[key] else "bytes"

    @override
    def get_raw(self, key: str) -> builtins.list[dict]:
        e = self.entries.get(key)
        if e is None:
            return [{"ok": True, "value": None}]
        if "ranges" in e:
            return [{"ok": True, "value": pbref.payload(e["ranges"])[1].hex()}]
        if e.get("compress"):
            # stored bytes: the value (codec none) or a raw DEFLATE stream of it (codec 1)
            return [{"ok": True, "value": e["bytes"]}, {"ok": True, "deflates_to": e["bytes"]}]
        return [{"ok": True, "value": e["bytes"]}]

    @override
    def list(self, prefix: str) -> builtins.list[dict]:
        keys = sorted((k for k in self.entries if k.startswith(prefix)), key=_utf8_key)
        return [{"ok": True, "keys": keys}]


def deflates_to(value_hex: str, expected_hex: str) -> bool:
    """Whether `value_hex` is a raw DEFLATE stream (RFC 1951) of exactly `expected_hex`'s bytes."""
    try:
        d = zlib.decompressobj(-15)
        out = d.decompress(bytes.fromhex(value_hex))
        return d.eof and not d.unused_data and out == bytes.fromhex(expected_hex)
    except (zlib.error, ValueError):
        return False
