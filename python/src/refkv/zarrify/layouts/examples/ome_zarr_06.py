"""`refkv.example.ome-zarr-06`: an OME-Zarr 0.6 draft image at the output's root, of a TIFF
(NDPI, QPTIFF, IMS) or of HDF5 datasets the options name (spec/layout.md
<https://github.com/d-v-b/refkv/blob/main/spec/layout.md>).

    from refkv.zarrify import zarrify
    from refkv.zarrify.layouts import Layout

    zarrify("image.ome.tif", layout=Layout("refkv.example.ome-zarr-06"))
    zarrify("ophys.nwb", layout=Layout("refkv.example.ome-zarr-06", {"images": [{
        "name": "movie", "array": "stimulus/templates/movie/data", "dims": ["t", "x", "y"],
        "order": ["t", "y", "x"], "axes": [{"name": "t", "type": "time", "unit": "second"},
        {"name": "y", "type": "space"}, {"name": "x", "type": "space"}], "scale": [0.033, 1, 1]}]}))

A TIFF's base is already an OME-Zarr 0.5 image, so its levels are copied to the same paths and
0.6 metadata written over them, each level's translation moved by half its pixel to the pixel's
center on the axes refkv places by a corner. An HDF5 file's images are its conventions' (NWB's,
BigDataViewer's), not HDF5's, so the options name them: each is copied into time, channel,
space order under a group of its own. With the option `source` (default true) the output keeps
the IR mirror, `refkv_source/`, which an OME-Zarr reader skips as a group it does not know.

Its output has no `check` problems: every array is a `copy`, every document is written by
`hierarchy.group` or `copy`, and the kept mirror is the base's, entry for entry.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any, ClassVar

from refkv.zarrify import Rejected
from refkv.zarrify.layouts import MIRROR, Base, Entries, EntryValue
from refkv.zarrify.layouts.hierarchy import copy, group, node_name

IMAGES = ("tiff", "ndpi", "qptiff", "ims")  # the profiles whose base is an OME-Zarr 0.5 image
AXIS_TYPES = ("time", "channel", "space")  # stricter than 0.6, which allows one axis of another type
AXIS_MEMBERS = frozenset(["name", "type", "unit", "longName", "discrete"])
IMAGE_MEMBERS = frozenset(["name", "array", "dims", "order", "axes", "scale"])
NUMERIC = re.compile(r"bool|u?int(8|16|32|64)|b?float(16|32|64)|complex(64|128)")  # what an image holds


def _axis(given: object, image: str) -> dict[str, Any]:
    """An OME-Zarr 0.6 axis: its name and type, and its unit, long name and discreteness if given."""
    if not isinstance(given, Mapping) or set(given) - AXIS_MEMBERS:
        raise ValueError(f"{image}: an axis is an object of {', '.join(sorted(AXIS_MEMBERS))}: {given!r}")
    if given.get("type") not in AXIS_TYPES:
        raise ValueError(f"{image}: an axis's type is time, channel or space, not {given.get('type')!r}")
    if not isinstance(given.get("name"), str) or not given["name"] \
            or not all(isinstance(given.get(m, ""), str) for m in ("unit", "longName")) \
            or not isinstance(given.get("discrete", False), bool):
        raise ValueError(f"{image}: an axis's name, unit and longName are strings, and discrete a boolean: "
                         f"{dict(given)}")
    return dict(given)


def _scale(given: object, image: str) -> float:
    if isinstance(given, bool) or not isinstance(given, (int, float)) or not math.isfinite(given) or given <= 0:
        raise ValueError(f"{image}: a scale is a finite, positive number, not {given!r}")
    return float(given)


def _image(given: object, source: bool) -> dict[str, Any]:
    """One image of the options in canonical form: `order` and `scale` filled in."""
    if not isinstance(given, Mapping) or set(given) - IMAGE_MEMBERS \
            or not {"name", "array", "dims", "axes"} <= set(given):
        raise ValueError(f"an image holds name, array, dims and axes, and order and scale if given: {given!r}")
    name, array, dims = given["name"], given["array"], given["dims"]
    node_name(name)  # one key segment, so that no image's group nests in another's
    if source and name == MIRROR:
        raise ValueError(f"{name!r} is not an image's name while the mirror is kept")
    if not isinstance(array, str) or not array.strip("/"):
        raise ValueError(f"{name}: array is the path of an HDF5 dataset, not {array!r}")
    order = given.get("order", dims)
    scale = given.get("scale", [1.0] * len(dims) if isinstance(dims, list) else None)
    if not all(isinstance(v, list) for v in (dims, order, given["axes"], scale)) \
            or not all(isinstance(d, str) for d in (*dims, *order)):
        raise ValueError(f"{name}: dims and order are lists of strings, axes and scale lists")
    axes = [_axis(a, name) for a in given["axes"]]
    if len(set(dims)) != len(dims) or sorted(order) != sorted(dims) or [a["name"] for a in axes] != order \
            or len(scale) != len(dims):
        raise ValueError(f"{name}: order, axes and scale give each of dims {dims} once, axes named as order")
    types = [a["type"] for a in axes]
    if types != sorted(types, key=AXIS_TYPES.index) or not 2 <= len(types) <= 5 \
            or not 2 <= types.count("space") <= 3 or types.count("time") > 1 or types.count("channel") > 1:
        raise ValueError(f"{name}: 2 to 5 axes, a time, then a channel, then 2 or 3 of space, not {types}")
    return {"name": name, "array": array.lstrip("/"), "dims": list(dims), "order": list(order), "axes": axes,
            "scale": [_scale(v, name) for v in scale]}


class OmeZarr06:
    """An OME-Zarr 0.6 draft image of a TIFF (NDPI, QPTIFF, IMS), or of an HDF5 file's datasets."""

    name = "refkv.example.ome-zarr-06"
    formats: ClassVar[dict[str, set[int]]] = {p: {0} for p in (*IMAGES, "hdf5")}

    def options(self, given: Mapping[str, Any]) -> dict[str, Any]:
        """`source`: whether to keep the IR mirror (default true). `images`, for an HDF5 file
        only: each image's `name` (its group's), the `array` that holds it, the names of the
        array's dimensions in the file (`dims`), their `order` in the image (time, channel,
        space; default `dims`), the image's `axes`, named as `order` names them, and a positive
        `scale` per axis, in `order`'s order (default 1). ValueError for options it cannot take."""
        if set(given) - {"source", "images"}:
            raise ValueError(f"the options are source and images, not {sorted(set(given) - {'source', 'images'})}")
        source = given.get("source", True)
        if not isinstance(source, bool):
            raise ValueError(f"source is true or false, not {source!r}")  # noqa: TRY004 (options() refuses with ValueError)
        out: dict[str, Any] = {"source": source}
        if "images" in given:
            if not isinstance(given["images"], list) or not given["images"]:
                raise ValueError("images is a list of at least one image")
            images = [_image(i, source) for i in given["images"]]
            names = [i["name"] for i in images]
            if len(set(names)) != len(names):
                raise ValueError(f"two images are named {next(n for n in names if names.count(n) > 1)!r}")
            out["images"] = images
        return out

    def write(self, base: Base, options: Mapping[str, Any]) -> Entries:
        out: dict[str, EntryValue] = base.entries(f"{MIRROR}/") if options["source"] else {}
        if base.profile == "hdf5":
            return out | self._datasets(base, options.get("images"))
        if "images" in options:
            raise Rejected(f"images name an HDF5 file's datasets; a {base.profile} file's image is its own")
        return out | self._image(base)

    def _image(self, base: Base) -> dict[str, EntryValue]:
        """The base's levels, copied, under 0.6 metadata. refkv's translation places a level's
        upper-left corner in y and x (spec/conventions.md §5
        <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#5-units>), and an IMS
        image's in z too, while an NDPI file's z is a plane's own position; 0.6 places a pixel's
        center, so each level moves by half its own pixel on the axes placed by a corner."""
        ome = base.document("zarr.json")["attributes"].get("ome")
        if not isinstance(ome, Mapping) or not ome.get("multiscales"):
            raise Rejected(f"the {base.profile} base's root holds no OME-Zarr multiscales")
        cornered = {"y", "x", *({"z"} if base.profile == "ims" else ())}
        out: dict[str, EntryValue] = {}
        images = []
        for ms in ome["multiscales"]:
            placed = [a.get("name") in cornered and a.get("type") == "space" for a in ms["axes"]]
            datasets = []
            for level in ms["datasets"]:
                p = level["path"]
                out |= copy(base, p, attributes=base.attributes(p), to=p)
                steps = level["coordinateTransformations"]
                scale = next(t["scale"] for t in steps if t["type"] == "scale")
                corner = next((t["translation"] for t in steps if t["type"] == "translation"), [0] * len(scale))
                center = [c + s / 2 if moved else c for c, s, moved in zip(corner, scale, placed, strict=True)]
                datasets.append({"path": p, "coordinateTransformations": [{
                    "type": "sequence", "input": {"path": p}, "output": {"name": "physical"},
                    "transformations": [{"type": "scale", "scale": scale},
                                        {"type": "translation", "translation": center}]}]})
            kept = {m: ms[m] for m in ("name", "type", "metadata") if m in ms}  # 0.5's members 0.6 keeps
            images.append({**kept, "coordinateSystems": [{"name": "physical", "axes": ms["axes"]}],
                           "datasets": datasets})
        attrs = {"version": "0.6", "multiscales": images, **({"omero": ome["omero"]} if "omero" in ome else {})}
        out["zarr.json"] = group({"ome": attrs})
        return out

    def _datasets(self, base: Base, images: list[dict[str, Any]] | None) -> dict[str, EntryValue]:
        """The images the options name, each its array transposed into the image's axis order, at
        `<name>/0`, under a 0.6 group `<name>`."""
        if images is None:
            raise Rejected("an HDF5 file's images are named by the layout's option images")
        out: dict[str, EntryValue] = {"zarr.json": group()}
        for image in images:
            name, path = image["name"], image["array"]
            try:
                doc, _ = base.array(path)
            except KeyError:
                raise Rejected(f"{name}: {path!r} is not an array of the base (refkv presents some datasets as "
                               "groups or not at all)") from None
            if len(doc["shape"]) != len(image["dims"]):
                raise Rejected(f"{name}: {path!r} has {len(doc['shape'])} dimensions, not {len(image['dims'])}")
            if not isinstance(doc["data_type"], str) or NUMERIC.fullmatch(doc["data_type"]) is None:
                raise Rejected(f"{name}: {path!r} holds {doc['data_type']!r}, not numbers")
            order = [image["dims"].index(d) for d in image["order"]]
            out |= copy(base, path, order=order, attributes=base.attributes(path), dimension_names=image["order"],
                        to=f"{name}/0")
            ome = {"version": "0.6", "multiscales": [{
                "name": name, "coordinateSystems": [{"name": "physical", "axes": image["axes"]}],
                "datasets": [{"path": "0", "coordinateTransformations": [{
                    "type": "scale", "scale": image["scale"], "input": {"path": "0"},
                    "output": {"name": "physical"}}]}]}]}
            out[f"{name}/zarr.json"] = group({"ome": ome})
        return out
