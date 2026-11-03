"""Error classes of spec §6.4 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#64-errors>.

Each is a ValueError, and its `cls` is the class's name as the spec gives it and readers
report it (spec §6.5 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#65-error-reporting>):
"archive", "entry", "body", "payload", "resolution" or "request"."""


class RefkvError(ValueError):
    """The base of the six error classes; `cls` names the class."""

    cls = "error"


class ArchiveError(RefkvError):
    """The store cannot be opened: something opening checks (spec §6.1
    <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#61-opening>) failed."""

    cls = "archive"


class EntryError(RefkvError):
    """A key's kind cannot be told, or its record breaks a rule of the binding: classify, get and
    raw fail for that key alone."""

    cls = "entry"


class BodyError(RefkvError):
    """The bytes the store holds under a key cannot be read as the binding requires: get and raw
    of that key fail."""

    cls = "body"


class PayloadError(RefkvError):
    """A reference payload is malformed (spec §4.4
    <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#44-reference-payloads>): get fails for
    that key."""

    cls = "payload"


class ResolutionError(RefkvError):
    """A range of a reference cannot be resolved: its source, a pin that does not hold, or a URL
    the reader policy refuses (spec §6.7
    <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>)."""

    cls = "resolution"


class RequestError(RefkvError):
    """A request is invalid (`start > end`) or exceeds a resource limit of the reader, such as the
    requests one get may make: that operation fails."""

    cls = "request"
