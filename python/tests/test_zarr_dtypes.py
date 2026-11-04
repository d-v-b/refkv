"""refkv's zarr data types (refkv.zarr.dtypes: ml_dtypes types, spec/zarrify/safetensors.md §3
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#3-data-types>) and its
`packbits` codec (refkv.zarr.codecs.packbits): fill values read and written, arrays written
and read back through zarr, and one test per refusal."""

from __future__ import annotations

import math

import ml_dtypes
import numpy as np
import pytest
import zarr
from zarr.core.common import JSON
from zarr.storage import MemoryStore

import refkv.zarr  # noqa: F401  (registers the data types and the codec)
from refkv.zarr.codecs.packbits import PackBitsCodec, pack, unpack
from refkv.zarr.dtypes import BFloat16, Float4E2M1FN, Float8E4M3FN, Float8E5M2, Float8E8M0FNU


def packbits(padding: str, **bits: int) -> dict[str, JSON]:
    return {"name": "packbits", "configuration": {"padding_encoding": padding, **bits}}


def test_reads_and_writes_fill_values_and_arrays():
    """Each type's fill values, as numbers, special strings and bits, and an array of each type
    and codec written and read back through zarr, its bits unchanged."""
    cases = [
        (BFloat16(), 1.5, 1.5, 1.5),
        (BFloat16(), "NaN", math.nan, "NaN"),
        (BFloat16(), "-Infinity", -math.inf, "-Infinity"),
        (BFloat16(), "0x3fc0", 1.5, 1.5),
        (Float8E5M2(), "Infinity", math.inf, "Infinity"),
        (Float8E4M3FN(), 448, 448.0, 448.0),
        (Float8E4M3FN(), "0x7e", 448.0, 448.0),
        (Float8E4M3FN(), "NaN", math.nan, "NaN"),
        (Float8E8M0FNU(), "NaN", math.nan, "NaN"),
        (Float8E8M0FNU(), 0.25, 0.25, 0.25),
        (Float4E2M1FN(), -6, -6.0, -6.0),
    ]
    for dtype, json_value, value, written in cases:
        got = float(dtype.from_json_scalar(json_value, zarr_format=3))
        assert got == value or (math.isnan(got) and math.isnan(value)), (dtype, json_value)
        assert dtype.to_json_scalar(dtype.cast_scalar(json_value), zarr_format=3) == written, (dtype, json_value)
    assert BFloat16(endianness="big").to_native_dtype().byteorder == ">"
    arrays: list[tuple[str, type, dict[str, JSON]]] = [
        ("bfloat16", ml_dtypes.bfloat16, {"name": "bytes", "configuration": {"endian": "big"}}),
        ("refkv.float8_e4m3fn", ml_dtypes.float8_e4m3fn, {"name": "bytes"}),
        ("float4_e2m1fn", ml_dtypes.float4_e2m1fn, packbits("none")),
        ("float6_e2m3fn", ml_dtypes.float6_e2m3fn, packbits("first_byte")),
        ("float6_e3m2fn", ml_dtypes.float6_e3m2fn, packbits("last_byte")),
    ]
    for name, t, codec in arrays:
        values = np.array([0.5, -1, 2, 3, -0.5, 1.5, 0], dtype=t)
        a = zarr.create_array(MemoryStore(), shape=(7,), chunks=(5,), dtype=name, serializer=codec, compressors=None,
                              fill_value=0)
        a[...] = values
        np.testing.assert_array_equal(np.asarray(a[...]).view(np.uint8), values.view(np.uint8), err_msg=name)
    # bool, and uint8 limited to some of its bits, written through zarr and read back
    bits = np.array([True, False, True, True, False, False, True], dtype=bool)
    a = zarr.create_array(MemoryStore(), shape=(7,), chunks=(5,), dtype="bool", serializer=packbits("none"),
                          compressors=None, fill_value=False)
    a[...] = bits
    np.testing.assert_array_equal(a[...], bits)
    nibbles = np.array([0, 1, 7, 8, 15, 3, 12], dtype=np.uint8)
    for codec, written in [(packbits("none", last_bit=3), nibbles),
                           (packbits("last_byte", first_bit=2, last_bit=5), nibbles << 2),
                           (packbits("none"), nibbles)]:
        a = zarr.create_array(MemoryStore(), shape=(7,), chunks=(5,), dtype="uint8", serializer=codec, compressors=None,
                              fill_value=0)
        a[...] = written
        np.testing.assert_array_equal(a[...], written, err_msg=str(codec))
    assert PackBitsCodec(last_bit=3).to_dict() == packbits("none", last_bit=3)
    # packbits' bit order: element i the bits [i k, (i + 1) k), least significant first
    assert pack(np.array([0x1, 0x2, 0xF], dtype=np.uint8), 4) == bytes([0x21, 0x0F])
    assert pack(np.array([0x3F, 0x00, 0x01, 0x2A], dtype=np.uint8), 6) == bytes([0x3F, 0x10, 0xA8])
    np.testing.assert_array_equal(unpack(bytes([0x3F, 0x10, 0xA8]), 4, 6), [0x3F, 0x00, 0x01, 0x2A])
    np.testing.assert_array_equal(unpack(bytes([0b00000101]), 3, 1), [1, 0, 1])


def test_refuses_a_value_the_type_has_not():
    with pytest.raises(TypeError, match=r"Cannot convert 'Infinity' to a scalar of the data type refkv\.float8_e4m3fn"):
        Float8E4M3FN().from_json_scalar("Infinity", zarr_format=3)


def test_refuses_nan_where_the_type_has_none():
    with pytest.raises(TypeError, match="Cannot convert 'NaN' to a scalar of the data type float4_e2m1fn"):
        Float4E2M1FN().cast_scalar("NaN")


def test_refuses_bits_of_another_width():
    with pytest.raises(TypeError, match=r"Cannot convert '0x3fc0' to a scalar of the data type refkv\.float8_e4m3fn"):
        Float8E4M3FN().cast_scalar("0x3fc0")


def test_refuses_a_zarr_v2_form():
    with pytest.raises(ValueError, match="bfloat16 has no Zarr v2 form"):
        BFloat16().to_json(zarr_format=2)


def test_packbits_refuses_a_negative_first_bit():
    with pytest.raises(ValueError, match="packbits: first_bit -1 is negative"):
        PackBitsCodec.from_dict({"name": "packbits", "configuration": {"first_bit": -1}})


def test_packbits_refuses_a_last_bit_before_the_first():
    with pytest.raises(ValueError, match="packbits: last_bit 1 is less than first_bit 2"):
        PackBitsCodec(first_bit=2, last_bit=1)


def test_packbits_refuses_a_bit_past_the_data_type():
    with pytest.raises(ValueError, match="packbits: the bits 0 to 3 are not all among the 1 of the data type"):
        zarr.create_array(MemoryStore(), shape=(7,), chunks=(5,), dtype="bool", serializer=packbits("none", last_bit=3),
                          compressors=None, fill_value=False)[...] = True


def test_packbits_refuses_an_unknown_padding_encoding():
    with pytest.raises(ValueError, match="packbits: padding_encoding 'middle' is not one of"):
        PackBitsCodec(padding_encoding="middle")


def test_packbits_refuses_a_data_type_it_does_not_pack():
    a = zarr.create_array(MemoryStore(), shape=(4,), dtype="uint16", serializer={"name": "packbits"}, compressors=None)
    with pytest.raises(ValueError, match="packbits: the data type 'uint16' is not one refkv packs"):
        a[...] = 1


def test_packbits_refuses_a_short_chunk():
    with pytest.raises(ValueError, match="packbits: a chunk of 1 bytes, fewer than 3 elements of 4 bits"):
        unpack(bytes(1), 3, 4)
