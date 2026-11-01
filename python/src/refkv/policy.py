"""The reader policy of spec §6.7
<https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>, and the range checksum
of spec §4.2 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#42-range>."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from refkv import _core
from refkv._extras import OBSTORE_HINT
from refkv.errors import ResolutionError

# The defaults of spec §6.7
# <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>'s table.
MAX_SOURCES = 1 << 22
MAX_READS = 1024
MAX_FORMAT_ENTRY = 256 << 20

_ENCODED = re.compile(r"%(2[eEfF]|5[cC])")


def crc32c(data: bytes) -> int:
    """CRC-32C (Castagnoli, RFC 3720 §B.4) of `data`."""
    return _core.crc32c(bytes(data))


def _scheme(url: str) -> str:
    """The scheme of an absolute URL, lowercase, with https counted as http (§6.7
    <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy> rule 1)."""
    s = url.split(":", 1)[0].lower() if ":" in url else ""
    return "http" if s == "https" else s


def matches_prefix(url: str, prefix: str) -> bool:
    """Does `url` match `prefix` (spec §6.7
    <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy> rule 2)?"""
    if not url.startswith(prefix):
        return False
    rest = url[len(prefix):]
    if not (prefix.endswith("/") or rest == "" or rest[0] in "/?#"):
        return False
    path = re.split(r"[?#]", url, maxsplit=1)[0]
    return not _ENCODED.search(path)


# ------------------------------------------------------------ address classes (§6.7
# <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy> rule 3)

LOOPBACK, PRIVATE, LINK_LOCAL, SPECIAL, PUBLIC = "loopback", "private", "link-local", "special", "public"

_NETS = [(c, ipaddress.ip_network(n)) for c, ns in [
    (LOOPBACK, ["127.0.0.0/8", "::1/128"]),
    (PRIVATE, ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7"]),
    (LINK_LOCAL, ["169.254.0.0/16", "fe80::/10"]),
    (SPECIAL, ["0.0.0.0/8", "100.64.0.0/10", "192.0.0.0/24", "192.0.2.0/24", "198.18.0.0/15",
               "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/3", "::/8", "64:ff9b:1::/48",
               "100::/63", "2001::/23", "2001:db8::/32", "3fff::/20", "5f00::/16", "fec0::/10",
               "ff00::/8"]),
] for n in ns]
_MAPPED = ipaddress.ip_network("::ffff:0:0/96")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_6TO4 = ipaddress.ip_network("2002::/16")


def address_class(address: str | ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    """The class of an IP address (spec §6.7
    <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy> rule 3): loopback,
    private, link-local, special or public. An IPv6 address that embeds an IPv4 one
    (IPv4-mapped, NAT64, 6to4) has the IPv4 address's class."""
    if isinstance(address, str):
        address = ipaddress.ip_address(address.split("%", 1)[0])  # drop an IPv6 zone
    if isinstance(address, ipaddress.IPv6Address):
        n = int(address)
        if address in _MAPPED or address in _NAT64:
            return address_class(ipaddress.IPv4Address(n & 0xFFFFFFFF))
        if address in _6TO4:
            return address_class(ipaddress.IPv4Address((n >> 80) & 0xFFFFFFFF))
    for c, net in _NETS:
        if address.version == net.version and address in net:
            return c
    return PUBLIC


def host_class(host: str) -> str | None:
    """The class of a URL host as it is written: an IP literal's class, loopback
    for `localhost` and names under `.localhost` (RFC 6761), and None for any
    other name, whose class depends on what it resolves to."""
    h = host.lower().rstrip(".")
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    if h == "localhost" or h.endswith(".localhost"):
        return LOOPBACK
    try:
        return address_class(h)
    except ValueError:
        return None


def public_s3_url(url: str) -> str | None:
    """The HTTPS URL of the object `s3://bucket/key`: `https://bucket.s3.amazonaws.com/key`,
    which S3 answers for a bucket in any region (at worst by redirecting to the
    region's endpoint, which the reader follows), and without credentials for a
    public bucket. None for any other URL, and for a bucket name with a dot,
    which that host name cannot carry under TLS.

    Pass it as `rewrite_urls` to `ReferenceStore.from_virtualizarr` to write a
    public bucket's references as URLs that the default reader policy allows."""
    m = re.fullmatch(r"(?i:s3)://([a-z0-9][a-z0-9-]*[a-z0-9])(/.*)?", url)
    return None if m is None else f"https://{m[1]}.s3.amazonaws.com{m[2] or '/'}"


def scheme_remedy(url: str) -> str:
    """How to read a source whose scheme the default reader policy refuses: the
    second sentence of the error, at write and at read time."""
    s = url.split(":", 1)[0].lower()
    root = f"{s}://{urlsplit(url).netloc}"
    if public_s3_url(url) is not None:
        return ("If the bucket is public, write its HTTPS URLs instead "
                "(ReferenceStore.from_virtualizarr(vds, rewrite_urls=refkv.public_s3_url)); "
                f"or read it with registry={{{root!r}: S3Store(...)}}, a store for the "
                f"bucket ({OBSTORE_HINT}; help(refkv.reader.Resolver)), and save it with "
                "policy=Policy(schemes={'s3'}).")
    return (f"Write http(s) URLs instead, or read it with registry={{{root!r}: store}}, a "
            f"store that serves those URLs (help(refkv.reader.Resolver)), and save it with "
            f"policy=Policy(schemes={{{s!r}}}).")


@dataclass(frozen=True)
class Policy:
    """What a reader may resolve, and its resource limits (spec §6.7
    <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>).

    - `schemes`: schemes allowed besides those rule 1 allows by default
      (`http` and `https` for every archive, `file` for a local one), such as
      `s3`. It may not name `file`: see `allow_files_from_remote_archives`.
      A reader needs a store for such a URL anyway (`registry=`), and a
      URL under a registered prefix is allowed without it; so `schemes`
      matters most to `ReferenceStore.save`, which checks the sources it
      writes against the policy of the archive's future readers.
    - `prefixes`: if given, a URL is allowed if and only if it matches one of
      them (rule 2), whatever its scheme. Rule 3 still applies.
    - `allow_files_from_remote_archives`: UNSAFE. Lets an archive opened from
      a URL name `file:` sources, that is, read the reader's local files.
    - `allow_file_hosts`: UNSAFE. Lets a `file:` URL name a host
      (`file://server/share/x`), which on Windows is the UNC path
      `\\\\server\\share\\x`: reading it connects to that server over the
      network, and may send it the user's credentials (spec §5
      <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#5-source-table>). Off by
      default, for every archive; on other systems such a URL has no local
      path, and is refused anyway. An archive opened from a UNC path reads
      itself without it; the sources it names need it.
    - `allow_private_hosts`: UNSAFE. Lets the archive send requests to
      loopback, private, link-local (cloud metadata) and other special
      addresses (rule 3), which every archive, local or remote, is otherwise
      refused. Rule 3's checks are then skipped, proxies included. The rule
      covers the reads of an archive at an `http(s)` URL too, which refkv
      makes with the client that reads sources: its host, every address
      connected to and every redirect, as spec §6.7
      <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy> allows a
      reader to; so opening `http://localhost:8000/a.vzip` needs this setting.
    - `allow_unchecked_proxy`: UNSAFE. Lets requests go through an HTTP proxy
      while rule 3 applies. The proxy connects, so the reader cannot check
      the address it reaches; it checks what the name resolves to locally,
      which DNS rebinding gets past. Without it, a request that would go
      through a proxy is refused.
    - `max_sources`, `max_reads`, `max_format_entry`: rules 4 to 6.

    Rule 7 (unverifiable pins) has no option here: this reader always sees a
    response's headers, so every pin it is given can be checked.

    For example, to read only sources under one prefix, or to save a
    `refkv.ReferenceStore` whose references name an S3 bucket, which the
    archive's readers will read with a store registered for the bucket
    (`help(refkv.reader.Resolver)`):

        policy = refkv.Policy(prefixes=("https://data.example/archive/",))
        store = refkv.open("data.vzip", policy=policy)

        refs = refkv.ReferenceStore.from_virtualizarr(vds, registry=registry)
        refs.save("data.vzip", policy=refkv.Policy(schemes={"s3"}))

    `ReferenceStore.save` checks the references it writes against a policy
    too, `Policy()` by default, so an archive that the default reader cannot
    read fails when it is written, not when it is read.
    """

    schemes: frozenset[str] = frozenset()
    prefixes: tuple[str, ...] | None = None
    allow_files_from_remote_archives: bool = False
    allow_file_hosts: bool = False
    allow_private_hosts: bool = False
    allow_unchecked_proxy: bool = False
    max_sources: int = MAX_SOURCES
    max_reads: int = MAX_READS
    max_format_entry: int = MAX_FORMAT_ENTRY

    def __post_init__(self) -> None:
        if any(s.lower() == "file" for s in self.schemes):
            raise ValueError("Policy.schemes may not name 'file': a local archive reads files "
                             "already, and a remote one needs the unsafe "
                             "allow_files_from_remote_archives=True")

    def check(self, base: str, url: str, *, registered: bool = False) -> None:
        """Raise ResolutionError unless rules 1 and 2 allow `url` for an archive at `base`,
        and rule 3 allows its host as written (an IP literal, or `localhost`).

        `registered`: the application registered a store for a prefix of `url`
        (`registry=`), which allows it under rule 1 whatever its scheme, except
        a `file:` URL for a remote archive. A prefix list (rule 2) still decides
        alone, and rule 3, and the refusal of a `file:` URL's host without
        `allow_file_hosts`, still apply."""
        if self.prefixes is not None:
            if not any(matches_prefix(url, p) for p in self.prefixes):
                raise ResolutionError(f"{url}: not under an allowed URL prefix (spec §6.7 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>)")
        else:
            s, local = _scheme(url), _scheme(base) == "file"
            if s == "file":
                if not (local or self.allow_files_from_remote_archives):
                    raise ResolutionError(
                        f"{url}: an archive opened from a URL may not read local files "
                        f"(spec §6.7 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>)")
            elif s != "http" and not registered and s not in {_scheme(x + ":") for x in self.schemes}:
                raise ResolutionError(
                    f"{url}: the scheme {s!r} is not allowed "
                    f"(spec §6.7 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>). "
                    + scheme_remedy(url))
        if _scheme(url) == "file" and not self.allow_file_hosts:
            try:
                host = urlsplit(url).netloc
            except ValueError:
                host = ""
            if host.lower() not in ("", "localhost"):
                raise ResolutionError(
                    f"{url}: a file: URL with the host {host!r} (a UNC path) reads over the "
                    f"network, which the reader does not do without allow_file_hosts=True "
                    f"(spec §5 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#5-source-table>)")
        self.check_host(url)

    def check_host(self, url: str) -> None:
        """Raise ResolutionError unless rule 3 allows the host of an http(s) `url` as
        it is written: an IP literal by its class, `localhost` as loopback. A name
        passes here; the reader checks each address it resolves to
        (`check_address`). Rules 1 and 2 are not applied, so this is the check
        for the archive's own URL, which the application chose, and its
        redirects."""
        if _scheme(url) == "http":
            try:
                host = urlsplit(url).hostname
            except ValueError:
                host = None
            c = host_class(host) if host else None
            if c is not None:
                self._check_class(url, c)

    def check_address(self, url: str, address: str) -> None:
        """Raise ResolutionError unless rule 3 lets the reader connect to `address` for `url`."""
        self._check_class(url, address_class(address), address)

    def _check_class(self, url: str, c: str, address: str | None = None) -> None:
        if c == PUBLIC or self.allow_private_hosts:
            return
        at = f" (at {address})" if address else ""
        raise ResolutionError(f"{url}: the host{at} is a {c} address, which the reader does not "
                              f"reach without allow_private_hosts=True (spec §6.7 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>)")
