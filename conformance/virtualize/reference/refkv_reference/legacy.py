"""The names the frozen profiles write: those of the revisions they implement (20 for
TIFF, ND2 and CZI, 25 for the profiles frozen later; compare.py's REFERENCE_REVISIONS),
which revision 27 changed. Their source metadata node is
`vzip_source`, their convention's property `vzip_virtualized`, and the convention
metadata names its URLs under d-v-b/vzip and calls the layout "virtualized by vzip".

The frozen profiles import what they share with the shipped code from here: this
module is `refkv.virtualize.common` and `refkv.virtualize.store`, but for those names
and the functions that write them (`convention`, `declare`, `root_json`, `emit_plans`,
`object_key`, `Output`, whose documents under `vzip_source/` are read only when asked
for, and `StoreOutput`, the store profiles' output, whose methods read the names
through their globals). A store twin also rejects a node at `refkv_source`, where the
shipped code keeps a store's other objects since revision 27, so that the corpus's
collision stores are rejected by every implementation. `current_names` reads
the old names as the current ones, for compare.py and the tests that compare the frozen
profiles with the Rust core (conformance/virtualize/HARNESS.md)."""

from __future__ import annotations

import types
from dataclasses import dataclass

from refkv.virtualize import common as current
from refkv.virtualize import store as _store
from refkv.virtualize.common import *  # noqa: F403
from refkv.virtualize.store import *  # noqa: F403
from refkv.virtualize.store import _NODE_NAME
from refkv.virtualize.common import PROFILES, metadata_array, metadata_group
from refkv.virtualize.common import Output as _Output
from refkv.virtualize.common import root_property

SOURCE_NODE = "vzip_source"
CONVENTION_KEY = "vzip_virtualized"
SOURCE_GROUP = SOURCE_NODE
OBJECTS = SOURCE_NODE + "/objects/"
EMPTY_KEY = SOURCE_NODE + "/empty.json"
_PROFILE_OF_UUID = {uuid: p for p, (uuid, _, _) in PROFILES.items()}


@dataclass
class Output(_Output):
    lazy: tuple[str, ...] = (SOURCE_NODE + "/",)


def convention(profile: str) -> dict:
    uuid, version, title = PROFILES[profile]
    ref, blob = ("heads/main", "main") if version == 0 else (f"tags/virtualize-{profile}-v{version}",) * 2
    return {
        "uuid": uuid,
        "schema_url": f"https://raw.githubusercontent.com/d-v-b/vzip/refs/{ref}/spec/virtualize/{profile}/schema.json",
        "spec_url": f"https://github.com/d-v-b/vzip/blob/{blob}/spec/virtualize/{profile}.md",
        "name": CONVENTION_KEY,
        "description": f"The Zarr layout of a {title} source virtualized by vzip, and the source's metadata",
    }


def declare(attributes: dict, profile: str, url: str | None, own: dict | None = None,
            revision: int | None = None) -> dict:
    value: dict = {}
    if url is not None:
        value = root_property(profile, url, revision)
    if own:
        value[profile] = own
    if not value:
        return dict(attributes)
    return {**attributes, "zarr_conventions": [convention(profile)], CONVENTION_KEY: value}


def root_json(ome: dict, profile: str, url: str, own: dict | None = None, revision: int | None = None) -> dict:
    return {"zarr_format": 3, "node_type": "group",
            "attributes": declare({"ome": ome}, profile, url, own, revision)}


def object_key(key: str) -> str:
    return OBJECTS + key + ("~" if _NODE_NAME.match(key.rpartition("/")[2]) else "")


def emit_plans(out: Output, plans: list, node_attributes: dict | None) -> None:
    metadata_group(out, SOURCE_NODE, node_attributes)
    groups = sorted({"/".join(a.path.split("/")[:k]) for a in plans for k in range(1, a.path.count("/") + 1)})
    for g in groups:
        metadata_group(out, f"{SOURCE_NODE}/{g}")
    for a in plans:
        metadata_array(out, f"{SOURCE_NODE}/{a.path}", a.data_type, a.shape, a.chunk_shape, a.dims, a.chunks,
                       a.fill, a.attributes, a.endian, a.compressor)


def current_names(value, _inside: bool = False):
    """A JSON value of a frozen profile's output with the names the shipped code writes:
    the convention's property `refkv_virtualized`, its convention metadata the current
    one, and, within the property (where the profiles name them), its paths into the
    source metadata node under `refkv_source`. The source's own values, outside the
    property, are left as they are; within it, a value copied from the source that
    starts with `vzip_source/` would read as such a path too, which no fixture has."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k == "zarr_conventions" and isinstance(v, list):
                v = [current.convention(_PROFILE_OF_UUID[c["uuid"]]) if isinstance(c, dict)
                     and c.get("name") == CONVENTION_KEY and c.get("uuid") in _PROFILE_OF_UUID
                     else current_names(c, _inside) for c in v]
            out[current.CONVENTION_KEY if k == CONVENTION_KEY else k] = current_names(
                v, _inside or k == CONVENTION_KEY)
        return out
    if isinstance(value, list):
        return [current_names(v, _inside) for v in value]
    if _inside and isinstance(value, str) and value.startswith(SOURCE_NODE + "/"):
        return current.SOURCE_NODE + value[len(SOURCE_NODE):]
    return value


def _with_names(f):
    """`f`, a function of refkv.virtualize.store, reading this module's names."""
    names = dict(f.__globals__, SOURCE_GROUP=SOURCE_GROUP, OBJECTS=OBJECTS, EMPTY_KEY=EMPTY_KEY,
                 CONVENTION_KEY=CONVENTION_KEY, declare=declare, object_key=object_key)
    return types.FunctionType(f.__code__, names, f.__name__, f.__defaults__, f.__closure__)


@dataclass
class StoreOutput(_store.StoreOutput):
    declare = _with_names(_store.StoreOutput.declare)
    write = _with_names(_store.StoreOutput.write)
    to_store = _with_names(_store.StoreOutput.to_store)
    _keep_objects = _with_names(_store.StoreOutput.keep_objects)

    def keep_objects(self, objects: dict[str, int], used: set[str], nodes, ignored=()) -> int:
        nodes = list(nodes)
        if any(k not in used for k in objects) or ignored:
            if self.docs.get(_store.doc_key(""), {}).get("node_type") != "array":
                for p in nodes:
                    if p == _store.SOURCE_GROUP or p.startswith(_store.SOURCE_GROUP + "/"):
                        raise Rejected(f"the node {p} is where the store's other objects go "
                                       f"({_store.OBJECTS}...)")
        return self._keep_objects(objects, used, nodes, ignored)
