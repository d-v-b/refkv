"""Inputs named by their profile, and the layouts a hierarchy holds (spec/zarrify.md §1.7
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#17-named-inputs-and-layouts>).

`zarrify(location)` decides what `location` is: a store when it is a directory or a URL
ending in `/`, else a file whose first bytes (not its name) select its profile. A source or
store class names the profile instead, so the input is that format or is rejected with the
profile's own message, and holds the input's read options:

    from refkv.zarrify import zarrify, NdpiSource, Zarr2Store, MrcSource, Image

    zarrify(NdpiSource("https://example.org/slide"))      # an NDPI slide, or rejected
    zarrify(Zarr2Store("https://example.org/data.zarr"))  # a store, though its URL has no "/"
    zarrify(MrcSource("emd.map"), layouts=[Image()])      # the image, without the dataset

There is one class per profile of the registry (spec/zarrify/profiles/), in
`refkv.zarrify.profile_classes`, which spec/tools/profiles.py writes from it (`just profiles`):
`<Name>Source` for a file profile (`TiffSource`, `Mp4Source`, `NdpiSource`, ...), `<Name>Store`
for a store profile (`Zarr2Store`, `N5Store`, `OmeZarrStore`), and both for a profile whose
input is either (`SafeSource`, a `.SAFE.zip` file, and `SafeStore`, a `.SAFE` directory).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import ClassVar
from urllib.parse import urlsplit

from refkv._narrow import string
from refkv.policy import Policy
from refkv.zarrify.profiles import REGISTRY
from refkv.zarrify.store import WORKERS


@dataclass(frozen=True)
class Image:
    """The format's image layout: its convention's hierarchy, the root of every output."""


@dataclass(frozen=True)
class Dataset:
    """The format's dataset layout (conventions §9
    <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#9-dataset-layouts>), beside the
    image in the same hierarchy, referencing the same bytes: for a format with a dataset
    convention, `<format>-dataset` in the registry (MRC's, `mrc-dataset`)."""


@dataclass(frozen=True)
class Source:
    """A file to zarrify by the profile `profile`, without testing its first bytes: the
    profile's parser reads it, and rejects it with its own message if it is not its format.

    - `location`: a local path or an http(s) URL.
    - `url`: the URL the archive names the source by (default: `location`).
    - `checksums`: each range of the file carries the CRC-32C of its bytes, which reads them.
    - `policy`: the reader policy the file is read under (default `Policy()`).
    - `connections`: requests at a time to a remote file (default 8).
    - `byte_cost`: the seconds of wall time the read planner charges a byte fetched beyond
      those asked (default 0.1 s per MB)."""

    profile: ClassVar[str] = ""  # the registry's name, set by each class
    location: str
    url: str | None = None
    checksums: bool = False
    policy: Policy | None = field(default=None, compare=False)
    connections: int | None = None
    byte_cost: float | None = None


@dataclass(frozen=True)
class Store:
    """A store to zarrify by the profile `profile`, without testing its root's keys: the
    profile reads it, and rejects it with its own message if it is not its format.

    - `location`: an http(s) URL, a trailing `/` added if it has none, or a local directory.
    - `url`: the store URL the archive names its objects by (needed for a local directory).
    - `checksums`: each range carries the CRC-32C of its bytes, which reads them.
    - `policy`: the reader policy the store is listed and read under (default `Policy()`).
    - `workers`: how many of the store's documents are read at a time.
    - `max_objects`: the most objects the store may list."""

    profile: ClassVar[str] = ""
    location: str
    url: str | None = None
    checksums: bool = False
    policy: Policy | None = field(default=None, compare=False)
    workers: int = WORKERS
    max_objects: int | None = None

    @property
    def store_url(self) -> str:
        """`location`, as a store URL when it is an http(s) one: its path ending in `/` (a query
        or fragment kept after it)."""
        if not self.location.startswith(("http://", "https://")):
            return self.location
        u = urlsplit(self.location)
        return u._replace(path=u.path if u.path.endswith("/") else u.path + "/").geturl()


# the profiles with a dataset layout: those whose `<name>-dataset` is a layout of the registry
DATASET_LAYOUTS = frozenset(string(e["name"]).removesuffix("-dataset") for e in REGISTRY if e["kind"] == "layout")

__all__ = ["Dataset", "Image", "Source", "Store"]


def layout_names(layouts: Iterable[object] | None, fmt: str) -> list[str] | None:
    """The layouts the projection of `fmt` writes beside the image, as the Rust core names
    them, for `layouts` (None: every one the format has)."""
    if layouts is None:
        return None
    layouts = list(layouts)
    if not any(isinstance(layout, Image) for layout in layouts):
        raise ValueError("layouts must hold Image(): the image layout is the hierarchy's root")
    for layout in layouts:
        if not isinstance(layout, (Image, Dataset)):
            raise TypeError(f"a layout is Image() or Dataset(), not {layout!r}")
    if any(isinstance(layout, Dataset) for layout in layouts):
        if fmt not in DATASET_LAYOUTS:
            have = ", ".join(sorted(DATASET_LAYOUTS)) or "none"
            raise ValueError(f"{fmt} has no dataset layout (the formats with one: {have})")
        return ["dataset"]
    return []
