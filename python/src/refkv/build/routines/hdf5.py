"""The HDF5 convention's routine: `hdf5_hierarchy`, the convention's hierarchy of an HDF5 input,
its groups and arrays as the convention presents them (`Hdf5Hierarchy`)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from refkv import _core
from refkv.zarrify.common import Rejected
from refkv.zarrify.store.urls import object_url

from ._hierarchy import _Hierarchy

if TYPE_CHECKING:
    from refkv.build.parsed import Parsed

__all__ = ["Hdf5Hierarchy", "hdf5_hierarchy"]


class Hdf5Hierarchy(_Hierarchy):
    """The HDF5 convention's hierarchy of an HDF5 input (spec/zarrify/hdf5.md §2
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#2-the-hierarchy>), as refkv's own
    output presents it but for its declarations and its IR mirror: each node the convention
    presents (in canonical order, spec/zarrify/hdf5.md §2.2
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#22-paths>, then, for a store,
    those its external links present, §8.6
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#86-external-files>), its
    attributes (§6 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#6-attributes>)
    and source metadata (§7
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#7-source-metadata>), and each
    array's document and chunks, as values of the input. `hdf5_hierarchy` makes it."""

    def __init__(self, parsed: Parsed, hdf5: _core.Hdf5) -> None:
        try:
            given = hdf5.hierarchy()
        except _core.Rejected as e:
            raise Rejected(str(e)) from None
        super().__init__(parsed, given)
        named = hdf5.named_key()
        self.url: str = parsed.url if named is None else object_url(parsed.url, named)
        """The URL the root names the input by as its source: a store's named file's
        (spec/zarrify/hdf5.md §8.6 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#86-external-files>),
        else the input's (`Parsed.url`)."""

    def element(self, path: str) -> int | None:
        """The IR element of the storage of the array at `path`: the struct of its chunks, or the
        element of its contiguous data (spec/zarrify/hdf5.md §8.7
        <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#87-the-description>,
        `storage.element`); None for an array the IR holds no such element of (compact, external,
        unallocated, or of strings, references or records). KeyError for a path that is no
        array."""
        if self.node_type(path) != "array":
            raise KeyError(path)
        store = self._nodes[path]["array"]["store"]
        if store.get("kind") not in ("contiguous", "chunked"):
            return None
        element = store.get("element")
        return element if isinstance(element, int) else None


def hdf5_hierarchy(parsed: Parsed) -> Hdf5Hierarchy:
    """The HDF5 convention's hierarchy of an HDF5 input, a file or a store of one with the files it
    names (spec/zarrify/hdf5.md §2
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#2-the-hierarchy>, §8.6
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#86-external-files>): the
    convention's decisions, which objects are nodes, their attributes, the arrays' data types,
    codecs, fill values, dimension names and chunks, made from the parser's description (§8.7
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#87-the-description>) by the
    function refkv's own projection calls. It reads nothing, and asks for no output of refkv's.
    ValueError for an input that is not HDF5; `refkv.zarrify.Rejected` where the convention rejects
    the input, as `zarrify` does."""
    hdf5 = parsed._view_as(_core.Hdf5)  # noqa: SLF001
    if hdf5 is None:
        raise ValueError(f"an HDF5 routine reads an HDF5 input, not a {parsed.profile} input")
    return Hdf5Hierarchy(parsed, hdf5)
