"""The example layout `refkv.example.xarray-v2` (spec/layout.md
<https://github.com/d-v-b/refkv/blob/main/spec/layout.md>): an HDF5 or NetCDF-4 file as a Zarr v2
hierarchy that `xarray.open_zarr(store, consolidated=True)` opens. Each group is a `.zgroup`, each
array a `.zarray` around the base's chunk references at v2's chunk keys (`0.1`), each node's
attributes a `.zattrs`, and the root's `.zmetadata` consolidates them all; a vzip reader is given
only `.zmetadata` and the root's documents at open. It writes its documents with `dump`, and reads
the base's chunk keys with `chunk_coords`, so it runs without the `layouts` extra.

    python -m refkv.zarrify data.nc out.vzip --layout refkv.example.xarray-v2

What xarray needs of Zarr v2, made from the base:

- `_ARRAY_DIMENSIONS`, every array's: the base's `dimension_names` (NetCDF-4's dimension scales,
  hdf5.md §3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#3-datasets>), and for an
  axis without one a `phony_dim_<n>`, one per size and use in a group, numbered across the file.
  NetCDF-4's `_nc4_non_coord_<name>` is presented as `<name>`, as netCDF-4 reads it.
- dtype, `compressor` and `filters`: this module's own translation of the base array's Zarr v3
  data type and codecs (`bytes` -> the byte order; `numcodecs.<id>`, `zstd`, `gzip`, `blosc` and
  `imagecodecs_<id>` -> numcodecs ids, the last one the `compressor`; `vlen-utf8` -> `|O`). A
  fixed-length string, uint8 with a trailing axis of its bytes in the base, is `|S<n>`; a flat
  `struct` a structured dtype. An array with no v2 form (`refkv.h5_bitshuffle`, a nested `struct`,
  a fill value v2 cannot hold) is `Rejected`, or left out with the option
  `{"unsupported": "skip"}`. A reader of `imagecodecs_<id>` registers imagecodecs' numcodecs first
  (`imagecodecs.numcodecs.register_codecs()`).
- `fill_value`, which xarray reads as a v2 array's `_FillValue`: the dataset's netCDF `_FillValue`
  attribute, decoded, when it has one; else `null`, so xarray masks nothing, as it does reading the
  file, unless chunks are missing and the dataset's fill value is not zero, when it is that fill
  value, so that the values are right (and xarray masks it).
- attributes: the source's, as `coding.hdf5_attributes` gives them (the encodings of
  hdf5.md §6 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#6-attributes>
  undone: big integers, NaN, infinities and a float dataset's Base64 `_FillValue` as numbers),
  without netCDF-4's bookkeeping, which xarray's netCDF-4 readers hide (`CLASS`, `NAME`,
  `DIMENSION_LIST`, `REFERENCE_LIST`, `_Netcdf4Dimid`, ...), and an array of one value as that
  value, as netCDF-4 reads it. A NaN or infinity is written as a bare `NaN` or `Infinity` token
  (`dump(..., allow_nan=True)`), as zarr-python writes v2 attributes, or kept as that text with
  the option `{"nonfinite": "text"}`.

`check` finds no problem on its output but `json`, at a `.zattrs` and the `.zmetadata` that hold a
`NaN` or `Infinity` token (only where a source attribute is not finite and `nonfinite` is
`"token"`): those are not strict JSON, but they are what Zarr v2's readers read and write.
"""

import math
import struct
from collections.abc import Mapping
from typing import Any, ClassVar

from refkv.zarrify import Rejected
from refkv.zarrify.coding import hdf5_attributes, hdf5_value
from refkv.zarrify.layouts import Base, EntryValue, Written, chunk_coords, dump

NON_COORD = "_nc4_non_coord_"  # netCDF-4's prefix of a variable named as a dimension it lacks
# netCDF-4's bookkeeping attributes, which h5netcdf and netCDF4-python hide
HIDDEN = frozenset(["CLASS", "NAME", "DIMENSION_LIST", "REFERENCE_LIST", "_Netcdf4Dimid", "_Netcdf4Coordinates",
                    "_nc3_strict", "_NCProperties"])
DTYPES = {"bool": "b1", "int8": "i1", "int16": "i2", "int32": "i4", "int64": "i8", "uint8": "u1", "uint16": "u2",
          "uint32": "u4", "uint64": "u8", "float16": "f2", "float32": "f4", "float64": "f8", "complex64": "c8",
          "complex128": "c16"}
FLOATS = {"float16": 2, "float32": 4, "float64": 8}
ENDIAN = {"little": "<", "big": ">", None: "<"}
BLOSC_SHUFFLE = {"noshuffle": 0, "shuffle": 1, "bitshuffle": 2}


class NoV2(ValueError):  # noqa: N818 (said as "no v2 form")
    """A base array with no Zarr v2 form: the message says why."""


# ---- data types and codecs

def v2_dtype(data_type: object, order: str) -> object:
    """A Zarr v3 data type as Zarr v2's dtype, `order` the byte order (`<` or `>`)."""
    if isinstance(data_type, str) and data_type in DTYPES:
        code = DTYPES[data_type]
        return ("|" if code[1:] == "1" else order) + code
    if isinstance(data_type, Mapping):
        config = data_type.get("configuration", {})
        if data_type.get("name") == "struct":
            fields = [[f["name"], v2_dtype(f["data_type"], order)] for f in config["fields"]]
            if any(isinstance(t, list) for _, t in fields):  # Zarr v2 allows it; zarr-python reads it not
                raise NoV2("a struct nested in a struct, which zarr-python does not read in Zarr v2")
            return fields
        if data_type.get("name") == "fixed_length_utf32":
            return f"{order}U{config['length_bytes'] // 4}"
    raise NoV2(f"the data type {data_type!r} has no Zarr v2 dtype")


def v2_codec(codec: Mapping[str, Any]) -> dict[str, Any]:
    """A Zarr v3 bytes-to-bytes codec as the numcodecs configuration a Zarr v2 reader decodes."""
    name, config = codec["name"], dict(codec.get("configuration", {}))
    if name.startswith("numcodecs."):
        return {"id": name.removeprefix("numcodecs."), **config}
    if name.startswith("imagecodecs_"):  # a reader registers them: imagecodecs.numcodecs.register_codecs()
        return {"id": name, **config}
    if name == "zstd":
        return {"id": "zstd", "level": config.get("level", 0), "checksum": config.get("checksum", False)}
    if name == "gzip":
        return {"id": "gzip", "level": config.get("level", 1)}
    if name == "blosc":
        return {"id": "blosc", "cname": config["cname"], "clevel": config["clevel"],
                "shuffle": BLOSC_SHUFFLE[config["shuffle"]], "blocksize": config.get("blocksize", 0)}
    raise NoV2(f"the codec {name} has no Zarr v2 form")


def v2_coding(doc: Mapping[str, Any]) -> tuple[str, bool, list[dict[str, Any]]]:
    """The byte order of a Zarr v3 array's codecs, whether they make an object array, and their
    numcodecs chain (an object codec first). An HDF5 base starts with no `transpose`."""
    head, *rest = doc["codecs"]
    if head["name"] == "bytes":
        return ENDIAN[head.get("configuration", {}).get("endian")], False, [v2_codec(c) for c in rest]
    if head["name"] in ("vlen-utf8", "vlen-bytes"):
        return "|", True, [{"id": head["name"]}, *(v2_codec(c) for c in rest)]
    raise NoV2(f"the codec {head['name']} has no Zarr v2 form")


# ---- values

def finite_or_text(value: object, original: object) -> object:
    """`value`, but where it holds a NaN or infinity, `original` (the base's text for it) instead."""
    if isinstance(value, float) and not math.isfinite(value):
        return original
    if isinstance(value, list):
        return [finite_or_text(v, o) for v, o in zip(value, original, strict=True)]  # type: ignore[call-overload]
    if isinstance(value, dict):
        return {k: finite_or_text(v, original[k]) for k, v in value.items()}  # type: ignore[index]
    return value


def v2_fill(value: object) -> object:
    """A float fill value as Zarr v2 writes one: a number, or `"NaN"`, `"Infinity"`,
    `"-Infinity"`."""
    if isinstance(value, float) and math.isnan(value):
        if struct.pack("<d", value) != struct.pack("<d", math.nan):
            raise NoV2("a NaN fill value with other bits than Zarr's NaN, which Zarr v2 cannot hold")
        return "NaN"
    if isinstance(value, float) and math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    return value


def zero(value: object) -> bool:
    """Whether a Zarr v3 fill value is its data type's zero, which a v2 array without one reads."""
    if isinstance(value, dict):
        return all(zero(v) for v in value.values())
    if isinstance(value, str):
        return value == ""  # a float's "NaN" or bits in hex are not
    return value == 0 and math.copysign(1.0, value) > 0


# ---- the layout

class XarrayV2:
    name = "refkv.example.xarray-v2"
    formats: ClassVar[Mapping[str, frozenset[int]]] = {"hdf5": frozenset({0})}

    def options(self, given: Mapping[str, object]) -> dict[str, object]:
        """`{"unsupported": "reject", "nonfinite": "token"}` by default: whether an array with no
        Zarr v2 form rejects the input or is left out (`"skip"`), and whether a non-finite float
        attribute is a bare JSON token or the base's text for it (`"text"`)."""
        unknown = sorted(set(given) - {"unsupported", "nonfinite"})
        if unknown:
            raise ValueError(f"unknown options {unknown}: {self.name} takes unsupported and nonfinite")
        out = {"unsupported": given.get("unsupported", "reject"), "nonfinite": given.get("nonfinite", "token")}
        if out["unsupported"] not in ("reject", "skip"):
            raise ValueError(f'unsupported is "reject" or "skip", not {out["unsupported"]!r}')
        if out["nonfinite"] not in ("token", "text"):
            raise ValueError(f'nonfinite is "token" or "text", not {out["nonfinite"]!r}')
        return out

    def write(self, base: Base, options: Mapping[str, object]) -> Written:
        """The file's groups and arrays as Zarr v2, consolidated at the root."""
        paths = base.nodes(datasets=False)  # the file's own nodes: no IR mirror, no dataset layouts
        present = set(paths)
        docs: dict[str, dict[str, Any]] = {}  # v2 document key -> document
        chunks: dict[str, EntryValue] = {}
        arrays: dict[str, tuple[str, list[str | None], list[int]]] = {}  # out path -> (group, names, shape)
        for path in paths:
            doc = base.document(f"{path}/zarr.json" if path else "zarr.json")
            parent, _, leaf = path.rpartition("/")
            if leaf.startswith(NON_COORD) and leaf != NON_COORD and \
                    "/".join(filter(None, [parent, leaf.removeprefix(NON_COORD)])) not in present:
                leaf = leaf.removeprefix(NON_COORD)
            out = "/".join(filter(None, [parent, leaf]))
            prefix = f"{out}/" if out else ""
            attrs = self.attributes(base, path, options)
            if doc["node_type"] == "group":
                docs[f"{prefix}.zgroup"] = {"zarr_format": 2}
            else:
                try:
                    zarray, names, held, attrs = self.array(base, path, doc, attrs)
                except NoV2 as e:
                    if options["unsupported"] == "skip":
                        continue
                    raise Rejected(f"{path}: {e}") from None
                docs[f"{prefix}.zarray"] = zarray
                chunks.update({f"{prefix}{k}": v for k, v in held.items()})
                arrays[out] = (parent, names, zarray["shape"])
            if attrs or doc["node_type"] == "array":
                docs[f"{prefix}.zattrs"] = attrs
        for out, names in self.dimensions(arrays).items():
            docs[f"{out}/.zattrs" if out else ".zattrs"]["_ARRAY_DIMENSIONS"] = names
        docs = dict(sorted(docs.items()))
        entries: dict[str, EntryValue] = {k: dump(d, allow_nan=True) for k, d in docs.items()}
        entries[".zmetadata"] = dump({"metadata": docs, "zarr_consolidated_format": 1}, allow_nan=True)
        entries.update(chunks)
        return Written(entries, read_at_open=[k for k in (".zmetadata", ".zgroup", ".zattrs") if k in entries])

    def attributes(self, base: Base, path: str, options: Mapping[str, object]) -> dict[str, Any]:
        """A node's attributes as the source holds them (`hdf5_attributes`), without netCDF-4's
        bookkeeping, an array of one value as that value."""
        written = base.attributes(path)  # the base's text for a NaN or infinity, for the option "text"
        out = {}
        for name, value in hdf5_attributes(base, path).items():
            if name in HIDDEN:
                continue
            one = isinstance(value, list) and len(value) == 1  # as netCDF-4 reads it
            plain, text = (value[0], written[name][0]) if one else (value, written[name])
            keep = options["nonfinite"] == "token" or name == "_FillValue"  # which becomes fill_value
            out[name] = plain if keep else finite_or_text(plain, text)
        return out

    def array(self, base: Base, path: str, doc: Mapping[str, Any], attrs: dict[str, Any]
              ) -> tuple[dict[str, Any], list[str | None], dict[str, EntryValue], dict[str, Any]]:
        """A base array's `.zarray`, dimension names (None for an axis it names no dimension of),
        chunks at v2 keys, and attributes, its netCDF `_FillValue` moved to `fill_value`."""
        if doc["chunk_grid"]["name"] != "regular":
            raise NoV2(f"a {doc['chunk_grid']['name']} chunk grid, which Zarr v2 cannot say")
        endian, is_object, chain = v2_coding(doc)
        shape = list(doc["shape"])
        chunk_shape = list(doc["chunk_grid"]["configuration"]["chunk_shape"])
        names: list[str | None] = list(doc.get("dimension_names") or [None] * len(shape))
        datatype = (base.source_metadata(path) or {}).get("datatype", {})
        fixed = datatype.get("class") == "string" and doc["data_type"] == "uint8" and shape[-1:] == [datatype["size"]]
        if fixed:  # a fixed-length string: its trailing axis of bytes is the dtype's
            if chunk_shape[-1] != shape[-1]:  # the base cut a string's bytes across chunks
                raise NoV2(f"a string of {shape[-1]} bytes cut into chunks of {chunk_shape[-1]}, which a Zarr v2 "
                           "string type cannot hold")
            dtype: object = f"|S{shape.pop()}"
            chunk_shape.pop()
            names.pop()
        elif is_object:
            dtype = "|O"
        else:
            dtype = v2_dtype(doc["data_type"], endian)
        base_doc, held = base.array(path)
        expected = math.prod(-(-s // c) for s, c in zip(doc["shape"], doc["chunk_grid"]["configuration"]["chunk_shape"],
                                                        strict=True))
        data_type = doc["data_type"] if isinstance(doc["data_type"], str) else None
        number = data_type in DTYPES and not fixed
        mine: Any = hdf5_value(doc["fill_value"], {"class": "float", "size": FLOATS[data_type]}) \
            if data_type in FLOATS else doc["fill_value"]  # a float fill value as the base writes it
        nc_fill = attrs.get("_FillValue")
        fill: object = None
        if number and isinstance(nc_fill, (int, float)) and not isinstance(nc_fill, bool):
            del attrs["_FillValue"]  # xarray reads a v2 array's fill_value as its _FillValue
            if len(held) < expected and not (mine == nc_fill or (math.isnan(mine) and math.isnan(nc_fill))):
                raise NoV2(f"missing chunks read as the fill value {doc['fill_value']}, not the _FillValue "
                           f"{nc_fill}, and Zarr v2 has one fill value")
            fill = v2_fill(nc_fill)
        elif len(held) < expected and not zero(doc["fill_value"]):
            if not number:
                raise NoV2(f"missing chunks read as the fill value {doc['fill_value']!r}, which this layout "
                           "writes only for numbers")
            fill = v2_fill(mine)
        objects, rest = (chain[:1], chain[1:]) if is_object else ([], chain)
        filters, compressor = objects + rest[:-1], rest[-1] if rest else None
        zarray: dict[str, Any] = {"zarr_format": 2, "shape": shape, "chunks": chunk_shape, "dtype": dtype,
                                  "compressor": compressor, "fill_value": fill, "order": "C",
                                  "filters": filters or None, "dimension_separator": "."}
        v2_chunks: dict[str, EntryValue] = {}
        for key, value in held.items():
            coords = chunk_coords(key, base_doc)
            if fixed:
                coords.pop()
            v2_chunks[".".join(map(str, coords)) or "0"] = value
        return zarray, names, v2_chunks, attrs

    def dimensions(self, arrays: Mapping[str, tuple[str, list[str | None], list[int]]]) -> dict[str, list[str]]:
        """Each array's `_ARRAY_DIMENSIONS`: its own names, and a `phony_dim_<n>` for each axis it
        names none of, one per size and use within a group, numbered across groups in path order."""
        taken = {n for _, names, _ in arrays.values() for n in names if n is not None}
        counter = 0
        phony: dict[tuple[str, int, int], str] = {}
        by_group: dict[str, dict[int, int]] = {}
        for group, names, shape in arrays.values():
            uses: dict[int, int] = {}
            for n, size in zip(names, shape, strict=True):
                if n is None:
                    uses[size] = uses.get(size, 0) + 1
            need = by_group.setdefault(group, {})
            for size, count in uses.items():
                need[size] = max(need.get(size, 0), count)
        for group in sorted(by_group):
            for size in sorted(by_group[group]):
                for use in range(by_group[group][size]):
                    while f"phony_dim_{counter}" in taken:
                        counter += 1
                    phony[group, size, use] = f"phony_dim_{counter}"
                    counter += 1
        out = {}
        for path, (group, names, shape) in sorted(arrays.items()):
            seen: dict[int, int] = {}
            dims = []
            for n, size in zip(names, shape, strict=True):
                if n is None:
                    seen[size] = seen.get(size, 0) + 1
                dims.append(n if n is not None else phony[group, size, seen[size] - 1])
            out[path] = dims
        return out
