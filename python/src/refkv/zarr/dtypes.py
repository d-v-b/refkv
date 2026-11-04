"""Zarr v3 data types needed to read zarrified files, which zarr-python does not provide: the
floating-point types of machine learning that a safetensors file's tensors have
(spec/zarrify/safetensors.md §3
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#3-data-types>), each an
`ml_dtypes` type. Importing this module registers them with zarr.

- zarr-extensions' `bfloat16`, `float8_e5m2`, `float8_e4m3fnuz`, `float8_e5m2fnuz` and
  `float8_e8m0fnu`, read with the `bytes` codec;
- zarr-extensions' `float4_e2m1fn`, `float6_e2m3fn` and `float6_e3m2fn`, one byte an element in
  memory, which archives store packed (the `packbits` codec, `refkv.zarr.codecs.packbits`);
- refkv's own `refkv.float8_e4m3fn` (spec/zarrify/safetensors.md §3.1
  <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#31-the-refkvfloat8_e4m3fn-data-type>),
  the E4M3 type without infinities that PyTorch's `float8_e4m3fn` is: zarr-extensions'
  `float8_e4m3` is the IEEE-style one, with infinities, a different type.

zarr also finds each one by name, without this module being imported first, through the
`zarr.data_type` entry points of python/pyproject.toml. A fill value is a JSON number, `"NaN"`,
`"Infinity"` or `"-Infinity"` where the type has that value, or the element's bits as a
hexadecimal string (`"0x7fc0"`), as Zarr v3's floating-point types write theirs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, ClassVar, Literal, Self, TypeGuard, overload, override

try:
    import ml_dtypes
    import numpy as np
    from zarr.core.common import JSON, ZarrFormat
    from zarr.core.dtype import data_type_registry
    from zarr.core.dtype.common import DTypeConfig_V2, DTypeJSON, HasEndianness, HasItemSize
    from zarr.core.dtype.npy.common import endianness_to_numpy_str, get_endianness_from_numpy_dtype
    from zarr.core.dtype.wrapper import ZDType
    from zarr.errors import DataTypeValidationError
except ImportError as e:
    from refkv._extras import missing

    raise missing("zarr", "refkv.zarr.dtypes", e) from e


@dataclass(frozen=True, kw_only=True)
class _MlFloat(ZDType[Any, Any], HasItemSize):
    """A Zarr v3 data type of an `ml_dtypes` floating-point type `_ml`, of one byte (or two, for
    `BFloat16`, which has a byte order), with the special values `_nan` and `_inf` says it has."""

    _zarr_v3_name: ClassVar[str]
    _ml: ClassVar[type]
    _nan: ClassVar[bool] = True
    _inf: ClassVar[bool] = False

    @classmethod
    def _native(cls) -> np.dtype[Any]:
        return np.dtype(cls._ml)

    @override
    @classmethod
    def from_native_dtype(cls, dtype: Any) -> Self:
        if isinstance(dtype, np.dtype) and dtype.type is cls._ml:
            return cls()
        raise DataTypeValidationError(f"Invalid data type: {dtype}. Expected {cls._native()}")

    @override
    def to_native_dtype(self) -> np.dtype[Any]:
        return self._native()

    @override
    @classmethod
    def _from_json_v2(cls, data: DTypeJSON) -> Self:
        raise DataTypeValidationError(f"{cls._zarr_v3_name} has no Zarr v2 form")

    @override
    @classmethod
    def _from_json_v3(cls, data: DTypeJSON) -> Self:
        if data == cls._zarr_v3_name:
            return cls()
        raise DataTypeValidationError(f"Invalid JSON representation of {cls.__name__}: {data!r}, "
                                      f"not {cls._zarr_v3_name!r}")

    @overload
    def to_json(self, zarr_format: Literal[2]) -> DTypeConfig_V2[str, None]: ...

    @overload
    def to_json(self, zarr_format: Literal[3]) -> str: ...

    @override
    def to_json(self, zarr_format: ZarrFormat) -> DTypeConfig_V2[str, None] | str:
        if zarr_format == 3:
            return self._zarr_v3_name
        raise ValueError(f"{self._zarr_v3_name} has no Zarr v2 form")

    @property
    @override
    def item_size(self) -> int:
        return self._native().itemsize

    def _special(self, data: str) -> float | None:
        """The float a special string names, when this type has that value."""
        value = {"NaN": math.nan, "Infinity": math.inf, "-Infinity": -math.inf}.get(data)
        if value is None or (math.isnan(value) and not self._nan) or (math.isinf(value) and not self._inf):
            return None
        return value

    def _from_bits(self, data: str) -> np.generic | None:
        """The element of the hexadecimal string `data`, its bits, or None."""
        digits = 2 * self.item_size
        if not (data.startswith("0x") and len(data) == 2 + digits):
            return None
        try:
            bits = int(data, 16)
        except ValueError:
            return None
        return np.frombuffer(bits.to_bytes(self.item_size, "little"), dtype=self.to_native_dtype())[0]

    @override
    def _check_scalar(self, data: object) -> TypeGuard[float | int | str]:
        if isinstance(data, str):
            return self._special(data) is not None or self._from_bits(data) is not None
        # an ml_dtypes scalar (as from_json_scalar gives) is no np.floating
        return isinstance(data, int | float | np.integer | np.floating | self._ml) and not isinstance(data, bool)

    @override
    def cast_scalar(self, data: object) -> Any:
        if not self._check_scalar(data):
            raise TypeError(f"Cannot convert {data!r} to a scalar of the data type {self._zarr_v3_name}")
        if isinstance(data, str):
            bits = self._from_bits(data)
            return bits if bits is not None else self._ml(self._special(data))
        return self._ml(data)

    @override
    def default_scalar(self) -> Any:
        return self._ml(0)

    @override
    def from_json_scalar(self, data: JSON, *, zarr_format: ZarrFormat) -> Any:
        if zarr_format != 3:
            raise ValueError(f"{self._zarr_v3_name} has no Zarr v2 form")
        return self.cast_scalar(data)

    @override
    def to_json_scalar(self, data: object, *, zarr_format: ZarrFormat) -> float | str:
        value = float(self.cast_scalar(data))
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        return value


@dataclass(frozen=True, kw_only=True)
class BFloat16(_MlFloat, HasEndianness):
    """zarr-extensions' `bfloat16`: binary32 truncated to 16 bits, in either byte order."""

    _zarr_v3_name: ClassVar[str] = "bfloat16"
    _ml: ClassVar[type] = ml_dtypes.bfloat16
    _inf: ClassVar[bool] = True

    @override
    @classmethod
    def from_native_dtype(cls, dtype: Any) -> Self:
        if isinstance(dtype, np.dtype) and dtype.type is cls._ml:
            return cls(endianness=get_endianness_from_numpy_dtype(dtype))
        raise DataTypeValidationError(f"Invalid data type: {dtype}. Expected {cls._native()}")

    @override
    def to_native_dtype(self) -> np.dtype[Any]:
        return self._native().newbyteorder(endianness_to_numpy_str(self.endianness))


@dataclass(frozen=True, kw_only=True)
class Float8E5M2(_MlFloat):
    """zarr-extensions' `float8_e5m2`, which has infinities."""

    _zarr_v3_name: ClassVar[str] = "float8_e5m2"
    _ml: ClassVar[type] = ml_dtypes.float8_e5m2
    _inf: ClassVar[bool] = True


@dataclass(frozen=True, kw_only=True)
class Float8E4M3FN(_MlFloat):
    """refkv's `refkv.float8_e4m3fn` (spec/zarrify/safetensors.md §3.1
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#31-the-refkvfloat8_e4m3fn-data-type>):
    E4M3 with no infinities, NaN `0x7F`."""

    _zarr_v3_name: ClassVar[str] = "refkv.float8_e4m3fn"
    _ml: ClassVar[type] = ml_dtypes.float8_e4m3fn


@dataclass(frozen=True, kw_only=True)
class Float8E4M3FNUZ(_MlFloat):
    """zarr-extensions' `float8_e4m3fnuz`: no infinities, no negative zero."""

    _zarr_v3_name: ClassVar[str] = "float8_e4m3fnuz"
    _ml: ClassVar[type] = ml_dtypes.float8_e4m3fnuz


@dataclass(frozen=True, kw_only=True)
class Float8E5M2FNUZ(_MlFloat):
    """zarr-extensions' `float8_e5m2fnuz`: no infinities, no negative zero."""

    _zarr_v3_name: ClassVar[str] = "float8_e5m2fnuz"
    _ml: ClassVar[type] = ml_dtypes.float8_e5m2fnuz


@dataclass(frozen=True, kw_only=True)
class Float8E8M0FNU(_MlFloat):
    """zarr-extensions' `float8_e8m0fnu`: a power of two, unsigned, with no zero: its default
    scalar, 0 cast to it, is NaN."""

    _zarr_v3_name: ClassVar[str] = "float8_e8m0fnu"
    _ml: ClassVar[type] = ml_dtypes.float8_e8m0fnu


@dataclass(frozen=True, kw_only=True)
class Float4E2M1FN(_MlFloat):
    """zarr-extensions' `float4_e2m1fn`: no infinities and no NaN."""

    _zarr_v3_name: ClassVar[str] = "float4_e2m1fn"
    _ml: ClassVar[type] = ml_dtypes.float4_e2m1fn
    _nan: ClassVar[bool] = False


@dataclass(frozen=True, kw_only=True)
class Float6E2M3FN(_MlFloat):
    """zarr-extensions' `float6_e2m3fn`: no infinities and no NaN."""

    _zarr_v3_name: ClassVar[str] = "float6_e2m3fn"
    _ml: ClassVar[type] = ml_dtypes.float6_e2m3fn
    _nan: ClassVar[bool] = False


@dataclass(frozen=True, kw_only=True)
class Float6E3M2FN(_MlFloat):
    """zarr-extensions' `float6_e3m2fn`: no infinities and no NaN."""

    _zarr_v3_name: ClassVar[str] = "float6_e3m2fn"
    _ml: ClassVar[type] = ml_dtypes.float6_e3m2fn
    _nan: ClassVar[bool] = False


DATA_TYPES: tuple[type[_MlFloat], ...] = (BFloat16, Float8E5M2, Float8E4M3FN, Float8E4M3FNUZ, Float8E5M2FNUZ,
                                          Float8E8M0FNU, Float4E2M1FN, Float6E2M3FN, Float6E3M2FN)
"""Every data type of this module, each registered with zarr under its name."""

BITS: dict[str, int] = {"float4_e2m1fn": 4, "float6_e2m3fn": 6, "float6_e3m2fn": 6}
"""The bits of each packed data type's element, as the `packbits` codec stores it."""


for _cls in DATA_TYPES:
    data_type_registry.register(_cls().to_json(zarr_format=3), _cls)
