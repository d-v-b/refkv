"""The OME-Zarr image routine: `ome_image`, the OME-Zarr 0.5 image refkv's convention writes at the
root of an input of the TIFF family or IMS (`OmeImage`)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from refkv import _core
from refkv.errors import Rejected
from refkv.host.urls import object_url

from ._hierarchy import _Hierarchy

if TYPE_CHECKING:
    from refkv.build.parsed import Parsed

__all__ = ["OmeImage", "ome_image"]


class OmeImage(_Hierarchy):
    """The OME-Zarr 0.5 image refkv's convention writes at the root of a TIFF, QPTIFF, NDPI or IMS
    input (spec/zarrify/tiff.md §4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#4-the-image>,
    spec/zarrify/qptiff.md §4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/qptiff.md#4-the-image>,
    spec/zarrify/ndpi.md §4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#4-the-image>,
    spec/zarrify/ims.md §4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ims.md#4-output>), as
    refkv's own output presents it but for its declarations and its IR mirror: the root group, with
    its `ome` attributes and source metadata, and each level's array (`"0"`, `"1"`, ...), its
    document and chunks as values of the input. `ome_image` makes it."""

    def __init__(self, parsed: Parsed, image: _core.OmeImage) -> None:
        try:
            ranges = image.reads()
            # read together, so that the input's reader joins the ranges it can
            got = parsed.bytes(*[(0, o, n) for o, n in ranges]) if ranges else []
            copied = list(zip(ranges, [got] if isinstance(got, bytes) else got, strict=True))
            nodes, out = image.image(copied, checksums=parsed.checksums)
        except _core.Rejected as e:
            raise Rejected(str(e)) from None
        super().__init__(parsed, (nodes, out, []))
        named = image.named_key()
        self.url: str = parsed.url if named is None else object_url(parsed.url, named)
        """The URL the root names the input by as its source: an image across files' named file's
        (spec/zarrify/tiff.md §7.4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#74-several-files>),
        else the input's (`Parsed.url`)."""


def ome_image(parsed: Parsed) -> OmeImage:
    """The OME-Zarr 0.5 image refkv's convention writes at the root of a TIFF, QPTIFF, NDPI or IMS
    input, a file or a TIFF's image across files (`OmeImage`). Of the TIFF family, the convention's
    decisions (which IFDs are the levels and planes, the axes, data type, codecs, chunks and their
    padding and shards, the scales, position and `omero`) are refkv's plan of the file, made from
    the parser's description (spec/zarrify/tiff.md §7.6
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#76-the-description>) by the
    function refkv's own projection calls; an IMS input's are its parser's still. It reads the
    chunks its `omero` windows sample (through `Parsed.bytes`), and asks for no output of refkv's.
    ValueError for an input of another profile; `refkv.zarrify.Rejected` where the convention
    rejects the input, as `zarrify` does."""
    image = parsed._view_as(_core.OmeImage)  # noqa: SLF001
    if image is None:
        raise ValueError(f"an OME image routine reads a TIFF, QPTIFF, NDPI or IMS input, not a {parsed.profile} input")
    return OmeImage(parsed, image)
