"""Tools to build an output from the IR, as refkv's own projections do (the design of
d-v-b/refkv#278, design/proposals/profile-plugins.md there): `read` an input into a `Parsed`, make
values of its elements, build arrays and entries, and `seal` them into a store that reads only
bytes of sources `read` opened, within them.

    from refkv.build import Written, read, seal

    with read("a.tif") as p:
        out = Written({"first-strip": p.reference(p.ir.index("ifds/0/strips/0"))})
        seal(out, [p]).save_vzip("out.vzip")

`read` parses an input as `zarrify` does and keeps its IR and the sources it opened; `project`
gives refkv's own output of it, made when asked for; `apply` is what `zarrify` does with a plugin
layout: the plugin's `write(inputs, options)`, sealed. A format's description, its plan and the
routines that build on them come later. The routines of each format's convention
(`refkv.build.routines`) are exported here too, by the names their modules export.
"""

from typing import Any

from refkv.entries import MAX_PAYLOAD

from . import routines
from .convention import declaration, filled, grid, gzip_member, mirror
from .parsed import MAX_BYTES, NoIrError, Parsed, read, read_async
from .plugin import apply
from .project import Projected, project, project_async
from .seal import CODES, SealError, seal
from .stored import stored_array, zarr_codecs
from .values import (
    MAX_SOURCES,
    Array,
    Copy,
    Data,
    Literal,
    Part,
    Ref,
    Shared,
    Value,
    Written,
    Zeros,
    concat,
    cut,
    fits,
    payload_size,
    value,
)

__all__ = [
    "CODES",
    "MAX_BYTES",
    "MAX_PAYLOAD",
    "MAX_SOURCES",
    "Array",
    "Copy",
    "Data",
    "Literal",
    "NoIrError",
    "Parsed",
    "Part",
    "Projected",
    "Ref",
    "SealError",
    "Shared",
    "Value",
    "Written",
    "Zeros",
    "apply",
    "concat",
    "cut",
    "declaration",
    "filled",
    "fits",
    "grid",
    "gzip_member",
    "mirror",
    "payload_size",
    "project",
    "project_async",
    "read",
    "read_async",
    "seal",
    "stored_array",
    "value",
    "zarr_codecs",
]
__all__ += routines.__all__


def __getattr__(name: str) -> Any:  # noqa: ANN401 (a routine or its class, of any format)
    """A routine of `refkv.build.routines`, or its class, by name."""
    if name in routines.__all__:
        return getattr(routines, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted({*globals(), *routines.__all__})
