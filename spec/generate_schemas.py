"""Writes spec/virtualize/<profile>/schema.json for each virtualization convention.

    uv run python spec/generate_schemas.py

Each schema validates the zarr.json of one node of a virtualized hierarchy that
declares the convention (spec/virtualize/<profile>.md): the convention's
metadata object in `zarr_conventions`, the property `refkv_virtualized`, and no
other attribute besides `ome`. The root carries the property's provenance
members; any other node (store profiles only) carries only the profile's
source-metadata member. SOURCE_METADATA holds each profile's member schema, by
name; the profiles are the registry's (spec/virtualize/profiles.json).
"""

from __future__ import annotations

import json
from pathlib import Path

from refkv.virtualize.common import CONVENTION_KEY, PROFILES, convention
from refkv.virtualize.profiles import REGISTRY

HERE = Path(__file__).parent
STORES = {e["name"] for e in REGISTRY if e["kind"] == "store"}

# The schema of each profile's source-metadata member (README "Source metadata").
SOURCE_METADATA: dict[str, dict] = {
    "tiff": {"anyOf": [
        {
            "type": "object",
            "description": "The root's: the file's byte order and variant",
            "properties": {"byte_order": {"enum": ["little", "big"]}, "bigtiff": {"type": "boolean"}},
            "required": ["byte_order", "bigtiff"],
            "additionalProperties": False,
        },
        {"$ref": "#/$defs/mirror"},
        {"$ref": "#/$defs/mirrorView"},
    ]},
    "ndpi": {"anyOf": [{"$ref": "#/$defs/mirror"}, {"$ref": "#/$defs/mirrorView"}]},
    "qptiff": {"anyOf": [
        {
            "type": "object",
            "description": "The root's: the file's byte order and variant (spec/virtualize/qptiff.md §5 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/qptiff.md#5-source-metadata>)",
            "properties": {"byte_order": {"enum": ["little", "big"]}, "bigtiff": {"type": "boolean"}},
            "required": ["byte_order", "bigtiff"],
            "additionalProperties": False,
        },
        {"$ref": "#/$defs/mirror"},
        {"$ref": "#/$defs/mirrorView"},
    ]},
    "nd2": {"anyOf": [
        {
            "type": "object",
            "description": "The root's: the signature and the decoded chunks it keeps (spec/virtualize/nd2.md §5 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/nd2.md#5-source-metadata>)",
            "properties": {
                "signature": {"$ref": "#/$defs/text", "description": "The signature chunk's data up to its first NUL"},
                "chunks": {
                    "type": "object",
                    "description": "Each decoded chunk (lite variant or XML variant) the root keeps, by name, as a "
                                   "JSON tree: an object, or [name, value] pairs when a name repeats, when a name is "
                                   "an array index (0, 1, ... without leading zeros), or when the object would read "
                                   "as a tag ({\"utf16\": B}, {\"int\": digits}, {\"float\": "
                                   "\"NaN\"|\"Infinity\"|\"-Infinity\"|\"-0\"}); at most 65536 bytes of JSON in all",
                    "additionalProperties": {"type": ["object", "array"]},
                },
            },
            "required": ["signature", "chunks"],
            "additionalProperties": False,
        },
        {"$ref": "#/$defs/mirror"},
        {"$ref": "#/$defs/mirrorView"},
    ]},
    "dicom": {"anyOf": [{
        "type": "object",
        "description": "The root's (spec/virtualize/dicom.md §5.1 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/dicom.md#51-the-root>); a path refkv_source/<p> names the IR's element at <p>",
        "properties": {
            "preamble": {"type": "string", "contentEncoding": "base64",
                         "description": "The 128-byte preamble, unless all zero"},
            "meta": {"$ref": "#/$defs/dataset",
                     "description": "The File Meta Information, without its large attributes (in the mirror only)"},
            "dataset": {"$ref": "#/$defs/dataset",
                        "description": "The dataset, without the per-frame groups and the large attributes (in the mirror only)"},
            "pixel_extra": {"type": "string",
                            "description": "refkv_source/pixel_extra: the IR's element holding the native pixel data after the frames"},
            "pixel_unreferenced": {"type": "string",
                                   "description": "refkv_source/pixel_unreferenced: the IR's struct holding the pixel data the Extended Offset Table skips"},
            "pixel_unreferenced_headers": {"const": True,
                                           "description": "Whether pixel_unreferenced also holds the frames' item "
                                                          "headers"},
            "pixel_fragments": {"type": "string",
                                "description": "refkv_source/pixel_fragments, when a frame has more than one fragment "
                                               "(the fragments are the IR's dataset/7FE00010/fragments/<j>)"},
            "pixel_offset_table": {"const": True, "description": "Whether the Basic Offset Table is not empty"},
            "trailing": {"type": "string", "description": "refkv_source/trailing: the IR's element holding the bytes after the last element"},
        },
        "additionalProperties": False,
    },
        {"$ref": "#/$defs/mirror"},
        {"$ref": "#/$defs/mirrorView"},
    ]},
    "nifti": {"anyOf": [{
        "type": "object",
        "description": "The root's: the version, the byte order, the header, and what the layout derives from it (spec/virtualize/nifti.md §5 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/nifti.md#5-source-metadata>)",
        "properties": {
            "nifti_version": {"enum": [1, 2]},
            "byte_order": {"enum": ["little", "big"]},
            "header": {
                "type": "object",
                "description": "Every header field, by its NIfTI-1 or NIfTI-2 name, in the standard's order",
                "additionalProperties": {"anyOf": [
                    {"$ref": "#/$defs/headerValue"},
                    {"type": "object", "properties": {"bits": {"type": "string", "pattern": "^([0-9a-f]{8}|[0-9a-f]{16})$"}},
                    "required": ["bits"], "additionalProperties": False,
                    "description": "A float's IEEE 754 bits, most significant first: a negative zero, or a NaN "
                                   "other than the canonical one"},
                    {"type": "array", "items": {"anyOf": [{"type": "number"}, {"type": "string"},
                                                          {"type": "object", "properties": {"bits": {"type": "string", "pattern": "^([0-9a-f]{8}|[0-9a-f]{16})$"}},
                                                          "required": ["bits"], "additionalProperties": False,
                                                          "description": "A float's IEEE 754 bits, most significant first: a negative zero, or a NaN "
                                                                         "other than the canonical one"}]}},
                ]},
            },
            "affine": {
                "type": "object",
                "properties": {"form": {"enum": ["sform", "qform"]}, "applied": {"type": "boolean"}},
                "required": ["form", "applied"],
                "additionalProperties": False,
            },
            "scaling": {
                "type": "object",
                "properties": {"slope": {"type": "number"}, "inter": {"type": "number"}},
                "required": ["slope", "inter"],
                "additionalProperties": False,
            },
        },
        "required": ["nifti_version", "byte_order", "header"],
        "additionalProperties": False,
    },
        {"$ref": "#/$defs/mirror"},
        {"$ref": "#/$defs/mirrorView"},
    ]},
    "mrc": {"anyOf": [{
        "type": "object",
        "description": "The root's: the byte order, the kind of file, the data type, the header, and what the layout derives from it (spec/virtualize/mrc.md §5 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mrc.md#5-source-metadata>)",
        "properties": {
            "byte_order": {"enum": ["little", "big"]},
            "kind": {"enum": ["image", "image_stack", "volume", "volume_stack"]},
            "data_type": {"enum": ["int8", "uint8", "int16", "float32", "complex64", "uint16", "float16"]},
            "header": {
                "type": "object",
                "description": "Every header field, by its MRC2014 name, in the standard's order; the extra space as base64, the labels as an array of ten texts",
                "additionalProperties": {"anyOf": [
                    {"$ref": "#/$defs/headerValue"},
                    {"type": "object", "properties": {"bits": {"type": "string", "pattern": "^[0-9a-f]{8}$"}},
                     "required": ["bits"], "additionalProperties": False,
                     "description": "A float's IEEE 754 bits, most significant first: a negative zero, or a NaN "
                                    "other than the canonical one"},
                    {"type": "array", "items": {"anyOf": [{"type": "number"}, {"$ref": "#/$defs/text"},
                                                          {"type": "object", "properties": {"bits": {"type": "string", "pattern": "^[0-9a-f]{8}$"}},
                                                           "required": ["bits"], "additionalProperties": False,
                                                           "description": "A float's IEEE 754 bits, most significant first: a negative zero, or a NaN "
                                                                          "other than the canonical one"}]}},
                ]},
            },
            "axis_map": {
                "type": "object",
                "description": "Whether mapc, mapr and maps named the axes",
                "properties": {"applied": {"type": "boolean"}},
                "required": ["applied"],
                "additionalProperties": False,
            },
            "voxel_size": {
                "type": "object",
                "description": "The voxel size in angstroms, when the cell and grid give one along every axis",
                "properties": {"x": {"type": "number"}, "y": {"type": "number"}, "z": {"type": "number"}},
                "required": ["x", "y", "z"],
                "additionalProperties": False,
            },
        },
        "required": ["byte_order", "kind", "data_type", "header", "axis_map"],
        "additionalProperties": False,
    },
        {"$ref": "#/$defs/mirror"},
        {"$ref": "#/$defs/mirrorView"},
    ]},
    "ims": {"anyOf": [
        {"$ref": "#/$defs/mirror"},
        {"$ref": "#/$defs/mirrorView"},
    ]},
    "n5": {"anyOf": [
        {
            "type": "object",
            "description": "A node's (spec/virtualize/n5.md §5 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/n5.md#5-source-metadata>)",
            "properties": {
                "attributes": {"type": "object", "minProperties": 1,
                               "description": "The node's attributes.json without the layout members"},
                "metadata": {
                    "type": "object", "minProperties": 1,
                    "description": "The layout members the Zarr metadata does not reproduce",
                    "properties": {"n5": {"description": "The member n5 (the container's version)"},
                                   "compression": {"type": "object", "minProperties": 1,
                                                   "description": "The compression's members the codec does not carry"},
                                   "compressionType": {"description": "compressionType, when compression is present"}},
                    "additionalProperties": False,
                },
            },
            "minProperties": 1,
            "additionalProperties": False,
        },
        # refkv_source's: a store IR's description, and the recorded ignored keys (§5.1
        # <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/n5.md#51-the-ir-mirror>)
        {"allOf": [{"$ref": "#/$defs/storeMirror"},
                   {"properties": {"ir": {"required": ["objects"]}, "empty_dirs": False}}]},
        {"$ref": "#/$defs/mirrorView"},
    ]},
    "zarr2": {"anyOf": [
        {
            "type": "object",
            "description": "A node's (spec/virtualize/zarr2.md §4 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/zarr2.md#4-source-metadata>)",
            "properties": {
                "attributes": {"type": "object", "minProperties": 1, "description": "The node's .zattrs, as copied"},
                "metadata": {"type": "object", "minProperties": 1,
                             "description": "The members of .zarray or .zgroup the Zarr metadata does not reproduce"},
            },
            "minProperties": 1,
            "additionalProperties": False,
        },
        # refkv_source's: a store IR's description, and the recorded ignored keys (§5
        # <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/zarr2.md#5-other-objects>)
        {"allOf": [{"$ref": "#/$defs/storeMirror"},
                   {"properties": {"ir": {"required": ["objects"]}, "empty_dirs": False}}]},
        {"$ref": "#/$defs/mirrorView"},
    ]},
    "ome-zarr": {"anyOf": [
        {
            "type": "object",
            "description": "A node's (spec/virtualize/ome-zarr.md §8 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/ome-zarr.md#8-source-metadata>)",
            "properties": {
                "attributes": {"type": "object", "minProperties": 1,
                               "description": "The node's .zattrs, without the OME members the ome object gives back"},
                "unversioned": {"type": "array", "minItems": 1, "uniqueItems": True,
                                "items": {"enum": ["multiscales", "omero", "image-label", "plate", "well"]},
                                "description": "The OME members that had no version, which ome gives back as they are"},
                "metadata": {"type": "object", "minProperties": 1,
                             "description": "The members of .zarray or .zgroup the Zarr metadata does not reproduce"},
            },
            "minProperties": 1,
            "additionalProperties": False,
        },
        # refkv_source's: the Zarr v2 store's IR, as zarr2's (spec/virtualize/ome-zarr.md §8.2
        # <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/ome-zarr.md#82-the-store-ir>)
        {"allOf": [{"$ref": "#/$defs/storeMirror"},
                   {"properties": {"ir": {"required": ["objects"]}, "empty_dirs": False}}]},
        {"$ref": "#/$defs/mirrorView"},
    ]},
}

# A dataset in the DICOM JSON Model (PS3.18 §F.2), as spec/virtualize/dicom.md §5
# <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/dicom.md#5-source-metadata> writes it.
DICOM_DEFS = {
    "dataset": {
        "type": "object",
        "properties": {
            "duplicates": {
                "type": "array",
                "description": "The later elements with a tag already in the dataset, in file order",
                "items": {
                    "type": "object",
                    "patternProperties": {"^[0-9A-F]{8}$": {"$ref": "#/$defs/attribute"}},
                    "additionalProperties": False,
                    "minProperties": 1,
                    "maxProperties": 1,
                },
            },
        },
        "patternProperties": {"^[0-9A-F]{8}$": {"$ref": "#/$defs/attribute"}},
        "additionalProperties": False,
    },
    "attribute": {
        "type": "object",
        "properties": {
            "vr": {"type": "string", "pattern": "^[A-Z]{2}$"},
            "Value": {"type": "array"},
            "InlineBinary": {"type": "string", "contentEncoding": "base64"},
            "BulkDataURI": {"type": "string",
                            "description": "refkv_source/<path>: the IR's element at <path>, whose value it is"},
            "Structures": {"type": "array", "items": {"$ref": "#/$defs/dataset"},
                           "description": "A gathered sequence's distinct item structures (its items are the IR's "
                                          "elements <its path>/<index>)"},
            "Gathered": {"const": True,
                         "description": "A value in an item of a gathered sequence: the IR's element <the "
                                        "sequence's path>/<item index>/<path within the item>"},
            "LittleEndian": {"const": True,
                             "description": "An explicit UN value in explicit VR big endian, whose bytes are in "
                                            "little endian"},
        },
        "required": ["vr"],
        "maxProperties": 3,
        "additionalProperties": False,
    },
}

_MEASURE = {"anyOf": [{"type": ["number", "string"]}, {
    "type": "object", "properties": {"value": {"type": ["number", "string"]}, "unit": {"type": "string"}},
    "required": ["value", "unit"], "additionalProperties": False}]}
_VALUE = {"type": ["number", "string"]}
_RECORDS = {"type": "array", "items": {"type": "object", "additionalProperties": _VALUE}, "minItems": 1}
SOURCE_METADATA["safe"] = {"oneOf": [
    {
        "type": "object",
        "description": "The root's: the product's identity, from the product and tile metadata (§6.3 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/safe.md#63-the-source-metadata-node>)",
        "properties": {
            **{k: {"type": "string"} for k in ("PRODUCT_URI", "PROCESSING_LEVEL", "PRODUCT_TYPE", "PROCESSING_BASELINE",
                                               "TILE_ID", "SENSING_TIME", "HORIZONTAL_CS_NAME", "HORIZONTAL_CS_CODE")},
            "Special_Values": _RECORDS,
            "U": _VALUE,
        },
        "required": ["HORIZONTAL_CS_CODE"],
        "additionalProperties": False,
    },
    {
        "type": "object",
        "description": "A band array's: a derived convenience copied from the product metadata (§6.2 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/safe.md#62-band-arrays>), and the band "
                       "file's SIZ segment",
        "properties": {
            "IMAGE_FILE": {"type": "string"},
            "bandId": {"type": "integer", "minimum": 0},
            "physicalBand": {"type": "string"},
            "RESOLUTION": _VALUE,
            "Wavelength": {"type": "object", "properties": {k: _MEASURE for k in ("MIN", "MAX", "CENTRAL")},
                           "additionalProperties": False, "minProperties": 1},
            **{k: _VALUE for k in ("PHYSICAL_GAINS", "RADIO_ADD_OFFSET", "BOA_ADD_OFFSET")},
            **{k: _MEASURE for k in ("SOLAR_IRRADIANCE", "QUANTIFICATION_VALUE", "BOA_QUANTIFICATION_VALUE",
                                     "AOT_QUANTIFICATION_VALUE", "WVP_QUANTIFICATION_VALUE")},
            "Scene_Classification_List": _RECORDS,
            "siz": {"type": "string", "contentEncoding": "base64",
                    "description": "The band file's SIZ marker segment, which its chunks replace"},
        },
        "required": ["IMAGE_FILE", "siz"],
        "additionalProperties": False,
    },
    # refkv_source's: the IR mirror's description, the empty directories and the keys of the
    # ignored objects (§6.3 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/safe.md#63-the-source-metadata-node>)
    {"$ref": "#/$defs/storeMirror"},
    # a view document, without the members the branches above require (or `refkv_source`'s of
    # revision 25), which name no SAFE IR element (`siz` and `xml` do: they are not excluded), so
    # that a document of those branches with a wrong member is invalid (one without any of them,
    # such as band metadata without IMAGE_FILE, reads as a view document: member names alone
    # cannot tell those apart)
    {"allOf": [{"$ref": "#/$defs/mirrorView"}, {"not": {"anyOf": [
        {"required": [m]} for m in ("ir", "IMAGE_FILE", "HORIZONTAL_CS_CODE", "empty_dirs", "ignored",
                                     "xml_arrays", "empty")]}}]},
]}
_GUID = {"type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"}
_DIMENSIONS = {"type": "object", "description": "The Start of each of S, B, H, I, R, V the series key has",
               "properties": {k: {"type": "integer"} for k in "SBHIRV"}, "minProperties": 1,
               "additionalProperties": False}
_ATTACHMENT = {"oneOf": [
    {
        "type": "object",
        "description": "An A1 attachment entry, and the form of its data at attachments/<k> (spec/virtualize/czi.md §5.3 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/czi.md#53-elements>)",
        "properties": {
            "name": {"$ref": "#/$defs/text"},
            "content_file_type": {"$ref": "#/$defs/text"},
            "content_guid": _GUID,
            "form": {"enum": ["time_stamps", "focus_positions", "event_list", "bytes", "empty"]},
            "segment_entry": {"type": "string", "contentEncoding": "base64",
                              "description": "The attachment segment's copy of the entry, when it differs"},
        },
        "required": ["name", "content_file_type", "content_guid", "form"],
        "additionalProperties": False,
    },
    {
        "type": "object",
        "description": "An entry of another schema: its 128 bytes",
        "properties": {"entry": {"type": "string", "contentEncoding": "base64"}},
        "required": ["entry"],
        "additionalProperties": False,
    },
]}
SOURCE_METADATA["czi"] = {"anyOf": [
    {
        "type": "object",
        "description": "The root's: the file header (spec/virtualize/czi.md §5.1 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/czi.md#51-the-root>)",
        "properties": {
            "version": {"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2},
            "primary_file_guid": _GUID,
            "file_guid": _GUID,
            "file_part": {"const": 0},
            "update_pending": {"type": "integer"},
        },
        "required": ["version", "primary_file_guid", "file_guid", "file_part", "update_pending"],
        "additionalProperties": False,
    },
    {
        "type": "object",
        "description": "An image's: the dimensions that select its series (spec/virtualize/czi.md §4.2 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/czi.md#42-images>)",
        "properties": {"dimensions": _DIMENSIONS},
        "required": ["dimensions"],
        "additionalProperties": False,
    },
    {
        "type": "object",
        "description": "A tile array's: its tile position, the planes at its index 0, and its copy (spec/virtualize/czi.md §4.4 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/czi.md#44-tiles>)",
        "properties": {
            "dimensions": _DIMENSIONS,
            "x": {"type": "integer"},
            "y": {"type": "integer"},
            "size": {"type": "array", "items": {"type": "integer", "minimum": 1}, "minItems": 2, "maxItems": 2},
            "stored_size": {"type": "array", "items": {"type": "integer", "minimum": 1}, "minItems": 2,
                            "maxItems": 2},
            "planes": {"type": "object", "properties": {k: {"type": "integer"} for k in "tcz"},
                       "required": ["t", "c", "z"], "additionalProperties": False},
            "copy": {"type": "integer", "minimum": 0},
        },
        "required": ["x", "y", "size", "stored_size", "planes", "copy"],
        "additionalProperties": False,
    },
    {"$ref": "#/$defs/mirror"},
    {"$ref": "#/$defs/mirrorView"},
]}
# A VSI slide (spec/virtualize/vsi.md §6 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/vsi.md#6-source-metadata>):
# the root's file, stacks and metadata tree; each image's stack properties and its tile file's
# headers or its IFD; the macro image's IFD; and the mirror's.
_VSI_PAIR = {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2}
_VSI_NULLABLE_PAIR = {"anyOf": [_VSI_PAIR, {"type": "null"}]}
_VSI_FIELD = {
    "type": "object",
    "description": "A field of the metadata tree (spec/virtualize/vsi.md §6.1 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/vsi.md#61-the-root>)",
    "properties": {
        "tag": {"type": "integer", "minimum": 0},
        "tag2": {"type": "integer", "minimum": 0},
        "type": {"type": "integer", "minimum": 0},
        "name": {"type": "string"},
        "inline": {"const": True},
        "value": {},
        "fields": {"type": "array", "items": {"$ref": "#/$defs/vsiField"}},
        "ifd": {"type": "object", "properties": {"index": {"type": "integer", "minimum": 0}, "bytes": {"type": "integer", "minimum": 0}},
                "required": ["bytes"], "additionalProperties": False},
    },
    "required": ["tag", "type"],
    "additionalProperties": False,
}
_VSI_IFD = {
    "type": "object",
    "description": "An IFD image's IFD (spec/virtualize/vsi.md §4.3 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/vsi.md#43-the-macro-image>)",
    "properties": {k: {"type": "integer", "minimum": 0} for k in ("index", "width", "height", "samples", "bits", "compression", "photometric")},
    "required": ["index", "width", "height", "samples", "bits", "compression", "photometric"],
    "additionalProperties": False,
}
SOURCE_METADATA["vsi"] = {"anyOf": [
    {
        "type": "object",
        "description": "The root's: the .vsi file, the stacks and the metadata tree (spec/virtualize/vsi.md §6.1 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/vsi.md#61-the-root>)",
        "properties": {
            "file": {"type": "object",
                     "properties": {"key": {"type": "string"}, "size": {"type": "integer", "minimum": 32}, "ifds": {"type": "integer", "minimum": 0}},
                     "required": ["key", "size", "ifds"], "additionalProperties": False},
            "stacks": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "minimum": 0},
                    "name": {"type": "string"},
                    "type": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                    "external": {"type": "boolean"},
                    "width": {"anyOf": [{"type": "integer", "minimum": 0}, {"type": "null"}]},
                    "height": {"anyOf": [{"type": "integer", "minimum": 0}, {"type": "null"}]},
                    "tile_file": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                },
                "required": ["id", "name", "type", "external", "width", "height", "tile_file"],
                "additionalProperties": False,
            }},
            "tree": {"type": "object", "properties": {"fields": {"type": "array", "items": {"$ref": "#/$defs/vsiField"}}},
                     "required": ["fields"], "additionalProperties": False},
        },
        "required": ["file", "stacks", "tree"],
        "additionalProperties": False,
    },
    {
        "type": "object",
        "description": "A stack's image's: its properties from the tree, then its tile file's headers and levels, or its IFD (spec/virtualize/vsi.md §6.1 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/vsi.md#61-the-root>)",
        "properties": {
            "id": {"type": "integer", "minimum": 0},
            "type": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
            "type_name": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "width": {"anyOf": [{"type": "integer", "minimum": 0}, {"type": "null"}]},
            "height": {"anyOf": [{"type": "integer", "minimum": 0}, {"type": "null"}]},
            "dimension_sizes": {"type": "array", "items": {"type": "integer"}},
            "dimensions": {"type": "array", "items": {
                "type": "object", "properties": {"coordinate": {"type": "integer", "minimum": 2}, "meaning": {"type": "string"}},
                "required": ["coordinate", "meaning"], "additionalProperties": False}},
            "scale": _VSI_NULLABLE_PAIR,
            "unit": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "origin": _VSI_NULLABLE_PAIR,
            "z_step": {"anyOf": [{"type": "number"}, {"type": "null"}]},
            "z_unit": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "display": _VSI_NULLABLE_PAIR,
            "channels": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "name": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    "channel_name": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    "color": {"anyOf": [{"type": "string", "pattern": "^[0-9A-F]{6}$"}, {"type": "null"}]},
                    "display": _VSI_NULLABLE_PAIR,
                    "wavelength": {"anyOf": [{"type": "number"}, {"type": "null"}]},
                },
                "required": ["name", "channel_name", "color", "display", "wavelength"],
                "additionalProperties": False,
            }},
            "creation_time": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
            "magnification": {"anyOf": [{"type": "number"}, {"type": "null"}]},
            "default_ifd": {"anyOf": [{"type": "integer", "minimum": 0}, {"type": "null"}]},
            "coarse_ifd": {"anyOf": [{"type": "integer", "minimum": 0}, {"type": "null"}]},
            "name": {"type": "string"},
            "tile_file": {"type": "string"},
            "ets": {
                "type": "object",
                "description": "The tile file's SIS and ETS headers (spec/virtualize/vsi.md §3 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/vsi.md#3-the-tile-files>)",
                "properties": {
                    "key": {"type": "string"},
                    "size": {"type": "integer", "minimum": 64},
                    "version": {"type": "integer"},
                    "coordinates": {"type": "integer", "minimum": 2, "maximum": 16},
                    "tiles": {"type": "integer", "minimum": 0},
                    "ets_version": {"type": "integer"},
                    "pixel_type": {"type": "integer", "minimum": 1, "maximum": 10},
                    "data_type": {"enum": ["int8", "uint8", "int16", "uint16", "int32", "uint32", "int64", "uint64", "float32", "float64"]},
                    "samples": {"enum": [1, 3, 4]},
                    "colorspace": {"type": "integer"},
                    "compression": {"enum": [0, 2, 3]},
                    "codec": {"enum": ["raw", "jpeg", "jpeg2000"]},
                    "quality": {"type": "integer"},
                    "tile_width": {"type": "integer", "minimum": 1, "maximum": 65535},
                    "tile_height": {"type": "integer", "minimum": 1, "maximum": 65535},
                    "tile_depth": {"const": 1},
                    "background": {"type": "string", "contentEncoding": "base64"},
                    "component_order": {"type": "integer"},
                    "use_pyramid": {"type": "boolean"},
                    "dimensions": {"type": "integer", "minimum": 2, "maximum": 10},
                    "dimension_sizes": {"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 10},
                },
                "required": ["key", "size", "version", "coordinates", "tiles", "ets_version", "pixel_type", "data_type", "samples",
                             "colorspace", "compression", "codec", "quality", "tile_width", "tile_height", "tile_depth", "background",
                             "component_order", "use_pyramid", "dimensions", "dimension_sizes"],
                "additionalProperties": False,
            },
            "levels": {"type": "array", "items": _VSI_PAIR, "minItems": 1},
            "tiles_outside": {"type": "integer", "minimum": 0},
            "ifd": _VSI_IFD,
        },
        "required": ["id", "type", "type_name", "width", "height", "dimension_sizes", "dimensions", "scale", "unit", "origin",
                     "z_step", "z_unit", "display", "channels", "creation_time", "magnification", "default_ifd", "coarse_ifd", "name"],
        "additionalProperties": False,
    },
    {
        "type": "object",
        "description": "The macro image's: its name and IFD (spec/virtualize/vsi.md §4.3 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/vsi.md#43-the-macro-image>)",
        "properties": {"name": {"const": "macro image"}, "ifd": _VSI_IFD},
        "required": ["name", "ifd"],
        "additionalProperties": False,
    },
    {"$ref": "#/$defs/mirror"},
    {"$ref": "#/$defs/mirrorView"},
]}
VSI_DEFS = {"vsiField": _VSI_FIELD}

# The dataset layout of an MRC file (spec/virtualize/mrc-dataset.md §4
# <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mrc-dataset.md#4-source-metadata>): the
# MRC convention's root metadata, member for member, on the group `dataset`.
# The FITS convention's cards (spec/virtualize/fits.md §5.3
# <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/fits.md#53-cards>): [keyword, value,
# comment] triples in header order, duplicates kept; null past the 65536-byte budget of a node's document.
FITS_CARDS = {
    "description": "The header's cards, [keyword, value, comment] each, in order: a string, a logical, an integer "
                   "(exact; as a string of digits past 2^53 - 1), a real (its binary64 value), a complex as [re, im], "
                   "null for no value, or {\"text\": t} for a value the standard does not define; a commentary "
                   "card's text is its comment. Null when the cards would take more than 65536 bytes of JSON",
    "anyOf": [
        {"type": "null"},
        {"type": "array", "items": {
            "type": "array", "minItems": 3, "maxItems": 3,
            "prefixItems": [
                {"type": "string"},
                {"anyOf": [{"type": "null"}, {"type": "boolean"}, {"type": "number"}, {"type": "string"},
                           {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2},
                           {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"], "additionalProperties": False}]},
                {"type": ["string", "null"]},
            ],
        }},
    ],
}
FITS_NUMBER = {"type": ["number", "null"]}
FITS_STRING = {"type": ["string", "null"]}
SOURCE_METADATA["fits"] = {"anyOf": [
    {
        "type": "object",
        "description": "The root's: the table of HDUs and the primary header's cards (spec/virtualize/fits.md §5.1 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/fits.md#51-the-root>)",
        "properties": {
            "hdus": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "minimum": 0},
                    "type": {"enum": ["primary", "image", "table", "bintable"]},
                    "kind": {"enum": ["empty", "image", "table"]},
                    "name": FITS_STRING, "version": {"type": ["integer", "null"]},
                    "bitpix": {"enum": [8, 16, 32, 64, -32, -64]},
                    "naxis": {"type": "array", "items": {"type": "integer", "minimum": 0}},
                    "pcount": {"type": "integer", "minimum": 0}, "gcount": {"type": "integer", "minimum": 1},
                    "offset": {"type": "integer", "minimum": 0}, "header_bytes": {"type": "integer", "minimum": 2880},
                    "data_offset": {"type": "integer", "minimum": 2880}, "data_bytes": {"type": "integer", "minimum": 0},
                    "compression": {"anyOf": [{"type": "null"}, {
                        "type": "object",
                        "description": "A tile-compressed image's compression (spec/virtualize/fits.md §3.3 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/fits.md#33-tile-compressed-images>)",
                        "properties": {
                            "type": {"enum": ["RICE_1", "GZIP_1", "GZIP_2", "NOCOMPRESS"]},
                            "parameters": {"type": "object", "properties": {"BLOCKSIZE": {"enum": [16, 32]}, "BYTEPIX": {"enum": [1, 2, 4]}},
                                           "required": ["BLOCKSIZE", "BYTEPIX"], "additionalProperties": False},
                            "zbitpix": {"enum": [8, 16, 32, 64, -32, -64]},
                            "tile": {"type": "array", "items": {"type": "integer", "minimum": 1}, "description": "The tile shape, in the array's order"},
                            "quantize": {"enum": ["NO_DITHER", "SUBTRACTIVE_DITHER_1", "SUBTRACTIVE_DITHER_2"]},
                            "dither_seed": {"type": ["integer", "null"], "minimum": 1, "maximum": 10000},
                            "zblank": {"type": ["integer", "null"]},
                            "tiles": {"type": "integer", "minimum": 0},
                        },
                        "required": ["type", "parameters", "zbitpix", "tile", "quantize", "dither_seed", "zblank", "tiles"],
                        "additionalProperties": False,
                    }]},
                },
                "required": ["index", "type", "kind", "name", "version", "bitpix", "naxis", "pcount", "gcount", "offset", "header_bytes", "data_offset", "data_bytes", "compression"],
                "additionalProperties": False,
            }},
            "cards": FITS_CARDS,
        },
        "required": ["hdus", "cards"],
        "additionalProperties": False,
    },
    {
        "type": "object",
        "description": "An HDU node's: its index and cards, an image's scaling cards, a table's columns",
        "properties": {
            "index": {"type": "integer", "minimum": 0},
            "cards": FITS_CARDS,
            "bscale": FITS_NUMBER, "bzero": FITS_NUMBER, "blank": FITS_NUMBER, "bunit": FITS_STRING,
            "columns": {"type": "array", "items": {
                "type": "object",
                "properties": {"name": FITS_STRING, "form": {"type": "string"}, "unit": FITS_STRING, "dim": FITS_STRING,
                               "scale": FITS_NUMBER, "zero": FITS_NUMBER, "null": FITS_NUMBER, "disp": FITS_STRING, "bcol": FITS_NUMBER},
                "required": ["name", "form", "unit", "dim", "scale", "zero", "null", "disp", "bcol"],
                "additionalProperties": False,
            }},
        },
        "required": ["index", "cards"],
        "additionalProperties": False,
    },
    {"$ref": "#/$defs/mirror"},
    {"$ref": "#/$defs/mirrorView"},
]}
SOURCE_METADATA["mrc-dataset"] = {"anyOf": [{
    **SOURCE_METADATA["mrc"]["anyOf"][0],
    "description": "The dataset group's: the MRC convention's root source metadata, member for member (spec/virtualize/mrc-dataset.md §4 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mrc-dataset.md#4-source-metadata>)",
}]}

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

# The GeoZarr members a SAFE node may hold besides the convention's (spec/virtualize/safe.md §5
# <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/safe.md#5-georeferencing-geozarr>); their
# own schemas (zarr-conventions proj, spatial and multiscales v0.1) validate them.
GEOZARR = {
    "proj:code": {"type": "string"},
    "proj:wkt2": {"type": "string"},
    **{f"spatial:{k}": {"type": "array"} for k in ("dimensions", "transform", "shape", "bbox")},
    "spatial:registration": {"type": "string"},
    "multiscales": {"type": "object"},
}


def schema(profile: str) -> dict:
    cmo = convention(profile)
    title = PROFILES[profile][2]
    if profile.endswith("-dataset"):
        url = {"description": "The URL of the source file"}
    url = ({"pattern": "^[Hh][Tt][Tt][Pp][Ss]?://[^?#]*/$",
            "description": "The store URL: http or https, its path ending in /, no query or fragment"}
           if profile in STORES else {"description": "The URL of the source: a .SAFE directory's store URL, or a zip file's"}
           if profile == "safe" else {"description": "The URL of the source file"})
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
        "sourceMetadata": SOURCE_METADATA[profile],
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
    if profile == "dicom":
        defs.update(DICOM_DEFS)
    if profile == "vsi":
        defs.update(VSI_DEFS)
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
                    **(GEOZARR if profile == "safe" else {}),
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
