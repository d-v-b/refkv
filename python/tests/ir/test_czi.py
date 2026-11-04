"""The CZI IR (round 3): the Rust parser and its schema, the image projection against
today's profile, the compact mirror, the streaming XML values, and round 1's CZI
review findings, each closed and probed."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import override

import numpy as np
import pytest
from ir_helpers import FIXTURES, REVISED, as_the_reference, same_hierarchy, zarrified
from refkv_reference import virtualize  # the frozen reference (ir_helpers)
from refkv_reference.czi.xml import read_xml_values

from refkv import _core
from refkv._narrow import integer, mapping, number
from refkv.ir import parse
from refkv.ir import zarrify as via_ir
from refkv.ir.planner import HttpTransport
from refkv.policy import Policy
from refkv.zarrify import Rejected

CZIS = sorted((FIXTURES / "czi").iterdir())


@pytest.mark.parametrize("path", [p for p in CZIS if p.name not in REVISED], ids=lambda p: p.name)
def test_projects_as_today_and_rebuilds(path, tmp_path):
    as_the_reference(path, tmp_path, "czi")


def ramp(dtype: str, samples: int, w: int, h: int, k: int = 0) -> np.ndarray:
    """The known pixels of the generator's codec fixtures, [h, w] or [h, w, samples], as stored."""
    kind = np.dtype(dtype)
    v = np.arange(w * h * samples, dtype=np.int64) * 37 + k * 101
    if kind.kind == "c":
        out = (v / 8 + 1j * v / 16).astype(kind)
    elif kind.kind == "f":
        out = (v / 8).astype(kind)
    else:
        out = (v % (int(np.iinfo(kind).max) + 1)).astype(kind)
    return out.reshape((h, w) if samples == 1 else (h, w, samples))


LZW_TYPES = [("u1", 1), ("<u2", 1), ("<f4", 1), ("u1", 3), ("<u2", 3), ("<f4", 3), ("u1", 4), ("<c8", 1), ("<c8", 3),
             ("<i4", 1), ("<f8", 1)]


def lzw_cases():
    """(image, array of it, what it holds, its codecs after `transpose`) of czi_lzw.czi."""
    for k, (dtype, samples) in enumerate(LZW_TYPES):
        one_byte = np.dtype(dtype).itemsize == 1
        little = {"name": "bytes"} if one_byte else {"name": "bytes", "configuration": {"endian": "little"}}
        yield k, ramp(dtype, samples, 12, 8, k), [little, {"name": "imagecodecs_lzw"}]


def test_lzw_lossless_and_12_bit_subblocks_read_their_pixels(tmp_path):
    """spec/zarrify/czi.md §3.1
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/czi.md#31-pixel-types-and-codecs>: every
    value, read through zarr-python and `refkv.zarr`, and the mirror rebuilds each file."""
    out, g, _ = zarrified(FIXTURES / "czi" / "czi_lzw.czi", tmp_path, "czi")
    for k, want, codecs in lzw_cases():
        meta = json.loads(out.bytes_entries[f"{k}/0/zarr.json"])
        assert meta["codecs"][-2:] == codecs
        got = np.asarray(g[f"{k}/0"][:])
        if want.ndim == 3:  # [c, y, x], the samples in the order they are stored
            got = np.moveaxis(got, 0, -1)
            labels = [c["label"] for c in g[str(k)].attrs["ome"]["omero"]["channels"]]
            assert [label[-1] for label in labels] == list("BGRA"[: want.shape[2]])
        assert got.dtype == want.dtype
        assert np.array_equal(got, want)
    clipped = g[f"{len(LZW_TYPES)}/0"]
    meta = json.loads(out.bytes_entries[f"{len(LZW_TYPES)}/0/zarr.json"])
    assert meta["chunk_grid"]["configuration"]["chunk_shapes"] == [[[8, 1], 5], [[10, 2], 7]]
    rows = [np.hstack([ramp("<u2", 1, 7 if i == 2 else 10, 5 if j == 1 else 8, 100 + 10 * j + i) for i in range(3)])
            for j in range(2)]
    assert np.array_equal(clipped[:], np.vstack(rows))

    out, g, _ = zarrified(FIXTURES / "czi" / "czi_ljpeg.czi", tmp_path, "czi")
    twelve = ramp("<u2", 1, 12, 8, 2) % 4096
    for k, want in enumerate([ramp("u1", 1, 12, 8, 0), ramp("<u2", 1, 12, 8, 1), twelve]):
        assert json.loads(out.bytes_entries[f"{k}/0/zarr.json"])["codecs"] == [{"name": "imagecodecs_ljpeg"}]
        got = np.asarray(g[f"{k}/0"][:])
        assert got.dtype == want.dtype
        assert np.array_equal(got, want)

    out, g, _ = zarrified(FIXTURES / "czi" / "czi_jpeg_12bit.czi", tmp_path, "czi")
    assert json.loads(out.bytes_entries["0/0/zarr.json"])["codecs"] == [{"name": "imagecodecs_jpeg"}]
    smooth = (np.arange(32 * 16, dtype=np.uint16).reshape(16, 32) * 7).astype("<u2")
    want = np.hstack([smooth, smooth + 64])
    got = np.asarray(g["0/0"][:])
    assert got.dtype == np.dtype("uint16")
    assert got.max() > 255  # 12-bit values, not an 8-bit stream's
    assert np.abs(got.astype(int) - want.astype(int)).max() <= 16  # the stream is lossy


def test_a_stream_that_does_not_fit_its_subblock_has_no_array(tmp_path):
    """spec/zarrify/czi.md §3.2
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/czi.md#32-conforming-and-unplaced-subblocks>:
    an 8-bit JPEG under Gray16 (it would read as 0 to 255), a lossless stream of the wrong
    precision or a stream of another kind, and LZW codes in the other bit order, are unplaced,
    their data kept as bytes; the rest of the file is read."""
    out, g, ir = zarrified(FIXTURES / "czi" / "czi_codec_unplaced.czi", tmp_path, "czi")
    assert list(g["OME"].attrs["ome"]["series"]) == ["0"]
    assert g["0"].attrs["refkv_zarrified"]["czi"] == {"dimensions": {"S": 8}}
    assert "tiles/zarr.json" not in out.bytes_entries
    kinds = []
    for i in range(9):
        sub = integer(ir.child(0, f"subblocks/{i}"))
        kinds.append((ir.kind(integer(ir.child(sub, "data"))), ir.kind(integer(ir.child(sub, "sizes")))))
    assert all(data == sizes for data, sizes in kinds[:8])  # a value, not coded data
    assert kinds[8][0] != kinds[8][1]


XMLS = [
    ("<ImageDocument><Metadata><Scaling><Items><Distance Id='Y'><Value>2e-7</Value></Distance>"
    "<Distance Id='X'><Value>1e-7</Value><Value>9</Value></Distance><Distance Id='X'><Value>5</Value></Distance>"
    "</Items></Scaling><Information><Image><ComponentBitCount>12</ComponentBitCount><Dimensions>"
    "<Channels><Channel Name='a'><Color>#FF00FF00</Color><ComponentBitCount>10</ComponentBitCount></Channel>"
    "<Channel/><Channel Name='c'><!-- x --><Color>#<![CDATA[]]>00FF00</Color></Channel></Channels>"
    "<T><Positions><Interval><Increment> 0.5 </Increment></Interval></Positions></T>"
    "<S><Scenes><Scene Index='1' Name='one'/><Scene Index='1' Name='two'/><Scene Index='0'/></Scenes></S>"
    "</Dimensions></Image></Information><DisplaySetting><Channels><Channel Name='d'><Low>0.1</Low>"
    "<High>0.9</High><Color>#123456</Color></Channel></Channels></DisplaySetting></Metadata></ImageDocument>"),
    ("<ImageDocument><Metadata></Scaling><Scaling><Items><Distance Id='X'><Value>3</Value></Items>"
    "</Metadata><Metadata><Scaling/></Metadata></ImageDocument>"),
    "<Other><Metadata/></Other>",
    ("﻿<ImageDocument><Metadata><Information><Image><ComponentBitCount>0000000000000000008</ComponentBitCount>"
    "</Image></Information></Metadata></ImageDocument>"),
    "<ImageDocument>" + "<a>" * 50 + "</b>" * 50 + "<Metadata><Information><Image><ComponentBitCount>8"
    "</ComponentBitCount></Image></Information></Metadata></ImageDocument>",
    ("<ImageDocument><Metadata><Information><Image><Dimensions><Channels><Channel Name='a &amp; b'>"
    "<Color>#gg0000</Color></Channel></Channels></Dimensions></Image></Information></Metadata></ImageDocument>"),
]


def as_dict(v):
    ch = lambda c: {"name": c.name, "color": c.color, "bits": c.bits}  # noqa: E731
    disp = lambda c: {"name": c.name, "color": c.color, "low": c.low, "high": c.high}  # noqa: E731
    return {"px": v.px, "py": v.py, "pz": v.pz, "inc": v.inc, "bits": v.bits, "info": [ch(c) for c in v.info],
            "display": [disp(c) for c in v.display], "scenes": sorted(v.scenes.items())}


def test_streaming_xml_values_are_the_tree_s():
    for xml in XMLS:
        r = json.loads(_core.czi_xml_values(xml.encode()))
        r["info"] = [{k: c[k] for k in ("name", "color", "bits")} for c in r["info"]]
        r["display"] = [{k: c[k] for k in ("name", "color", "low", "high")} for c in r["display"]]
        r["scenes"] = sorted((i, n) for i, n in r["scenes"])
        assert r == as_dict(read_xml_values(xml.encode())), xml[:60]


def test_bands_have_a_floor():
    """Divisor bands (spec/zarrify/czi.md §4.3
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/czi.md#43-image-arrays>) when they hold
    at least 1 MiB; else bands of the
    most rows within 2^24 bytes, the last of each tile row shorter."""
    cases = [
        # (h, h2, r, w, q, uncompressed) -> (y lengths, rows, per)
        ((4, 4, 3, 4, 1, True), (4, 4, 1)),  # small: no bands
        ((4, 2, 3, 4, 1, True), ([[4, 2], 2], 4, 1)),
        ((4, 2, 1, 4, 1, True), ([2], 4, 1)),
        ((4096, 4096, 3, 8192, 1, True), (2048, 2048, 2)),  # divisor bands of 16 MiB
        ((16777259, 16777259, 1, 1, 1, True), ([1 << 24, 43], 1 << 24, 2)),  # a prime height: the floor
        ((16777259, 16777259, 1, 1, 1, False), (16777259, 16777259, 1)),  # compressed: one chunk
    ]
    for args, (y, rows, per) in cases:
        got = _core.czi_bands(*args)
        assert (json.loads(got[0]), got[1], got[2]) == (y, rows, per), args


def test_entries_that_share_a_subblock_are_aliases_and_place_it_once(probes):
    _, out, ir = via_ir(str(probes / "czi_dup.czi"), "u", mirror=False)
    subs = [integer(ir.child(0, f"subblocks/{i}")) for i in range(1000)]
    assert ir.kind(subs[0]) == 0
    assert all(ir.kind(s) == 4 and ir.target(s) == subs[0] for s in subs[1:])
    assert list(out.refs) == ["0/0/c/0/0"]
    assert "tiles/zarr.json" not in out.bytes_entries  # today: 1,000 tile arrays


def test_rejects_a_tile_array_past_2_to_the_31(probes):
    with pytest.raises(Rejected, match="more than 2\\^31"):
        via_ir(str(probes / "czi_jxrbig.czi"), "u", mirror=False)


def test_unmatched_end_tags_and_flat_xml_stream(probes):
    for name in ("czi_deep2000.czi", "czi_flat.czi"):
        _, today = virtualize(str(probes / name), "u")
        _, out, _ = via_ir(str(probes / name), "u", mirror=False)
        same_hierarchy(today, out)


def test_attachment_names_keep_their_bytes_after_the_nul(probes):
    _, _, ir = via_ir(str(probes / "czi_attname.czi"), "u", mirror=False)
    d = integer(ir.child(0, "attachment_directory"))
    e = mapping(ir.value(integer(ir.child(d, "entries/0"))))
    assert e["name"] == {"text": "Label", "after": b"\0hidden name" + bytes(80 - 17)}
    assert e["content_file_type"] == {"text": "CZTIMS", "after": b"\0x"}
    e = mapping(ir.value(integer(ir.child(d, "entries/1"))))
    assert e["name"] == {"text": "Thumb", "after": b"\0" * 6 + b"x" + bytes(80 - 12)}
    a = integer(ir.child(0, "attachments/0"))
    assert ir.child(integer(ir.child(a, "data")), "values") is not None  # read as CZTIMS, by its bytes up to the NUL


def test_empty_segments_fold_into_one_run(probes):
    _, _, ir = via_ir(str(probes / "czi_walk0.czi"), "u", mirror=False)
    segs = [i for i in ir.children(0) if ir.name(i).startswith("segments/")]
    assert len(segs) == 1
    run = ir.run(segs[0])
    assert run is not None
    assert run[0] == 10000
    assert len(ir) < 100


class _Ranges:
    """A local server of single ranges (no multipart) over a file."""

    def __init__(self, path):
        data = path.read_bytes()
        self.bytes = 0

        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            @override
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                rng = self.headers.get("Range", "")
                if "," in rng:
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                a, b = rng[6:].split("-")
                a, b = int(a), min(int(b) + 1, len(data))
                outer.bytes += b - a
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {a}-{b - 1}/{len(data)}")
                self.send_header("Content-Length", str(b - a))
                self.end_headers()
                self.wfile.write(data[a:b])

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/f"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def test_the_planner_reads_headers_not_the_whole_file(probes):
    """2,000 subblocks of 4 KiB over a single-range server: the parser asks for their
    headers in batches, and the planner fetches at most its cap times the bytes asked."""
    path = probes / "czi_xt.czi"
    server = _Ranges(path)
    try:
        _ir, facts, st = parse("czi", HttpTransport(server.url, Policy(allow_private_hosts=True)), 4)
        # requests cost the batch less than half a second here, so the cap is 4; on a
        # loaded machine they may cost more, and the ceiling of 16 applies
        assert st["packs"] is False
        assert st["cap"] in (4, 16)
        assert number(st["bytes"]) <= number(st["cap"]) * number(st["asked"]) + (1 << 16) * number(st["batches"])
        assert facts["subblocks"] == 2000
        assert number(facts["rounds"]) < 12
    finally:
        server.close()
