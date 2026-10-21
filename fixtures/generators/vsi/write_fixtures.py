"""Writes synthetic Olympus VSI slides to fixtures/vsi/, covering the rules of the VSI profile
(spec/virtualize/vsi.md §15
<https://github.com/d-v-b/refkv/blob/main/spec/virtualize/vsi.md#15-vsi-profile>) and its
convention: each `<name>.vsi` (a little-endian TIFF whose byte 8 starts the metadata tree, with
the macro image and the thumbnails as IFDs inside the tree's regions) and its sidecar directory
`_<name>_/stack<N>/frame_t.ets` (or `frame_t_0.ets`), the tile files: a 64-byte SIS header, a
228-byte ETS header, the tiles, and the tile directory at the end.

The layout is the one read from Olympus's files (Image_V4.1_BF.vsi and the Zenodo
metadataTest_01.vsi of the public samples): the tree's containers and fields are laid out as
§2.1 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/vsi.md#21-containers-and-fields>
describes them (a field with a second tag counts its data from the container's start plus 20),
and the tags are those Bio-Formats' CellSensReader reads, so that Bio-Formats opens the
accepting fixtures too (python/tests/ir/test_vsi.py checks them against it when a JVM is at
hand). Pixels are seeded gradients with noise, unique per tile, so that a misplaced tile cannot
pass; the generator writes each stack's planes as `.npy` arrays beside the fixtures
(`fixtures/vsi/expected/<name>/<stack>.npy`, the level-0 image in TCZYX order), which the tests
compare the virtualized arrays with.

Accepting fixtures: `vsi_rgb_jpeg` (an overview and a 20x stack of RGB JPEG tiles in pyramids,
a tile missing, a focus map stored as an IFD, a macro JPEG with JPEG tables and an EXIF IFD),
`vsi_raw_tzc` (a raw uint16 stack with T, Z and C dimensions and an unused coordinate, one
tile a plane, `frame_t_0.ets`, 12-bit background), `vsi_jpeg2000_c` (JPEG 2000 uint16 tiles
with a channel dimension, in a pyramid), `vsi_raw_bgr` (raw RGB tiles in BGR order),
`vsi_raw_gray_pyramid` (raw uint8 tiles, a sparse level), `vsi_raw_types` (one stack per raw
pixel type the tables name), `vsi_no_stacks` (the macro image alone), `vsi_metadata_edges`
(units other than micrometers, no origin, no display limits, an unknown dimension of size 1,
an unknown stack type, a blob field with a second tag, an empty container, text that is not
ASCII, both tile file names present).

Rejecting fixtures (`vsi_reject_*`), each the smallest input that breaks one rule.

Usage: uv run python fixtures/generators/vsi/write_fixtures.py
"""

from __future__ import annotations

import shutil
import struct
from pathlib import Path

import imagecodecs
import numpy as np

OUT = Path(__file__).parents[2] / "vsi"
EXPECTED = OUT / "expected"

# ---------------------------------------------------------------- the tree

SHORT, LONG, UNDEFINED, ASCII, RATIONAL = 3, 4, 7, 2, 5
PACK = {1: "B", 2: "c", 3: "H", 4: "I", 5: "I", 7: "c"}

# field types (the schema's table)
UCHAR, SHORT_T, USHORT_T, INT, UINT, LONG_T, FLOAT, DOUBLE, BOOLEAN, TCHAR, DWORD, TIMESTAMP = 2, 3, 4, 5, 6, 7, 9, 10, 12, 13, 14, 17
INT_RECT, DOUBLE_2, DOUBLE_INTERVAL, BGR, UNICODE_TCHAR, DIM_INDEX_1, DIM_INDEX_2, VOLUME_INDEX = 259, 260, 268, 270, 8192, 8195, 8199, 8200
EXT_VOLUME, EXT_PSET, EXT_MDIM, EXT_IFD = 0, 1, 2, 10

# tags
COLLECTION_VOLUME, MULTIDIM_IMAGE_VOLUME, IMAGE_FRAME_VOLUME, DIMENSION_SIZE = 2000, 2001, 2002, 2003
IMAGE_COLLECTION_PROPERTIES, MULTIDIM_STACK_PROPERTIES, DIMENSION_DESCRIPTION_VOLUME, CHANNEL_PROPERTIES = 2004, 2005, 2007, 2008
Z_START, Z_INCREMENT, CREATION_TIME, DEFAULT_SAMPLE_IFD, RWC_FRAME_ORIGIN, EXTERNAL_FILE_PROPERTIES = 2012, 2013, 2015, 2016, 2018, 2018
RWC_FRAME_SCALE, COARSE_FRAME_IFD, RWC_FRAME_UNIT, DIMENSION_NAME, COARSE_PYRAMID_LEVEL, DIMENSION_MEANING = 2019, 2019, 2020, 2021, 2022, 2023
DIMENSION_VALUE_ID, STACK_NAME, CHANNEL_DIM, DEFAULT_BACKGROUND_COLOR, VERSION_NUMBER, STACK_TYPE = 2027, 2030, 2031, 2034, 2035, 2074
DISPLAY_LIMITS, STACK_DISPLAY_LUT, IMAGE_BOUNDARY, TILE_ORIGIN, TILE_SYSTEM, HAS_EXTERNAL_FILE, EXTERNAL_DATA_VOLUME = 2003, 2004, 2053, 2410, 20004, 20005, 20025
CHANNEL_WAVELENGTH, CHANNEL_NAME, UNITS, VALUE, OBJECTIVE_MAG = 2417, 2419, 268435456, 268435458, 120060
DOCUMENT_NAME, DOCUMENT_TIME, DOCUMENT_AUTHOR, PRODUCT_NAME, PRODUCT_VERSION = 11, 14, 15, 34, 35


def utf16(s: str) -> bytes:
    return s.encode("utf-16-le") + b"\0\0"


class Field:
    """A field of a container: a plain value (`data`), an inline value (`inline`), an extended
    field holding child containers (`children`, each a list of fields) or a TIFF region
    (`region`, a callable of the region's absolute offset giving its bytes), with `rest`, a
    callable giving the bytes between the field's data and the next field (the coarse IFDs
    sit there, after an inline `Coarse pyramid level` field)."""

    def __init__(self, tag, ftype, data=None, *, inline=None, tag2=None, ext=None, children=None, region=None,
                 rest=None, flags=0, size=None, next_=None):
        self.tag, self.ftype, self.data, self.inline, self.tag2 = tag, ftype, data, inline, tag2
        self.ext, self.children, self.region, self.rest = ext, children, region, rest
        self.flags, self.size_override, self.next_override = flags, size, next_


def value(tag, ftype, data, **kw) -> Field:
    return Field(tag, ftype, data, **kw)


def inline(tag, v, ftype=INT) -> Field:
    return Field(tag, ftype, inline=v)


def volume(tag, tag2, fields, **kw) -> Field:
    return Field(tag, EXT_VOLUME, tag2=tag2, ext=True, children=[fields], **kw)


def pset(tag, fields, **kw) -> Field:
    return Field(tag, EXT_PSET, ext=True, children=[fields], **kw)


def mdim(tag, fields, **kw) -> Field:
    return Field(tag, EXT_MDIM, ext=True, children=[fields], **kw)


def ifd_field(tag, tag2, region, **kw) -> Field:
    return Field(tag, EXT_IFD, tag2=tag2, ext=True, region=region, **kw)


def i32(v) -> bytes:
    return struct.pack("<i", v)


def f64s(*v) -> bytes:
    return struct.pack(f"<{len(v)}d", *v)


class Builder:
    """Lays a container out at an absolute offset, as the real files do (§2.1)."""

    def __init__(self):
        self.ifds: list[tuple[int, int]] = []  # (chain index, absolute offset) of each IFD written

    def container(self, fp: int, fields: list[Field], *, header_size=24, version=21321, volume_version=402,
                  data_offset=None, count=None) -> bytes:
        count = len(fields) if count is None else count
        dfo = (24 if fields else 0) if data_offset is None else data_offset
        out = bytearray(struct.pack("<HHiqii", header_size, version, volume_version, dfo, count, 0))
        for k, f in enumerate(fields):
            p = fp + len(out)
            extra = f.tag2 is not None
            q = p + 16 + (4 if extra else 0)
            if f.inline is not None:
                body = b""
            elif f.region is not None:
                body = f.region(q)
            elif f.children is not None:
                body = b""
                for child in f.children:
                    body += self.container(q + len(body), child)
            else:
                body = f.data
            rest = f.rest(q + len(body)) if f.rest is not None else b""
            end = q + len(body) + len(rest)
            last = k == len(fields) - 1
            nxt = 0 if last else end - fp
            if f.next_override is not None:
                nxt = f.next_override
            if f.size_override is not None:
                size = f.size_override
            elif f.inline is not None:
                size = f.inline
            elif f.ext and f.ftype in (EXT_PSET, EXT_MDIM):
                size = 0
            elif extra and len(body) > 0:
                size = (q + len(body)) - fp - 20
            else:
                size = len(body)
            ftype = f.ftype | f.flags
            if extra:
                ftype |= 1 << 27
            if f.ext:
                ftype |= 1 << 28
            if f.inline is not None:
                ftype |= 1 << 30
            out += struct.pack("<IIIi", ftype & 0xFFFFFFFF, f.tag, nxt, size)
            if extra:
                out += struct.pack("<I", f.tag2)
            out += body + rest
        return bytes(out)


# ---------------------------------------------------------------- TIFF images in the tree

SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1}


def jpeg_tables_and_body(rgb: np.ndarray) -> tuple[bytes, bytes]:
    """A JPEG stream split into its tables (as JPEGTables: SOI, DQT/DHT segments, EOI) and the
    rest (SOI, the frame and scan, EOI), RGB with no color transform."""
    full = imagecodecs.jpeg8_encode(rgb, level=90, colorspace="RGB", outcolorspace="RGB", subsampling="444")
    segs, p = [], 2
    while p < len(full):
        marker = full[p + 1]
        if marker == 0xD9:
            segs.append((marker, full[p:p + 2]))
            break
        n = struct.unpack(">H", full[p + 2:p + 4])[0]
        end = p + 2 + n
        if marker == 0xDA:
            end = full.index(b"\xff\xd9", end)
        segs.append((marker, full[p:end]))
        p = end
    tables = b"\xff\xd8" + b"".join(s for m, s in segs if m in (0xDB, 0xC4)) + b"\xff\xd9"
    body = b"\xff\xd8" + b"".join(s for m, s in segs if m not in (0xDB, 0xC4, 0xEE, 0xE0)) + b"\xff\xd9"
    return tables, body


class TiffImage:
    """An image stored as an IFD inside the tree: `rows` rows per strip, uncompressed, or one
    JPEG strip with JPEGTables (and the Adobe marker's absence, as the real files have it)."""

    def __init__(self, pixels: np.ndarray, *, jpeg=False, rows=None, exif=False, extra=(), skip=(), two_strips=False):
        self.pixels, self.jpeg, self.exif, self.extra, self.skip, self.two_strips = pixels, jpeg, exif, extra, skip, two_strips
        self.h, self.w = pixels.shape[:2]
        self.spp = pixels.shape[2] if pixels.ndim == 3 else 1
        self.rows = rows or self.h

    def region(self, at: int, b: Builder, index: int) -> bytes:
        """The region's bytes at `at`: the values, the strips, the EXIF IFD, then the IFD (its
        next pointer patched by `chain`)."""
        out = bytearray()
        entries = []
        bits = self.pixels.dtype.itemsize * 8

        def put(data: bytes) -> int:
            if len(out) % 2:
                out.append(0)
            o = at + len(out)
            out.extend(data)
            return o

        if self.jpeg:
            tables, body = jpeg_tables_and_body(self.pixels)
            strips = [body, body] if self.two_strips else [body]
            tables_at = put(tables)
        else:
            raw = np.ascontiguousarray(self.pixels.astype(self.pixels.dtype.newbyteorder("<"))).tobytes()
            row_bytes = self.w * self.spp * self.pixels.dtype.itemsize
            strips = [raw[r * row_bytes:(r + self.rows) * row_bytes] for r in range(0, self.h, self.rows)]
        offsets = [put(s) for s in strips]
        counts = [len(s) for s in strips]
        sample_bits = [bits] * self.spp
        entries += [(256, LONG, [self.w]), (257, LONG, [self.h]), (258, SHORT, sample_bits),
                    (259, SHORT, [7 if self.jpeg else 1]), (262, SHORT, [2 if self.spp == 3 else 1]),
                    (273, LONG, offsets), (277, SHORT, [self.spp]), (278, LONG, [self.h if self.jpeg else self.rows]),
                    (279, LONG, counts), (284, SHORT, [1])]
        if self.jpeg:
            entries.append((347, UNDEFINED, ("at", len(tables), tables_at)))
        entries = [e for e in entries if e[0] not in self.skip]
        entries += list(self.extra)
        if self.exif:
            # as the real files: the EXIF pixel dimensions are the scanned area's, which Bio-Formats
            # caps the first stack's size by; `exif` is (width, height), or True for the image's own
            ew, eh = self.exif if isinstance(self.exif, tuple) else (self.w, self.h)
            exif_at = self._ifd(out, at, [(36864, UNDEFINED, b"0220"), (40961, SHORT, [1]), (40962, LONG, [ew]), (40963, LONG, [eh])])
            entries.append((34665, LONG, [exif_at]))
        entries.sort(key=lambda e: e[0])
        o = self._ifd(out, at, entries)
        b.ifds.append((index, o))
        return bytes(out)

    @staticmethod
    def _ifd(out: bytearray, at: int, entries) -> int:
        """Writes the out-of-line values, then the IFD; returns the IFD's offset."""
        packed = []
        for tag, typ, v in entries:
            if isinstance(v, tuple):
                packed.append(struct.pack("<HHII", tag, typ, v[1], v[2]))
                continue
            raw = bytes(v) if isinstance(v, (bytes, bytearray)) else struct.pack(f"<{len(v)}{PACK[typ]}", *v)
            count = len(raw) // SIZE[typ]
            if len(raw) <= 4:
                field = raw.ljust(4, b"\0")
            else:
                if len(out) % 2:
                    out.append(0)
                field = struct.pack("<I", at + len(out))
                out.extend(raw)
            packed.append(struct.pack("<HHI", tag, typ, count) + field)
        if len(out) % 2:
            out.append(0)
        o = at + len(out)
        out.extend(struct.pack("<H", len(packed)) + b"".join(packed) + b"\0\0\0\0")
        return o


# ---------------------------------------------------------------- tile files

SIS_SIZE, ETS_SIZE = 64, 228
PIXEL_TYPES = {"int8": 1, "uint8": 2, "int16": 3, "uint16": 4, "int32": 5, "uint32": 6, "int64": 7, "uint64": 8,
               "float32": 9, "float64": 10}
RAW, JPEG, JPEG2000 = 0, 2, 3


def ets_file(tiles: list[tuple[tuple[int, ...], bytes]], *, coordinates: int, dtype: str, samples: int, compression: int,
             tile: tuple[int, int], dims: list[int], pyramid: bool, background: bytes = b"", component_order=-1,
             colorspace=None, quality=90, version=3, magic=b"SIS\0", header_size=64, ets_magic=b"ETS\0", ets_size=228,
             tile_depth=1, pixel_type=None, tiles_count=None, tiles_offset=None, ets_offset=None, truncate=0,
             dimensions=None, tile_offset=None) -> bytes:
    """A tile file: the SIS header, the ETS header, the tiles' bytes in order, then the directory
    (`tile_offset`: (index, offset) of one record's offset written as given)."""
    data = bytearray()
    records = []
    data_at = SIS_SIZE + ets_size
    n = len(tiles[0][0]) if tiles else coordinates  # the records' coordinates, whatever the header says
    for k, (coords, b) in enumerate(tiles):
        o = data_at + len(data)
        data.extend(b)
        if tile_offset is not None and tile_offset[0] == k:
            o = tile_offset[1]
        records.append(struct.pack(f"<i{n}iqii", n, *coords, o, len(b), k))
    dir_at = data_at + len(data) if tiles_offset is None else tiles_offset
    total = SIS_SIZE + ets_size + len(data) + len(records) * (20 + 4 * n)
    sis = struct.pack("<4siiiqiiqiiq8s", magic, header_size, version, coordinates, SIS_SIZE if ets_offset is None else ets_offset,
                      ets_size, 0, dir_at, len(tiles) if tiles_count is None else tiles_count, 0, total, b"\0" * 8)
    bg = background.ljust(40, b"\0")[:40]
    pt = PIXEL_TYPES[dtype] if pixel_type is None else pixel_type
    cs = (4 if samples == 3 else 1) if colorspace is None else colorspace
    sizes = (dims + [0] * 10)[:10]
    ets = struct.pack("<4siiiiiiiii", ets_magic, 196614, pt, samples, cs, compression, quality, tile[0], tile[1], tile_depth)
    ets += b"\0" * 68 + bg + struct.pack("<ii", component_order, 1 if pyramid else 0) + b"\0" * 28
    ets += struct.pack("<i", len(dims) if dimensions is None else dimensions) + struct.pack("<10i", *sizes)
    ets = ets.ljust(ets_size, b"\0")
    out = sis + ets + bytes(data) + b"".join(records)
    return out[:len(out) - truncate] if truncate else out


def gradient(seed: int, h: int, w: int, dtype: str, samples: int = 1) -> np.ndarray:
    """Seeded values, unique per (seed, position): a gradient with noise."""
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:h, 0:w]
    base = (x * 3 + y * 5 + seed * 17) % 200
    shape = (h, w, samples) if samples > 1 else (h, w)
    v = np.broadcast_to(base[..., None] if samples > 1 else base, shape).astype(np.float64)
    if samples > 1:
        v = v + np.arange(samples) * 40
    v = v + rng.integers(0, 20, shape)
    info = np.iinfo(dtype) if np.issubdtype(np.dtype(dtype), np.integer) else None
    if info is not None:
        lo, hi = max(info.min, -3000), min(info.max, 4000)
        v = lo + (v % (hi - lo + 1))
    return np.ascontiguousarray(v.astype(dtype))


def downsample(a: np.ndarray) -> np.ndarray:
    """Level k + 1 of a plane: every other pixel (the real files average; the fixtures' values
    are known either way)."""
    return np.ascontiguousarray(a[::2, ::2])


def plane_tiles(plane: np.ndarray, tw: int, th: int, compression: int, dtype: str, samples: int, *, bgr=False,
                skip=(), quality=90) -> list[tuple[tuple[int, int], bytes]]:
    """A plane cut into full tiles (padded with zeros), each encoded."""
    h, w = plane.shape[:2]
    out = []
    for ty in range(-(-h // th)):
        for tx in range(-(-w // tw)):
            if (tx, ty) in skip:
                continue
            block = np.zeros((th, tw) + plane.shape[2:], plane.dtype)
            part = plane[ty * th:(ty + 1) * th, tx * tw:(tx + 1) * tw]
            block[:part.shape[0], :part.shape[1]] = part
            if compression == RAW:
                stored = block[..., ::-1] if bgr and samples > 1 else block
                b = np.ascontiguousarray(stored.astype(plane.dtype.newbyteorder("<"))).tobytes()
            elif compression == JPEG:
                b = imagecodecs.jpeg8_encode(block, level=quality, colorspace="RGB" if samples == 3 else "GRAY",
                                             outcolorspace="YCBCR" if samples == 3 else "GRAY")
            else:
                b = imagecodecs.jpeg2k_encode(block, level=0, reversible=True, codecformat="J2K")
            out.append(((tx, ty), b))
    return out


def decoded(tiles, tw, th, h, w, compression, dtype, samples, bgr=False) -> np.ndarray:
    """The plane a tile list decodes to (what a reader gives): the lossy tiles' own pixels."""
    plane = np.zeros((h, w) + ((samples,) if samples > 1 else ()), dtype)
    for (tx, ty), b in tiles:
        if compression == RAW:
            # as stored: a BGR file's arrays hold B, G, R (its omero channels say so)
            block = np.frombuffer(b, dtype=np.dtype(dtype).newbyteorder("<")).reshape((th, tw) + ((samples,) if samples > 1 else ()))
        elif compression == JPEG:
            block = imagecodecs.jpeg8_decode(b)
        else:
            block = imagecodecs.jpeg2k_decode(b)
        y0, x0 = ty * th, tx * tw
        hh, ww = min(th, h - y0), min(tw, w - x0)
        plane[y0:y0 + hh, x0:x0 + ww] = block[:hh, :ww]
    return plane


# ---------------------------------------------------------------- slides

class Stack:
    """A stack of the slide: its id, name, type, size, dimension sizes (t, z, c), pixel type,
    samples, compression, tile size, levels, and what the tree says about it."""

    def __init__(self, id_, name, *, width, height, dtype="uint8", samples=1, compression=RAW, tile=(32, 32), levels=1,
                 t=1, z=1, c=1, stack_type=0, scale=(0.5, 0.5), unit="10^-6m^1", origin=(100.0, 200.0), display=(0.0, 256.0),
                 channels=None, z_step=None, bgr=False, skip=(), file_name="frame_t.ets", unused_coord=False,
                 dim_meanings=None, background=b"", magnification=20.0, seed=1, extra_fields=(), tree_only=False,
                 ifd_pixels=None, quality=90, extra_coords=0, declared_dims=None):
        self.id, self.name, self.width, self.height = id_, name, width, height
        self.dtype, self.samples, self.compression, self.tile, self.levels = dtype, samples, compression, tile, levels
        self.t, self.z, self.c, self.stack_type = t, z, c, stack_type
        self.scale, self.unit, self.origin, self.display, self.channels, self.z_step = scale, unit, origin, display, channels or [], z_step
        self.bgr, self.skip, self.file_name, self.unused_coord = bgr, skip, file_name, unused_coord
        self.dim_meanings, self.background, self.magnification, self.seed = dim_meanings, background, magnification, seed
        self.extra_fields, self.tree_only, self.ifd_pixels, self.quality = extra_fields, tree_only, ifd_pixels, quality
        self.extra_coords, self.declared_dims = extra_coords, declared_dims
        self.planes = {}  # (t, c, z) -> level-0 plane (decoded)
        self.tiles = []   # ((x, y, ...coords), bytes)

    @property
    def dims(self) -> list[tuple[str, int]]:
        """The dimensions beyond x and y, in coordinate order: those with a size over 1, in
        the order t, z, c (as the real files order them), each a (meaning, size)."""
        if self.dim_meanings is not None:
            return self.dim_meanings
        return [(m, n) for m, n in (("t", self.t), ("z", self.z), ("c", self.c)) if n > 1]

    def build(self) -> np.ndarray | None:
        """Encodes every tile; returns the expected level-0 array in TCZYX(S) order."""
        if self.tree_only or self.ifd_pixels is not None:
            return None
        tw, th = self.tile
        dims = self.dims
        shape = (self.t, self.c, self.z, self.height, self.width) + ((self.samples,) if self.samples > 1 else ())
        expected = np.zeros(shape, self.dtype)
        k = 0
        for ti in range(self.t):
            for ci in range(self.c):
                for zi in range(self.z):
                    plane = gradient(self.seed * 1000 + k, self.height, self.width, self.dtype, self.samples)
                    k += 1
                    for level in range(self.levels):
                        tiles = plane_tiles(plane, tw, th, self.compression, self.dtype, self.samples, bgr=self.bgr,
                                            skip=self.skip if level == 0 else (), quality=self.quality)
                        if level == 0:
                            got = decoded(tiles, tw, th, self.height, self.width, self.compression, self.dtype, self.samples, self.bgr)
                            fill = np.frombuffer(self.background.ljust(8, b"\0"), dtype=np.dtype(self.dtype).newbyteorder("<"), count=1)[0]
                            for (tx, ty) in self.skip:
                                got[ty * th:(ty + 1) * th, tx * tw:(tx + 1) * tw] = fill
                            expected[ti, ci, zi] = got
                        for (tx, ty), b in tiles:
                            coords = [tx, ty]
                            for meaning, _ in dims:
                                coords.append({"t": ti, "z": zi, "c": ci}.get(meaning, 0))
                            coords += [0] * self.extra_coords
                            if self.unused_coord:
                                coords.append(0)
                            if self.levels > 1:
                                coords.append(level)
                            self.tiles.append((tuple(coords), b))
                        plane = downsample(plane)
        return expected

    def ets_bytes(self, **kw) -> bytes:
        dims = self.dims
        coordinates = 2 + len(dims) + self.extra_coords + (1 if self.unused_coord else 0) + (1 if self.levels > 1 else 0)
        sizes = [self.width, self.height] + [n for _, n in dims] + [1] * self.extra_coords
        args = dict(coordinates=coordinates, dtype=self.dtype, samples=self.samples, compression=self.compression,
                    tile=self.tile, dims=sizes, pyramid=self.levels > 1, background=self.background,
                    component_order=1 if (self.bgr or (self.samples == 3 and self.compression == JPEG)) else -1,
                    quality=self.quality)
        args.update(kw)
        return ets_file(self.tiles, **args)

    def fields(self, b: Builder, ifd_index: dict) -> Field:
        """The stack's `Multidimensional image volume`."""
        dims = self.dims
        declared = self.declared_dims if self.declared_dims is not None else [n for _, n in dims]
        frame = []
        external = not self.tree_only and self.ifd_pixels is None
        props = [value(IMAGE_BOUNDARY, INT_RECT, struct.pack("<4i", 0, 0, self.width, self.height)),
                 value(TILE_ORIGIN, DIM_INDEX_2, struct.pack("<2i", 0, 0)),
                 inline(TILE_SYSTEM, 13), inline(HAS_EXTERNAL_FILE, 1 if external else 0, BOOLEAN),
                 value(EXTERNAL_DATA_VOLUME, VOLUME_INDEX, struct.pack("<8i", 0, 0, 0, 0, self.width, self.height, 0, 1))]
        if self.ifd_pixels is not None:
            # a stack stored as an IFD (a focus map): its size in a `2037` property set, then the IFD,
            # as the real files have it (no external file properties, which would make it a pyramid
            # to Bio-Formats)
            frame.append(pset(2037, [value(IMAGE_BOUNDARY, INT_RECT, struct.pack("<4i", 0, 0, self.width, self.height)),
                                     value(EXTERNAL_DATA_VOLUME, VOLUME_INDEX, struct.pack("<8i", 0, 0, 0, 0, self.width, self.height, 0, 1))]))
        else:
            frame.append(pset(EXTERNAL_FILE_PROPERTIES, props))
        if self.ifd_pixels is not None:
            k = ifd_index["next"]
            ifd_index["next"] += 1
            image = TiffImage(self.ifd_pixels)
            frame.append(ifd_field(DEFAULT_SAMPLE_IFD, k, lambda at, image=image, k=k: image.region(at, b, k)))
        elif external:
            # the coarse thumbnail: an IFD in the rest of the inline field's region, as the real files
            k = ifd_index["next"]
            ifd_index["next"] += 1
            thumb = TiffImage(gradient(self.seed + 7, 6, 8, "uint8", self.samples), rows=4)
            frame.append(ifd_field(COARSE_FRAME_IFD, k, lambda at: b""))
            frame.append(Field(COARSE_PYRAMID_LEVEL, INT, inline=self.levels, rest=lambda at, thumb=thumb, k=k: thumb.region(at, b, k)))
        stack_props = [value(CREATION_TIME, LONG_T, struct.pack("<q", 1700000000 + self.id)),
                       value(RWC_FRAME_ORIGIN, DOUBLE_2, f64s(*self.origin)) if self.origin else None,
                       value(RWC_FRAME_UNIT, TCHAR, utf16(self.unit)) if self.unit else None,
                       value(DISPLAY_LIMITS, DOUBLE_INTERVAL, f64s(*self.display)) if self.display else None,
                       value(RWC_FRAME_SCALE, DOUBLE_2, f64s(*self.scale)) if self.scale else None,
                       inline(CHANNEL_DIM, 1),
                       value(STACK_NAME, UNICODE_TCHAR, utf16(self.name)) if self.name is not None else None,
                       mdim(2043, [inline(268435459, 671096836), value(268435457, DIM_INDEX_1, i32(2)),
                                   pset(1, [value(120132, UNICODE_TCHAR, utf16("Objective 20x")), value(OBJECTIVE_MAG, DOUBLE, f64s(self.magnification))]),
                                   pset(2, [value(120116, UNICODE_TCHAR, utf16("Camera"))])]),
                       inline(STACK_TYPE, self.stack_type) if self.stack_type is not None else None,
                       *self.extra_fields]
        stack_props = [f for f in stack_props if f is not None]
        fields = [value(DIMENSION_VALUE_ID, INT, i32(0), flags=1 << 29), value(STACK_NAME, INT, i32(0), flags=1 << 29),
                  value(DIMENSION_SIZE, DIM_INDEX_1, struct.pack(f"<{len(declared)}i", *declared)),
                  inline(CHANNEL_DIM, 1),
                  value(DEFAULT_BACKGROUND_COLOR, SHORT_T, struct.pack("<h", -1), flags=1 << 29),
                  volume(IMAGE_FRAME_VOLUME, 0, frame),
                  pset(2029, []),
                  pset(MULTIDIM_STACK_PROPERTIES, stack_props)]
        for d, (meaning, _) in enumerate(dims):
            code = {"z": 1, "t": 2, "lambda": 3, "c": 4, "phase": 9}.get(meaning)
            psets = []
            if meaning == "c":
                for ci in range(self.c):
                    ch = self.channels[ci] if ci < len(self.channels) else {}
                    name = ch.get("name", f"Channel {ci}")
                    lut = bytes(sum(([int(ch.get("color", "FFFFFF")[4:6], 16) * i // 255, int(ch.get("color", "FFFFFF")[2:4], 16) * i // 255,
                                      int(ch.get("color", "FFFFFF")[0:2], 16) * i // 255] for i in range(256)), []))
                    fields_c = [inline(DIMENSION_MEANING, code) if ci == 0 else None,
                                value(2005, DOUBLE, f64s(1.0)),
                                value(DIMENSION_NAME, UNICODE_TCHAR, utf16(name)),
                                mdim(STACK_DISPLAY_LUT, [inline(268435459, 536871182), value(268435457, DIM_INDEX_1, i32(256)),
                                                         value(VALUE, BGR, lut)]),
                                value(DISPLAY_LIMITS, DOUBLE_INTERVAL, f64s(*ch["display"])) if "display" in ch else None,
                                mdim(CHANNEL_WAVELENGTH, [value(UNITS, TCHAR, utf16("10^-9m^1")), value(VALUE, DOUBLE, f64s(ch["wavelength"]))]) if "wavelength" in ch else None,
                                value(CHANNEL_NAME, UNICODE_TCHAR, utf16(ch.get("channel_name", "Fluorescence")))]
                    psets.append(pset(CHANNEL_PROPERTIES, [f for f in fields_c if f is not None], tag2=ci))
            elif meaning == "z":
                fields_z = [mdim(Z_START, [value(UNITS, TCHAR, utf16("10^-6m^1")), inline(268435459, 268435466),
                                           value(268435457, DIM_INDEX_1, b""), value(VALUE, DOUBLE, f64s(1000.0))]),
                            inline(DIMENSION_MEANING, code), inline(2026, 2013), inline(2025, 2012)]
                if self.z_step is not None:
                    fields_z.append(mdim(Z_INCREMENT, [value(UNITS, TCHAR, utf16("10^-6m^1")), value(VALUE, DOUBLE, f64s(self.z_step))]))
                psets.append(pset(CHANNEL_PROPERTIES, fields_z, tag2=0))
            elif meaning == "t":
                psets.append(pset(CHANNEL_PROPERTIES, [inline(DIMENSION_MEANING, code), inline(DIMENSION_VALUE_ID, 2017),
                                                       mdim(10019, [value(UNITS, TCHAR, utf16("10^0s^-1")), value(VALUE, DOUBLE, f64s(4.0))])], tag2=0))
            else:
                psets.append(pset(CHANNEL_PROPERTIES, ([inline(DIMENSION_MEANING, code)] if code else []) + [value(DIMENSION_NAME, UNICODE_TCHAR, utf16(meaning))], tag2=0))
            fields.append(volume(DIMENSION_DESCRIPTION_VOLUME, d, psets))
        return volume(MULTIDIM_IMAGE_VOLUME, self.id, fields)


class Slide:
    def __init__(self, name: str, stacks: list[Stack], *, macro: TiffImage | None = None, document=True, root_extra=(),
                 collection_extra=(), top_extra=(), first_ifd=None):
        self.name, self.stacks, self.macro = name, stacks, macro
        self.document, self.root_extra, self.collection_extra, self.top_extra, self.first_ifd = document, root_extra, collection_extra, top_extra, first_ifd

    def write(self, out: Path = OUT, *, ets_kw=None, patch=None, expected=True) -> None:
        b = Builder()
        ifd_index = {"next": 1 if self.macro is not None else 0}
        if self.macro is not None and self.macro.exif is True:
            sizes = [(st.width, st.height) for st in self.stacks if not st.tree_only and st.ifd_pixels is None]
            self.macro.exif = (max([w for w, _ in sizes] + [self.macro.w]), max([h for _, h in sizes] + [self.macro.h]))
        arrays = {}
        for s in self.stacks:
            a = s.build()
            if a is not None:
                arrays[s.id] = a
        coll = [value(VERSION_NUMBER, DWORD, struct.pack("<2I", 0x30003, 0x865C), flags=1 << 29)]
        if self.macro is not None:
            coll.append(ifd_field(DEFAULT_SAMPLE_IFD, 0, lambda at: self.macro.region(at, b, 0)))
        coll += [s.fields(b, ifd_index) for s in self.stacks]
        coll += list(self.collection_extra)
        coll.append(pset(IMAGE_COLLECTION_PROPERTIES, [pset(2109, [value(DOCUMENT_NAME, UNICODE_TCHAR, utf16(self.name)),
                                                                   value(DOCUMENT_TIME, LONG_T, struct.pack("<q", 1700000000)),
                                                                   value(DOCUMENT_AUTHOR, UNICODE_TCHAR, utf16("refkv fixtures")),
                                                                   value(PRODUCT_NAME, UNICODE_TCHAR, utf16("synthetic cellSens")),
                                                                   value(PRODUCT_VERSION, UNICODE_TCHAR, utf16("4.1"))])]))
        top = ([pset(2, [value(DOCUMENT_NAME, UNICODE_TCHAR, utf16(self.name)), value(PRODUCT_NAME, UNICODE_TCHAR, utf16("synthetic cellSens"))])]
               if self.document else [pset(2, [])])
        top += list(self.top_extra)
        top.append(volume(COLLECTION_VOLUME, 0, coll + list(self.root_extra)))
        tree = b.container(8, top)
        buf = bytearray(b"II*\0" + b"\0\0\0\0" + tree)
        # the IFD chain, in index order
        chain = sorted(b.ifds)
        first = chain[0][1] if chain else 0
        struct.pack_into("<I", buf, 4, first if self.first_ifd is None else self.first_ifd)
        for (_, o), nxt in zip(chain, chain[1:]):
            n = struct.unpack_from("<H", buf, o)[0]
            struct.pack_into("<I", buf, o + 2 + 12 * n, nxt[1])
        if patch is not None:
            buf = patch(buf)
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{self.name}.vsi").write_bytes(bytes(buf))
        for s in self.stacks:
            if s.tree_only or s.ifd_pixels is not None:
                continue
            d = out / f"_{self.name}_" / f"stack{s.id}"
            d.mkdir(parents=True, exist_ok=True)
            kw = (ets_kw or {}).get(s.id, {})
            (d / s.file_name).write_bytes(s.ets_bytes(**kw))
        if expected and arrays:
            e = EXPECTED / self.name
            e.mkdir(parents=True, exist_ok=True)
            for id_, a in arrays.items():
                np.save(e / f"{id_}.npy", a)


def macro_image(seed=3, w=80, h=30, **kw) -> TiffImage:
    return TiffImage(gradient(seed, h, w, "uint8", 3), jpeg=True, exif=True, **kw)


# ---------------------------------------------------------------- the fixtures

def rgb_channels():
    return [{"name": "DAPI", "color": "0000FF", "display": (10.0, 1000.0), "wavelength": 461.0, "channel_name": "DAPI"},
            {"name": "FITC", "color": "00FF00", "display": (20.0, 2000.0), "wavelength": 519.0, "channel_name": "FITC"},
            {"name": "Cy5", "color": "FF0000", "wavelength": 670.0, "channel_name": "Cy5"}]


def accepting() -> None:
    # like the brightfield sample: RGB JPEG pyramids, a focus map as an IFD, a macro with tables
    Slide("vsi_rgb_jpeg", [
        Stack(10000, "Overview", width=150, height=100, samples=3, compression=JPEG, tile=(64, 64), levels=2, stack_type=1,
              scale=(2.7, 2.7), origin=(300.0, 600.0), seed=2, background=b"\xee\xee\xee"),
        Stack(10002, "20x_BF_01", width=300, height=200, samples=3, compression=JPEG, tile=(64, 64), levels=3, stack_type=0,
              scale=(0.27, 0.27), origin=(37000.0, 4600.0), skip=((2, 1),), seed=3, background=b"\xee\xee\xee"),
        Stack(10003, "20x FocusMap", width=6, height=5, dtype="uint16", stack_type=4, scale=(627.0, 376.0), origin=(37000.0, 4600.0),
              ifd_pixels=gradient(9, 5, 6, "uint16"), display=None),
    ], macro=macro_image()).write()
    # like the Zenodo sample: a raw uint16 stack with T, Z and C, one tile a plane, frame_t_0.ets
    Slide("vsi_raw_tzc", [
        Stack(1, "488/640_50um_1x", width=40, height=30, dtype="uint16", tile=(40, 30), t=2, z=3, c=2, scale=(0.065, 0.065),
              origin=(-66211.0, -41421.0), display=(91.0, 138.0), z_step=0.2, file_name="frame_t_0.ets", unused_coord=True,
              background=b"\xff\x0f", channels=[{"name": "Cy5", "color": "FF0000", "display": (88.0, 169.0), "wavelength": 670.0, "channel_name": "488/640_50um_1x"},
                                                {"name": "GFP", "color": "00FF00", "display": (86.0, 170.0), "wavelength": 508.0, "channel_name": "488/640_50um_1x"}],
              seed=4, magnification=40.0),
    ], macro=macro_image(seed=5, w=40, h=30)).write()
    # like the fluorescence sample: JPEG 2000 uint16 tiles with a channel dimension, in a pyramid
    Slide("vsi_jpeg2000_c", [
        Stack(10002, "20x_DAPI, FITC, Cy5_01", width=70, height=50, dtype="uint16", compression=JPEG2000, tile=(32, 32), levels=2, c=3,
              scale=(0.345, 0.345), origin=(44720.0, 4540.0), display=None, channels=rgb_channels(), file_name="frame_t_0.ets", seed=6),
    ], macro=macro_image(seed=7)).write()
    # raw RGB in BGR order
    Slide("vsi_raw_bgr", [
        Stack(10000, "Overview", width=40, height=24, samples=3, tile=(16, 16), levels=2, stack_type=1, bgr=True, seed=8,
              background=b"\x10\x20\x30"),
    ], macro=macro_image(seed=9, w=20, h=10)).write()
    # raw uint8 gray in a pyramid whose level 0 is sparse
    Slide("vsi_raw_gray_pyramid", [
        Stack(10002, "10x", width=100, height=70, tile=(32, 32), levels=3, skip=((0, 0), (3, 2), (1, 1)), seed=10, background=b"\x7f"),
    ], macro=macro_image(seed=11)).write()
    # every raw pixel type the tables name
    types = ["int8", "int16", "int32", "uint32", "int64", "uint64", "float32", "float64"]
    Slide("vsi_raw_types", [
        Stack(10000 + k, f"type {t}", width=12, height=9, dtype=t, tile=(8, 8), levels=1, seed=20 + k) for k, t in enumerate(types)
    ], macro=macro_image(seed=12)).write()
    # the macro image alone
    Slide("vsi_no_stacks", [], macro=macro_image(seed=13)).write()
    # metadata edges: millimeters, no origin, no display limits, an unknown dimension of size 1
    # and a phase dimension of size 1, an unknown stack type, a blob with a second tag, an empty
    # container, text that is not ASCII, both tile file names present
    s = Stack(10002, "µm édge", width=20, height=12, dtype="uint16", tile=(8, 8), levels=1, unit="10^-3m^1", origin=None,
              display=None, stack_type=4096, scale=(0.001, 0.0015), seed=14, extra_coords=2,
              dim_meanings=[("unknown", 1), ("phase", 1)], declared_dims=[1, 1],
              extra_fields=(value(2024, UCHAR, bytes(range(256)) * 5, tag2=0, flags=1 << 29), pset(2038, []),
                            value(175307, UNICODE_TCHAR, utf16("Défaut"))))
    slide = Slide("vsi_metadata_edges", [s], macro=macro_image(seed=15, w=16, h=8))
    slide.write()
    # the second name beside the first: the first is read
    (OUT / "_vsi_metadata_edges_" / "stack10002" / "frame_t_0.ets").write_bytes(b"SIS\0not the file read")


def rejecting() -> None:
    def base(name, **kw) -> Slide:
        return Slide(name, [Stack(10002, "s", width=24, height=16, tile=(8, 8), levels=2, seed=30)], macro=macro_image(seed=31, w=16, h=8), **kw)

    def write_patched(name, patch, **kw):
        base(name).write(patch=patch, expected=False, **kw)

    # file: too short (sniffed as VSI by its first 12 bytes), and the first IFD outside
    (OUT / "vsi_reject_short.vsi").write_bytes(b"II*\0\0\0\0\0\x18\0IS\0\0\0\0\0\0\0\0")
    write_patched("vsi_reject_first_ifd_outside", lambda b: (struct.pack_into("<I", b, 4, len(b) + 100), b)[1])
    # containers
    write_patched("vsi_reject_container_header_size", lambda b: (struct.pack_into("<H", b, 48, 23), b)[1])
    write_patched("vsi_reject_container_version", lambda b: (struct.pack_into("<H", b, 8 + 24 + 16 + 2, 21322), b)[1])
    write_patched("vsi_reject_container_count", lambda b: (struct.pack_into("<I", b, 8 + 16, 1048577), b)[1])
    write_patched("vsi_reject_container_fields_fit", lambda b: (struct.pack_into("<I", b, 8 + 16, 100000), b)[1])
    write_patched("vsi_reject_container_data_offset", lambda b: (struct.pack_into("<q", b, 8 + 8, 32), b)[1])
    nested = Field(COLLECTION_VOLUME, EXT_VOLUME, tag2=0, ext=True, children=[[]])
    for _ in range(65):
        nested = volume(COLLECTION_VOLUME, 0, [nested])
    Slide("vsi_reject_container_depth", [], root_extra=(), top_extra=(nested,)).write(expected=False)
    # fields
    base("vsi_reject_field_header", top_extra=(volume(99, 0, [Field(1, 5, inline=1, tag2=0)]),)).write(patch=_short_volume, expected=False)
    base("vsi_reject_field_next_zero", top_extra=(value(99, INT, i32(1), next_=0),)).write(expected=False)
    write_patched("vsi_reject_field_last_next", lambda b: _set_last_next(b))
    base("vsi_reject_field_next_outside", top_extra=(value(99, INT, i32(1), next_=1 << 30),)).write(expected=False)
    base("vsi_reject_field_data_outside", top_extra=(value(99, INT, i32(1), size=1 << 20),)).write(expected=False)
    base("vsi_reject_field_extended_type", top_extra=(Field(99, 5, tag2=0, ext=True, children=[[]]),)).write(expected=False)
    base("vsi_reject_field_pset_size", top_extra=(pset(99, [], size=4),)).write(expected=False)
    # IFDs: outside the file, a chain that loops, too many
    write_patched("vsi_reject_ifd_outside", lambda b: _set_first_ifd(b + struct.pack("<HI", 5, 0), len(b)))
    write_patched("vsi_reject_ifd_loop", lambda b: _loop_chain(b))
    many = TiffImage(gradient(1, 2, 2, "uint8", 3), jpeg=True, exif=False)
    Slide("vsi_reject_ifd_count", [], macro=many, root_extra=(Field(99, UCHAR, b"\0" * 8, rest=_many_ifds),)).write(expected=False)
    # a stack with a tile file but no size, and one whose tile file is missing
    Slide("vsi_reject_stack_no_size", [Stack(10002, "s", width=0, height=0, tile=(8, 8), seed=30)], macro=macro_image(seed=31, w=16, h=8)).write(expected=False)
    base("vsi_reject_missing_tile_file").write(expected=False)
    shutil.rmtree(OUT / "_vsi_reject_missing_tile_file_")
    # the SIS header
    for name, kw in [("sis_magic", {"magic": b"SOS\0"}), ("sis_header_size", {"header_size": 48}), ("sis_coordinates", {"coordinates": 1}),
                     ("sis_ets_outside", {"ets_offset": 1 << 30}), ("sis_tiles_negative", {"tiles_count": -1}),
                     ("sis_directory_outside", {"tiles_offset": 1 << 30}),
                     # the ETS header
                     ("ets_magic", {"ets_magic": b"ETX\0"}), ("ets_pixel_type", {"pixel_type": 11}), ("ets_samples", {"samples": 2}),
                     ("ets_compression_png", {"compression": 8}), ("ets_compression_unknown", {"compression": 7}),
                     ("ets_tile_size", {"tile": (0, 8)}), ("ets_tile_depth", {"tile_depth": 2}), ("ets_dimensions", {"dimensions": 1})]:
        base(f"vsi_reject_{name}").write(ets_kw={10002: kw}, expected=False)
    # dimensions: a size of 0, and an unknown meaning with more than one value
    Slide("vsi_reject_dimension_size", [Stack(10002, "s", width=24, height=16, tile=(8, 8), levels=1, seed=30, c=2)],
          macro=macro_image(seed=31, w=16, h=8)).write(ets_kw={10002: {"dims": [24, 16, 0]}}, expected=False)
    Slide("vsi_reject_dimension_unknown", [Stack(10002, "s", width=24, height=16, tile=(8, 8), levels=1, seed=30, dim_meanings=[("phase", 1)],
                                                 declared_dims=[2])], macro=macro_image(seed=31, w=16, h=8)).write(ets_kw={10002: {"dims": [24, 16, 2]}}, expected=False)
    # samples and a channel dimension together
    Slide("vsi_reject_samples_channels", [Stack(10002, "s", width=24, height=16, samples=3, tile=(8, 8), levels=1, c=2, seed=30)],
          macro=macro_image(seed=31, w=16, h=8)).write(expected=False)
    # tiles
    base("vsi_reject_tile_outside").write(ets_kw={10002: {"tile_offset": (3, 1 << 30)}}, expected=False)
    Slide("vsi_reject_tile_coordinate", [Stack(10002, "s", width=24, height=16, tile=(8, 8), levels=1, seed=30, c=2)],
          macro=macro_image(seed=31, w=16, h=8)).write(ets_kw={10002: {"dims": [24, 16, 1]}}, expected=False)
    dup = Stack(10002, "s", width=24, height=16, tile=(8, 8), levels=2, seed=30)
    dup.build()
    dup.tiles.append(dup.tiles[0])
    Slide("vsi_reject_tile_duplicate", [dup], macro=macro_image(seed=31, w=16, h=8)).write(expected=False)
    short_tile = Stack(10002, "s", width=24, height=16, tile=(8, 8), levels=1, seed=30)
    short_tile.build()
    short_tile.tiles[2] = (short_tile.tiles[2][0], short_tile.tiles[2][1][:-1])
    Slide("vsi_reject_tile_raw_size", [short_tile], macro=macro_image(seed=31, w=16, h=8)).write(expected=False)
    # an ETS file shorter than its SIS header
    base("vsi_reject_ets_short").write(expected=False)
    (OUT / "_vsi_reject_ets_short_" / "stack10002" / "frame_t.ets").write_bytes(b"SIS\0" + b"\0" * 20)
    # the macro IFD
    m = lambda **kw: macro_image(seed=31, w=16, h=8, **kw)  # noqa: E731
    for name, img in [("ifd_image_size", m(extra=((256, LONG, [0]),), skip=(256,))),
                      ("ifd_image_compression", m(extra=((259, SHORT, [5]),), skip=(259,))),
                      ("ifd_image_samples", TiffImage(gradient(31, 8, 16, "uint8", 3)[..., :2].copy())),
                      ("ifd_image_bits", m(extra=((258, SHORT, [16, 16, 16]),), skip=(258,))),
                      ("ifd_image_planar", m(extra=((284, SHORT, [2]),), skip=(284,))),
                      ("ifd_image_format", m(extra=((339, SHORT, [3]),))),
                      ("ifd_image_strips", m(two_strips=True))]:
        Slide(f"vsi_reject_{name}", [], macro=img).write(expected=False)
    write_patched("vsi_reject_ifd_strip_outside", lambda b: _strip_outside(b))


def _set_last_next(b: bytearray) -> bytearray:
    # the last field of the top container (the collection volume) gets a next
    count = struct.unpack_from("<I", b, 8 + 16)[0] & 0x0FFFFFFF
    p = 8 + 24
    for _ in range(count - 1):
        p = 8 + struct.unpack_from("<I", b, p + 8)[0]
    struct.pack_into("<I", b, p + 8, 24)
    return b


def _short_volume(b: bytearray) -> bytearray:
    """The tag-99 volume's data ends 4 bytes short of its container's one field, whose 20-byte
    header (it has a second tag) then runs past the region: 24 + 16 fit, the header does not."""
    head = struct.pack("<II", (1 << 27) | (1 << 28), 99)
    at = b.index(head)
    size = struct.unpack_from("<i", b, at + 12)[0]
    struct.pack_into("<i", b, at + 12, size - 4)
    return b


def _set_first_ifd(b: bytearray, o: int) -> bytearray:
    struct.pack_into("<I", b, 4, o)
    return b


def _loop_chain(b: bytearray) -> bytearray:
    first = struct.unpack_from("<I", b, 4)[0]
    n = struct.unpack_from("<H", b, first)[0]
    struct.pack_into("<I", b, first + 2 + 12 * n, first)
    return b


def _many_ifds(at: int) -> bytes:
    """65537 empty IFDs chained one to the next (the first is the macro's next)."""
    out = bytearray()
    n = 65537
    for k in range(n):
        nxt = at + 6 * (k + 1) if k + 1 < n else 0
        out += struct.pack("<HI", 0, nxt)
    return bytes(out)


def _strip_outside(b: bytearray) -> bytearray:
    first = struct.unpack_from("<I", b, 4)[0]
    n = struct.unpack_from("<H", b, first)[0]
    for k in range(n):
        at = first + 2 + 12 * k
        if struct.unpack_from("<H", b, at)[0] == 273:
            struct.pack_into("<I", b, at + 8, len(b) - 10)
    return b


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    accepting()
    rejecting()
    # the chain of 65537 IFDs starts at the macro's IFD: link them
    p = OUT / "vsi_reject_ifd_count.vsi"
    b = bytearray(p.read_bytes())
    first = struct.unpack_from("<I", b, 4)[0]
    n = struct.unpack_from("<H", b, first)[0]
    at = b.index(struct.pack("<HI", 0, 0), 8) if False else None  # noqa: F841
    # the many IFDs sit after the 8-byte value of tag 99: find them from the macro IFD's end
    tail = struct.unpack_from("<I", b, first + 2 + 12 * n)[0]
    if tail == 0:
        # the rest region follows the field's data: locate the first empty IFD record
        k = b.find(struct.pack("<HI", 0, 0) if False else b"\0\0", first + 2 + 12 * n + 4)
        struct.pack_into("<I", b, first + 2 + 12 * n, _first_empty_ifd(b))
    p.write_bytes(bytes(b))


def _first_empty_ifd(b: bytearray) -> int:
    """The offset of the first of the empty IFDs `_many_ifds` wrote: the record whose next
    points 6 bytes on."""
    for o in range(8, len(b) - 6):
        if struct.unpack_from("<H", b, o)[0] == 0 and struct.unpack_from("<I", b, o + 2)[0] == o + 6:
            return o
    raise AssertionError("no empty IFD")


if __name__ == "__main__":
    main()
