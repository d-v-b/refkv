"""The host rules of the Rust core (rust/refkv-ir/src/host), by name: the store URL rules, the
listings' parsing, the keys a listing ignores, an input's kind, directory and scope, the late
documents of an archive, and the hosts' defaults. Each host calls the same rules, so that the
Python host, the browser's and the Rust CLI cannot disagree; this module only does the I/O around
them."""

from __future__ import annotations

import json
from typing import Any

from refkv import _core
from refkv.errors import Rejected


def rule(name: str, **params: object) -> Any:  # noqa: ANN401 (each rule's own JSON result)
    """The core's host rule `name` of `params`, its JSON result: Rejected for an input the rule
    rejects, ValueError for an argument it does not take."""
    try:
        return json.loads(_core.coding(name, json.dumps(params, ensure_ascii=False)))
    except _core.Rejected as e:
        raise Rejected(str(e)) from None


def body(data: bytes) -> str:
    """A response's bytes as a rule takes them: one character U+0000 to U+00FF per byte."""
    return bytes(data).decode("latin-1")


DEFAULTS: dict[str, Any] = rule("defaults")
"""The hosts' defaults (`refkv_ir::host::defaults`)."""


def late(keys: list[str], lazy: tuple[str, ...] | list[str] = ()) -> list[bool]:
    """Whether each bytes entry of `keys` is late, read when the archive opens: a node's metadata
    document (`zarr.json`, or a key ending in `/zarr.json`) not under a prefix of `lazy`
    (`refkv_ir::host::late`)."""
    return rule("late", keys=list(keys), lazy=list(lazy))


PAGE_SIZE: int = DEFAULTS["page_size"]
"""The page size of a zarrified archive's index (spec/bindings/vzip.md §12
<https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#12-page-index-optional>)."""
