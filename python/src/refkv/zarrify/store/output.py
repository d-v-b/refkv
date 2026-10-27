"""A store profile's output (spec/zarrify.md §1.4
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>), as the frozen
reference profiles write it (conformance/zarrify/reference): JSON documents, and one url source
per chunk object. The Rust core's store runs write theirs through
`refkv.ir.store.StoreIrOutput`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

from refkv._narrow import at, mapping, string
from refkv.pb import Range, Source
from refkv.policy import Policy
from refkv.refstore import ReferenceStore
from refkv.vzip.archive import VZipWriter
from refkv.zarrify.common import CONVENTION_KEY, Rejected, declare, reference_store
from refkv.zarrify.store.hierarchy import join
from refkv.zarrify.store.urls import object_url

GROUP_IMPLICIT: dict[str, object] = {"zarr_format": 3, "node_type": "group", "attributes": {}}
SOURCE_GROUP = "refkv_source"
OBJECTS = "refkv_source/objects/"
EMPTY_KEY = "refkv_source/empty.json"  # the empty objects' keys under a root array (spec/zarrify/zarr2.md §5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#5-other-objects>)
# A last segment that a Zarr reader takes for a node's document, followed by any number of `~`.
_NODE_NAME = re.compile(r"(?:zarr\.json|\.zarray|\.zgroup)~*\Z")


def object_key(key: str) -> str:
    """The hierarchy's key of the other object `key` (spec/zarrify/zarr2.md §5
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#5-other-objects>,
    spec/zarrify/n5.md §6
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/n5.md#6-other-objects>):
    `refkv_source/objects/<key>`, with `~` appended to a last
    segment that is `zarr.json`, `.zarray` or `.zgroup` followed by any number of `~`, so
    that no Zarr reader opens a node there, and the key maps back one to one."""
    return OBJECTS + key + ("~" if _NODE_NAME.match(key.rpartition("/")[2]) else "")


def source_metadata(attributes: Mapping[str, object], metadata: Mapping[str, object],
                    unversioned: list[str] | None = None) -> dict[str, object]:
    """A store node's source metadata S (spec/zarrify/zarr2.md §4
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#4-source-metadata>,
    spec/zarrify/n5.md §5
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/n5.md#5-source-metadata>,
    spec/zarrify/ome-zarr.md §8
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ome-zarr.md#8-source-metadata>):
    `{"attributes": A, "metadata": M, "unversioned": U}`, each member only when it is not empty."""
    s: dict[str, object] = {}
    if attributes:
        s["attributes"] = attributes
    if metadata:
        s["metadata"] = metadata
    if unversioned:
        s["unversioned"] = unversioned
    return s


def doc_key(path: str) -> str:
    return join(path, "zarr.json")


@dataclass
class StoreOutput:
    """A store profile's output (§1.4
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>): JSON documents,
    and the chunk entries, each referencing its whole object through its own url source."""

    url: str
    docs: dict[str, dict[str, object]] = field(default_factory=dict)
    chunks: list[tuple[str, int]] = field(default_factory=list)  # (key, size), sorted, size > 0
    summary: dict[str, object] = field(default_factory=dict)
    # The source key of each entry whose key is not its object's (the objects under refkv_source/objects/).
    origins: dict[str, str] = field(default_factory=dict)
    # The CRC-32C of a url source's bytes, when the references are to carry
    # checksums (spec/refkv.md §4.2
    # <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#42-range>); set by
    # refkv.zarrify._zarrify_store.
    checksum: Callable[[str, int, int], int] | None = None

    def declare(self, profile: str, omes: Mapping[str, Mapping[str, object]] | None = None) -> None:
        """Declares the profile's convention (conventions §2
        <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#2-attributes>). Each document's
        `attributes` holds, until then, the attributes copied from the source;
        they move into the property `refkv_zarrified` (as its member named
        after the profile), and the node's attributes are the member `ome` that
        `omes` gives for its path, if any, and the convention's members. The
        root always declares the convention; any other node only when it has
        copied attributes."""
        omes = omes or {}
        for key, doc in self.docs.items():
            path = key[: -len("zarr.json")].rstrip("/")
            target = {"ome": omes[path]} if path in omes else {}
            url = self.url if path == "" else None
            attributes = mapping(doc["attributes"], f"{key}: attributes")
            self.docs[key] = {**doc, "attributes": declare(target, profile, url, attributes)}

    def check_keys(self) -> None:
        for key in [*self.docs, *(k for k, _ in self.chunks)]:
            if len(key.encode()) > 65535:
                raise Rejected("an output key is longer than 65535 bytes")
            if key.startswith("__vz__/"):
                raise Rejected(f"output key {key!r} is in the reserved __vz__/ space")

    def keep_objects(self, objects: dict[str, int], used: set[str], nodes: Iterable[str],
                     ignored: Iterable[str] = ()) -> int:
        """Adds every nonempty object of the store that is not in `used` (the node
        documents the hierarchy represents, the nonempty chunk objects, the objects
        referenced under their own key, and those the convention leaves out), whole,
        at its `object_key`, lists the keys of the empty ones (empty chunk objects
        included) and the recorded `ignored` keys (§1.4
        <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>), and adds the
        group
        `refkv_source` when there is one (spec/zarrify/zarr2.md §5
        <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#5-other-objects>,
        spec/zarrify/n5.md §6
        <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/n5.md#6-other-objects>). Returns
        how many objects are kept. A hierarchy with a
        node at `refkv_source` or below it then rejects the input (`nodes`: every node path)."""
        empty = sorted(k for k, n in objects.items() if n == 0 and k not in used)
        kept = [(k, n) for k, n in objects.items() if n > 0 and k not in used]
        ignored = sorted(ignored)
        if not kept and not empty and not ignored:
            return 0
        own: dict[str, object] | None = {**({"empty": empty} if empty else {}),
                                         **({"ignored": ignored} if ignored else {})} or None
        if self.docs.get(doc_key(""), {}).get("node_type") != "array":
            for p in nodes:
                if p == SOURCE_GROUP or p.startswith(SOURCE_GROUP + "/"):
                    raise Rejected(f"the node {p} is where the store's other objects go ({OBJECTS}...)")
            # The keys of empty and ignored objects are the source metadata of refkv_source.
            profile = string(at(self.docs[doc_key("")], "attributes", CONVENTION_KEY, "profile"), "profile")
            self.docs[doc_key(SOURCE_GROUP)] = dict(GROUP_IMPLICIT, attributes=declare({}, profile, None, own))
        elif own:
            # An array has no children: under a root array, the keys are plain keys of the archive.
            self.docs[EMPTY_KEY] = own
        for k, _ in kept:
            self.origins[object_key(k)] = k
        self.chunks = sorted(self.chunks + [(object_key(k), n) for k, n in kept])
        return len(kept)

    @property
    def sources(self) -> list[str]:
        return [object_url(self.url, self.origins.get(k, k)) for k, _ in self.chunks]

    def refs(self) -> dict[str, list[tuple[int, int, int]]]:
        return {k: [(i, 0, n)] for i, (k, n) in enumerate(self.chunks)}

    def write(self, out_path: str) -> None:
        with open(out_path, "wb") as fh:
            w = VZipWriter(fh, page_size=1 << 16, checksum=self.checksum)
            # each source pins its object's listed size (§1.4
            # <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>)
            w.add_url_refs(((k, object_url(self.url, self.origins.get(k, k)), n) for k, n in self.chunks),
                           pin_size=True)
            for key in sorted(self.docs):
                # The empty objects' keys are UTF-8, as JSON.stringify writes them (the documents
                # may hold strings that are not, and are escaped).
                data = json.dumps(self.docs[key], separators=(",", ":"), ensure_ascii=key != EMPTY_KEY).encode()
                # The hierarchy's documents are read when the archive opens; those of refkv_source
                # (the empty objects' keys), like chunks, only when asked for.
                source = key.startswith(SOURCE_GROUP + "/")
                w.add_bytes(key, data, compress=source, late=not source)
            w.close()

    def to_store(self, *, policy: Policy | None = None, info: dict[str, object] | None = None) -> ReferenceStore:
        """The output as a `refkv.ReferenceStore`, which `save` writes as the archive
        `write` writes, byte for byte (`refkv.zarrify.common.reference_store`)."""
        sources = [Source(url=object_url(self.url, self.origins.get(k, k)), size=n) for k, n in self.chunks]
        refs = [(k, [Range(source=i, offset=0, length=n)]) for i, (k, n) in enumerate(self.chunks)]
        entries = [(key, json.dumps(self.docs[key], separators=(",", ":"), ensure_ascii=key != EMPTY_KEY).encode(),
                    not key.startswith(SOURCE_GROUP + "/")) for key in sorted(self.docs)]
        return reference_store(refs, entries, sources, checksum=self.checksum, policy=policy, info=info)
