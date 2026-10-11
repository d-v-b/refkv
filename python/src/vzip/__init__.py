"""vzip: ZIP plus a reference value type.

A vzip store maps keys to values that are either bytes or references to byte ranges
of other objects (spec/archive.md, Part 1
<https://github.com/d-v-b/vzip/blob/main/spec/archive.md#part-1-reference-values>). Any
backend that can tell a bytes value from a reference value can hold one; this package
reads and writes the ZIP binding, a vzip archive (Part 2
<https://github.com/d-v-b/vzip/blob/main/spec/archive.md#part-2-the-zip-binding>).
"""

from vzip.archive import VZipWriter
from vzip.pb import Concat, Range
from vzip.store import Resolver, Stats, VZipStore

__all__ = ["Concat", "Range", "Resolver", "Stats", "VZipStore", "VZipWriter"]
