"""Writes spec/virtualize/<profile>/schema.json for each virtualization convention.

    uv run python spec/generate_schemas.py

Each schema validates the zarr.json of one node of a virtualized hierarchy that
declares the convention (spec/virtualize/<profile>.md): the convention's
metadata object in `zarr_conventions`, the property `refkv_virtualized`, and no
other attribute besides `ome`. The root carries the property's provenance
members; any other node (store profiles only) carries only the profile's
source-metadata member. Each profile's member schema is its module of spec/source_metadata/
(one per profile, so that adding a profile adds a module); the profiles are the registry's
(spec/virtualize/profiles/<name>.json).
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

from refkv.virtualize.common import CONVENTION_KEY, PROFILES, convention
from refkv.virtualize.profiles import REGISTRY

HERE = Path(__file__).parent
STORES = {e["name"] for e in REGISTRY if e["kind"] == "store"}

# Each profile's source-metadata schema (README "Source metadata"), and what it adds: its module of
# spec/source_metadata/, named for it.
sys.path.insert(0, str(HERE))
MODULES = {e["name"]: importlib.import_module(f"source_metadata.{e['name'].replace('-', '_')}") for e in REGISTRY}


# The IR mirror (spec/conventions.md §8
# <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#8-the-ir-mirror>) of every
# profile's convention (a dataset layout has none): refkv_source's source metadata, and the view
# documents of the groups under refkv_source/tree.
IR_PROFILES = tuple(e["name"] for e in REGISTRY if e["kind"] != "layout")
# The store conventions, whose refkv_source holds, beside the mirror's description, members of
# their own (`storeMirror`; spec/conventions.md §8.9
# <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#89-stores>).
IR_STORES = tuple(e["name"] for e in REGISTRY if e["kind"] in ("store", "both"))
MIRROR_DEFS = {
    "mirror": {
        "type": "object",
        "description": "refkv_source's: the IR mirror's description (spec/conventions.md §8.2 <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#82-the-table>)",
        "properties": {"ir": {
            "type": "object",
            "properties": {
                "version": {"const": 2},
                "size": {"type": "integer", "minimum": 0,
                         "description": "The source's size in bytes (a store IR's: its objects' sizes, summed)"},
                "objects": {"type": "integer", "minimum": 0,
                            "description": "A store IR's objects (spec/conventions.md §8.9 <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#89-stores>); a file IR has none"},
                "elements": {"type": "integer", "minimum": 1, "description": "The elements, column runs expanded"},
                "rows": {"type": "integer", "minimum": 1, "description": "The rows the table stores"},
                "names": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                          "prefixItems": [{"const": ""}],
                          "description": "The interned names, sorted; the first, \"\", is the root's (conventions §8.1 <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#81-elements>)"},
                "types": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                          "prefixItems": [{"const": ""}], "description": "The interned types (§8.6 <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#86-types>)"},
                "forms": {"type": "array", "items": {"type": "string"},
                          "description": "The interned forms, each a JSON text (§8.5 <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#85-forms-and-recipes>)"},
            },
            "required": ["version", "size", "elements", "rows", "names", "types", "forms"],
            "additionalProperties": False,
        }},
        "required": ["ir"],
        "additionalProperties": False,
    },
    "mirrorView": {
        "type": "object",
        "description": "A view group's (refkv_source/tree/...): a struct's view document (spec/conventions.md §8.7 <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#87-the-view>)",
        "properties": {
            "$vz": {"const": "derived", "description": "A derived element's document"},
            "$form": {"type": ["string", "null"], "description": "A derived element's form (§8.5 <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#85-forms-and-recipes>)"},
            "$partial": {"const": True, "description": "The document stops short of the struct's children"},
        },
        "minProperties": 1,
        "additionalProperties": {"$ref": "#/$defs/viewValue"},
    },
    "viewValue": {
        "description": "A child's view: a JSON value, a nested document, or a $vz form",
        "anyOf": [
            {"type": ["string", "number", "boolean", "null", "array"]},
            {"type": "object", "properties": {"$vz": {"enum": [
                "alias", "group", "element", "derived", "int", "float", "bytes", "utf16"]}},
             "required": ["$vz"]},
            {"type": "object"},
        ],
    },
}


# The store conventions' refkv_source (spec/conventions.md §8.9
# <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#89-stores>): the mirror's description
# (a store IR's, with its objects; a SAFE zip file's, a file IR's) and the members the convention
# keeps beside it, outside the IR, each only when it is not empty: a SAFE product's empty
# directories and the recorded ignored keys. A convention narrows it (Zarr v2's: a store IR's, no
# empty directories).
STORE_MIRROR_DEFS = {
    "storeMirror": {
        "type": "object",
        "description": "refkv_source's, for a store convention: the IR mirror's description (spec/conventions.md §8.2 <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#82-the-table>, §8.9 <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#89-stores>), and the members the convention keeps beside it",
        "properties": {
            "ir": {"$ref": "#/$defs/mirror/properties/ir"},
            "empty_dirs": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                           "description": "A SAFE product's empty directories (spec/virtualize/safe.md §6.3 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/safe.md#63-the-source-metadata-node>)"},
            "ignored": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                        "description": "The recorded keys of the listed objects that spec/virtualize.md "
                                       "§1.4 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize.md#14-store-inputs> ignores"},
        },
        "required": ["ir"],
        "additionalProperties": False,
    },
}

def schema(profile: str) -> dict:
    cmo = convention(profile)
    title = PROFILES[profile][2]
    module = MODULES[profile]
    url = getattr(module, "URL", None) or (
        {"pattern": "^[Hh][Tt][Tt][Pp][Ss]?://[^?#]*/$",
         "description": "The store URL: http or https, its path ending in /, no query or fragment"}
        if profile in STORES else {"description": "The URL of the source file"})
    root = {
        "type": "object",
        "description": "The root's property: the profile and version that produced the hierarchy, its source, "
                       "and the root's source metadata",
        "properties": {
            "profile": {"type": "string", "const": profile},
            "version": {"type": "integer", "const": PROFILES[profile][1]},
            "source": {"type": "object", "properties": {"url": {"type": "string", **url}}, "required": ["url"],
                       "additionalProperties": False},
            "layouts": {"type": "object", "minProperties": 1,
                        "additionalProperties": {"type": "string", "pattern": "^[^/]+(/[^/]+)*$"},
                        "description": "The other layouts of the source in this hierarchy, by name, each the path of its node below the root (spec/conventions.md §9 <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#9-dataset-layouts>)"},
            profile: {"$ref": "#/$defs/sourceMetadata"},
        },
        "required": ["profile", "version", "source"],
        "additionalProperties": False,
    }
    node = {
        "type": "object",
        "description": "Any other node's property: its source metadata only",
        "properties": {profile: {"$ref": "#/$defs/sourceMetadata"}},
        "required": [profile],
        "additionalProperties": False,
    }
    prop = {"oneOf": [{"$ref": "#/$defs/rootProperty"}, {"$ref": "#/$defs/nodeProperty"}]}
    defs = {
        "conventionMetadata": {
            "type": "object",
            "description": f"The {title} convention's metadata object in zarr_conventions",
            "properties": {k: {"type": "string", "const": v} for k, v in cmo.items()},
            "required": ["uuid"],
            "additionalProperties": False,
        },
        "rootProperty": root,
        "sourceMetadata": module.SCHEMA,
        "headerValue": {
            "description": "A translated header value (spec/conventions.md §6 <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#6-source-metadata-as-json>)",
            "anyOf": [{"type": "number"}, {"$ref": "#/$defs/text"},
                      {"type": "array", "items": {"anyOf": [{"type": "number"}, {"type": "string"}]}}],
        },
        "text": {
            "description": "A text value: a string (UTF-8), or its ISO 8859-1 reading when it is not UTF-8",
            "anyOf": [{"type": "string"}, {"type": "object", "properties": {"latin1": {"type": "string"}},
                                           "required": ["latin1"], "additionalProperties": False}],
        },
    }
    if profile in IR_PROFILES:
        defs.update(MIRROR_DEFS)
    if profile in IR_STORES:
        defs.update(STORE_MIRROR_DEFS)
    defs.update(getattr(module, "DEFS", {}))
    defs["nodeProperty"] = node
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": cmo["schema_url"],
        "title": f"refkv {title} convention, version {PROFILES[profile][1]}",
        "description": f"The zarr.json of a node that declares the refkv {title} convention ({cmo['spec_url']})",
        "type": "object",
        "properties": {
            "zarr_format": {"type": "integer", "const": 3},
            "node_type": {"type": "string", "enum": ["group", "array"]},
            "attributes": {
                "type": "object",
                "properties": {
                    "ome": {"type": "object", "description": "OME-NGFF 0.5 metadata"},
                    "zarr_conventions": {
                        "type": "array",
                        "contains": {"$ref": "#/$defs/conventionMetadata"},
                        "minContains": 1,
                        "maxContains": 1,
                    },
                    CONVENTION_KEY: prop,
                    **getattr(module, "ATTRIBUTES", {}),
                },
                "required": ["zarr_conventions", CONVENTION_KEY],
                "additionalProperties": False,
            },
        },
        "required": ["zarr_format", "node_type", "attributes"],
        "$defs": defs,
    }


def main() -> None:
    for profile in PROFILES:
        d = HERE / "virtualize" / profile
        d.mkdir(exist_ok=True)
        (d / "schema.json").write_text(json.dumps(schema(profile), indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {len(PROFILES)} schemas")


if __name__ == "__main__":
    main()
