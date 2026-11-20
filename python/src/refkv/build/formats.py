"""Routines of a format's convention that a layout may share with refkv's own projection, each the
binding of the function that projection calls (the design of d-v-b/refkv#278, "The routines"):
`fits_arrays`, the arrays of a FITS HDU as the FITS convention writes them, and `fits_cards`, its
header's cards as the convention writes them in JSON."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from refkv import _core

from .values import MODELED, Array, Copy, Literal, Part, Ref, Value, Zeros

if TYPE_CHECKING:
    from .parsed import Parsed

__all__ = ["fits_arrays", "fits_cards"]


def _part(parsed: Parsed, piece: _core.FitsPiece) -> Part:
    if piece[0] == "src":
        return Ref(parsed, 0, piece[1], piece[2])
    if piece[0] == "copy":
        return Copy(Ref(parsed, 0, piece[1], piece[2]))
    if piece[0] == "zeros":
        return Zeros(piece[1])
    return Literal(piece[1])


def _fits_description(parsed: Parsed) -> str:
    description = parsed.description
    if parsed.profile != "fits" or description is None:
        raise ValueError(f"a FITS routine reads a FITS file, not a {parsed.profile} input")
    return json.dumps(description)


def fits_cards(parsed: Parsed, index: int) -> list[list[Any]]:
    """The cards of HDU `index`'s header as the FITS convention writes them in JSON
    (spec/zarrify/fits.md §5.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/fits.md#53-cards>):
    each `[keyword, value, comment]` before `END`, an integer exact whatever its magnitude, a real
    as a float or `{"text": T}` where it has no finite value, a complex as `[re, im]`, no value
    as None; whatever their size (refkv's own source metadata leaves them out past its budget).
    ValueError for an input that is not a FITS file, or no HDU `index`."""
    return list(json.loads(_core.fits_cards(parsed.ir._core, _fits_description(parsed), index)))  # noqa: SLF001


def fits_arrays(parsed: Parsed, index: int) -> dict[str, Array]:
    """The arrays of HDU `index` of a FITS file as the FITS convention writes them
    (spec/zarrify/fits.md §3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/fits.md#3-images>
    and §4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/fits.md#4-tables>), by their
    path under the HDU's node: `""` for an image or a tile-compressed image, which is the node
    itself, and `rows` and `heap` for a table (`heap` where it has one); none for an HDU of no data.
    Each has its shape, data type, chunk grid, codecs, fill value and dimension names as refkv's own
    output has them, and its chunks as values of the input; its attributes are empty, since the
    declarations are refkv's.

    An edge chunk whose padded reference would not fit in a payload has a `Copy`, which the caller
    writes as bytes (`refkv.build.filled`). It reads nothing, and asks for no output of refkv's.
    ValueError for an input that is not a FITS file, or no HDU `index`."""
    out: dict[str, Array] = {}
    for path, text, chunks in _core.fits_arrays(parsed.ir._core, _fits_description(parsed), index):  # noqa: SLF001
        doc: dict[str, Any] = json.loads(text)
        grid = doc["chunk_grid"]["configuration"]
        out[path] = Array(
            list(doc["shape"]), doc["data_type"], list(grid["chunk_shape"]), list(doc["codecs"]),
            {tuple(coords): Value(tuple(_part(parsed, p) for p in pieces)) for coords, pieces in chunks},
            doc.get("fill_value"), doc.get("dimension_names"), dict(doc.get("attributes") or {}),
            doc.get("chunk_key_encoding"), {k: v for k, v in doc.items() if k not in MODELED},
        )
    return out
