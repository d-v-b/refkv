"""A store's listing (spec/zarrify.md §1.5
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>): the XML subset
an S3 ListObjectsV2 response is read in, and the objects and continuation token of one
response.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import NoReturn

from refkv.zarrify.common import Rejected
from refkv.zarrify.store.documents import MAX_DEPTH, MAX_SAFE


def _is_char(c: int) -> bool:
    return c in (0x9, 0xA, 0xD) or 0x20 <= c <= 0xD7FF or 0xE000 <= c <= 0xFFFD or 0x10000 <= c <= 0x10FFFF


_NAME_START = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_:")
_NAME_CHAR = _NAME_START | frozenset("0123456789.-")
_WS = frozenset(" \t\r\n")
_ENTITIES = {"lt": "<", "gt": ">", "amp": "&", "quot": '"', "apos": "'"}
_HEX = frozenset("0123456789abcdefABCDEF")
_DEC = frozenset("0123456789")


@dataclass
class Element:
    name: str
    children: list[Element] = field(default_factory=list)
    text: list[str] = field(default_factory=list)
    # The attributes, (name, value) in document order, values with references replaced.
    attributes: list[tuple[str, str]] = field(default_factory=list)


class _Xml:
    """The XML subset of §1.5
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>. `what` names
    the document in rejections."""

    def __init__(self, s: str, what: str = "listing") -> None:
        self.s = s
        self.pos = 0
        self.what = what
        self.depth = 0  # of the element being read

    def fail(self, why: str) -> NoReturn:
        raise Rejected(f"{self.what} is not well formed: {why} at {self.pos}")

    def ws(self) -> None:
        s, n = self.s, len(self.s)
        while self.pos < n and s[self.pos] in _WS:
            self.pos += 1

    def at(self, t: str) -> bool:
        return self.s.startswith(t, self.pos)

    def comment(self) -> None:
        end = self.s.find("--", self.pos + 4)
        if end < 0 or not self.s.startswith("-->", end):
            self.fail("bad comment")
        self.chars(self.s[self.pos + 4 : end])
        self.pos = end + 3

    def misc(self) -> None:
        while True:
            self.ws()
            if self.at("<!--"):
                self.comment()
            else:
                return

    def chars(self, t: str) -> None:
        for ch in t:
            if not _is_char(ord(ch)):
                self.fail(f"character U+{ord(ch):04X}")

    def name(self) -> str:
        s, start = self.s, self.pos
        if start >= len(s) or s[start] not in _NAME_START:
            self.fail("expected a name")
        self.pos += 1
        while self.pos < len(s) and s[self.pos] in _NAME_CHAR:
            self.pos += 1
        return s[start : self.pos]

    def reference(self) -> str:
        s = self.s
        end = s.find(";", self.pos)
        if end < 0:
            self.fail("unterminated reference")
        body = s[self.pos + 1 : end]
        self.pos = end + 1
        if body in _ENTITIES:
            return _ENTITIES[body]
        if body.startswith("#x") and len(body) > 2 and all(c in _HEX for c in body[2:]):
            v = int(body[2:], 16) if len(body) < 12 else -1  # noqa: FURB166 (an XML reference, &#x41;, not 0x41)
        elif body.startswith("#") and len(body) > 1 and all(c in _DEC for c in body[1:]):
            v = int(body[1:]) if len(body) < 12 else -1
        else:
            self.fail(f"unknown reference &{body};")
        if not _is_char(v):
            self.fail(f"reference &{body}; is not a character")
        return chr(v)

    def chardata(self, stop: str) -> str:
        """Text up to `<` or `&` (or the quote `stop`), checked."""
        s, start = self.s, self.pos
        while self.pos < len(s) and s[self.pos] not in "<&" and s[self.pos] != stop:
            self.pos += 1
        t = s[start : self.pos]
        self.chars(t)
        return t

    def element(self) -> Element:
        if not self.at("<"):
            self.fail("expected an element")
        self.depth += 1
        if self.depth > MAX_DEPTH:
            self.fail(f"elements nest more than {MAX_DEPTH} deep")
        el = self._element()
        self.depth -= 1
        return el

    def _element(self) -> Element:
        self.pos += 1
        el = Element(self.name())
        s = self.s
        while True:
            if self.pos < len(s) and s[self.pos] in _WS:
                self.ws()
                if self.pos < len(s) and s[self.pos] in _NAME_START:
                    name = self.name()
                    self.ws()
                    if not self.at("="):
                        self.fail("expected =")
                    self.pos += 1
                    self.ws()
                    q = s[self.pos] if self.pos < len(s) else ""
                    if q not in ("'", '"'):
                        self.fail("expected a quoted value")
                    self.pos += 1
                    value = []
                    while True:
                        value.append(self.chardata(q))
                        if self.at("&"):
                            value.append(self.reference())
                        elif self.at(q):
                            self.pos += 1
                            break
                        else:
                            self.fail("bad attribute value")
                    el.attributes.append((name, "".join(value)))
                    continue
            break
        if self.at("/>"):
            self.pos += 2
            return el
        if not self.at(">"):
            self.fail("expected >")
        self.pos += 1
        while True:
            if self.pos >= len(s):
                self.fail("unterminated element")
            if self.at("</"):
                self.pos += 2
                if self.name() != el.name:
                    self.fail("mismatched end tag")
                self.ws()
                if not self.at(">"):
                    self.fail("expected >")
                self.pos += 1
                return el
            if self.at("<!--"):
                self.comment()
            elif self.at("<!") or self.at("<?"):
                self.fail("unsupported markup")
            elif self.at("<"):
                el.children.append(self.element())
            elif self.at("&"):
                el.text.append(self.reference())
            else:
                t = self.chardata("<")
                if "]]>" in t:
                    self.fail("]]> in character data")
                el.text.append(t)

    def document(self) -> Element:
        self.ws()
        if self.at("<?xml") and self.pos + 5 < len(self.s) and self.s[self.pos + 5] in _WS:
            end = self.s.find("?>", self.pos + 5)
            if end < 0:
                self.fail("unterminated XML declaration")
            self.chars(self.s[self.pos : end])
            self.pos = end + 2
        self.misc()
        root = self.element()
        self.misc()
        if self.pos != len(self.s):
            self.fail("content after the root element")
        return root


def parse_xml(text: str, what: str) -> Element:
    """The root element of a document in the XML subset of §1.5
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>, or Rejected
    (naming `what`)."""
    return _Xml(text, what).document()


def _text(el: Element) -> str:
    if el.children:
        raise Rejected(f"listing: <{el.name}> has a child element")
    return "".join(el.text)


def parse_listing(body: bytes, prefix: str) -> tuple[list[tuple[str, int]], str | None]:
    """The objects listed by one ListObjectsV2 response, and the continuation
    token if the listing is truncated (§1.5
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>)."""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise Rejected("listing is not UTF-8") from None
    root = _Xml(text).document()
    if root.name != "ListBucketResult":
        raise Rejected(f"listing root element is <{root.name}>, not <ListBucketResult>")
    truncated = [c for c in root.children if c.name == "IsTruncated"]
    if len(truncated) != 1 or _text(truncated[0]) not in ("true", "false"):
        raise Rejected("listing needs exactly one IsTruncated of true or false")
    tokens = [c for c in root.children if c.name == "NextContinuationToken"]
    if len(tokens) > 1:
        raise Rejected("listing has more than one NextContinuationToken")
    token = _text(tokens[0]) if tokens else None
    objects = []
    for c in root.children:
        if c.name != "Contents":
            continue
        keys = [x for x in c.children if x.name == "Key"]
        sizes = [x for x in c.children if x.name == "Size"]
        if len(keys) != 1 or len(sizes) != 1:
            raise Rejected("listing Contents needs exactly one Key and one Size")
        key, size = _text(keys[0]), _text(sizes[0])
        if not size or not all(ch in _DEC for ch in size) or len(size.lstrip("0")) > 16 or int(size) > MAX_SAFE:
            raise Rejected(f"listing Size {size[:40]!r} is not a size")
        if not key.startswith(prefix):
            raise Rejected(f"listed key {key!r} is outside the prefix {prefix!r}")
        objects.append((key, int(size)))
    if _text(truncated[0]) == "true":
        if not token:
            raise Rejected("truncated listing without a NextContinuationToken")
        if not objects:  # a page that lists nothing could be followed forever
            raise Rejected("truncated listing without a Contents")
        return objects, token
    return objects, None
