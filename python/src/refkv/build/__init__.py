"""Tools to build an output from the IR, as refkv's own projections do (the design of
d-v-b/refkv#278, design/proposals/profile-plugins.md there): `read` an input into a `Parsed`, make
values of its elements, build arrays and entries, and `seal` them into a store that reads only
bytes of sources `read` opened, within them.

    from refkv.build import Written, read, seal

    with read("a.tif") as p:
        out = Written({"first-strip": p.reference(p.ir.index("ifds/0/strips/0"))})
        seal(out, [p]).save_vzip("out.vzip")

This is the proposal's first step: `read` runs `zarrify`'s path and keeps the IR and the sources
it opened, `project` gives the built-in output it made, and a format's description, its plan and
the routines that build on them come later.
"""

from refkv.zarrify.layouts import MAX_PAYLOAD

from .parsed import MAX_BYTES, Parsed, read, read_async
from .project import Projected, project
from .seal import CODES, SealError, seal
from .values import (
    MAX_SOURCES,
    Array,
    Binding,
    Check,
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

__all__ = ["CODES", "MAX_BYTES", "MAX_PAYLOAD", "MAX_SOURCES", "Array", "Binding", "Check", "Copy", "Data",
           "Literal", "Parsed", "Part", "Projected", "Ref", "SealError", "Shared", "Value", "Written", "Zeros",
           "concat", "cut", "fits", "payload_size", "project", "read", "read_async", "seal", "value"]
