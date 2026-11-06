"""An IR element's kind (spec/conventions.md §8.1
<https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#81-elements>), in a module of its
own, which imports nothing: `refkv.zarrify.layouts` reads it while `refkv.ir` is still being
imported."""

import enum

__all__ = ["Kind"]


class Kind(enum.IntEnum):
    """An element's kind, as `Ir.kind` gives it."""

    STRUCT = 0
    VALUE = 1
    DATA = 2
    DERIVED = 3
    ALIAS = 4
    GAP = 5
    OBJECT = 6  # a store IR's object: a file of a file read as a store, or an object of a store
