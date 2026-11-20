"""How a format's coding parameters become the Zarr codecs and data type its profile writes
(spec/layout.md <https://github.com/d-v-b/refkv/blob/main/spec/layout.md>), for a layout
plugin that builds an array the base does not present, from the IR. Each routine but `tiff_tags`
is a binding of the function the built-in profile itself calls (rust/refkv-ir/src/routines.rs),
so a plugin and refkv cannot disagree; each returns Zarr v3 JSON values (codecs as `dict`s, a
data type as a `str`), and raises `NoCodec` for parameters the format's section rejects.

A codec chain starts after any `transpose`, which is the array's, and so the plugin's.

    from refkv.zarrify.coding import tiff_codecs, tiff_data_type, tiff_tags

    tags = tiff_tags(base, base.ir.at("ifds/1"))
    data_type = tiff_data_type(tags)
    codecs = tiff_codecs(tags, little_endian=True, x_axis=1, row_length=width)
"""

from __future__ import annotations

import base64
import json
import math
import struct
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from refkv import _core
from refkv.ir.kind import Kind

if TYPE_CHECKING:
    from refkv.ir import Value
    from refkv.zarrify.layouts import Base

__all__ = ["NoCodec", "czi_codecs", "dicom_codecs", "gzip_codecs", "hdf5_attributes", "hdf5_value", "tiff_byte_order",
           "tiff_codecs", "tiff_data_type", "tiff_tags", "vsi_codecs"]

# No codec undoes the coding parameters given: the format's section rejects them, naming them (a
# ValueError; a plugin turns it into refkv.zarrify.Rejected).
NoCodec = _core.NoCodec

# the tags `tiff_data_type` and `tiff_codecs` read: BitsPerSample, Compression,
# PhotometricInterpretation, FillOrder, SamplesPerPixel, PlanarConfiguration, Predictor,
# SampleFormat, YCbCrSubSampling, and EER's ImageLength, TileWidth, TileLength and bits
_CODING_TAGS = (257, 258, 259, 262, 266, 277, 284, 317, 322, 323, 339, 530, 65007, 65008, 65009)
# the text that spec/zarrify/hdf5.md §6
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#6-attributes> gives a float JSON has no number for
_NONFINITE = {"NaN": math.nan, "Infinity": math.inf, "-Infinity": -math.inf}


def _call(name: str, **params: object) -> Any:  # noqa: ANN401 (the JSON the core gives)
    return json.loads(_core.coding(name, json.dumps(params)))


def _tags_json(tags: Mapping[int, Value | None]) -> dict[str, object]:
    """The coding tags of `tags`, keyed as the core takes them; a tag without a value is left out."""
    out: dict[str, object] = {}
    for tag in _CODING_TAGS:
        value = tags.get(tag)
        if value is not None:
            out[str(tag)] = value
    return out


def tiff_tags(base: Base, i: int) -> dict[int, Value | None]:
    """The tags of the IFD that is IR element `i` (`ifds/<n>`, `ifds/<n>/subifds/<j>`, an Exif
    IFD) by number, each the IR's value as it is (a count-1 tag a one-item list; a rational a
    pair), `None` for one the IR keeps no value of (text of more than 1024 bytes, a value
    outside the file): only a tag's first entry, which the TIFF profile reads (spec/zarrify/tiff.md
    §5.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#51-elements>). Raises
    KeyError for an element that is no IFD."""
    ir = base.ir
    if not 0 <= i < len(ir):
        raise KeyError(f"no element {i} in the IR")
    while ir.kind(i) == Kind.ALIAS and ir.target(i) >= 0:  # an IFD read twice: its tags are its target's
        i = ir.target(i)
    out: dict[int, Value | None] = {}
    for t in ir.children(i):
        name = ir.name(t)  # `tags/<tag>`, or `tags/<tag>~<k>` for a later entry of it
        if not name.startswith("tags/") or not name[5:].isdigit():
            continue
        value = ir.child(t, "value")
        out[int(name[5:])] = None if value is None else ir.value(value)
    if not out:
        raise KeyError(f"element {i} ({ir.path(i)!r}) is not an IFD of the IR: it has no tags")
    return out


def tiff_data_type(tags: Mapping[int, Value | None]) -> str:
    """The data type of an IFD's samples, from SampleFormat and BitsPerSample (spec/zarrify/tiff.md
    §4.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#43-data-types-and-codecs>):
    packed samples, of 1 or 12 bits, held in the smallest type that takes them."""
    return str(_call("tiff_data_type", tags=_tags_json(tags)))


def tiff_byte_order(base: Base, i: int) -> bool:
    """Whether the TIFF file that holds IR element `i` (an IFD, say) is little-endian (`II`): its
    header's byte order, the file's own for a file of a set read as a store."""
    ir = base.ir
    at = i
    while at > 0 and ir.kind(at) != Kind.OBJECT:
        at = ir.parent(at)
    header = ir.child(at, "header") if at >= 0 else None
    order = ir.child(header, "byte_order") if header is not None else None
    if order is None:
        raise KeyError(f"element {i} ({ir.path(i)!r}) is in no TIFF file the IR has a header of")
    return ir.value(order) == "II"


def tiff_codecs(tags: Mapping[int, Value | None], *, little_endian: bool, x_axis: int,
                row_length: int, chunk_shape: Sequence[int] | None = None) -> list[dict[str, Any]]:
    """The codecs of an IFD's chunks after any `transpose`, as the TIFF profile writes them
    (spec/zarrify/tiff.md §4.3
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#43-data-types-and-codecs>),
    the tags checked as the profile checks them: `x_axis` the index of `x` in a chunk as stored
    (1 for interleaved RGB, stored `(y, x, s)`), `row_length` the samples of a stored row of a
    chunk (its width, times SamplesPerPixel when interleaved), which packed samples need, and
    `chunk_shape` its `[rows, columns]`, which EER's codec needs (NoCodec for EER without it)."""
    return list(_call("tiff_codecs", tags=_tags_json(tags), little_endian=little_endian, x_axis=x_axis,
                      row_length=row_length, chunk_shape=None if chunk_shape is None else list(chunk_shape)))


def czi_codecs(pixel_type: int, compression: int, *, flag: bool) -> list[dict[str, Any]]:
    """The codecs of a CZI subblock form after any `transpose` (a subblock's IR form holds them
    already, in its `codec`, which a plugin can take as they are) (spec/zarrify/czi.md §3.1
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/czi.md#31-pixel-types-and-codecs>):
    `flag` is Zstd1's hi-lo packing, or a JpgFile stream's being lossless, which the subblock's data
    tells and its form's `codec` records."""
    return list(_call("czi_codecs", pixel_type=pixel_type, compression=compression, flag=flag))


def vsi_codecs(codec: str, item_size: int) -> list[dict[str, Any]]:
    """The codec of a VSI tile file's tiles after any `transpose`, by its name (`raw`, `jpeg`,
    `jpeg2000`, `ljpeg`, `png`, `bmp`; spec/zarrify/vsi.md §4.1
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#41-the-pyramid>), for samples
    of `item_size` bytes."""
    return list(_call("vsi_codecs", codec=codec, item_size=item_size))


def dicom_codecs(transfer_syntax: str, data_type: str, *, samples: int) -> list[dict[str, Any]]:
    """The codec of a DICOM frame after any `transpose` (which 3 interleaved samples take, but
    for RLE, whose frames decode to planar ones), by its Transfer Syntax UID, for samples of
    `data_type`, in the transfer syntax's byte order (spec/zarrify/dicom.md §2.1
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/dicom.md#21-transfer-syntax>)."""
    return list(_call("dicom_codecs", transfer_syntax=transfer_syntax, data_type=data_type, samples=samples))


def gzip_codecs(skip: int, level: int) -> list[dict[str, Any]]:
    """The codecs after `bytes` of a chunk that is a gzip stream (they are bytes-to-bytes: an
    array's chain is `bytes` and then these): `gzip` of compression level `level` (the NRRD
    profile's 1, the others' 6), after `refkv.skip` of the first `skip` bytes of the inflated
    stream when `skip` is not 0 (a NumPy file's preamble and header; spec/zarrify/npy.md §4.4
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/npy.md#44-the-refkvskip-codec>)."""
    return list(_call("gzip_codecs", skip=skip, level=level))


def hdf5_value(value: object, datatype: Mapping[str, Any] | None) -> object:
    """An HDF5 attribute's value as the base writes it (spec/zarrify/hdf5.md §6
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#6-attributes>), its encodings
    undone by its described datatype (§7.2
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#72-datasets>): a float value
    (`"NaN"`, `"Infinity"`, a NaN's or −0's bits in hex) as a Python float, a string that is not
    UTF-8 (`{"latin1": T}`) as `T`, its bytes read as Latin-1, a compound's members each by
    theirs; an integer is one already, exact whatever its magnitude. ValueError for a value its
    datatype does not take."""
    if not datatype:
        return value
    if isinstance(value, list):
        return [hdf5_value(v, datatype) for v in value]
    kind = datatype.get("class")
    if kind == "float" and isinstance(value, str):
        if value in _NONFINITE:
            return _NONFINITE[value]
        size = int(datatype.get("size", 8))
        if value.startswith("0x") and len(value) == 2 + 2 * size and size in (2, 4, 8):
            return struct.unpack(">" + {2: "e", 4: "f", 8: "d"}[size], int(value, 16).to_bytes(size, "big"))[0]
        raise ValueError(f"{value!r} is not a float value of hdf5.md §6 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#6-attributes>")
    if kind in ("string", "vlen_string") and isinstance(value, dict) and set(value) == {"latin1"}:
        # text that is not UTF-8, each byte its Latin-1 character (spec/conventions.md §6
        # <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#6-source-metadata-as-json>)
        return str(value["latin1"])
    if kind == "compound" and isinstance(value, dict):
        members = {m.get("name"): m.get("datatype") for m in datatype.get("members", []) if isinstance(m, dict)}
        return {k: hdf5_value(v, members.get(k)) for k, v in value.items()}
    return value


def hdf5_attributes(base: Base, path: str) -> dict[str, Any]:
    """The HDF5 attributes of the node at `path`, as the source holds them (`base.attributes` with
    spec/zarrify/hdf5.md §6
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#6-attributes>'s encodings
    undone, by the node's source metadata, §7
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#7-source-metadata>): floats as
    numbers, and a netCDF `_FillValue` of a float dataset, which hdf5.md §6
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#6-attributes> writes as the Base64 of
    its binary64, as that float. An attribute of one element that the base wrote as its value, on a dataset with
    `dimension_names`, stays that value: the source metadata does not say which it was. The values
    are for computing with: NaN and the infinities are floats, which strict JSON (`dump`) does not
    write, so a plugin that writes attributes writes `base.attributes`' encoded form, or chooses an
    encoding of its own."""
    metadata = base.source_metadata(path) or {}
    described = metadata.get("attributes", {}) if isinstance(metadata, dict) else {}
    float_dataset = isinstance(metadata, dict) and (metadata.get("datatype") or {}).get("class") == "float"
    out: dict[str, Any] = {}
    for name, value in base.attributes(path).items():
        kind = (described.get(name) or {}).get("class") if isinstance(described, dict) else None
        # hdf5.md §6 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#6-attributes>
        # writes only such a _FillValue as Base64
        numeric = kind in ("integer", "float")
        if name == "_FillValue" and float_dataset and numeric and isinstance(value, str):
            try:
                raw = base64.b64decode(value, validate=True)
            except ValueError:
                raw = b""
            if len(raw) == 8:
                out[name] = struct.unpack("<d", raw)[0]
                continue
        out[name] = hdf5_value(value, described.get(name) if isinstance(described, dict) else None)
    return out
