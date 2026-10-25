"""The round-3 probes (design/IR_NOTES.md §21
<https://github.com/d-v-b/refkv/blob/main/design/IR_NOTES.md#21-round-1s-findings-closed>): round 1's
CZI review findings,
rebuilt, and round 1's TIFF amplification probes, plus IFD loops and huge counts; the
DICOM probes of revision 26; and NDPI's (revision 26): many intervals, loops, huge counts and
offsets, overlapping extents.
Usage: uv run python python/tests/ir/gen_probes.py <out dir> [--small]
(--small writes the tests' sizes: no file over 1 MB.)"""
import os
import struct
import sys
from pathlib import Path

sys.path.insert(0, "fixtures/generators/czi")
from write_fixtures import Att, Czi, Sb, plane, segment, time_stamps  # noqa: E402

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
small = "--small" in sys.argv


def write(name: str, data: bytes) -> None:
    (out / name).write_bytes(data)


# ---- TIFF

def tiff(entries_per_ifd: list[list[tuple]], blobs: bytes = b"", big: bool = False, chain=None) -> bytes:
    """A little-endian TIFF: `blobs` after the header, then the IFDs (entries (tag, type, count, value)),
    chained in order unless `chain` gives each IFD's next (an index, or None)."""
    hs = 16 if big else 8
    es, cs, fs = (20, 8, 8) if big else (12, 2, 4)
    w = "Q" if big else "I"
    start = hs + len(blobs)
    sizes = [cs + es * len(e) + fs for e in entries_per_ifd]
    offs = [start + sum(sizes[:k]) for k in range(len(sizes))]
    body = bytearray()
    for k, e in enumerate(entries_per_ifd):
        nxt = chain[k] if chain is not None else (k + 1 if k + 1 < len(entries_per_ifd) else None)
        body += struct.pack("<" + ("Q" if big else "H"), len(e))
        for tag, typ, count, value in sorted(e):
            body += struct.pack(f"<HH{w}{w}", tag, typ, count, value)
        body += struct.pack("<" + w, offs[nxt] if nxt is not None else 0)
    head = (b"II+\0" + struct.pack("<HHQ", 8, 0, offs[0])) if big else (b"II*\0" + struct.pack("<I", offs[0]))
    return head + blobs + bytes(body)


def amp(planes: int, tiles: int) -> bytes:
    """An OME-TIFF whose planes all name IFD 0, whose tiles all name the same 256 bytes."""
    side = int(tiles ** 0.5)
    tiles = side * side
    td = "".join(f'<TiffData IFD="0" FirstZ="{k}" PlaneCount="1"/>' for k in range(planes))
    xml = (f'<OME><Image ID="Image:0"><Pixels DimensionOrder="XYZCT" Type="uint8" SizeX="{16 * side}" '
           f'SizeY="{16 * side}" SizeZ="{planes}" SizeC="1" SizeT="1">{td}</Pixels></Image></OME>').encode() + b"\0"
    blobs = bytearray()

    def put(b):
        o = 8 + len(blobs)
        blobs.extend(b + b"\0" * (len(b) % 2))
        return o

    tile, xo = put(bytes(256)), put(xml)
    offs, counts = put(struct.pack(f"<{tiles}I", *[tile] * tiles)), put(struct.pack(f"<{tiles}I", *[256] * tiles))
    e = [(256, 4, 1, 16 * side), (257, 4, 1, 16 * side), (258, 3, 1, 8), (259, 3, 1, 1), (262, 3, 1, 1),
         (270, 2, len(xml), xo), (277, 3, 1, 1), (322, 3, 1, 16), (323, 3, 1, 16), (324, 4, tiles, offs),
         (325, 4, tiles, counts)]
    return tiff([e], bytes(blobs))


def shared(n: int, tiles: int) -> bytes:
    """n main-chain IFDs of one size (only IFD 0 is an image) that all name one table of
    `tiles` tiles, which all name the same 256 bytes."""
    side = int(tiles ** 0.5)
    tiles = side * side
    blobs = bytearray()

    def put(b):
        o = 8 + len(blobs)
        blobs.extend(b + b"\0" * (len(b) % 2))
        return o

    tile = put(bytes(256))
    offs, counts = put(struct.pack(f"<{tiles}I", *[tile] * tiles)), put(struct.pack(f"<{tiles}I", *[256] * tiles))
    e = [(256, 4, 1, 16 * side), (257, 4, 1, 16 * side), (258, 3, 1, 8), (259, 3, 1, 1), (262, 3, 1, 1),
         (277, 3, 1, 1), (322, 3, 1, 16), (323, 3, 1, 16), (324, 4, tiles, offs), (325, 4, tiles, counts)]
    return tiff([e] * n, bytes(blobs))


def image(w=32, h=32, extra=()):
    """One tiled 8-bit image of w x h in 16 x 16 tiles, its tiles after the header."""
    n = ((w + 15) // 16) * ((h + 15) // 16)
    blobs = bytes(256 * n)
    offs = struct.pack(f"<{n}I", *[8 + 256 * k for k in range(n)])
    counts = struct.pack(f"<{n}I", *[256] * n)
    at = 8 + len(blobs)
    e = [(256, 4, 1, w), (257, 4, 1, h), (258, 3, 1, 8), (259, 3, 1, 1), (262, 3, 1, 1), (277, 3, 1, 1),
         (322, 3, 1, 16), (323, 3, 1, 16), (324, 4, n, at), (325, 4, n, at + 4 * n), *extra]
    return e, blobs + offs + counts


e, blobs = image()
write("tiff_loop_chain.tif", tiff([e, e], blobs, chain=[1, 0]))  # IFD 1's next is IFD 0: rejected
write("tiff_loop_self.tif", tiff([e], blobs, chain=[0]))  # an IFD whose next is itself: rejected
write("tiff_loop_sub.tif", tiff([[*e, (330, 4, 1, 8 + len(blobs))]], blobs))  # a SubIFD naming its own IFD: rejected
# EXIF pointers to IFD 0 and to the EXIF IFD itself: aliases, accepted
ex = 8 + len(blobs) + 2 + 12 * (len(e) + 1) + 4
write("tiff_loop_pointers.tif", tiff([[*e, (34665, 4, 1, ex)], [(34665, 4, 1, 8 + len(blobs)), (34853, 4, 1, ex)]],
                                     blobs, chain=[None, None]))
# huge counts
write("tiff_huge_entry_count.tif", b"II+\0" + struct.pack("<HHQ", 8, 0, 16) + struct.pack("<Q", 1 << 40) + bytes(64))
write("tiff_huge_table_count.tif", tiff([[*e[:8], (324, 4, 0xFFFFFFFF, 64), e[9]]], blobs))  # outside: rejected
write("tiff_huge_other_count.tif", tiff([[*e, (65000, 7, 0xFFFFFFFF, 64)]], blobs))  # not read: accepted
write("tiff_huge_bigtiff_value.tif", tiff([[*e[:8], (324, 16, 1, 1 << 60), e[9]]], blobs, big=True))  # > 2^53: rejected
write("tiff_many_ifds.tif", tiff([[(256, 4, 1, 1)]] * (1000 if small else 100001)))  # too many IFDs: rejected past 100000
write("tiff_amp_100_100.tif", amp(100, 100))
write("tiff_shared_1000_1024.tif", shared(1000, 1024))
if not small:
    write("tiff_amp_1000_1024.tif", amp(1000, 1024))
    write("tiff_amp_3000_4096.tif", amp(3000, 4096))
    write("tiff_shared_10000_4096.tif", shared(10000, 4096))

# ---- CZI

def sb_plane(t, w, h, data):
    return Sb(0, 0, [("X", 0, w, w), ("Y", 0, h, h), ("T", t, 1, 1)], data)


# 1. Row-band floor: a prime height of 1-byte rows (today's divisor bands: a chunk per row)
if not small:
    H = 16777259
    write("czi_bands1.czi", Czi(subblocks=[Sb(0, 0, [("X", 0, 1, 1), ("Y", 0, H, H)], bytes(H))]).build())
# 2. Quadratic XML: n opens, then n unmatched end tags
n = 2000 if small else 40000
write(f"czi_deep{n}.czi", Czi(subblocks=[plane(0, 0, 0, 0, 4, 4)],
                              xml=("<ImageDocument>" + "<a>" * n + "</b>" * n + "</ImageDocument>").encode()).build())
# 3. XML scan memory: 16 MB of empty tags
n = 100000 if small else 4000000
write("czi_flat.czi", Czi(subblocks=[plane(0, 0, 0, 0, 4, 4)],
                          xml=("<ImageDocument>" + "<a/>" * n + "</ImageDocument>").encode()).build())
# 4. Shared FilePosition: n directory entries naming the first subblock
c = Czi(subblocks=[plane(0, 0, 0, 0, 4, 4), plane(0, 0, 4, 0, 4, 4)])
b = bytearray(c.build())
d = c.offsets["directory"]
entry = bytes(b[d + 160: d + 160 + 32 + 20 * struct.unpack_from("<i", b, d + 160 + 28)[0]])
n = 1000 if small else 100000
body = entry * n
b = b[:d] + segment("ZISRAWDIRECTORY", struct.pack("<i", n).ljust(128, b"\0") + body)
write("czi_dup.czi", bytes(b))
# 5. Coalesced prefetch: n subblocks of 4 KiB, one line each (only their headers are needed)
n, w = (2000, 4096) if small else (100000, 4096)
data = os.urandom(w)
write("czi_xt.czi", Czi(subblocks=[sb_plane(t, w, 1, data) for t in range(n)]).build())
# 6. XML size past the scan: 64 MiB + 1 of XML is not read for the layout
# 7. Unchecked tile extent: a JPEG XR subblock whose coded size is 2^32 - 1 (a tile array of that shape)
sb = plane(0, 4, 0, 0, 16, 16)
bb = bytearray(sb.data)
ifd = struct.unpack_from("<I", bb, 4)[0]
for k in range(struct.unpack_from("<H", bb, ifd)[0]):
    at = ifd + 2 + 12 * k
    tag, typ, cnt, val = struct.unpack_from("<HHII", bb, at)
    if tag in (0xBC80, 0xBC81):
        struct.pack_into("<HHII", bb, at, tag, 4, 1, 0xFFFFFFFF)
sb.data = bytes(bb)
write("czi_jxrbig.czi", Czi(subblocks=[sb]).build())
# 8. Attachment names with bytes after their NUL
write("czi_attname.czi", Czi(subblocks=[plane(0, 0, 0, 0, 4, 4)],
                             attachments=[Att("Label\0hidden name", "CZTIMS\0x", time_stamps([0.5, 1.5])),
                                          Att("Thumb\0\0\0\0\0\0x", "JPG\0junk", b"\xff\xd8\xff\xd9")]).build())
# 9. Per-element cost: many empty segments after the directory
n = 10000 if small else 2000000
write("czi_walk0.czi", Czi(subblocks=[plane(0, 0, 0, 0, 4, 4)], late=[segment("DELETED", b"", allocated=0, used=0) * n]).build())

# ---- DICOM (revision 26): repeated items, depth, fragment chains, offset tables, skipped
# values and per-element cost, at the sizes of a large file

LONG = {b"OB", b"OD", b"OF", b"OL", b"OV", b"OW", b"SQ", b"SV", b"UC", b"UN", b"UR", b"UT", b"UV"}


def el(g: int, e: int, vr: bytes, value: bytes, undefined: bool = False) -> bytes:
    """An element in explicit VR little endian (`value` padded to an even length)."""
    if len(value) % 2:
        value += b"\0" if vr in (b"UI", b"OB", b"UN") else b" "
    n = 0xFFFFFFFF if undefined else len(value)
    if vr in LONG:
        return struct.pack("<HH2s2xI", g, e, vr, n) + value
    return struct.pack("<HH2sH", g, e, vr, n) + value


def us(g: int, e: int, v: int) -> bytes:
    return el(g, e, b"US", struct.pack("<H", v))


def item(body: bytes) -> bytes:
    return struct.pack("<HHI", 0xFFFE, 0xE000, len(body)) + body


def dcm(dataset: bytes, syntax: bytes = b"1.2.840.10008.1.2.1") -> bytes:
    meta = el(2, 0x10, b"UI", syntax)
    return bytes(128) + b"DICM" + el(2, 0, b"UL", struct.pack("<I", len(meta))) + meta + dataset


def image(rows: int, columns: int, frames: int = 1) -> bytes:
    return (us(0x28, 2, 1) + el(0x28, 4, b"CS", b"MONOCHROME2") + (el(0x28, 8, b"IS", str(frames).encode()) if frames > 1 else b"")
            + us(0x28, 0x10, rows) + us(0x28, 0x11, columns) + us(0x28, 0x100, 8) + us(0x28, 0x101, 8) + us(0x28, 0x103, 0))


def encapsulated(fragments: list[bytes], bot: list[int] | None = None) -> bytes:
    body = item(struct.pack(f"<{len(bot)}I", *bot) if bot else b"") + b"".join(item(f) for f in fragments)
    return el(0x7FE0, 0x10, b"OB", body + struct.pack("<HHI", 0xFFFE, 0xE0DD, 0), undefined=True)[:12] + body + \
        struct.pack("<HHI", 0xFFFE, 0xE0DD, 0)


J2K = b"1.2.840.10008.1.2.4.90"
# 1. n identical items of a sequence nothing reads: one run in the IR
n = 2000 if small else 200000
items = b"".join(item(el(0x0008, 0x0100, b"SH", b"CODE")) for _ in range(n))
write("dicom_items_same.dcm", dcm(el(0x0040, 0x0275, b"SQ", items) + image(1, 1) + el(0x7FE0, 0x10, b"OB", b"\0\0")))
# 2. n items of distinct values: no run, the mirror's column runs
items = b"".join(item(el(0x0008, 0x0018, b"UI", f"1.2.3.{k}".encode())) for k in range(n))
write("dicom_items_distinct.dcm", dcm(el(0x0040, 0x0275, b"SQ", items) + image(1, 1) + el(0x7FE0, 0x10, b"OB", b"\0\0")))
# 3. sequences of defined length nested 200 deep: past 64, the innermost walked one keeps its bytes
body = el(0x0008, 0x0100, b"SH", b"X")
for _ in range(200):
    body = el(0x0040, 0xA730, b"SQ", item(body))
write("dicom_deep_defined.dcm", dcm(body + image(1, 1) + el(0x7FE0, 0x10, b"OB", b"\0\0")))
# 4. a fragment chain without an offset table: n frames of 70,000 bytes (each fragment's header is
#    past the last one's window); and the same with a Basic Offset Table, which predicts them
n = 20 if small else 200
frame = b"\xff\x4f" + bytes(69998)
write("dicom_chain_nobot.dcm", dcm(image(1, 1, n) + encapsulated([frame] * n), J2K))
write("dicom_chain_bot.dcm", dcm(image(1, 1, n) + encapsulated([frame] * n, [k * 70008 for k in range(n)]), J2K))
# 5. one frame of many fragments: past the reference payload (65519 bytes), rejected
n = 1000 if small else 20000
write("dicom_fragments_many.dcm", dcm(image(1, 1) + encapsulated([b"\xff\x4f\0\0"] * n), J2K))
# 6. an Extended Offset Table of n frames of 4 bytes
n = 500 if small else 100000
offsets = [k * 12 for k in range(n)]
write("dicom_eot_many.dcm", dcm(image(1, 1, n) + el(0x7FE0, 1, b"OV", struct.pack(f"<{n}Q", *offsets))
                                + el(0x7FE0, 2, b"OV", struct.pack(f"<{n}Q", *[4] * n))
                                + encapsulated([b"\xff\x4f\0\0"] * n), J2K))
# 7. a large private value before Pixel Data: not read
n = (2 << 20) if small else (64 << 20)
write("dicom_big_private.dcm", dcm(el(0x0009, 0x0010, b"LO", b"VZIP") + el(0x0009, 0x1000, b"OB", bytes(n))
                                   + image(1, 1) + el(0x7FE0, 0x10, b"OB", b"\0\0")))
# 8. n frames of one byte: one run of data elements
n = 5000 if small else 1000000
write("dicom_frames_many.dcm", dcm(image(1, 1, n) + el(0x7FE0, 0x10, b"OB", bytes(n))))
# 9. n short private elements
n = 10000 if small else 500000
body = b"".join(el(0x0009 + 2 * (k >> 15), 0x1000 + (k & 0x7FFF), b"SH", b"ab") for k in range(n))
write("dicom_many_elements.dcm", dcm(el(0x0009, 0x0010, b"LO", b"VZIP") + body + image(1, 1) + el(0x7FE0, 0x10, b"OB", b"\0\0")))

# ---- NDPI (spec/zarrify/ndpi.md <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md>)

def jpeg_head(w: int, h: int, r: int = 1) -> bytes:
    """SOI, SOF0 (3 components sampled 1 × 1: MCUs of 8 × 8), DRI `r`, SOS."""
    sof = struct.pack(">BHHB", 8, h, w, 3) + bytes([1, 0x11, 0, 2, 0x11, 0, 3, 0x11, 0])
    sos = bytes([3, 1, 0, 2, 0, 3, 0, 0, 0x3F, 0])
    return (b"\xff\xd8\xff\xc0" + struct.pack(">H", 2 + len(sof)) + sof + b"\xff\xdd\x00\x04" + struct.pack(">H", r)
            + b"\xff\xda" + struct.pack(">H", 2 + len(sos)) + sos)


def ndpi(ifds: list[list[tuple]], blobs: bytes, chain=None) -> bytes:
    """An NDPI: a 16-byte header, `blobs` from 16, then the IFDs (entries (tag, type, count, value),
    a value of up to 64 bits: its low 32 bits in the entry, the rest in its high word)."""
    start = 16 + len(blobs)
    sizes = [2 + 16 * len(e) + 8 for e in ifds]
    offs = [start + sum(sizes[:k]) for k in range(len(sizes))]
    body = bytearray()
    for k, e in enumerate(ifds):
        nxt = chain[k] if chain is not None else (k + 1 if k + 1 < len(ifds) else None)
        e = sorted(e)
        body += struct.pack("<H", len(e))
        for tag, typ, count, value in e:
            body += struct.pack("<HHII", tag, typ, count, value & 0xFFFFFFFF)
        body += struct.pack("<Q", offs[nxt] if nxt is not None else 0)
        for *_, value in e:
            body += struct.pack("<I", value >> 32)
    return b"II*\0" + struct.pack("<Q", offs[0]) + bytes(4) + blobs + bytes(body)


def ndpi_level(q: int, r: int, size: int = 1, mag: float = 20.0):
    """(blobs, entries) of one McuStarts level of q × r intervals of `size` bytes (R = 1)."""
    head = jpeg_head(8 * q, 8 * r)
    strip = bytearray(head)
    starts = []
    for i in range(q * r):
        if i:
            strip += bytes([0xFF, 0xD0 + (i - 1) % 8])
        starts.append(len(strip))
        strip += bytes([0x55]) * size
    strip += b"\xff\xd9"
    blobs = bytearray(struct.pack("<3H", 8, 8, 8) + bytes(2))
    so = 16 + len(blobs)
    blobs += strip + bytes(len(strip) % 2)
    mo = 16 + len(blobs)
    blobs += struct.pack(f"<{len(starts)}I", *starts)
    f = struct.unpack("<I", struct.pack("<f", mag))[0]
    entries = [(256, 4, 1, 8 * q), (257, 4, 1, 8 * r), (258, 3, 3, 16), (259, 3, 1, 7), (262, 3, 1, 6),
               (277, 3, 1, 3), (273, 4, 1, so), (279, 4, 1, len(strip)), (65420, 4, 1, 1), (65421, 11, 1, f),
               (65426, 4, len(starts), mo)]
    return bytes(blobs), entries


# huge counts: many intervals, each chunk's payload over the limit until b is small (each b tried
# is a pass over every interval)
q, r = (256, 256) if small else (1024, 1024)
blobs, e = ndpi_level(q, r)
write(f"ndpi_intervals_{q}x{r}.ndpi", ndpi([e], blobs))
blobs, e = ndpi_level(4, 4)
macro = [(256, 4, 1, 8), (257, 4, 1, 8), (65420, 4, 1, 1), (65421, 11, 1, struct.unpack("<I", struct.pack("<f", -1.0))[0])]
# loops: rejected
write("ndpi_loop_chain.ndpi", ndpi([e, macro], blobs, chain=[1, 0]))
write("ndpi_loop_self.ndpi", ndpi([e], blobs, chain=[0]))
# pointer loops: the macro image's EXIF pointer to itself and to IFD 0, SubIFDs to both: aliases, accepted
first = 16 + len(blobs)
second = first + 2 + 16 * len(e) + 8
write("ndpi_loop_pointers.ndpi", ndpi([e, [*macro, (34665, 4, 1, second), (330, 13, 1, first)]], blobs))
# huge counts and offsets: McuStarts past the file, an entry count past the file, a strip offset
# whose high word puts it past the file: rejected; a pointer whose high word does: not followed
write("ndpi_huge_mcustarts.ndpi", ndpi([[*e[:10], (65426, 4, 0xFFFFFFFF, 16)]], blobs))
huge = bytearray(ndpi([e], blobs))
struct.pack_into("<H", huge, first, 0xFFFF)
write("ndpi_huge_entry_count.ndpi", bytes(huge))
write("ndpi_high_strip.ndpi", ndpi([[*e[:6], (273, 4, 1, e[6][3] + (1 << 40)), *e[7:]]], blobs))
write("ndpi_high_pointer.ndpi", ndpi([e, [*macro, (34665, 4, 1, second + (1 << 40))]], blobs))
# overlapping extents: the macro image's strip is the level's strip, and its McuStarts the level's
# (aliases); a value inside the level's strip (an alias of the strip): accepted
write("ndpi_overlap.ndpi", ndpi([e, [*macro, e[6], e[7], (65426, 4, e[10][2], e[10][3]), (270, 2, 16, e[6][3] + 40)]],
                                blobs))
# McuStartsHighBytes, as many values as McuStarts (all 0, as the slides over 4 GiB write them
# for intervals below it): accepted
blobs2, e2 = ndpi_level(8, 8)
ho = 16 + len(blobs2)
write("ndpi_high_bytes.ndpi", ndpi([[*e2, (65432, 4, e2[10][2], ho)]], blobs2 + bytes(4 * e2[10][2])))
# fewer McuStartsHighBytes than McuStarts: rejected; McuStartsHighBytes of 2^32 as LONG8 (an offset of
# 2^64 and more, which 64 bits would wrap to the low words alone): rejected
write("ndpi_high_bytes_short.ndpi", ndpi([[*e2, (65432, 4, 2, ho)]], blobs2 + bytes(8)))
write("ndpi_high_bytes_wrap.ndpi", ndpi([[*e2, (65432, 16, e2[10][2], ho)]],
                                        blobs2 + struct.pack(f"<{e2[10][2]}Q", *[1 << 32] * e2[10][2])))
# many IFDs: past 100000, rejected (the full size only; with --small, 1001 IFDs, accepted)
write("ndpi_many_ifds.ndpi", ndpi([e] + [macro] * (1000 if small else 100001), blobs))
print(sorted(p.name for p in out.iterdir()))
