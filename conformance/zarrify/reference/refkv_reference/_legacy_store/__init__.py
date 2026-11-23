"""The modules of `refkv.host` that only the frozen reference profiles use, moved here
from the package (d-v-b/refkv#459): `hierarchy`, the nodes and chunk objects of a Zarr v2 or N5
store, `blosc`, the blosc codec of their compressors, `output`, their output (`StoreOutput`), and
`xml`, the Python reader of the listings' XML subset (`parse_xml`, `Element`).
The names the package still has (the store readers, the URL, listing and document helpers) are
imported from `refkv.host`, so that this package's names are those it had."""

from __future__ import annotations

from refkv.host import *  # noqa: F403
from refkv_reference._legacy_store.xml import Element, parse_xml
from refkv_reference._legacy_store.blosc import BLOSC_NAMES, MAX_BLOCK, blosc_codec
from refkv_reference._legacy_store.hierarchy import (
    PathTrie,
    canonical_index,
    classify,
    depth,
    directories_of,
    find_chunks,
    grid,
    implicit_groups,
    join,
    parent_of,
)
from refkv_reference._legacy_store.output import (
    EMPTY_KEY,
    GROUP_IMPLICIT,
    OBJECTS,
    SOURCE_GROUP,
    StoreOutput,
    doc_key,
    object_key,
    source_metadata,
)
