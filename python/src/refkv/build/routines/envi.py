"""The ENVI convention's routine: `envi_image`, the convention's image of an ENVI raster, its array
and source metadata (`EnviImage`)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from refkv import _core
from refkv.build.values import Ref, Value
from refkv.zarrify.store.urls import object_url

if TYPE_CHECKING:
    from refkv.build.parsed import Parsed

__all__ = ["EnviImage", "envi_image"]


class EnviImage:
    """The ENVI convention's image of an ENVI raster (spec/zarrify/envi.md §4
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/envi.md#4-the-image>), as refkv's own
    output presents it but for its declarations and its IR mirror: the root's source metadata, and
    the array at `0`, its document and its chunks as values of the data file. `envi_image` makes
    it."""

    def __init__(self, parsed: Parsed, envi: _core.Envi) -> None:
        own, doc, chunks, header_key, data_key = envi.image()
        self.url: str = object_url(parsed.url, header_key)
        """The URL the root names the input by as its source: the header file's object URL
        (spec/zarrify/envi.md §1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/envi.md#1-declaration>)."""
        self.data_key: str = data_key
        """The data file's key in the store, which every chunk references."""
        self._own: dict[str, Any] = json.loads(own)
        self._document: dict[str, Any] = json.loads(doc)
        source = parsed._objects.index(data_key)  # noqa: SLF001 (the url sources, by key)
        self._chunks = {tuple(coords): Value((Ref(parsed, source, offset, length),))
                        for coords, offset, length in chunks}

    def source_metadata(self) -> dict[str, Any]:
        """The root's source metadata (spec/zarrify/envi.md §5.1
        <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/envi.md#51-the-root>): every field
        of the header as written, its comments, the Zarr data type, the interleave, the byte order,
        the compression and the data file's key; a copy."""
        return json.loads(json.dumps(self._own))

    def document(self) -> dict[str, Any]:
        """The array's `zarr.json` (spec/zarrify/envi.md §4.1
        <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/envi.md#41-the-array>), parsed, its
        members in the convention's order and its attributes empty: a copy."""
        return json.loads(json.dumps(self._document))

    def chunks(self) -> dict[tuple[int, ...], Value]:
        """The array's chunks by their coordinates along its axes (`c`, `y`, `x`), each a value of
        its range of the data file (spec/zarrify/envi.md §8.5
        <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/envi.md#85-chunks>)."""
        return dict(self._chunks)


def envi_image(parsed: Parsed) -> EnviImage:
    """The ENVI convention's image of an ENVI raster, read as a store of its header file and the
    data file beside it (spec/zarrify/envi.md §4
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/envi.md#4-the-image>, §8
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/envi.md#8-envi-profile>): the
    convention's decisions, the Zarr data type, the transpose order, the codecs, the chunk cut, the
    axis names, the fill value and the root's source metadata, made from the parser's description
    (§8.7 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/envi.md#87-the-description>) by the
    function refkv's own projection calls. It reads nothing, and asks for no output of refkv's.
    ValueError for an input that is not an ENVI raster."""
    envi = parsed._envi  # noqa: SLF001
    if envi is None:
        raise ValueError(f"an ENVI routine reads an ENVI raster, not a {parsed.profile} input")
    return EnviImage(parsed, envi)
