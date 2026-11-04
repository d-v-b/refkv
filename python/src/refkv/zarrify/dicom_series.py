"""The DICOM store form (spec/zarrify/dicom.md §9
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/dicom.md#9-the-store-form>): a store of
DICOM files, as a prefix of `idc-open-data` is, zarrified file by file and joined with
`refkv.compose` into one group per series: a stack of slices sorted by position, or a whole-slide
image's levels as multiscales. Each file's own hierarchy stays under `instances/`, with its source
metadata and its mirror, so no file's attributes are lost to the join.

The form lives here, in the Python binding, because `refkv.compose` does: the Rust core reads one
file at a time, and the browser code and the CLI refuse such a store.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import math
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from refkv._narrow import mapping
from refkv.bounds import Bound, ChunkBox, Key, Prefix
from refkv.compose import map_keys, mount, normalize, union
from refkv.policy import Policy
from refkv.refstore import ReferenceStore
from refkv.zarrify.common import Rejected

Doc = dict[str, Any]
"""A JSON document of the store, as read from or written to a `zarr.json`."""

WHOLE_SLIDE = "1.2.840.10008.5.1.4.1.1.77.1.6"
SERIES_UID = "0020000E"
SOP_CLASS = "00080016"
SOP_INSTANCE = "00080018"
IMAGE_TYPE = "00080008"
INSTANCE_NUMBER = "00200013"
POSITION = "00200032"
ORIENTATION = "00200037"
TOLERANCE = 1e-4
"""How far, in millimeters or direction cosines, two values may differ and be the same."""
UNIFORM = 0.01
"""How far, as a fraction of their mean, slice gaps may differ and be one spacing."""
ARRAY_FIELDS = ("data_type", "chunk_grid", "chunk_key_encoding", "fill_value", "codecs", "dimension_names")


@dataclass
class Instance:
    """A file of the store, zarrified."""

    key: str
    store: ReferenceStore
    root: Doc
    array: Doc
    dataset: Doc

    def text(self, tag: str) -> str | None:
        values = self.dataset.get(tag, {}).get("Value")
        return values[0] if values and isinstance(values[0], str) else None

    def numbers(self, tag: str) -> list[float] | None:
        values = self.dataset.get(tag, {}).get("Value")
        try:
            return [float(v) for v in values] if values else None
        except (TypeError, ValueError):
            return None

    @property
    def ome(self) -> Doc:
        return self.root["attributes"]["ome"]

    @property
    def scale(self) -> list[float]:
        return self.ome["multiscales"][0]["datasets"][0]["coordinateTransformations"][0]["scale"]


@dataclass
class Series:
    """What the series's files are joined into (§9.2
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/dicom.md#92-a-series>)."""

    uid: str
    kind: str
    instances: list[Instance]
    levels: list[Instance] = field(default_factory=list)
    spacing: float | None = None
    positions: list[float] | None = None
    reason: str | None = None


def _sorted_by_position(instances: list[Instance]) -> tuple[list[Instance], list[float]] | None:
    """`instances` along the normal of their common orientation, each with its distance; None
    unless all have one orientation and a position."""
    orientations = [i.numbers(ORIENTATION) for i in instances]
    positions = [i.numbers(POSITION) for i in instances]
    first = orientations[0]
    if first is None or len(first) != 6 or any(
            o is None or len(o) != 6 or max(abs(a - b) for a, b in zip(o, first, strict=True)) > TOLERANCE
            for o in orientations):
        return None
    if any(p is None or len(p) != 3 for p in positions):
        return None
    row, column = first[:3], first[3:]
    normal = (row[1] * column[2] - row[2] * column[1], row[2] * column[0] - row[0] * column[2],
              row[0] * column[1] - row[1] * column[0])
    distances = [math.fsum(n * x for n, x in zip(normal, p, strict=True)) for p in positions if p is not None]
    order = sorted(range(len(instances)), key=distances.__getitem__)
    return [instances[k] for k in order], [distances[k] for k in order]


def _difference(instances: Sequence[Instance]) -> str | None:
    """What keeps `instances` from being one array's planes, if anything."""
    first = instances[0]
    for other in instances[1:]:
        for name in ("shape", *ARRAY_FIELDS):
            if other.array[name] != first.array[name]:
                return f"{other.key} differs from {first.key} in {name}"
    return None


def _stack(uid: str, instances: list[Instance]) -> Series:
    """A series of one-frame files, as a stack (§9.2
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/dicom.md#92-a-series>), or its reason
    to be its instances alone."""
    def apart(reason: str) -> Series:
        return Series(uid, "instances", sorted(instances, key=lambda i: i.key), reason=reason)

    axes = instances[0].array["dimension_names"]
    if any(i.array["dimension_names"] != axes for i in instances) or "z" in axes or "t" in axes:
        return apart("a series of multi-frame files")
    difference = _difference(instances)
    if difference is not None:
        return apart(difference)
    if any(i.scale != instances[0].scale for i in instances):
        return apart("its files differ in pixel spacing")
    placed = _sorted_by_position(instances)
    if placed is not None:
        ordered, distances = placed
        gaps = [b - a for a, b in itertools.pairwise(distances)]
        if any(g <= TOLERANCE for g in gaps):
            return apart("its files repeat a position")
        mean = math.fsum(gaps) / len(gaps)
        spacing = mean if max(gaps) - min(gaps) <= UNIFORM * mean else None
        return Series(uid, "stack", ordered, spacing=spacing, positions=distances)
    numbers = [i.numbers(INSTANCE_NUMBER) for i in instances]
    if any(n is None for n in numbers):
        return apart("its files have neither one orientation and a position, nor an instance number")
    values = [n[0] for n in numbers if n is not None]
    if len(set(values)) != len(values):
        return apart("its files repeat an instance number")
    return Series(uid, "stack", [instances[k] for k in sorted(range(len(values)), key=values.__getitem__)])


def _pyramid(uid: str, instances: list[Instance]) -> Series:
    """A whole-slide image's series: its VOLUME files as levels, largest first (§9.2
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/dicom.md#92-a-series>)."""
    ordered = sorted(instances, key=lambda i: (-math.prod(i.array["shape"]), i.key))
    volumes = [i for i in ordered if len(i.dataset.get(IMAGE_TYPE, {}).get("Value", [])) > 2
               and i.dataset[IMAGE_TYPE]["Value"][2] == "VOLUME"]

    def apart(reason: str) -> Series:
        return Series(uid, "instances", ordered, reason=reason)

    if not volumes:
        return apart("no file of the slide has the image type VOLUME")
    sizes = [tuple(i.array["shape"]) for i in volumes]
    if len(set(sizes)) != len(sizes):
        return apart("its VOLUME files repeat a size, as optical paths or focal planes do")
    if any(i.array["dimension_names"] != volumes[0].array["dimension_names"]
           or i.array["data_type"] != volumes[0].array["data_type"] for i in volumes):
        return apart("its VOLUME files differ in their axes or data type")
    others = [i for i in ordered if i not in volumes]
    return Series(uid, "pyramid", [*volumes, *others], levels=volumes)


def _series_of(uid: str, instances: list[Instance]) -> Series:
    if len(instances) == 1:
        return Series(uid, "single", instances, levels=instances)
    if all(i.text(SOP_CLASS) == WHOLE_SLIDE for i in instances):
        return _pyramid(uid, instances)
    return _stack(uid, instances)


def _chunks_per_axis(array: Doc) -> list[int]:
    grid = array["chunk_grid"]["configuration"]["chunk_shape"]
    return [-(-s // c) for s, c in zip(array["shape"], grid, strict=True)]


def _plane(series_path: str, array: Doc, instance: Instance, i: int) -> ReferenceStore:
    """The chunks of `instance`'s array as plane `i` of the stack's, the new axis after `c`."""
    counts = _chunks_per_axis(instance.array)
    at = 1 if array["dimension_names"][0] == "c" else 0

    def rename(key: str) -> str | None:
        if not key.startswith("0/c/"):
            return None
        coords = key[len("0/c/"):].split("/")
        return f"{series_path}/0/c/" + "/".join([*coords[:at], str(i), *coords[at:]])

    low = [0] * at + [i] + [0] * (len(counts) - at)
    high = [*counts[:at], i + 1, *counts[at:]]
    return map_keys(instance.store, rename, bounds=[ChunkBox(f"{series_path}/0", tuple(low), tuple(high))])


def _level(series_path: str, k: int, instance: Instance) -> ReferenceStore:
    """The file's array as level `k`, its document and chunks moved."""

    def rename(key: str) -> str | None:
        return f"{series_path}/{k}/{key[len('0/'):]}" if key.startswith("0/") else None

    return map_keys(instance.store, rename, bounds=[Prefix(f"{series_path}/{k}")])


def _stacked_array(first: Doc, count: int) -> Doc:
    """The array of a stack of `count` planes like `first`, the new axis `z` after `c`."""
    at = 1 if first["dimension_names"][0] == "c" else 0
    array = json.loads(json.dumps(first))
    array["shape"].insert(at, count)
    array["chunk_grid"]["configuration"]["chunk_shape"].insert(at, 1)
    array["dimension_names"].insert(at, "z")
    for codec in array["codecs"]:
        if codec["name"] == "transpose":
            # the samples last, the other axes in order (the array's axes are c, z, y, x)
            rank = len(array["shape"])
            codec["configuration"]["order"] = [*range(1, rank), 0]
    return array


def _stacked_ome(first: Doc, spacing: float | None) -> Doc:
    ome = json.loads(json.dumps(first))
    image = ome["multiscales"][0]
    at = 1 if image["axes"][0]["name"] == "c" else 0
    axis: Doc = {"name": "z", "type": "space"}
    if spacing is not None:
        axis["unit"] = "millimeter"
    image["axes"].insert(at, axis)
    image["datasets"][0]["coordinateTransformations"][0]["scale"].insert(at, 1 if spacing is None else spacing)
    return ome


def _levels_ome(levels: list[Instance]) -> Doc:
    """Multiscales of a slide's levels. Their scales are their own Pixel Spacing; a level without
    one (no unit on its axes) is scaled by its size against the first."""
    ome = json.loads(json.dumps(levels[0].ome))
    image = ome["multiscales"][0]
    names = levels[0].array["dimension_names"]
    datasets = []
    for k, level in enumerate(levels):
        if "unit" in image["axes"][-1]:
            scale = level.scale
        else:
            scale = [(a / b if name in ("y", "x") else 1)
                     for name, a, b in zip(names, levels[0].array["shape"], level.array["shape"], strict=True)]
        datasets.append({"path": str(k), "coordinateTransformations": [{"type": "scale", "scale": scale}]})
    image["datasets"] = datasets
    return ome


def _description(series: Series, path: str) -> Doc:
    """The series's own source metadata, a `DicomSeries` (§9.3
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/dicom.md#93-source-metadata>)."""
    own: Doc = {"series_instance_uid": series.uid, "kind": series.kind}
    if series.reason is not None:
        own["reason"] = series.reason
    if series.spacing is not None:
        own["spacing"] = series.spacing
    instances = []
    for j, instance in enumerate(series.instances):
        entry: Doc = {"key": instance.key, "path": f"{path}/instances/{j}"}
        uid = instance.text(SOP_INSTANCE)
        if uid is not None:
            entry["sop_instance_uid"] = uid
        if series.positions is not None:
            entry["position"] = series.positions[j]
        instances.append(entry)
    own["instances"] = instances
    return own


def _attributes(conventions: object, url: str, own: Doc, ome: Doc | None = None) -> Doc:
    attributes: Doc = {} if ome is None else {"ome": ome}
    attributes["zarr_conventions"] = conventions
    attributes["refkv_zarrified"] = {"profile": "dicom", "version": 0, "source": {"url": url}, "dicom": own}
    return attributes


def _group(attributes: Doc) -> bytes:
    document = {"zarr_format": 3, "node_type": "group", "attributes": attributes}
    return json.dumps(document, separators=(",", ":")).encode()


def _array_doc(array: Doc) -> bytes:
    return json.dumps(array, separators=(",", ":")).encode()


def _joined(series: Series, url: str) -> tuple[dict[str, bytes], list[ReferenceStore]]:
    """(the series's documents, the stores that hold its chunks and instances)."""
    path = series.uid
    first = series.instances[0]
    conventions = first.root["attributes"]["zarr_conventions"]
    parts: list[ReferenceStore] = [mount(i.store, f"{path}/instances/{j}") for j, i in enumerate(series.instances)]
    documents: dict[str, bytes] = {}
    own = _description(series, path)
    if series.kind == "stack":
        array = _stacked_array(first.array, len(series.instances))
        documents[f"{path}/0/zarr.json"] = _array_doc(array)
        ome = _stacked_ome(first.ome, series.spacing)
        documents[f"{path}/zarr.json"] = _group(_attributes(conventions, url, own, ome))
        parts += [_plane(path, array, instance, i) for i, instance in enumerate(series.instances)]
    elif series.kind in ("pyramid", "single"):
        documents[f"{path}/zarr.json"] = _group(_attributes(conventions, url, own, _levels_ome(series.levels)))
        parts += [_level(path, k, level) for k, level in enumerate(series.levels)]
    else:
        documents[f"{path}/zarr.json"] = _group(_attributes(conventions, url, own))
    return documents, parts


def _document(store: ReferenceStore, key: str) -> Doc:
    value = store.mapping[key]
    if not isinstance(value, bytes):
        raise TypeError(f"{key} is not held as bytes")
    return json.loads(value)


@dataclass
class SeriesOutput:
    """The hierarchy of a store of DICOM files, as `refkv.zarrify` hands on an output."""

    documents: dict[str, bytes]
    parts: list[ReferenceStore]
    summary: Doc

    def to_store(self, *, policy: Policy | None = None, info: Doc | None = None) -> ReferenceStore:
        bounds: list[Bound] = [Key(k) for k in self.documents]
        header = ReferenceStore(dict(self.documents), bounds=bounds)
        joined = union(header, *self.parts)
        options: Doc = {} if policy is None else {"policy": policy}
        return normalize(joined, info=info or {}, **options)


async def zarrify_series_async(keys: Sequence[str], others: Sequence[str], url: str,
                               read: Callable[[str], Awaitable[ReferenceStore]], *,
                               workers: int) -> SeriesOutput:
    """The store at `url` of the files `keys`, `others` its other objects' keys; `read` zarrifies
    the file at a key, up to `workers` at a time. A file the profile rejects is recorded with its
    reason, and the store is rejected only when none is left."""
    limit = asyncio.Semaphore(workers)

    async def one(key: str) -> Instance | tuple[str, str]:
        async with limit:
            try:
                store = await read(key)
            except Rejected as e:
                return key, str(e)
        root = _document(store, "zarr.json")
        dataset = mapping(root["attributes"]["refkv_zarrified"]["dicom"]["dataset"])
        return Instance(key, store, root, _document(store, "0/zarr.json"), dict(dataset))

    results = await asyncio.gather(*(one(k) for k in keys))
    instances = [r for r in results if isinstance(r, Instance)]
    rejected = [{"key": r[0], "reason": r[1]} for r in results if not isinstance(r, Instance)]
    if not instances:
        first = f": {rejected[0]['key']}: {rejected[0]['reason']}" if rejected else ""
        raise Rejected(f"no file of the store is a DICOM file the profile reads{first}")
    by_series: dict[str, list[Instance]] = {}
    for instance in instances:
        by_series.setdefault(instance.text(SERIES_UID) or "unknown", []).append(instance)
    all_series = [_series_of(uid, by_series[uid]) for uid in sorted(by_series)]
    documents: dict[str, bytes] = {}
    parts: list[ReferenceStore] = []
    for series in all_series:
        d, p = _joined(series, url)
        documents.update(d)
        parts += p
    listed = [{"series_instance_uid": s.uid, "path": s.uid, "kind": s.kind, "instances": len(s.instances)}
              for s in all_series]
    own: Doc = {"series": listed}
    if rejected:
        own["rejected"] = rejected
    if others:
        own["other_objects"] = list(others)
    conventions = instances[0].root["attributes"]["zarr_conventions"]
    documents["zarr.json"] = _group(_attributes(conventions, url, own))
    summary = {
        "series": [{"uid": s.uid, "kind": s.kind, "instances": len(s.instances)} for s in all_series],
        "rejected": len(rejected),
        "others": len(others),
    }
    return SeriesOutput(documents, parts, summary)


__all__ = ["SeriesOutput", "zarrify_series_async"]

