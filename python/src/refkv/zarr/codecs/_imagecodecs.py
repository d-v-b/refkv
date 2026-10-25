"""imagecodecs, which the image codecs need (the `images` extra)."""

from __future__ import annotations

from types import ModuleType

from refkv._extras import missing


def imagecodecs() -> ModuleType:
    """imagecodecs, which the image codecs need, imported when a chunk is first coded."""
    try:
        import imagecodecs  # noqa: PLC0415 (the images extra)
    except ImportError as e:
        raise missing("images", "Reading JPEG, JPEG 2000, JPEG XR and LZW chunks", e) from e
    return imagecodecs
