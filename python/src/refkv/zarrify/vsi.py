"""The host's side of an input read with the files beside it: a VSI input (spec/zarrify/vsi.md
§9.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#92-the-tile-files>), the
`.vsi` file named by the input and the tile files beside it, and a NRRD input
(spec/zarrify/nrrd.md §8.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nrrd.md#83-the-data-file>),
the header file named by the input and a detached header's data file. The Rust core's `vsi` and
`nrrd` store formats ask for those files by key (`refkv.ir.store.drive_store`'s probes) rather
than from a listing, since a plain web server cannot list a directory. The files are the objects
of a store whose URL is the named file's directory, each read through a transport of its own
(`refkv.ir.planner`: a mapped local file, or an http(s) object under the reader policy); a
probe opens the file, and an object that does not exist (a local file that is missing, an
HTTP 404) is absent.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import override
from urllib.parse import unquote

from refkv.ir.planner import FileTransport, HttpTransport, Transport, _FatalError
from refkv.policy import Policy
from refkv.uri import path_reference
from refkv.zarrify.common import Rejected
from refkv.zarrify.store import Store, check_store_url, object_url


def split_input(location: str, title: str = "VSI") -> tuple[str, str]:
    """(the directory, the named file's key) of an input of the format `title` (VSI, NRRD): for
    an http(s) URL the directory is the URL up to its last `/` and the key the last segment,
    percent-decoded; for a local path, its directory (empty for a bare name) and its name."""
    if location.startswith(("http://", "https://")):
        base = location.split("?", 1)[0].split("#", 1)[0]
        if "?" in location or "#" in location:
            raise Rejected(f"{title} input {location!r} has a query or a fragment")
        d, _, name = base.rpartition("/")
        key = unquote(name)
        if not key or "/" in key:
            raise Rejected(f"{title} input {location!r} names no file")
        return d + "/", key
    return os.path.split(location)


class VsiFiles(Store):
    """The files of a VSI or NRRD input, as a store (§1.4
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>) the run probes:
    `objects` holds the named file (the `.vsi` file, a NRRD header file), then every file found
    beside it. `url` is the directory's URL, the store URL the output's object URLs are made from
    (`object_url`): the directory of the input URL, or, for a local path, `url` as given (the URL
    the directory will be served from, ending in `/`), else the path's URI reference. `title`
    names the format in a rejection, and `noun` the named file."""

    def __init__(self, location: str, url: str | None = None, *, policy: Policy | None = None,
                 workers: int | None = None, transport: Transport | None = None, title: str = "VSI",
                 noun: str = ".vsi file") -> None:
        self.remote = location.startswith(("http://", "https://"))
        self.location, self.key = split_input(location, title)
        if url is not None:
            # the URL the named file is named by (--url): the store URL is its directory's
            d, name = split_input(url, title)
            check_store_url(d)
            if name != self.key:
                raise Rejected(f"--url {url!r} does not name the {noun} {self.key!r}")
            self.url = d
        elif self.remote:
            self.url = self.location
        elif self.location:
            ref = path_reference(self.location)
            self.url = ref if ref.endswith("/") else ref + "/"
        else:
            self.url = ""  # a bare file name: its files are named relative to the archive
        self.policy = policy
        self.workers = max(1, workers or (8 if self.remote else 1))
        self.objects = {}
        self.ignored = []
        self.folders = []
        self.transports = {}
        self._lock = threading.Lock()
        self.requests = 0
        t = transport or self._open(self.key)
        if t is None:
            raise FileNotFoundError(location)
        self.transports[self.key] = t
        self.objects[self.key] = t.size

    def _open(self, key: str) -> FileTransport | HttpTransport | None:
        """A transport of the object `key`, or None when it does not exist."""
        if self.remote:
            try:
                t = HttpTransport(object_url(self.location, key), self.policy)
            except _FatalError as e:
                if " HTTP 404 " in str(e):
                    return None
                raise
            return t
        path = Path(self.location or os.curdir, *key.split("/"))
        if not path.is_file():
            return None
        return FileTransport(str(path))

    @override
    def probe(self, key: str) -> int | None:
        with self._lock:
            if key in self.objects:
                return self.objects[key]
        t = self._open(key)
        if t is None:
            return None
        with self._lock:
            self.transports[key] = t
            self.objects[key] = t.size
            self.requests += 1  # an object opened beyond the named file (the summary's listingRequests)
        return t.size

    @override
    def read(self, key: str) -> bytes:
        return self.read_range(key, 0, self.objects[key])

    @override
    def read_range(self, key: str, offset: int, length: int) -> bytes:
        t = self.transports[key]
        if length == 0:
            return b""
        held = t.head[1]
        if offset + length <= len(held):
            return held[offset:offset + length]
        data = t.get(offset, offset + length)
        if len(data) != length:
            raise OSError(f"{key}: {len(data)} bytes for [{offset}, {offset + length})")
        return data

    def etag(self, key: str) -> str | None:
        """The object's strong ETag, when every response gave the same one."""
        t = self.transports.get(key)
        return t.etag() if t is not None and self.remote else None

    @override
    def _release(self) -> None:
        pass

    @override
    def close(self) -> None:
        pass
