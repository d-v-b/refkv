"""Writes synthetic Akoya/PerkinElmer QPTIFF files to fixtures/qptiff/ for
spec/zarrify/qptiff.md <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/qptiff.md>,
with tifffile, in the arrangement of the format's specification and of Akoya's public samples:
the channels as FullResolution images first, a stripped RGB Thumbnail, the ReducedResolution
images of each level (tiled, or stripped for the smallest), then the Overview and Label images.
Every IFD has Software `PerkinElmer-QPI` and a `PerkinElmer-QPI-ImageDescription` XML.

- `qptiff_fl_uint8_lzw.qptiff`: three LZW fluorescence channels, two tiled levels and one
  stripped level whose last strip is short (a rectilinear chunk grid), stage position tags,
  channel colors and names (one with an entity reference, one nested in a ScanProfile
  that must not be read).
- `qptiff_fl_uint16_lzw_bigtiff.qptiff`: BigTIFF, two uint16 channels, 96 dpi (no pixel
  size), an IFD of an unknown ImageType and one without a description, both ignored.
- `qptiff_bf_rgb_jpeg.qptiff`: a brightfield RGB scan with JPEG tiles, and a stripped JPEG
  level; the thumbnail, overview and label are LZW.
- `qptiff_bf_rgb_uncompressed.qptiff`: uncompressed RGB tiles, and a level in uncompressed
  strips whose last strip is short (padded with a zero data source).
- `qptiff_component_float32_strips.qptiff`: inForm component data, float32 strips only.
- `qptiff_fl_lzw_narrow_eoi.qptiff`: one LZW channel whose first tile's stream ends the way
  some Vectra scanners write it, with the end code one bit too narrow (read at the right
  width it is code 514, one more table entry), so the stream decodes past the tile; the
  codecs read it into the tile's size, as tifffile does.
- `edge_reject_qptiff_*.qptiff`: inputs the profile rejects.

Pixels are checked against tifffile by js/test/qptiff/verify.py.

Usage: uv run python fixtures/generators/qptiff/write_fixtures.py
"""

from __future__ import annotations

import struct
from pathlib import Path

import numpy as np
import tifffile

OUT = Path(__file__).parents[2] / "qptiff"
SOFTWARE = "PerkinElmer-QPI"
RESOLUTION = (20080, 20080)  # pixels per centimeter: 0.498 µm per pixel
# XPosition and YPosition (RATIONAL, in ResolutionUnits): 0.886 cm, 4.844 cm
POSITION = [(286, 5, 1, (886, 100), False), (287, 5, 1, (4844, 100), False)]
rng = np.random.default_rng(82)


def image(shape, dtype, seed=0):
    y, x = np.mgrid[0 : shape[-2], 0 : shape[-1]]
    base = (x * 3 + y * 5 + seed * 17) % 251
    out = np.broadcast_to(base, shape).astype(np.float64) + rng.integers(0, 4, shape)
    if dtype == np.float32:
        return (out / 7.0).astype(dtype)
    return out.astype(dtype)


def description(image_type, *, name=None, color=None, slide_id="Fixture-Slide", unmixed=False,
                scan_profile=False) -> str:
    lines = [
        '<?xml version="1.0" encoding="utf-16"?>',
        "<PerkinElmer-QPI-ImageDescription>",
        "  <DescriptionVersion>2</DescriptionVersion>",
        "  <AcquisitionSoftware>refkv fixtures</AcquisitionSoftware>",
        f"  <ImageType>{image_type}</ImageType>",
        "  <Identifier>7c1b4d1a-0000-4000-8000-000000000082</Identifier>",
    ]
    if slide_id is not None:
        lines.append(f"  <SlideID>{slide_id}</SlideID>")
    lines += [
        "  <Barcode />",
        f"  <IsUnmixedComponent>{'True' if unmixed else 'False'}</IsUnmixedComponent>",
        "  <ExposureTime>100</ExposureTime>",
        "  <SignalUnits>64</SignalUnits>",
    ]
    if name is not None:
        lines.append(f"  <Name>{name}</Name>")
    if color is not None:
        lines.append(f"  <Color>{color}</Color>")
    lines.append("  <Objective>10x</Objective>")
    if scan_profile:
        # nested elements named like the channel's, which a reader must not take for it
        lines += ["  <ScanProfile>", "    <root><Name>not the channel</Name><Color>1,2,3</Color>",
                  "    <!-- <ImageType>Label</ImageType> --></root>", "  </ScanProfile>"]
    lines += ["  <ValidationCode>0</ValidationCode>", "</PerkinElmer-QPI-ImageDescription>"]
    return "\r\n".join(lines) + "\r\n"


def page(tif, data, image_type, *, tile=None, rps=None, compression="lzw", photometric="minisblack",
         reduced=False, position=True, resolution=RESOLUTION, unit="CENTIMETER", desc=None, **kw) -> None:
    """One IFD: tiled (`tile`) or stripped (`rps` rows per strip)."""
    args = dict(photometric=photometric, compression=compression, software=SOFTWARE, metadata=None,
                resolution=resolution, resolutionunit=unit, subfiletype=1 if reduced else 0,
                extratags=POSITION if position else [])
    if desc is not False:
        args["description"] = desc if desc is not None else description(image_type, **kw)
    if tile is not None:
        args["tile"] = tile
    else:
        args["rowsperstrip"] = rps
    tif.write(data, **args)


def rgb(h, w, seed=0):
    g = image((h, w), np.uint8, seed)
    return np.stack([g, (g // 2).astype(np.uint8), (255 - g).astype(np.uint8)], axis=-1)


def pyramid(tif, channels, names, colors, *, tile, levels, rps=None, stripped_last=True, reduced_rps=16, slide_id="Fixture-Slide",
            compression="lzw", photometric="minisblack", unmixed=False, position=True, resolution=RESOLUTION, unit="CENTIMETER",
            thumbnail=True, extras=True, reduced_names=True):
    """The QPTIFF arrangement: channels (tiled, or in strips of `rps` rows), thumbnail, levels (each
    channel again), overview, label."""
    kw = dict(compression=compression, photometric=photometric, position=position, resolution=resolution, unit=unit)
    for c, data in enumerate(channels):
        page(tif, data, "FullResolution", tile=tile, rps=rps, name=names[c], color=colors[c], slide_id=slide_id,
             unmixed=unmixed, scan_profile=(c == 0), **kw)
    if thumbnail:
        page(tif, rgb(24, 32, 9), "Thumbnail", rps=16, compression="lzw", photometric="rgb", slide_id=slide_id,
             position=position, resolution=(2000, 2000), unit=unit)
    for level in range(1, levels + 1):
        s = 2**level
        last = level == levels and stripped_last
        for c, data in enumerate(channels):
            small = data[::s, ::s]
            page(tif, small, "ReducedResolution", tile=None if last else tile, rps=reduced_rps if last else None,
                 reduced=True, name=names[c] if reduced_names else None, color=colors[c] if reduced_names else None,
                 slide_id=slide_id, unmixed=unmixed, compression=compression, photometric=photometric,
                 position=position, resolution=tuple(r / s for r in resolution), unit=unit)
    if extras:
        page(tif, rgb(40, 20, 3), "Overview", rps=16, compression="lzw", photometric="rgb", slide_id=slide_id,
             position=position, resolution=(600, 600), unit=unit)
        page(tif, rgb(22, 26, 5), "Label", rps=16, compression="lzw", photometric="rgb", slide_id=slide_id,
             position=position, resolution=(1500, 1500), unit=unit)


def lzw_codes(stream: bytes):
    """The codes of a TIFF LZW stream with the bit position and width each was read at,
    following the decoder's table growth (TIFF 6.0 §13 with libtiff's early change)."""
    bit, width, nxt, prev = 0, 9, 258, None
    while bit + width <= len(stream) * 8:
        code = 0
        for i in range(width):
            b = bit + i
            code = (code << 1) | ((stream[b // 8] >> (7 - b % 8)) & 1)
        yield code, bit, width
        bit += width
        if code == 256:
            width, nxt, prev = 9, 258, None
            continue
        if code == 257:
            return
        if prev is not None and nxt < 4096:
            nxt += 1
            if nxt + 1 == 1 << width and width < 12:
                width += 1
        prev = code


def narrow_eoi(path: Path, page_index: int, tile: int = 0) -> None:
    """Rewrites a tile's end-of-information code one bit too narrow, as some Vectra scanners
    do when the last code lands on a code-width boundary: a reader then sees code 514."""
    with tifffile.TiffFile(path) as tf:
        page = tf.pages[page_index]
        offset, count = page.dataoffsets[tile], page.databytecounts[tile]
        order = tf.byteorder
    with open(path, "r+b") as f:
        f.seek(offset)
        stream = bytearray(f.read(count))
        last = None
        for code, bit, width in lzw_codes(stream):
            last = (code, bit, width)
        code, bit, width = last
        assert code == 257 and width >= 10, (code, width)
        # clear the bits from the end code on, then write 257 in width - 1 bits
        for b in range(bit, len(stream) * 8):
            stream[b // 8] &= ~(1 << (7 - b % 8)) & 0xFF
        for i in range(width - 1):
            if (257 >> (width - 2 - i)) & 1:
                b = bit + i
                stream[b // 8] |= 1 << (7 - b % 8)
        f.seek(offset)
        f.write(bytes(stream))
    assert order == "<"


def patch_long(path: Path, page_index: int, tag: int, value: int) -> None:
    """Overwrites the in-line LONG value of `tag` in IFD `page_index`."""
    with tifffile.TiffFile(path) as tf:
        t = tf.pages[page_index].tags[tag]
        offset, order = t.valueoffset, tf.byteorder
    with open(path, "r+b") as f:
        f.seek(offset)
        f.write(struct.pack(order + "I", value))


def main() -> None:
    OUT.mkdir(exist_ok=True)
    fl8 = [image((96, 128), np.uint8, c) for c in range(3)]
    names8 = ["DAPI", "Cy3 &amp; TRITC", "Opal 520"]
    colors8 = ["0,0,255", "255, 60, 157", "0,255,0"]
    with tifffile.TiffWriter(OUT / "qptiff_fl_uint8_lzw.qptiff") as tif:
        pyramid(tif, fl8, names8, colors8, tile=(32, 32), levels=2)

    fl16 = [image((64, 80), np.uint16, c) * 100 for c in range(2)]
    with tifffile.TiffWriter(OUT / "qptiff_fl_uint16_lzw_bigtiff.qptiff", bigtiff=True) as tif:
        pyramid(tif, fl16, ["DAPI", "FITC"], ["0,0,255", "0,255,0"], tile=(32, 32), levels=1, stripped_last=False,
                position=False, resolution=(96, 96), unit="INCH", extras=False, slide_id=None)
        # an image of an ImageType this convention does not know, and one without a description: ignored
        page(tif, rgb(20, 24, 2), "Macro", rps=16, compression="lzw", photometric="rgb", position=False,
             resolution=(96, 96), unit="INCH")
        page(tif, rgb(20, 24, 4), "", rps=16, compression="lzw", photometric="rgb", position=False, resolution=(96, 96),
             unit="INCH", desc=False)

    bf = [rgb(96, 128, 1)]
    with tifffile.TiffWriter(OUT / "qptiff_bf_rgb_jpeg.qptiff") as tif:
        pyramid(tif, bf, ["BrightField"], [None], tile=(32, 32), levels=2, compression="jpeg", photometric="rgb",
                reduced_names=False)

    with tifffile.TiffWriter(OUT / "qptiff_bf_rgb_uncompressed.qptiff") as tif:
        pyramid(tif, [rgb(64, 64, 6)], ["BrightField"], [None], tile=(32, 32), levels=1, compression=None,
                photometric="rgb", reduced_rps=24, extras=False)

    noisy = rng.integers(0, 256, (64, 64)).astype(np.uint8)  # many table entries: 10-bit codes
    narrow = OUT / "qptiff_fl_lzw_narrow_eoi.qptiff"
    with tifffile.TiffWriter(narrow) as tif:
        page(tif, noisy, "FullResolution", tile=(32, 32), name="DAPI", color="0,0,255")
    narrow_eoi(narrow, 0)

    comp = [image((40, 48), np.float32, c) for c in range(2)]
    with tifffile.TiffWriter(OUT / "qptiff_component_float32_strips.qptiff") as tif:
        pyramid(tif, comp, ["Eosin", "Hematoxylin"], ["255,60,157", "50,69,157"], tile=None, rps=16, levels=0,
                unmixed=True, extras=False, slide_id="HnE_3")

    # Rejected inputs.
    small = [image((48, 64), np.uint8, c) for c in range(3)]
    with tifffile.TiffWriter(OUT / "edge_reject_qptiff_channel_count.qptiff") as tif:
        for c in range(3):
            page(tif, small[c], "FullResolution", tile=(32, 32), name=f"C{c}", color="0,0,255")
        page(tif, rgb(24, 32), "Thumbnail", rps=16, photometric="rgb")
        for c in range(2):
            page(tif, small[c][::2, ::2], "ReducedResolution", tile=(32, 32), reduced=True, name=f"C{c}")
    with tifffile.TiffWriter(OUT / "edge_reject_qptiff_rgb_channels.qptiff") as tif:
        for c in range(2):
            page(tif, rgb(48, 64, c), "FullResolution", tile=(32, 32), photometric="rgb", name=f"C{c}")
    with tifffile.TiffWriter(OUT / "edge_reject_qptiff_not_baseline.qptiff") as tif:
        page(tif, rgb(24, 32), "Thumbnail", rps=16, photometric="rgb")
        page(tif, small[0], "FullResolution", tile=(32, 32), name="DAPI")
    with tifffile.TiffWriter(OUT / "edge_reject_qptiff_no_description.qptiff") as tif:
        page(tif, small[0], "FullResolution", tile=(32, 32), desc=False)
    with tifffile.TiffWriter(OUT / "edge_reject_qptiff_level_grows.qptiff") as tif:
        page(tif, small[0], "FullResolution", tile=(32, 32), name="DAPI")
        page(tif, fl8[0], "ReducedResolution", tile=(32, 32), reduced=True, name="DAPI")
    with tifffile.TiffWriter(OUT / "edge_reject_qptiff_late_full.qptiff") as tif:
        page(tif, small[0], "FullResolution", tile=(32, 32), name="DAPI")
        page(tif, rgb(24, 32), "Thumbnail", rps=16, photometric="rgb")
        page(tif, small[1], "FullResolution", tile=(32, 32), name="FITC")
    with tifffile.TiffWriter(OUT / "edge_reject_qptiff_level_mismatch.qptiff") as tif:
        for c in range(2):
            page(tif, small[c], "FullResolution", tile=(32, 32), name=f"C{c}")
        page(tif, small[0][::2, ::2], "ReducedResolution", tile=(32, 32), reduced=True, name="C0")
        page(tif, small[1][::4, ::4], "ReducedResolution", tile=(32, 32), reduced=True, name="C1")
    with tifffile.TiffWriter(OUT / "edge_reject_qptiff_predictor.qptiff") as tif:
        tif.write(small[0], photometric="minisblack", compression="lzw", predictor=True, tile=(32, 32),
                  software=SOFTWARE, metadata=None, description=description("FullResolution", name="DAPI"),
                  resolution=RESOLUTION, resolutionunit="CENTIMETER")
    strips = OUT / "edge_reject_qptiff_strip_count.qptiff"
    with tifffile.TiffWriter(strips) as tif:
        page(tif, comp[0], "FullResolution", rps=16, name="Eosin", unmixed=True)
    patch_long(strips, 0, 278, 8)  # RowsPerStrip 8: 5 strips expected, 3 present

    for p in sorted(OUT.glob("*.qptiff")):
        print(p.name, p.stat().st_size)


if __name__ == "__main__":
    main()
