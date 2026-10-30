"""Writes GRIB edition 2 files to fixtures/grib/ for the `refkv.grib` codec (spec/conventions.md
§12 <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#12-grib-messages>) and the GRIB
profile (spec/zarrify/grib.md), with the values each field decodes to.

Synthetic messages, on a 16 × 12 grid (Ni = 16 points along a row, Nj = 12 rows), each changing
one thing from `grib_simple` (template 5.0, 12 bits, grid template 3.0, scanning mode 0x00).
eccodes writes them, or they are assembled here from sections eccodes wrote, for what its
encoder does not write:

- packing (WMO-No. 306, FM 92 templates 5.x and 7.x): `grib_simple`, `grib_simple_decimal`
  (D = 2) and `grib_simple_decimal7` (D = 7, where eccodes' 10^−D, D divisions of 1 by 10,
  is not the nearest binary64 to 10^−D); `grib_complex` (5.2), `grib_complex_sd1` and
  `grib_complex_sd2` (5.3, spatial differencing of order 1 and 2), `grib_complex_missing1` and
  `grib_complex_sd2_missing1` (missing-value management 1, primary missing values in the
  packed data), `grib_complex_missing2` (5.2 with management 2, primary and secondary missing
  values, in groups of width 0 too: packed here, as eccodes writes only management 1);
  `grib_ieee32` and `grib_ieee64` (5.4); `grib_png` (5.41, 16-bit gray, each row with another
  PNG filter, the image in two IDAT chunks), `grib_png_1bit`, `grib_png_2bit`, `grib_png_4bit`,
  `grib_png_8bit` (gray), `grib_png_24bit` (RGB) and `grib_png_32bit` (RGBA), the PNG written
  here from the integers of a 5.0 message (eccodes 2.50's wheel fails to write PNG), its
  section 5 octets 12–21 kept but the bit width, which is the PNG's; `grib_ccsds` (5.42,
  eccodes' default block size 32 and reference sample interval 128), `grib_ccsds_rsi` (block
  size 8, interval 3, over a field with constant runs: zero blocks, second extension, split
  samples and uncompressed blocks, across reference sample intervals), `grib_ccsds_3bit` and
  `grib_ccsds_24bit`; `grib_jpeg2000` (two fields, sections 4–7 repeated: `grib_simple`'s,
  then one of 5.40, which the codec does not decode);
- bitmaps: `grib_bitmap` (indicator 0), `grib_bitmap_254` (three fields, sections 4–7
  repeated: a bitmap, then none (255), then the first one again (254));
- `grib_constant`: a bit width of 0 (eccodes keeps D = 2, and every decoder reads R);
- scanning modes: `grib_scan_40`, `grib_scan_80`, `grib_scan_50` (rows alternate direction),
  `grib_scan_20` (adjacent points in j consecutive) and `grib_scan_30`;
- grids: `grib_lambert` (3.30, scanning mode 0x40);
- messages of three fields: `grib_fields_4to7` (sections 4–7 repeated) and `grib_fields_3to7`
  (sections 3–7 repeated, each field on another grid);
- rejects, one per role of the profile's schema (rust/refkv-ir/schema/grib.json) a fixture can
  break: `grib_reject_truncated` (the message cut short), `grib_reject_end` (its last four
  bytes are not `7777`), `grib_reject_short` (a file shorter than a section 0),
  `grib_reject_edition1` (an edition 1 message after an edition 2 one), `grib_reject_section`
  (section 1's length past the message), `grib_reject_order` (section 3 numbered 4),
  `grib_reject_padding` (more than 4096 bytes after the last message) and
  `grib_reject_jpeg2000` (one field, of 5.40, so none can be presented).

Samples of public forecasts, one whole message each, read by HTTP range at the offsets their
feed's index gives (fixtures/grib/README.md records them, with their licenses): GFS 0.25°
(5.3 with a bitmap, 5.3 with missing-value management 1, and 5.3 without either), HRRR
(3.30, scanning mode 0x40: a 5.3 field, and a 5.0 field of bit width 0), ECMWF IFS (5.42,
with and without a bitmap) and NBM (3.30, scanning mode 0x50, missing-value management 1).

Expected values: eccodes' decoding of each field (with multi-field support), float64, NaN
where eccodes reports a missing value, in the codec's order. eccodes' `values` are in the
file's order: the generator checks it against its own decoding of the simple-packed scanning
mode fixtures. The codec keeps the rows and their directions, but reverses every second line
when scanning mode bit 0x10 is set, then transposes when bit 0x20 is set (a line is a row of
Ni points, or with 0x20 a column of Nj points), so the expected array is eccodes' values
reshaped to the lines, every second line reversed (0x10), then transposed (0x20): [Nj, Ni].
The 1-, 2- and 4-bit PNG fields (eccodes' wheel decodes only whole-byte PNG samples) expect
eccodes' values of the 5.0 message they were made from. For the synthetic messages they are
`fixtures/grib/expected/<name>.<field>.npy`; for the samples, whose arrays are megabytes,
`fixtures/grib/expected/samples.json` gives each field's shape, count of NaNs and the FNV-1a
64-bit hash of its float64 values' little-endian bytes (each NaN as 0x7ff8000000000000).

`just fixtures` does not run this generator (it needs eccodes 2.50.0, from the `eccodes` and
`eccodeslib` wheels, and the network): `just fixtures-of grib` does. The samples are cached in
/tmp/vzip-fixture-cache/grib/.

Usage: uv run python fixtures/generators/grib/write_fixtures.py
"""

# fixtures: on demand (it needs eccodes and downloads the samples; `just fixtures` skips it)

from __future__ import annotations

import json
import struct
import tempfile
import urllib.request
import zlib
from pathlib import Path

import eccodes as ec
import numpy as np

OUT = Path(__file__).parents[2] / "grib"
EXPECTED = OUT / "expected"
CACHE = Path("/tmp/vzip-fixture-cache/grib")  # noqa: S108 (a download cache, kept between runs)
NI, NJ = 16, 12
MISSING = 1e300  # eccodes' missing value while reading: no field holds it

# (fixture, source URL, offset, length, the index the offset is from, license)
NOAA = "public domain (NOAA Open Data Dissemination)"
ECMWF = "CC BY 4.0 (ECMWF open data, © ECMWF)"
GFS = "https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.20261029/00/atmos/gfs.t00z.pgrb2.0p25.f006"
HRRR = "https://noaa-hrrr-bdp-pds.s3.amazonaws.com/hrrr.20261029/conus/hrrr.t00z.wrfsfcf06.grib2"
IFS = "20261029/00z/ifs/0p25/oper/20261029000000-6h-oper-fc"
NBM = "https://noaa-nbm-grib2-pds.s3.amazonaws.com/blend.20261029/00/core/blend.t00z.core.f006.co.grib2"
SAMPLES = [
    # GFS 0.25°, f006: HINDEX (5.3 with a bitmap), ICETMP (5.3 with missing-value management
    # 1) and LAND (5.3 with neither)
    ("grib_gfs_hindex", GFS, 376931486, 204434, GFS + ".idx", NOAA),
    ("grib_gfs_icetmp", GFS, 518323874, 187836, GFS + ".idx", NOAA),
    ("grib_gfs_land", GFS, 517458469, 32036, GFS + ".idx", NOAA),
    # HRRR CONUS surface, f06: LAND (3.30, scanning mode 0x40, 5.3) and DSWRF (5.0, bit width 0)
    ("grib_hrrr_land", HRRR, 145880938, 50476, HRRR + ".idx", NOAA),
    ("grib_hrrr_dswrf", HRRR, 94978102, 188, HRRR + ".idx", NOAA),
    # ECMWF IFS 0.25°, step 6: 2t (5.42) and sithick (5.42 with a bitmap), from the AWS mirror
    # of data.ecmwf.int, whose index gives the offsets
    ("grib_ecmwf_2t", f"https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com/{IFS}.grib2", 22335104, 313766,
     f"https://data.ecmwf.int/forecasts/{IFS}.index", ECMWF),
    ("grib_ecmwf_sithick", f"https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com/{IFS}.grib2", 10074626,
     179396, f"https://data.ecmwf.int/forecasts/{IFS}.index", ECMWF),
    # NBM CONUS core, f006: TMP at the surface (3.30, scanning mode 0x50, 5.3 with missing-value
    # management 1)
    ("grib_nbm_tmp", NBM, 161452866, 417462, NBM + ".idx", NOAA),
]


# ---- sections

def sections(msg: bytes) -> list[bytes]:
    """The sections 1–7 of a message, in order, each with its length and number."""
    out, pos = [], 16
    while msg[pos:pos + 4] != b"7777":
        n = int.from_bytes(msg[pos:pos + 4], "big")
        out.append(msg[pos:pos + n])
        pos += n
    return out


def number(sec: bytes) -> int:
    return sec[4]


def section(num: int, body: bytes) -> bytes:
    return struct.pack(">IB", 5 + len(body), num) + body


def message(discipline: int, parts: list[bytes]) -> bytes:
    """Section 0, the sections `parts`, and `7777`."""
    body = b"".join(parts)
    return b"GRIB\0\0" + bytes([discipline, 2]) + struct.pack(">Q", 16 + len(body) + 4) + body + b"7777"


def sm(v: int, n: int) -> bytes:
    """A signed integer in `n` octets, sign and magnitude (FM 92 regulation 92.1.5)."""
    return ((1 << (8 * n - 1) if v < 0 else 0) | abs(v)).to_bytes(n, "big")


def with_s5(msg: bytes, s5: bytes, s7: bytes) -> bytes:
    """The single-field message `msg` with sections 5 and 7 replaced (section 6 kept)."""
    parts = [s if number(s) not in (5, 7) else (s5 if number(s) == 5 else s7) for s in sections(msg)]
    return message(msg[6], parts)


# ---- eccodes

def handle(packing: str = "grid_simple", bits: int = 12, scan: int = 0, **keys: object) -> int:
    """A message of the 16 × 12 regular latitude/longitude grid (60°N–5°N, 0°E–75°E, 5° steps),
    its first and last points as `scan` orders them, with `packing` and `bits`."""
    h = ec.codes_grib_new_from_samples("regular_ll_sfc_grib2")
    lat, lon = (5.0, 60.0) if scan & 0x40 else (60.0, 5.0), (75.0, 0.0) if scan & 0x80 else (0.0, 75.0)
    for k, v in {"Ni": NI, "Nj": NJ, "latitudeOfFirstGridPointInDegrees": lat[0],
                 "latitudeOfLastGridPointInDegrees": lat[1], "longitudeOfFirstGridPointInDegrees": lon[0],
                 "longitudeOfLastGridPointInDegrees": lon[1], "iDirectionIncrementInDegrees": 5.0,
                 "jDirectionIncrementInDegrees": 5.0, "scanningMode": scan, "packingType": packing,
                 "bitsPerValue": bits, **keys}.items():
        ec.codes_set(h, k, v)
    return h


def encode(h: int, values: np.ndarray) -> bytes:
    ec.codes_set_values(h, values)
    msg = bytes(ec.codes_get_message(h))
    ec.codes_release(h)
    return msg


def decoded(msg: bytes) -> list[tuple[np.ndarray, int, int, int]]:
    """eccodes' values of each field of `msg` (NaN where missing), with its Ni, Nj and scanning
    mode."""
    out = []
    ec.codes_grib_multi_support_on()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "m.grib2"
        path.write_bytes(msg)
        with path.open("rb") as f:
            while (h := ec.codes_grib_new_from_file(f)) is not None:
                ec.codes_set(h, "missingValue", MISSING)
                v = np.array(ec.codes_get_values(h), dtype=np.float64)
                v[v == MISSING] = np.nan
                ni, nj, scan = (ec.codes_get_long(h, k) for k in ("Ni", "Nj", "scanningMode"))
                out.append((v, ni, nj, scan))
                ec.codes_release(h)
    ec.codes_grib_multi_support_off()
    return out


def ordered(values: np.ndarray, ni: int, nj: int, scan: int) -> np.ndarray:
    """Values in the file's order, in the codec's: [Nj, Ni], every second line reversed (0x10),
    columns transposed to rows (0x20)."""
    lines = values.reshape((ni, nj) if scan & 0x20 else (nj, ni)).copy()
    if scan & 0x10:
        lines[1::2] = lines[1::2, ::-1]
    return lines.T.copy() if scan & 0x20 else lines


def field(seed: int, offset: float = 250.0, scale: float = 30.0) -> np.ndarray:
    """A smooth field with some texture, in the file's order (192 values)."""
    k = np.arange(NI * NJ, dtype=np.float64)
    i, j = k % NI, k // NI
    rng = np.random.default_rng(seed)
    return offset + scale * (np.sin(0.4 * i + seed) * np.cos(0.3 * j) + 0.1 * rng.standard_normal(k.size))


# ---- packings eccodes does not write

def unpacked(msg: bytes) -> tuple[np.ndarray, bytes]:
    """The integers X of a single-field 5.0 message, and its section 5's octets 12–21."""
    s = sections(msg)
    s5, s7 = next(x for x in s if number(x) == 5), next(x for x in s if number(x) == 7)
    n, bits = int.from_bytes(s5[5:9], "big"), s5[19]
    raw = np.unpackbits(np.frombuffer(s7[5:], dtype=np.uint8))[: n * bits].reshape(n, bits)
    x = (raw.astype(np.uint64) << np.arange(bits - 1, -1, -1, dtype=np.uint64)).sum(axis=1)
    return x, s5[11:21]


def simple_values(msg: bytes) -> np.ndarray:
    """A 5.0 message's values as eccodes computes them: (X × 2^E + R) × 10^−D, its 10^−D by
    successive divisions."""
    x, head = unpacked(msg)
    r = struct.unpack(">f", head[:4])[0]
    e = -(int.from_bytes(head[4:6], "big") & 0x7FFF) if head[4] & 0x80 else int.from_bytes(head[4:6], "big")
    d = -(int.from_bytes(head[6:8], "big") & 0x7FFF) if head[6] & 0x80 else int.from_bytes(head[6:8], "big")
    dscale = 1.0
    for _ in range(abs(d)):
        dscale = dscale / 10 if d > 0 else dscale * 10
    return (x.astype(np.float64) * 2.0**e + r) * dscale


def png(samples: np.ndarray, depth: int, color: int) -> bytes:
    """A PNG image of the integers `samples` [height, width]: gray (color type 0) of `depth`
    bits, or 8-bit RGB (2) or RGBA (6) holding each sample big-endian. Row r is filtered with
    filter type r mod 5, and the image data is split in two IDAT chunks."""
    height, width = samples.shape
    channels = {0: 1, 2: 3, 6: 4}[color]
    pixel = depth * channels
    bpp = max(1, pixel // 8)
    rows = []
    for row in samples:
        if pixel < 8:
            bits = np.array([(int(v) >> (pixel - 1 - b)) & 1 for v in row for b in range(pixel)], dtype=np.uint8)
            rows.append(np.packbits(bits).tobytes())
        else:
            rows.append(b"".join(int(v).to_bytes(pixel // 8, "big") for v in row))
    raw = bytearray()
    prior = bytes(len(rows[0]))
    for r, line in enumerate(rows):
        f = r % 5
        out = bytearray()
        for k, x in enumerate(line):
            a = line[k - bpp] if k >= bpp else 0
            b = prior[k]
            c = prior[k - bpp] if k >= bpp else 0
            if f == 0:
                p = 0
            elif f == 1:
                p = a
            elif f == 2:
                p = b
            elif f == 3:
                p = (a + b) // 2
            else:
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                p = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
            out.append((x - p) & 0xFF)
        raw += bytes([f]) + out
        prior = line

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    z = zlib.compress(bytes(raw), 9)
    half = len(z) // 2
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, depth, color, 0, 0, 0))
            + chunk(b"IDAT", z[:half]) + chunk(b"IDAT", z[half:]) + chunk(b"IEND", b""))


def png_message(source: bytes, depth: int, color: int = 0) -> bytes:
    """The 5.0 message `source` as a 5.41 message: its integers in a PNG of Nj rows of Ni
    samples, octet 20 the PNG's bits per pixel."""
    x, head = unpacked(source)
    pixel = depth * {0: 1, 2: 3, 6: 4}[color]
    s5 = section(5, struct.pack(">IH", NI * NJ, 41) + head[:8] + bytes([pixel]) + head[9:10])
    return with_s5(source, s5, section(7, png(x.reshape(NJ, NI), depth, color)))


class Bits:
    """Bits written most significant first, each run padded to an octet on request."""

    def __init__(self) -> None:
        self.bits: list[int] = []

    def put(self, v: int, n: int) -> None:
        self.bits += [(v >> (n - 1 - k)) & 1 for k in range(n)]

    def pad(self) -> None:
        self.bits += [0] * (-len(self.bits) % 8)

    def bytes(self) -> bytes:
        self.pad()
        return np.packbits(np.array(self.bits, dtype=np.uint8)).tobytes()


def complex_missing2(source: bytes) -> tuple[bytes, np.ndarray]:
    """The 5.0 message `source` as a 5.2 message with missing-value management 2, in groups of 8:
    the points of two whole groups missing (one primary, one secondary, each a group of width 0),
    and single points of other groups primary or secondary missing; and the mask of missing
    points (1 primary, 2 secondary)."""
    x, head = unpacked(source)
    n = x.size
    miss = np.zeros(n, dtype=np.uint8)
    miss[16:24] = 1
    miss[40:48] = 2
    miss[[3, 77, 150]] = 1
    miss[[5, 78, 191]] = 2
    nbits = int(x.max()).bit_length() + 1  # refs below 2^nbits − 2
    refs, widths = [], []
    for g in range(n // 8):
        xs, ms = x[8 * g: 8 * g + 8], miss[8 * g: 8 * g + 8]
        if (ms == 1).all():
            refs.append((1 << nbits) - 1)
            widths.append(0)
        elif (ms == 2).all():
            refs.append((1 << nbits) - 2)
            widths.append(0)
        else:
            present = xs[ms == 0]
            ref = int(present.min())
            refs.append(ref)
            # codes 2^w − 1 and 2^w − 2 are the missing values: the group's range stays below
            widths.append((int(present.max()) - ref + 2).bit_length())
    wref, wbits = min(widths), max(max(widths) - min(widths), 1).bit_length()
    b = Bits()
    for r in refs:
        b.put(r, nbits)
    b.pad()
    for w in widths:
        b.put(w - wref, wbits)
    b.pad()
    # group lengths: all 8 (reference 8, no bits), the last given as 8 in section 5
    for g, (r, w) in enumerate(zip(refs, widths, strict=True)):
        for k in range(8 * g, 8 * g + 8):
            if w == 0:
                continue
            code = (1 << w) - int(miss[k]) if miss[k] else int(x[k]) - r
            b.put(code, w)
    s5 = section(5, struct.pack(">IH", n, 2) + head[:8] + bytes([nbits, 0, 1, 2]) + struct.pack(">ff", 9999, 9998)
                 + struct.pack(">IBB", len(refs), wref, wbits) + struct.pack(">IBIB", 8, 1, 8, 0))
    return with_s5(source, s5, section(7, b.bytes())), miss


# ---- multi-field messages

def fields(msgs: list[bytes], restate: int, bitmaps: list[bytes | None] | None = None) -> bytes:
    """One message of the fields of the single-field messages `msgs` (section 1 the first's):
    each field after the first restates sections `restate` to 7; `bitmaps[k]`, when given,
    replaces field k's section 6."""
    parts: list[bytes] = []
    for k, m in enumerate(msgs):
        bitmap = bitmaps[k] if bitmaps is not None else None
        parts += [bitmap if number(s) == 6 and bitmap is not None else s for s in sections(m)
                  if (k == 0 or number(s) >= restate) and number(s) != 2]
    return message(msgs[0][6], parts)


# ---- writing

EXPECTED_DIGESTS: dict[str, list[dict[str, object]]] = {}


def fnv1a64(values: np.ndarray) -> str:
    v = values.astype("<f8").copy()
    raw = v.view("<u8")
    raw[np.isnan(v)] = 0x7FF8000000000000
    h = 0xCBF29CE484222325
    for byte in raw.tobytes():
        h = ((h ^ byte) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return f"{h:016x}"


def write(name: str, msg: bytes, expected: list[np.ndarray] | None = None, sample: bool = False,
          decodes: bool = True) -> None:
    """Writes the fixture `name` and, if the codec `decodes` it, its fields' expected values
    (eccodes', or `expected`, in the file's order)."""
    (OUT / f"{name}.grib2").write_bytes(msg)
    if decodes:
        got = decoded(msg) if expected is None else [(v, NI, NJ, 0) for v in expected]
        digests: list[dict[str, object]] = []
        for k, (v, ni, nj, scan) in enumerate(got):
            out = ordered(v, ni, nj, scan)
            if sample:
                digests.append({"shape": list(out.shape), "nan": int(np.isnan(out).sum()), "fnv1a64": fnv1a64(out)})
            else:
                np.save(EXPECTED / f"{name}.{k}.npy", out)
        if sample:
            EXPECTED_DIGESTS[f"{name}.grib2"] = digests
    print(f"{name}.grib2: {len(msg)} bytes")


def fetch(name: str, url: str, offset: int, length: int) -> bytes:
    path = CACHE / f"{name}.grib2"
    if not path.exists():
        # fixed https URLs
        req = urllib.request.Request(url, headers={"Range": f"bytes={offset}-{offset + length - 1}"})  # noqa: S310
        with urllib.request.urlopen(req, timeout=120) as r:  # noqa: S310
            data = r.read()
        CACHE.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    data = path.read_bytes()
    assert data[:4] == b"GRIB", name
    assert data[-4:] == b"7777", name
    assert int.from_bytes(data[8:16], "big") == length, name
    return data


def main() -> None:
    OUT.mkdir(exist_ok=True)
    EXPECTED.mkdir(exist_ok=True)
    for old in list(OUT.glob("*.grib2")) + list(EXPECTED.glob("*")):
        old.unlink()
    base = field(0)

    # packings
    simple = encode(handle(), base)
    write("grib_simple", simple)
    write("grib_simple_decimal", encode(handle(bits=0, decimalScaleFactor=2), base))
    tiny = encode(handle(bits=0, decimalScaleFactor=7), field(1, 5e-4, 3e-4))
    assert np.array_equal(decoded(tiny)[0][0], simple_values(tiny))
    write("grib_simple_decimal7", tiny)
    write("grib_complex", encode(handle("grid_complex"), base))
    write("grib_complex_sd1", encode(handle("grid_complex_spatial_differencing", orderOfSpatialDifferencing=1), base))
    write("grib_complex_sd2", encode(handle("grid_complex_spatial_differencing", orderOfSpatialDifferencing=2), base))
    holes = base.copy()
    holes[[0, 3, 4, 50, 51, 52, 100, 190, 191]] = 9999
    holes[120:136] = 9999  # a whole row
    for name, packing in (("grib_complex_missing1", "grid_complex"),
                          ("grib_complex_sd2_missing1", "grid_complex_spatial_differencing")):
        msg = encode(handle(packing, bitmapPresent=0, missingValue=9999, missingValueManagementUsed=1), holes)
        assert np.array_equal(np.isnan(decoded(msg)[0][0]), holes == 9999), name
        write(name, msg)
    msg2, miss = complex_missing2(simple)
    values2 = decoded(msg2)[0][0]
    assert np.array_equal(np.isnan(values2), miss > 0)
    assert np.array_equal(values2[miss == 0], simple_values(simple)[miss == 0])
    write("grib_complex_missing2", msg2)
    write("grib_ieee32", encode(handle("grid_ieee", bits=32, precision=1), base))
    write("grib_ieee64", encode(handle("grid_ieee", bits=64, precision=2), base))
    for name, bits, depth, color in (("grib_png", 12, 16, 0), ("grib_png_8bit", 6, 8, 0), ("grib_png_24bit", 20, 8, 2),
                                     ("grib_png_32bit", 28, 8, 6), ("grib_png_1bit", 1, 1, 0),
                                     ("grib_png_2bit", 2, 2, 0), ("grib_png_4bit", 4, 4, 0)):
        source = encode(handle(bits=bits), base)
        msg = png_message(source, depth, color)
        want = decoded(source)[0][0]
        if depth * {0: 1, 2: 3, 6: 4}[color] % 8 == 0:
            assert np.array_equal(decoded(msg)[0][0], want), name
        write(name, msg, [want])
    write("grib_ccsds", encode(handle("grid_ccsds"), base))
    runs = base.copy()
    runs[30:110] = 250.0  # constant runs, longer than a block
    runs[150:160] = 270.0
    write("grib_ccsds_rsi", encode(handle("grid_ccsds", ccsdsBlockSize=8, ccsdsRsi=3), runs))
    write("grib_ccsds_3bit", encode(handle("grid_ccsds", bits=3), base))
    write("grib_ccsds_24bit", encode(handle("grid_ccsds", bits=24), base))
    write("grib_jpeg2000", fields([simple, encode(handle("grid_jpeg"), base)], 4), decodes=False)

    # bitmaps
    masked = base.copy()
    masked[[1, 2, 17, 18, 19, 60, 61, 62, 63, 64, 120, 191]] = 9999
    mask = masked != 9999
    write("grib_bitmap", encode(handle(bitmapPresent=1, missingValue=9999), masked))
    with_bitmap = encode(handle(bitmapPresent=1, missingValue=9999), masked)
    plain = encode(handle(), field(2))
    other = field(3)
    other[~mask] = 9999
    reused = encode(handle(bitmapPresent=1, missingValue=9999), other)
    msg = fields([with_bitmap, plain, reused], 4, [None, None, section(6, bytes([254]))])
    want = [decoded(m)[0][0] for m in (with_bitmap, plain, reused)]
    write("grib_bitmap_254", msg, want)

    # a constant field: R, which eccodes writes as the value itself, with D = 2 kept
    constant = encode(handle(decimalScaleFactor=2), np.full(NI * NJ, 273.15))
    s5 = next(s for s in sections(constant) if number(s) == 5)
    assert s5[19] == 0
    assert s5[17:19] == sm(2, 2)
    write("grib_constant", constant)

    # scanning modes: eccodes' values are the file's order (checked against the integers)
    for scan in (0x40, 0x80, 0x50, 0x20, 0x30):
        msg = encode(handle(scan=scan), base)
        assert np.array_equal(decoded(msg)[0][0], simple_values(msg)), hex(scan)
        write(f"grib_scan_{scan:02x}", msg)

    # Lambert conformal (3.30): 16 × 12 points 25 km apart, scanning south to north as HRRR's
    h = ec.codes_grib_new_from_samples("regular_ll_sfc_grib2")
    ec.codes_set(h, "gridDefinitionTemplateNumber", 30)
    for k, v in {"Nx": NI, "Ny": NJ, "DxInMetres": 25000, "DyInMetres": 25000,
                 "latitudeOfFirstGridPointInDegrees": 30.0, "longitudeOfFirstGridPointInDegrees": 250.0,
                 "LaDInDegrees": 38.5, "LoVInDegrees": 262.5, "Latin1InDegrees": 38.5, "Latin2InDegrees": 38.5,
                 "scanningMode": 0x40, "packingType": "grid_simple", "bitsPerValue": 12}.items():
        ec.codes_set(h, k, v)
    lambert = encode(h, field(4))
    write("grib_lambert", lambert)

    # three fields in a message
    three = [encode(handle(), field(5 + k)) for k in range(3)]
    write("grib_fields_4to7", fields(three, 4))
    h = handle()
    small_grid = {"Ni": 8, "Nj": 6, "longitudeOfLastGridPointInDegrees": 35.0, "latitudeOfLastGridPointInDegrees": 35.0}
    for k, v in small_grid.items():
        ec.codes_set(h, k, v)
    small = encode(h, field(9)[:48])
    write("grib_fields_3to7", fields([three[0], small, lambert], 3))

    # rejects
    write("grib_reject_truncated", simple[: len(simple) // 2], decodes=False)
    write("grib_reject_end", simple[:-4] + b"7776", decodes=False)
    write("grib_reject_short", simple[:12], decodes=False)
    write("grib_reject_edition1", simple + simple[:7] + b"\x01" + simple[8:], decodes=False)
    write("grib_reject_section", simple[:16] + b"\xff\xff\xff\x00" + simple[20:], decodes=False)
    s1 = int.from_bytes(simple[16:20], "big")
    assert simple[16 + s1 + 4] == 3, "grib_simple has a section 2"
    write("grib_reject_order", simple[:16 + s1 + 4] + b"\x04" + simple[16 + s1 + 5:], decodes=False)
    write("grib_reject_padding", simple + bytes(5000), decodes=False)
    write("grib_reject_jpeg2000", encode(handle("grid_jpeg"), base), decodes=False)

    # samples of public forecasts
    for name, url, offset, length, _index, _license in SAMPLES:
        write(name, fetch(name, url, offset, length), sample=True)
    (EXPECTED / "samples.json").write_text(json.dumps(EXPECTED_DIGESTS, indent=1) + "\n")


if __name__ == "__main__":
    main()
