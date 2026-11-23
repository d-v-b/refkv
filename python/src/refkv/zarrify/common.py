"""Shared parts of the zarrifiers (spec/zarrify.md §1
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#1-introduction>, §2
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#2-the-zarr-layout>)."""

from __future__ import annotations

from refkv._narrow import integer, string
from refkv.entries import MAX_PAYLOAD
from refkv.errors import Rejected
from refkv.host.http import REDIRECTS, follow
from refkv.ir.output import PAGE_SIZE, Output, Part, reading_own_files, reference_store
from refkv.ir.planner import STRONG_ETAG, UA, EtagLog
from refkv.profiles import REGISTRY

# Re-exported where they were defined before they moved below `refkv.zarrify` (d-v-b/refkv#459),
# so that `refkv.ir`, `refkv.host` and `refkv.build` can import them without importing this package.
__all__ = ["CONVENTION_KEY", "MAX_PAYLOAD", "PAGE_SIZE", "PROFILES", "REDIRECTS", "SOURCE_NODE", "STRONG_ETAG",
           "UA", "UUIDS", "EtagLog", "Output", "Part", "Rejected", "convention", "follow", "reading_own_files",
           "reference_store"]

SOURCE_NODE = "refkv_source"  # the source metadata node (spec/conventions.md §2 <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#2-attributes>)


# The zarrification conventions (conventions §2
# <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#2-attributes>): one per profile, each
# with its fixed UUID, its current version and its name in the description, from the profile
# registry (spec/zarrify/profiles/, as `refkv.profiles` reads it from the core).
CONVENTION_KEY = "refkv_zarrified"
PROFILES = {string(e["name"]): (string(e["uuid"]), integer(e["version"]), string(e["title"])) for e in REGISTRY}
UUIDS = frozenset(uuid for uuid, _, _ in PROFILES.values())


def convention(profile: str) -> dict[str, object]:
    """The Convention Metadata Object of a profile's convention (conventions §2
    <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#2-attributes>)."""
    uuid, version, title = PROFILES[profile]
    # Version 0 has no tag: its URLs name the development branch (conventions §1
    # <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#1-conventions>).
    ref, blob = ("heads/main", "main") if version == 0 else (f"tags/zarrify-{profile}-v{version}",) * 2
    return {
        "uuid": uuid,
        "schema_url": f"https://raw.githubusercontent.com/d-v-b/refkv/refs/{ref}/spec/zarrify/{profile}/schema.json",
        "spec_url": f"https://github.com/d-v-b/refkv/blob/{blob}/spec/zarrify/{profile}.md",
        "name": CONVENTION_KEY,
        "description": f"The Zarr layout of a {title} source zarrified by refkv, and the source's metadata",
    }
