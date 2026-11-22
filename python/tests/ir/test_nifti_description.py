"""The description of every NIfTI fixture (spec/zarrify/nifti.md §7.6
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nifti.md#76-the-description>), compared with
nibabel, the reference reader: the version and byte order of its header; every field of the header,
a float's bits included and its texts up to their first NUL; the voxels' offset and size, those of
nibabel's raw values; the extender's bytes; each extension's size and code, as nibabel reads the
chain (where it loads the image; it refuses three fixtures the profile accepts, whose header its
header class reads); a gzipped file's header, byte for byte, in what the stream inflates to; a pair's
header and image files; and the IR elements it names. Each description is valid by its JSON Schema,
and holds none of the output's decisions."""

import gzip
import io
import json
import math
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema
import nibabel as nib
import numpy as np
import pytest
from ir_helpers import FIXTURES, ROOT
from nibabel.spatialimages import HeaderDataError

from refkv.build import read

FILES = sorted(p for p in (FIXTURES / "nifti").iterdir()
               if "reject" not in p.name and p.name.lower().endswith((".nii", ".nii.gz", ".hdr")))
SCHEMA = json.loads((ROOT / "spec" / "zarrify" / "nifti" / "description.json").read_text())
# members a description never has: the output's decisions (spec/zarrify/nifti.md §7.6
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nifti.md#76-the-description>)
DECISIONS = {"data_type", "axes", "scale", "affine", "translation", "chunks", "grid", "own", "declared_by",
             "copy_threshold", "copied", "start", "length"}


@dataclass(frozen=True)
class Read:
    """What nibabel reads of a file: its header, its raw values' bytes, where they start, the file
    that holds them, and its extensions (None where nibabel refuses the image)."""

    header: Any  # nibabel's header classes are not annotated
    nbytes: int
    offset: int
    data: Path
    extensions: list[tuple[int, bytes]] | None


def nibabel_read(path: Path) -> Read:
    try:
        image: Any = nib.load(path)
        values = np.asarray(image.dataobj.get_unscaled())
        extensions = [(e.get_code(), raw(e)) for e in image.header.extensions]
        return Read(image.header, values.nbytes, int(image.dataobj.offset), Path(image.dataobj.file_like), extensions)
    except (HeaderDataError, ValueError):
        # an image nibabel refuses, for its scaling or its extensions: its header class's reading
        held = path.read_bytes()
        kind: Any = next(k for k in (nib.Nifti1Header, nib.Nifti2Header) if k.may_contain_header(held[:k.sizeof_hdr]))
        header = kind(held[:kind.sizeof_hdr], check=False)
        size = math.prod(int(s) for s in header.get_data_shape()) * header.get_data_dtype().itemsize
        return Read(header, size, int(header.get_data_offset()), path, None)


def raw(extension: Any) -> bytes:
    """An extension's content as nibabel writes it back, without its padding: `get_content` decodes
    some codes' (DICOM's, JSON's)."""
    out = io.BytesIO()
    extension.write_to(out, False)
    return out.getvalue()[8:].rstrip(b"\0")


def float_bits(v: Any, width: int) -> int:
    """The IEEE 754 bits of a float as the source metadata writes it (spec/conventions.md §6
    <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#6-source-metadata-as-json>)."""
    if isinstance(v, dict):
        return int(v["bits"], 16)
    value = {"NaN": math.nan, "Infinity": math.inf, "-Infinity": -math.inf}.get(v, v)
    code, unsigned = ("f", "I") if width == 4 else ("d", "Q")
    return struct.unpack(f">{unsigned}", struct.pack(f">{code}", value))[0]


def theirs_bits(v: Any) -> int:
    a = np.asarray(v)
    return int(a.reshape(1).view(np.uint32 if a.itemsize == 4 else np.uint64)[0])


def text(v: Any) -> bytes:
    """A text value's bytes (spec/conventions.md §6
    <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#6-source-metadata-as-json>)."""
    return v["latin1"].encode("latin-1") if isinstance(v, dict) else v.encode()


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_the_description_is_nibabels_reading(path):
    theirs = nibabel_read(path)
    gzipped = path.name.endswith(".gz")
    held = gzip.decompress(path.read_bytes()) if gzipped else path.read_bytes()
    length = type(theirs.header).sizeof_hdr
    # the header as nibabel's header class reads it from the bytes: a loaded image's header has its
    # offset and scaling reset
    h: Any = type(theirs.header)(held[:length], check=False)
    fields = h.structarr
    with read(path) as parsed:
        d: Any = parsed.description
        jsonschema.validate(d, SCHEMA)
        assert not DECISIONS & set(d), DECISIONS & set(d)
        assert d["version"] == (2 if isinstance(h, nib.Nifti2Header) else 1)
        assert d["byte_order"] == ("big" if h.endianness == ">" else "little")
        # nibabel splits NIfTI-2's 8-byte magic into `magic` and `eol_check`, which the text's first
        # NUL ends
        names = [n for n in fields.dtype.names or () if n != "eol_check"]
        assert list(d["header"]) == names
        for name in names:
            v, t = d["header"][name], fields[name]
            kind = fields.dtype[name].base.kind
            if kind == "S":
                assert text(v) == bytes(t).split(b"\0")[0], name
            elif kind == "f":
                values = v if isinstance(v, list) else [v]
                width = fields.dtype[name].base.itemsize
                assert [float_bits(x, width) for x in values] == [theirs_bits(x) for x in np.ravel(t)], name
            else:
                assert v == np.asarray(t).tolist(), name
        # the voxels, where nibabel reads its raw values, of their size
        assert (d["voxels"]["offset"], d["voxels"]["bytes"]) == (theirs.offset, theirs.nbytes)
        extender = held[length:length + 4]
        assert d["extender"] == (list(extender) if len(extender) == 4 else None)
        elements = d["elements"]
        assert parsed.ir.path(elements["voxels"]).endswith("voxels")
        if gzipped:
            assert set(elements) == {"voxels"}
            assert bytes.fromhex(d["gzip"]["head"]) == held[:length]
            assert d["gzip"]["skip"] == theirs.offset
            # the stream's extensions' sizes are where nibabel reads their contents, and a short text
            # code's data is that content
            extensions = []
            at = length + 4
            for e in d["gzip"]["extensions"]:
                content = held[at + 8:at + e["esize"]].rstrip(b"\0")
                assert struct.unpack(f"{h.endianness}ii", held[at:at + 8]) == (e["esize"], e["ecode"])
                if "data" in e:
                    assert text(e["data"]) == content.split(b"\0")[0]
                extensions.append((e["ecode"], content))
                at += e["esize"]
            key = elements["voxels"]
        else:
            assert "gzip" not in d
            prefix = f"{path.name}/" if theirs.data != path else ""
            assert parsed.ir.path(elements["header"]) == f"{prefix}header"
            if "extender" in elements:
                assert parsed.ir.path(elements["extender"]) == f"{prefix}extender"
            extensions = []
            for e in elements["extensions"]:
                start, size = parsed.ir.extent(e)
                esize, code = struct.unpack(f"{h.endianness}ii", held[start:start + 8])
                assert esize == size
                extensions.append((code, held[start + 8:start + size].rstrip(b"\0")))
            key = elements["header"]
        if theirs.extensions is not None:
            assert extensions == theirs.extensions
        if theirs.data != path:
            assert (d["header_key"], d["data_key"]) == (path.name, theirs.data.name)
        else:
            assert not {"header_key", "data_key"} & set(d)
        # the stored array is keyed by the header, or by a gzipped file's stream
        assert [a.element for a in parsed.ir.stored_arrays().arrays] == [key]
