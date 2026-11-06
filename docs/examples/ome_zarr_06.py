"""An OME-Zarr 0.6 layout of TIFF and HDF5 files, as a refkv layout plugin (spec/layout.md).

A package registers the plugin in its pyproject.toml, under the layout's name:

    [project.entry-points."refkv.layouts"]
    "acme-ome.ome06" = "acme_ome:Ome06"

and its users ask for the layout by that name:

    from refkv.zarrify import zarrify
    from refkv.zarrify.layouts import Layout

    store = zarrify("image.ome.tif", plugins=[Layout("acme-ome.ome06")])
    store.mapping["refkv_layouts/ome06/zarr.json"]  # the OME-Zarr 0.6 image

A TIFF (and an NDPI, QPTIFF or IMS file) is already an OME-Zarr 0.5 image in refkv's own
layout, so the plugin copies its levels and writes 0.6 metadata over them. An HDF5 file's
groups and datasets are the file's own, and which of them are images is the file's convention's
business (NWB's, BigDataViewer's), so the options name them.
"""

import json
import math
import re
from collections.abc import Mapping
from typing import Any, ClassVar

from refkv import Concat, Range
from refkv.zarrify import Rejected
from refkv.zarrify.layouts import Base, ConventionInfo, copy

Entries = dict[str, bytes | Range | Concat]
IMAGES = ("tiff", "ndpi", "qptiff", "ims")  # the profiles whose image layout is OME-Zarr 0.5
AXIS_TYPES = ("time", "channel", "space")  # stricter than 0.6, which allows other types in the channel slot
IMAGE_MEMBERS = frozenset(["name", "array", "dims", "order", "axes", "scale"])


def axis(given: Mapping[str, Any]) -> dict[str, Any]:
    """An OME-Zarr 0.6 axis: its name and type, and its unit, long name and discreteness if given,
    each of its JSON type."""
    if set(given) - {"name", "type", "unit", "longName", "discrete"}:
        raise ValueError(f"an axis has name, type, unit, longName and discrete, not {sorted(given)}")
    if given.get("type") not in AXIS_TYPES:
        raise ValueError(f"an axis's type is time, channel or space, not {given.get('type')!r}")
    if not all(isinstance(given.get(m, ""), str) for m in ("name", "unit", "longName")) or not given.get("name") \
            or not isinstance(given.get("discrete", False), bool):
        raise ValueError(f"an axis's name, unit and longName are strings, and discrete a boolean: {dict(given)}")
    return dict(given)


def number(given: object) -> float:
    """A scale: a finite, positive number."""
    if isinstance(given, bool) or not isinstance(given, (int, float)) or not math.isfinite(given) or given <= 0:
        raise ValueError(f"a scale is a finite, positive number, not {given!r}")
    return float(given)


def group(attributes: Mapping[str, object]) -> bytes:
    """A Zarr v3 group's document."""
    return json.dumps({"zarr_format": 3, "node_type": "group", "attributes": attributes}).encode()


class Ome06:
    name = "acme-ome.ome06"
    path = "ome06"
    formats: ClassVar[dict[str, set[int]]] = {p: {0} for p in (*IMAGES, "hdf5")}
    convention = ConventionInfo(
        uuid="5f0c2a3e-8d1b-4c7e-9a64-2b1f0e9d7c55",
        version=0,
        schema_url="https://raw.githubusercontent.com/acme/acme-ome/refs/tags/v0/schema.json",
        spec_url="https://github.com/acme/acme-ome/blob/v0/spec.md",
        description="An OME-Zarr 0.6 image of a source zarrified by refkv",
    )

    def options(self, given: Mapping[str, Any]) -> dict[str, Any]:
        """The images of an HDF5 file, each one image group of the layout: its `name`, the
        `array` that holds it, the names of the array's dimensions in the file (`dims`), their
        `order` in the image (time, then channel, then space; default: `dims`), the image's
        `axes`, named as `order` names them, and a positive `scale` per axis (default 1)."""
        if set(given) - {"images"}:
            raise ValueError(f"the options hold images, not {sorted(set(given) - {'images'})}")
        images = []
        for image in given.get("images", []):
            if not isinstance(image, Mapping) or set(image) - IMAGE_MEMBERS or not {"name", "array", "dims",
                                                                                    "axes"} <= set(image):
                raise ValueError(f"an image holds name, array, dims, axes, and order and scale if given: {image!r}")
            name, array, dims = image["name"], image["array"], image["dims"]
            order = image.get("order", dims)
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*", name) \
                    or name == "zarr.json" or name.startswith("__"):
                raise ValueError(f"{name!r} is not an image's name, a key segment")
            if not isinstance(array, str) or not all(isinstance(d, str) for d in (*dims, *order)) \
                    or isinstance(dims, str) or isinstance(order, str):
                raise ValueError(f"{name}: array is a string, and dims and order lists of strings")
            axes = [axis(a) for a in image["axes"]]
            scale = [number(v) for v in image.get("scale", [1.0] * len(dims))]
            if len(set(dims)) != len(dims) or sorted(order) != sorted(dims) \
                    or [a["name"] for a in axes] != list(order) or len(scale) != len(dims):
                raise ValueError(f"{name}: order, axes and scale give each of dims {dims} once")
            types = [a["type"] for a in axes]
            if types != sorted(types, key=AXIS_TYPES.index) or not 2 <= len(types) <= 5 \
                    or not 2 <= types.count("space") <= 3 or types.count("time") > 1 or types.count("channel") > 1:
                raise ValueError(f"{name}: 2 to 5 axes: a time, then a channel, then 2 or 3 of space")
            if name in [i["name"] for i in images]:
                raise ValueError(f"two images are named {name}")
            images.append({"name": name, "array": array, "dims": list(dims), "order": list(order), "axes": axes,
                           "scale": scale})
        return {"images": images}

    def write(self, base: Base, options: Mapping[str, Any]) -> Entries:
        if base.profile == "hdf5":
            return self.write_hdf5(base, options)
        return self.write_image(base)

    def write_image(self, base: Base) -> Entries:
        """A 0.5 image's levels, copied, under 0.6 metadata: each level's scale and translation,
        the image's name and its omero channels kept."""
        ome = base.document("zarr.json")["attributes"]["ome"]
        ms = ome["multiscales"][0]
        out: Entries = {}
        datasets = []
        for level in ms["datasets"]:
            p = level["path"]
            out |= {f"{p}/{k}": v for k, v in copy(base, p).items()}
            steps = level["coordinateTransformations"]
            transform = steps[0] if len(steps) == 1 else {"type": "sequence", "transformations": steps}
            datasets.append({"path": p, "coordinateTransformations": [
                {**transform, "input": {"path": p}, "output": {"name": "physical"}}]})
        image = {"name": ms.get("name", "image"),
                 "coordinateSystems": [{"name": "physical", "axes": ms["axes"]}], "datasets": datasets}
        out_ome = {"version": "0.6", "multiscales": [image], **({"omero": ome["omero"]} if "omero" in ome else {})}
        out["zarr.json"] = group({"ome": out_ome})
        return out

    def write_hdf5(self, base: Base, options: Mapping[str, Any]) -> Entries:
        """The images the options name, each an array of the file transposed into the image's
        axis order, under its own 0.6 group."""
        images = options["images"]
        if not images:
            raise Rejected("an HDF5 file's images are named by the layout's options (images)")
        out: Entries = {"zarr.json": group({})}
        for image in images:
            name = image["name"]
            try:
                doc, _ = base.array(image["array"])
            except KeyError:
                raise Rejected(f"{image['array']} is not an array of the file") from None
            if len(doc["shape"]) != len(image["dims"]):
                raise Rejected(f"{image['array']} has {len(doc['shape'])} dimensions, not {len(image['dims'])}")
            order = [image["dims"].index(d) for d in image["order"]]
            out |= {f"{name}/0/{k}": v
                    for k, v in copy(base, image["array"], order=order, dimension_names=image["order"]).items()}
            scale = {"type": "scale", "scale": image["scale"], "input": {"path": "0"}, "output": {"name": "physical"}}
            ome = {"version": "0.6", "multiscales": [{
                "name": name, "coordinateSystems": [{"name": "physical", "axes": image["axes"]}],
                "datasets": [{"path": "0", "coordinateTransformations": [scale]}]}]}
            out[f"{name}/zarr.json"] = group({"ome": ome})
        return out
