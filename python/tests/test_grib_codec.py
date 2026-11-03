"""The `refkv.grib` codec (python/src/refkv/zarr/codecs/grib.py; spec/conventions.md §12
<https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#12-grib-messages>) as zarr reads
it: every field of every GRIB fixture (fixtures/grib, from fixtures/generators/grib) decodes to
eccodes' values, in a float64 array, and to them rounded to nearest in a float32 one; and one
test per error a reader meets."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import zarr
from zarr.storage import LocalStore

from refkv._narrow import ndarray
from refkv.zarr.codecs.grib import GribCodec

try:
    import eccodes
except (ImportError, RuntimeError):  # no eccodes library for this platform
    eccodes = None

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "grib"

# the fixtures the codec rejects (the tests below)
ERRORS = {"grib_reject_truncated", "grib_reject_end"}
# the fixtures eccodes 2.50 does not decode as the codec does, which are compared with the values
# the generator wrote (from the 5.0 messages they were made from): PNG samples of 1, 2 and 4 bits
# (its wheel's PNG decoder asserts whole-byte samples, and aborts), and a bitmap reused through
# indicator 254 (it does not apply it)
EXPECTED_ONLY = {"grib_png_1bit", "grib_png_2bit", "grib_png_4bit", "grib_bitmap_254"}


def ordered(values: np.ndarray, ni: int, nj: int, scan: int) -> np.ndarray:
    """eccodes' values, which are in the file's order, in the codec's: [Nj, Ni], every second
    line (row, or column with 0x20) reversed when scanning mode bit 0x10 is set, then columns
    transposed to rows when 0x20 is set."""
    lines = values.reshape((ni, nj) if scan & 0x20 else (nj, ni)).copy()
    if scan & 0x10:
        lines[1::2] = lines[1::2, ::-1]
    return lines.T.copy() if scan & 0x20 else lines


def eccodes_fields(path: Path) -> list[np.ndarray]:
    """eccodes' values of each field of the message at `path`, NaN where missing, in the codec's
    order."""
    assert eccodes is not None
    out = []
    eccodes.codes_grib_multi_support_on()
    try:
        with path.open("rb") as f:
            while (h := eccodes.codes_grib_new_from_file(f)) is not None:
                eccodes.codes_set(h, "missingValue", 1e300)  # no field holds it
                v = np.array(eccodes.codes_get_values(h), dtype=np.float64)
                v[v == 1e300] = np.nan
                ni, nj, scan = (eccodes.codes_get_long(h, k) for k in ("Ni", "Nj", "scanningMode"))
                out.append(ordered(v, ni, nj, scan))
                eccodes.codes_release(h)
    finally:
        eccodes.codes_grib_multi_support_off()
    return out


def expected_fields(name: str) -> list[np.ndarray]:
    return [np.load(p) for p in sorted((FIXTURES / "expected").glob(f"{name}.*.npy"))]


def read(path: Path, config: dict[str, int | bool], shape: tuple[int, ...], dtype: str, root: Path) -> np.ndarray:
    """The message at `path` as the one chunk of a zarr array of `shape` and `dtype` whose codec
    is `refkv.grib` with `config`."""
    store = LocalStore(root)
    fill = np.nan if dtype.startswith("float") else 0
    zarr.create_array(store, shape=shape, chunks=shape, dtype=dtype, fill_value=fill, compressors=None,
                      serializer=GribCodec.from_dict({"name": "refkv.grib", "configuration": config}),
                      overwrite=True)
    (root / "c" / "/".join("0" * len(shape))).parent.mkdir(parents=True, exist_ok=True)
    (root / "c" / "/".join("0" * len(shape))).write_bytes(path.read_bytes())
    return ndarray(zarr.open_array(LocalStore(root, read_only=True), mode="r")[...])


def test_every_field_decodes_to_eccodes_values(tmp_path):
    if eccodes is None:
        pytest.skip("eccodes is not installed")
    checked = 0
    for path in sorted(FIXTURES.glob("*.grib2")):
        name = path.stem
        if name in ERRORS or "_reject_" in name:  # the profile's rejects: files, not messages
            continue
        fields = expected_fields(name) if name in EXPECTED_ONLY else eccodes_fields(path)
        for k, want in enumerate(fields):
            # the chunk of a 3-dimensional array, its first dimension of length 1, and of a 2-dimensional one
            got = read(path, {"field": k} if k else {}, (1, *want.shape), "float64", tmp_path / f"{name}.{k}")
            np.testing.assert_array_equal(got[0], want, err_msg=f"{name} field {k}", strict=True)
            got32 = read(path, {"field": k}, want.shape, "float32", tmp_path / f"{name}.{k}.f4")
            np.testing.assert_array_equal(got32, want.astype(np.float32), err_msg=f"{name} field {k}", strict=True)
            checked += 1
    assert checked == 57


def test_rejects_a_packing_it_does_not_decode(tmp_path):
    # grib_jpeg2000_lossy's codestream as if of the 9/7 wavelet transform (its COD's last
    # parameter)
    m = bytearray((FIXTURES / "grib_jpeg2000_lossy.grib2").read_bytes())
    cod = m.index(b"\xff\x52", m.index(b"\xff\x4f\xff\x51"))
    m[cod + 13] = 0
    (tmp_path / "m.grib2").write_bytes(m)
    nine_seven = r"^refkv\.grib: a JPEG 2000 codestream of the irreversible 9/7 wavelet transform is not decoded$"
    with pytest.raises(ValueError, match=nine_seven):
        read(tmp_path / "m.grib2", {}, (12, 16), "float64", tmp_path / "a")


def test_rejects_a_chunk_shape_that_is_not_the_grids(tmp_path):
    with pytest.raises(ValueError, match=r"does not hold the grid's 12 × 16 points"):
        read(FIXTURES / "grib_simple.grib2", {}, (1, 12, 15), "float64", tmp_path)


def test_rejects_a_field_past_the_last(tmp_path):
    with pytest.raises(ValueError, match="field 1 was asked for, and the message has 1 fields"):
        read(FIXTURES / "grib_simple.grib2", {"field": 1}, (12, 16), "float64", tmp_path)


def test_rejects_an_unknown_configuration_member(tmp_path):
    with pytest.raises(ValueError, match='unknown member "north_up"'):
        read(FIXTURES / "grib_simple.grib2", {"north_up": True}, (12, 16), "float64", tmp_path)


def test_rejects_an_array_that_is_not_floating_point(tmp_path):
    with pytest.raises(ValueError, match="an array of int16 is not decoded"):
        read(FIXTURES / "grib_simple.grib2", {}, (12, 16), "int16", tmp_path)


def test_names_its_configuration():
    codec = GribCodec.from_dict({"name": "refkv.grib", "configuration": {"field": 2}})
    assert codec.to_dict() == {"name": "refkv.grib", "configuration": {"field": 2}}
    assert json.loads(codec.config) == {"field": 2}
