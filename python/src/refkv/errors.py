"""Error classes of spec §6.4 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#64-errors>.
Each is a ValueError so existing callers keep working."""


class RefkvError(ValueError):
    cls = "error"


class ArchiveError(RefkvError):
    cls = "archive"


class EntryError(RefkvError):
    cls = "entry"


class BodyError(RefkvError):
    cls = "body"


class PayloadError(RefkvError):
    cls = "payload"


class ResolutionError(RefkvError):
    cls = "resolution"


class RequestError(RefkvError):
    cls = "request"
