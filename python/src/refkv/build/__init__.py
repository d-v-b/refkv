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
routines that build on them come later.
"""

from refkv.zarrify.layouts import MAX_PAYLOAD

from .convention import declaration, filled, grid, gzip_member, mirror
from .formats import Hdf5Hierarchy, Netcdf3Hierarchy, fits_arrays, fits_cards, hdf5_hierarchy, netcdf3_hierarchy
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
    "Hdf5Hierarchy",
    "Literal",
    "Netcdf3Hierarchy",
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
    "fits_arrays",
    "fits_cards",
    "grid",
    "gzip_member",
    "hdf5_hierarchy",
    "mirror",
    "netcdf3_hierarchy",
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
