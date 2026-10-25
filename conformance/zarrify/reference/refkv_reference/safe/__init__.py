"""The Sentinel-2 SAFE profile (spec/zarrify/safe.md, §8
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safe.md#8-safe-profile>)."""

from refkv_reference.safe.virtualize import SafeOutput, is_zip, virtualize_safe_store, virtualize_safe_zip

__all__ = ["SafeOutput", "is_zip", "virtualize_safe_store", "virtualize_safe_zip"]
