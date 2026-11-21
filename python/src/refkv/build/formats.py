"""Routines of a format's convention that a layout may share with refkv's own projection, each the
binding of the function that projection calls (the design of d-v-b/refkv#278, "The routines"):
`fits_arrays`, the arrays of a FITS HDU as the FITS convention writes them, and `fits_cards`, its
header's cards as the convention writes them in JSON; `hdf5_hierarchy`, the HDF5 convention's
hierarchy of an HDF5 input, its groups and arrays as that convention presents them; `envi_image`,
the ENVI convention's image of an ENVI raster, its array and source metadata."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from refkv import _core
from refkv.zarrify.common import Rejected
from refkv.zarrify.store.urls import object_url

from .values import MODELED, Array, Copy, Literal, Part, Ref, Shared, Value, Zeros

if TYPE_CHECKING:
    from .parsed import Parsed

__all__ = ["EnviImage", "Hdf5Hierarchy", "ZarrHierarchy", "envi_image", "fits_arrays", "fits_cards", "hdf5_hierarchy",
           "zarr_hierarchy"]


def _part(parsed: Parsed, piece: _core.FitsPiece) -> Part:
    if piece[0] == "src":
        return Ref(parsed, 0, piece[1], piece[2])
    if piece[0] == "copy":
        return Copy(Ref(parsed, 0, piece[1], piece[2]))
    if piece[0] == "zeros":
        return Zeros(piece[1])
    return Literal(piece[1])


def _fits(parsed: Parsed) -> _core.Fits:
    """The FITS file `parsed` read, its IR and description shared with its run: one for the file,
    whatever HDUs are asked for."""
    fits = parsed._fits  # noqa: SLF001
    if fits is None:
        raise ValueError(f"a FITS routine reads a FITS file, not a {parsed.profile} input")
    return fits


def fits_cards(parsed: Parsed, index: int) -> list[list[Any]]:
    """The cards of HDU `index`'s header as the FITS convention writes them in JSON
    (spec/zarrify/fits.md §5.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/fits.md#53-cards>):
    each `[keyword, value, comment]` before `END`, an integer exact whatever its magnitude, a real
    as a float or `{"text": T}` where it has no finite value, a complex as `[re, im]`, no value
    as None; whatever their size (refkv's own source metadata leaves them out past its budget).
    ValueError for an input that is not a FITS file, or no HDU `index`."""
    return list(json.loads(_fits(parsed).cards(index)))


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
    for path, text, chunks in _fits(parsed).arrays(index):
        doc: dict[str, Any] = json.loads(text)
        grid = doc["chunk_grid"]["configuration"]
        out[path] = Array(
            list(doc["shape"]), doc["data_type"], list(grid["chunk_shape"]), list(doc["codecs"]),
            {tuple(coords): Value(tuple(_part(parsed, p) for p in pieces)) for coords, pieces in chunks},
            doc.get("fill_value"), doc.get("dimension_names"), dict(doc.get("attributes") or {}),
            doc.get("chunk_key_encoding"), {k: v for k, v in doc.items() if k not in MODELED},
        )
    return out


class Hdf5Hierarchy:
    """The HDF5 convention's hierarchy of an HDF5 input (spec/zarrify/hdf5.md §2
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#2-the-hierarchy>), as refkv's own
    output presents it but for its declarations and its IR mirror: each node the convention
    presents, its attributes and source metadata, and each array's document and chunks, as values of
    the input. `hdf5_hierarchy` makes it."""

    def __init__(self, parsed: Parsed, hdf5: _core.Hdf5) -> None:
        try:
            nodes, (_, entries, refs, data, _, _), copies = hdf5.hierarchy()
        except _core.Rejected as e:
            raise Rejected(str(e)) from None
        self._nodes: dict[str, dict[str, Any]] = {n["path"]: n for n in json.loads(nodes)}
        named = hdf5.named_key()
        self.url: str = parsed.url if named is None else object_url(parsed.url, named)
        """The URL the root names the input by as its source: a store's named file's
        (spec/zarrify/hdf5.md §8.6 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#86-external-files>),
        else the input's (`Parsed.url`)."""
        self._documents = {k: v for k, v in entries if k.rpartition("/")[2] == "zarr.json"}
        objects = getattr(parsed, "_objects", None) or []

        def part(p: Any) -> Part:  # noqa: ANN401 (a part in `out_py`'s form)
            if isinstance(p, (bytes, bytearray)):
                return Literal(bytes(p))
            if p[0] == "object":
                return Ref(parsed, objects.index(p[1]), p[2], p[3])
            if len(p) == 3:
                held = data[p[0] - 1][p[1]:p[1] + p[2]]
                return Zeros(p[2]) if not held.strip(b"\0") else Shared(held)
            return Ref(parsed, 0, p[0], p[1])

        chunks: dict[str, bytes | Value] = {k: v for k, v in entries if k.rpartition("/")[2] != "zarr.json"}
        chunks.update({k: Value(tuple(part(p) for p in parts)) for k, parts in refs})
        for key, offset, length, pad in copies:
            copied = Copy(Ref(parsed, 0, offset, length))
            chunks[key] = Value((copied, Zeros(pad)) if pad else (copied,))
        # each array's chunks, by key relative to it, filed once: a chunk's key is its array's path,
        # then `c` (a node may be named `c` too, so the path is the one that names an array)
        arrays = {path for path, node in self._nodes.items() if "array" in node}
        self._chunks: dict[str, dict[str, bytes | Value]] = {path: {} for path in arrays}
        for key, value in chunks.items():
            segments = key.split("/")
            for i, segment in enumerate(segments):
                path = "/".join(segments[:i])
                if segment == "c" and path in arrays:
                    self._chunks[path]["/".join(segments[i:])] = value
                    break

    def nodes(self) -> list[str]:
        """The paths of the nodes the convention presents, the root's `""` first: the file's in
        canonical order (spec/zarrify/hdf5.md §2.2
        <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#22-paths>), then, for a
        store, those its external links present (§8.6
        <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#86-external-files>)."""
        return list(self._nodes)

    def node_type(self, path: str) -> str | None:
        """`"group"` or `"array"` for the node at `path`, or None where there is none."""
        node = self._nodes.get(path)
        return None if node is None else "array" if "array" in node else "group"

    def attributes(self, path: str) -> dict[str, Any]:
        """The attributes of the node at `path` as the convention presents them (spec/zarrify/hdf5.md
        §6 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#6-attributes>), without
        refkv's declarations: a copy."""
        return json.loads(json.dumps(self._nodes[path]["attributes"]))

    def source_metadata(self, path: str) -> Any:  # noqa: ANN401 (JSON the convention specifies)
        """The source metadata of the node at `path` (spec/zarrify/hdf5.md §7
        <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#7-source-metadata>): a copy,
        or None where there is no node."""
        node = self._nodes.get(path)
        return None if node is None else json.loads(json.dumps(node["own"]))

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

    def document(self, key: str) -> dict[str, Any]:
        """The Zarr document at `key` (`<path>/zarr.json`), parsed, its attributes the node's
        without refkv's declarations: a copy. KeyError for a key of no node."""
        path = key.removesuffix("zarr.json").removesuffix("/")
        doc: dict[str, Any] = json.loads(self._documents[key])
        doc["attributes"] = self.attributes(path)
        return doc

    def array_entries(self, path: str) -> tuple[dict[str, Any], dict[str, bytes | Value]]:
        """The array at `path`: its document (`document`), and its chunk entries by key relative
        to the array, each bytes or a value. An edge chunk refkv's own output copies, because its
        padded reference would not fit in a payload, is a value of a `Copy`, which the caller writes
        as bytes (`refkv.build.filled`). KeyError for a path that is no array."""
        if self.node_type(path) != "array":
            raise KeyError(path)
        prefix = f"{path}/" if path else ""
        return self.document(f"{prefix}zarr.json"), dict(self._chunks[path])


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
    hdf5 = parsed._hdf5  # noqa: SLF001
    if hdf5 is None:
        raise ValueError(f"an HDF5 routine reads an HDF5 input, not a {parsed.profile} input")
    return Hdf5Hierarchy(parsed, hdf5)


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


class ZarrHierarchy:
    """The Zarr v2 or v3 convention's hierarchy of a Zarr store, listed or zipped (spec/zarrify/zarr2.md
    §2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#2-nodes>, spec/zarrify/zarr3.md
    §2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr3.md#2-nodes>), as refkv's own output
    presents it but for its declarations and the objects under `refkv_source/`: each node presented,
    its source metadata, and each array's document and chunks, as values of the input.
    `zarr_hierarchy` makes it."""

    def __init__(self, parsed: Parsed, zarr: _core.Zarr) -> None:
        try:
            nodes, (_, entries, refs, _, _, _) = zarr.hierarchy()
        except _core.Rejected as e:
            raise Rejected(str(e)) from None
        self._nodes: dict[str, dict[str, Any]] = {n["path"]: n for n in json.loads(nodes)}
        self._documents: dict[str, bytes] = {k: v for k, v in entries if k.rpartition("/")[2] == "zarr.json"}
        objects = getattr(parsed, "_objects", None) or []

        def part(p: Any) -> Part:  # noqa: ANN401 (a part in `out_py`'s form)
            if p[0] == "object":  # a listed store's object
                return Ref(parsed, objects.index(p[1]), p[2], p[3])
            return Ref(parsed, 0, p[0], p[1])  # a range of the zip file

        # each array's chunks, by key relative to it: the nearest array the key is below (no node
        # is inside an array)
        arrays = sorted((p for p, n in self._nodes.items() if n["kind"] == "array"), key=len, reverse=True)
        self._chunks: dict[str, dict[str, bytes | Value]] = {p: {} for p in arrays}
        for key, parts in refs:
            for path in arrays:
                if not path or key.startswith(f"{path}/"):
                    relative = key.removeprefix(f"{path}/") if path else key
                    self._chunks[path][relative] = Value(tuple(part(p) for p in parts))
                    break

    def nodes(self) -> list[str]:
        """The paths of the nodes the convention presents, explicit ones in ascending order of their
        paths, then the implicit groups."""
        return list(self._nodes)

    def node_type(self, path: str) -> str | None:
        """`"group"` or `"array"` for the node at `path`, or None where there is none."""
        node = self._nodes.get(path)
        return None if node is None else node["kind"]

    def source_metadata(self, path: str) -> Any:  # noqa: ANN401 (JSON the convention specifies)
        """The source metadata of the node at `path` (spec/zarrify/zarr2.md §4
        <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#4-source-metadata>,
        spec/zarrify/zarr3.md §4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr3.md#4-source-metadata>):
        what refkv's declaration on the node holds, or None for a node with none."""
        node = self._nodes.get(path)
        return None if node is None or node["s"] is None else json.loads(node["s"])

    def document_bytes(self, key: str) -> bytes:
        """The Zarr document at `key` (`<path>/zarr.json`) as refkv writes it but for its
        declarations: a Zarr v2 node's with no attributes but an OME group's `ome`, a Zarr v3
        node's with the attributes it keeps in place. KeyError for a key of no node."""
        return self._documents[key]

    def document(self, key: str) -> dict[str, Any]:
        """`document_bytes`, parsed."""
        return json.loads(self._documents[key])  # type: ignore[no-any-return]

    def array_entries(self, path: str) -> tuple[dict[str, Any], dict[str, bytes | Value]]:
        """The array at `path`: its document (`document`), and its chunk entries by key relative to
        the array, each a value of the input. KeyError for a path that is no array."""
        if self.node_type(path) != "array":
            raise KeyError(path)
        prefix = f"{path}/" if path else ""
        return self.document(f"{prefix}zarr.json"), dict(self._chunks[path])


def zarr_hierarchy(parsed: Parsed) -> ZarrHierarchy:
    """The Zarr convention's hierarchy of a Zarr v2 store or of a Zarr v3 store, listed or zipped
    (spec/zarrify/zarr2.md §2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#2-nodes>,
    spec/zarrify/zarr3.md §2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr3.md#2-nodes>):
    the convention's decisions, which nodes are presented, each array's document and chunks and each
    node's source metadata, made from the parser's description (zarr2.md §7.3
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#73-the-description>, zarr3.md
    §7.6 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr3.md#76-the-description>) by the
    function refkv's own projection calls. It reads nothing, and asks for no output of refkv's.
    ValueError for an input that is not a Zarr store; `refkv.zarrify.Rejected` where the convention
    rejects the input, as `zarrify` does."""
    zarr = parsed._zarr  # noqa: SLF001
    if zarr is None:
        raise ValueError(f"a Zarr routine reads a Zarr v2 or v3 store, not a {parsed.profile} input")
    return ZarrHierarchy(parsed, zarr)
