"""Writes the synthetic LIF, LOF and LIFEXT fixtures in fixtures/lif/ (spec/zarrify/lif.md).

No library writes these files, so small writers build them byte for byte. A LIF file (`Lif`) is the
header block (test value 0x70, its length, the magic byte 0x2A, the XML's characters and the XML in
UTF-16), then one memory block per `Memory` element (version 2: a 64-bit data size; version 1:
32-bit). A LOF (`Lof`) is a header block whose text is `LMS_Object_File`, its version fields, its
one memory block, which has no ID, and its XML in a block after the data. A LIFEXT file (`Lifext`)
is a LIF file whose root is `LMSDataContainerEnhancedHeader`, whose images lie under `ChildrenOf`
elements and whose blocks have 64-bit sizes whatever its `Version`. Each image is an `Img`: its axes
from the fastest, `c` where its channels are (`c` first: interleaved), with the BytesInc of each
packed in that order, as LAS X writes them. Every accepted fixture is read once here by liffile,
which must give each image's array, as the generator laid it out. Rejected fixtures
(`edge_reject_lif_*`) each break one rule the profile rejects.

Usage: uv run python fixtures/generators/lif/write_fixtures.py
"""

from __future__ import annotations

import struct
import sys
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import quoteattr

import liffile
import numpy as np

HERE = Path(__file__).parent
OUT = HERE.parents[1] / "lif"
TEST, MAGIC = 0x70, 0x2A
# DimID by axis: x, y, z, t, emission wavelength, mosaic (spec/zarrify/lif.md §2.4
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#24-dimensions-and-channels>)
DIM_ID = {"x": 1, "y": 2, "z": 3, "t": 4, "w": 5, "m": 10}
UNIT = {"x": "m", "y": "m", "z": "m", "t": "s", "w": "m", "m": ""}


def utf16(s: str) -> bytes:
    return s.encode("utf-16-le")


def attrs(**kw: object) -> str:
    return "".join(f" {k}={quoteattr(str(v))}" for k, v in kw.items())


def uid(name: str) -> str:
    return f"00000000-0000-4000-8000-{zlib.crc32(name.encode()):012x}"


@dataclass
class Img:
    """An image element: `order`, its axes from the fastest (`c` where its channels are), each
    one's length in `sizes`; its samples' NumPy type and Resolution; `lengths`, each axis's
    Length; `row_pad`, the bytes after each row."""

    name: str
    order: str
    sizes: dict[str, int]
    dtype: str = "u1"
    resolution: int | None = None
    lengths: dict[str, float] = field(default_factory=dict)
    tags: list[int] | None = None
    luts: list[str] | None = None
    row_pad: int = 0
    attachments: str = ""
    memory: str = ""
    seed: int = 0
    # what to write instead of a DimensionDescription's or ChannelDescription's attributes
    dim_attrs: dict[str, dict[str, object]] = field(default_factory=dict)
    channel_attrs: dict[int, dict[str, object]] = field(default_factory=dict)

    def item(self) -> int:
        return np.dtype(self.dtype).itemsize

    def strides(self) -> dict[str, int]:
        """Each axis's BytesInc: the axes packed in `order`, each row padded by `row_pad`."""
        out, s = {}, self.item()
        for a in self.order:
            out[a] = s
            s *= self.sizes[a]
            if a == "x":
                s += self.row_pad
        return out

    def array(self) -> np.ndarray:
        """Values of the image, shaped from the slowest axis to the fastest."""
        rng = np.random.default_rng(self.seed or zlib.crc32(self.name.encode()))
        shape = tuple(self.sizes[a] for a in reversed(self.order))
        kind = np.dtype(self.dtype)
        if kind.kind == "f":
            return rng.standard_normal(shape).astype(self.dtype)
        top = (1 << (self.resolution or kind.itemsize * 8)) - 1
        return rng.integers(0, top, shape, endpoint=True, dtype=np.uint64).astype(self.dtype)

    def data(self) -> bytes:
        """The block's bytes: the array in memory order, each row followed by `row_pad` zero bytes."""
        a = self.array()
        if not self.row_pad:
            return a.tobytes()
        row = int(np.prod([self.sizes[a] for a in self.order[: self.order.index("x") + 1]]))
        return b"".join(r.tobytes() + bytes(self.row_pad) for r in a.reshape(-1, row))

    def xml(self, size: int) -> str:
        st = self.strides()
        nc = self.sizes.get("c", 1)
        dt = 1 if np.dtype(self.dtype).kind == "f" else 0
        res = self.resolution or self.item() * 8

        def channel(k: int) -> str:
            a: dict[str, object] = {"DataType": dt, "ChannelTag": (self.tags or [0] * nc)[k], "Resolution": res,
                 "NameOfMeasuredQuantity": "", "Min": "0.000000e+000", "Max": f"{(1 << res) - 1:.6e}", "Unit": "",
                 "LUTName": (self.luts or ["Gray"] * nc)[k], "IsLUTInverted": 0, "BytesInc": k * st.get("c", 0),
                 "BitInc": 0}
            a.update(self.channel_attrs.get(k, {}))
            return f"<ChannelDescription{attrs(**{k: v for k, v in a.items() if v is not None})}/>"

        def dim(x: str) -> str:
            a: dict[str, object] = {"DimID": DIM_ID[x], "NumberOfElements": self.sizes[x], "Origin": "0.000000e+000",
                 "Length": f"{self.lengths.get(x, 0.0):.6e}", "Unit": UNIT[x], "BitInc": 0, "BytesInc": st[x]}
            a.update(self.dim_attrs.get(x, {}))
            return f"<DimensionDescription{attrs(**{k: v for k, v in a.items() if v is not None})}/>"

        memory = f"<Memory{attrs(Size=size, MemoryBlockID=self.memory)}/>" if self.memory else ""
        return (f"<Element{attrs(Name=self.name, Visibility=1, CopyOption=1, UniqueID=uid(self.name))}>"
                f"<Data><Image{attrs(TextDescription='')}><ImageDescription>"
                f"<Channels>{''.join(channel(k) for k in range(nc))}</Channels>"
                f"<Dimensions>{''.join(dim(a) for a in self.order if a != 'c')}</Dimensions>"
                f"</ImageDescription>{self.attachments}<TimeStampList{attrs(NumberOfTimeStamps=0)}/></Image></Data>"
                f"{memory}<Children/></Element>")


@dataclass
class Lif:
    """A file: its images (or other elements' XML, as strings, with their blocks), the root
    element's name, the version, and blocks in another order or added (`extra`)."""

    name: str
    items: list[Img | tuple[str, str, bytes]]
    version: int = 2
    order: list[int] | None = None
    extra: list[tuple[str, bytes]] = field(default_factory=list)
    tail: bytes = b""
    root: str = "LMSDataContainerHeader"
    folder: tuple[str, int] | None = None  # (name, the first item under it)

    def blocks(self) -> list[tuple[str, bytes]]:
        out = [("MemBlock_0", b"")]
        for i, it in enumerate(self.items):
            if isinstance(it, Img):
                if not it.memory:
                    it.memory = f"MemBlock_{i + 1}"
                out.append((it.memory, it.data()))
            else:
                out.append((it[1], it[2]))
        return out

    def xml(self) -> str:
        blocks = self.blocks()
        children = []
        for i, it in enumerate(self.items):
            children.append(it.xml(len(blocks[i + 1][1])) if isinstance(it, Img) else it[0])
        if self.folder is not None:
            name, at = self.folder
            inner = "".join(children[at:])
            folder = (f"<Element{attrs(Name=name, Visibility=1, CopyOption=1, UniqueID=uid(name))}><Data/>"
                      f"<Memory{attrs(Size=0, MemoryBlockID='MemBlock_F')}/><Children>{inner}</Children></Element>")
            children = [*children[:at], folder]
        return (f"<{self.root}{attrs(Version=self.version)}>"
                f"<Element{attrs(Name=self.name, Visibility=1, CopyOption=1, UniqueID=uid(self.name))}>"
                f"<Data><Experiment{attrs(Path='C:/data/' + self.name, IsSavedFlag=1)}/></Data>"
                f"<Memory{attrs(Size=0, MemoryBlockID='MemBlock_0')}/><Children>{''.join(children)}</Children>"
                f"</Element></{self.root}>")

    def build(self, xml: str | None = None) -> bytes:
        x = utf16(self.xml() if xml is None else xml)
        out = bytearray(struct.pack("<IIBI", TEST, len(x) + 5, MAGIC, len(x) // 2) + x)
        blocks = self.blocks()
        if self.folder is not None:
            blocks.insert(1, ("MemBlock_F", b""))
        if self.order is not None:
            blocks = [blocks[i] for i in self.order]
        for id_, data in [*blocks, *self.extra]:
            out += block(id_, data, self.version)
        return bytes(out + self.tail)


def block(id_: str, data: bytes, version: int = 2) -> bytes:
    """A memory block: its fixed part (the length field holds the bytes after it up to the data),
    its ID and its data."""
    i = utf16(id_)
    if version == 1:
        return struct.pack("<IIBIBI", TEST, 1 + 4 + 1 + 4 + len(i), MAGIC, len(data), MAGIC, len(i) // 2) + i + data
    return struct.pack("<IIBQBI", TEST, 1 + 8 + 1 + 4 + len(i), MAGIC, len(data), MAGIC, len(i) // 2) + i + data


@dataclass
class Lof:
    """A Leica object file of one element: `item` an image, or another element's XML and its data
    (the writer's own `Element` XML, or a tuple of it and the data); `old`, the XML is only the
    image's `Data`, as an older writer's is; `xml`, this XML in place of the element's."""

    item: Img | tuple[str, bytes]
    old: bool = False
    tail: bytes = b""
    xml: str | None = None

    def data(self) -> bytes:
        return self.item.data() if isinstance(self.item, Img) else self.item[1]

    def text(self) -> str:
        if self.xml is not None:
            return self.xml
        if isinstance(self.item, Img):
            self.item.memory = self.item.memory or "MemBlock_541"
            element = self.item.xml(len(self.data()))
        else:
            element = self.item[0]
        if self.old:
            return element[element.index("<Data>"):element.index("</Data>") + len("</Data>")]
        return f"<LMSDataContainerHeader{attrs(Version=2)}>{element}</LMSDataContainerHeader>"

    def build(self) -> bytes:
        return lof_bytes(self.data(), self.text(), self.tail)


def lof_bytes(data: bytes, xml: str, tail: bytes = b"", *, versions: tuple[int, int] = (1, 0), magic: int = MAGIC,
              xml_test: int = TEST) -> bytes:
    """A LOF's bytes: the header block, its version fields, its memory block, its XML block."""
    head = utf16("LMS_Object_File")
    x = utf16(xml)
    return (struct.pack("<IIBI", TEST, len(head) + 5, MAGIC, len(head) // 2) + head
            + struct.pack("<BIBI", magic, versions[0], MAGIC, versions[1])
            + struct.pack("<BQ", MAGIC, len(data)) + data
            + struct.pack("<IIBI", xml_test, len(x) + 5, MAGIC, len(x) // 2) + x + tail)


@dataclass
class Lifext:
    """A LIFEXT file: for each MemoryBlockID of its parent LIF, the images it adds, each an `Img`
    with its own block, written with 64-bit sizes under a root of `Version` 1, as LAS X writes."""

    children: dict[str, list[Img]]
    version: int = 1

    def images(self) -> list[Img]:
        return [im for images in self.children.values() for im in images]

    def build(self) -> bytes:
        out = []
        blocks = []
        n = 0
        for parent, images in self.children.items():
            xml = []
            for im in images:
                n += 1
                im.memory = f"MemBlock_{n + 100}"
                data = im.data()
                blocks.append((im.memory, data))
                xml.append(im.xml(len(data)))
            out.append(f"<ChildrenOf{attrs(MemoryBlockID=parent)}>{''.join(xml)}</ChildrenOf>")
        root = "LMSDataContainerEnhancedHeader"
        x = utf16(f"<{root}{attrs(Version=self.version)}>{''.join(out)}</{root}>")
        file = bytearray(struct.pack("<IIBI", TEST, len(x) + 5, MAGIC, len(x) // 2) + x)
        for id_, data in blocks:
            file += block(id_, data, 2)
        return bytes(file)


MICRON = 1e-6


def tile_scan(positions: list[tuple[int, int, float, float]]) -> str:
    tiles = "".join(f"<Tile{attrs(FieldX=fx, FieldY=fy, PosX=f'{px:.9f}', PosY=f'{py:.9f}', PosZ='0')}/>"
                    for fx, fy, px, py in positions)
    scan = attrs(Name="TileScanInfo", Application="LAS AF", FlipX=0, FlipY=0, SwapXY=0)
    return f"<Attachment{scan}>{tiles}</Attachment>"


def accepted() -> dict[str, Lif]:
    yx = {"x": 15 * MICRON, "y": 11 * MICRON}
    return {
        "lif_2d_uint8": Lif("a.lif", [Img("plane", "xy", {"x": 16, "y": 12}, lengths=yx)]),
        "lif_zstack_channels": Lif("b.lif", [Img(
            "stack", "xycz", {"x": 10, "y": 8, "c": 3, "z": 5}, "<u2", 12,
            lengths={"x": 9e-6, "y": 7e-6, "z": -4e-6}, luts=["Red", "Green", "Blue"])]),
        "lif_time_series": Lif("c.lif", [Img(
            "movie", "xytc", {"x": 9, "y": 7, "t": 6, "c": 2}, "<u2",
            lengths={"x": 8e-6, "y": 6e-6, "t": 25.0}, luts=["Cyan", "magenta"])]),
        "lif_rgb_interleaved": Lif("d.lif", [Img(
            "rgb", "cxy", {"c": 3, "x": 11, "y": 9}, tags=[1, 2, 3], lengths=yx)]),
        "lif_rgb_padded_rows": Lif("e.lif", [Img(
            "padded", "cxy", {"c": 3, "x": 7, "y": 5}, tags=[1, 2, 3], row_pad=3)]),
        # 16384 padded rows: the farthest band of 8192 rows is a payload past 65519 bytes, so 4096
        "lif_rgb_padded_tall": Lif("e2.lif", [Img(
            "tall", "cxy", {"c": 3, "x": 2, "y": 16384}, tags=[3, 2, 1], row_pad=3)]),
        "lif_multiple_images": Lif("f.lif", [
            Img("first", "xy", {"x": 8, "y": 6}),
            Img("second", "xyz", {"x": 5, "y": 4, "z": 3}, "<u2", lengths={"z": 2e-6}),
            (('<Element Name="flim" Visibility="1" CopyOption="1"><Data><SingleMoleculeDetection IsImage="false"/>'
              '</Data><Memory Size="16" MemoryBlockID="MemBlock_3"/><Children/></Element>'), "MemBlock_3",
             bytes(range(16))),
            Img("nested", "xy", {"x": 4, "y": 4}, "<f4"),
        ], folder=("folder", 3)),
        "lif_mosaic_overlap": Lif("g.lif", [Img(
            "mosaic", "xycm", {"x": 8, "y": 6, "c": 2, "m": 4}, "<u2", lengths=yx,
            attachments=tile_scan([(0, 0, 0.0001, 0.0002), (1, 0, 0.0001065, 0.0002),
                                   (0, 1, 0.0001, 0.0002048), (1, 1, 0.0001065, 0.0002048)]))]),
        "lif_float32": Lif("h.lif", [Img("float32", "xyz", {"x": 6, "y": 5, "z": 2}, "<f4")]),
        "lif_float16": Lif("i.lif", [Img("float16", "xy", {"x": 6, "y": 5}, "<f2")]),
        "lif_uint32": Lif("j.lif", [Img("uint32", "xy", {"x": 4, "y": 3}, "<u4", 24)]),
        "lif_version1": Lif("k.lif", [Img("v1", "xyt", {"x": 6, "y": 4, "t": 3}, "<u2", 10)], version=1),
        "lif_xt_line": Lif("l.lif", [Img("xt", "xt", {"x": 12, "t": 7}, lengths={"x": 11e-6, "t": 0.6})]),
        "lif_fields": Lif("m.lif", [Img("lambda", "xyw", {"x": 5, "y": 4, "w": 3}, lengths=yx)]),
        "lif_blocks_unordered": Lif("n.lif", [Img("one", "xy", {"x": 4, "y": 3}), Img("two", "xy", {"x": 3, "y": 4}),
                                              Img("three", "xy", {"x": 2, "y": 2})], order=[0, 3, 1, 2]),
        # a block no Memory element predicts, whose ID is past the walk's guess at a header (128
        # characters): its header is read in a round of its own
        "lif_long_block_id": Lif("o2.lif", [Img("long", "xy", {"x": 4, "y": 3})],
                                 extra=[("MemBlock_" + "9" * 300, bytes(5))]),
        "lif_tail": Lif("o.lif", [Img("plane", "xy", {"x": 5, "y": 5})], tail=bytes(37)),
        "lif_no_images": Lif("p.lif", []),
    }


def accepted_lof() -> dict[str, Lof]:
    yx = {"x": 15 * MICRON, "y": 11 * MICRON}
    frame_properties = ('<Element Name="FrameProperties" Visibility="2" CopyOption="1" UniqueID="f8b4a296">'
                        '<Data><SimpleListInMemory/></Data><Memory Size="13" MemoryBlockID="MemBlock_541"/>'
                        "<Children/></Element>")
    return {
        "lif_lof": Lof(Img("series", "xycz", {"x": 6, "y": 5, "c": 2, "z": 3}, "<u2", 12, lengths=yx,
                           luts=["Red", "Green"])),
        "lif_lof_rgb": Lof(Img("rgb", "cxy", {"c": 3, "x": 5, "y": 4}, tags=[3, 2, 1], lengths=yx), tail=bytes(5)),
        # an older writer's XML, only the image's Data: an element with no Name or UniqueID
        "lif_lof_data": Lof(Img("old", "xy", {"x": 7, "y": 4}), old=True),
        # a LOF of no image, as LAS X writes beside each frame image
        "lif_lof_no_image": Lof((frame_properties, bytes(13))),
    }


def accepted_lifext() -> dict[str, Lifext]:
    return {
        # a pyramid level and a histogram of an image of the parent LIF, as LAS X writes
        "lif_lifext": Lifext({
            "MemBlock_504": [Img("Position 3_pmd_0", "xyc", {"x": 8, "y": 6, "c": 2}, luts=["Red", "Green"]),
                             Img("Position 3_histo", "xy", {"x": 16, "y": 2}, "<u2")],
        }),
        # of two images: liffile reads the first ChildrenOf alone, so its check sees one image
        "edge_lif_lifext_two_parents": Lifext({
            "MemBlock_504": [Img("Position 3_pmd_0", "xy", {"x": 4, "y": 3})],
            "MemBlock_510": [Img("Position 2_pmd_0", "xyz", {"x": 4, "y": 3, "z": 2})],
        }),
    }


def swap(lif: Lif, old: str, new: str) -> bytes:
    """The file of `lif` with the first `old` of its XML written `new` (as long, in UTF-16)."""
    return lif.build().replace(utf16(old), utf16(new), 1)


def plane(dim_attrs: dict[str, dict[str, object]] | None = None,
          channel_attrs: dict[int, dict[str, object]] | None = None) -> Lif:
    """A file of one 4 x 3 uint8 image, with these attributes in place of its own."""
    return Lif("r.lif", [Img("plane", "xy", {"x": 4, "y": 3}, dim_attrs=dim_attrs or {},
                             channel_attrs=channel_attrs or {})])


def pair() -> Lif:
    return Lif("s.lif", [Img("a", "xy", {"x": 4, "y": 3}), Img("b", "xy", {"x": 4, "y": 3})])


def rejected_lof() -> dict[str, bytes]:
    """The LOFs the profile rejects, each breaking one rule of its parts."""
    im = Img("plane", "xy", {"x": 4, "y": 3})
    good = Lof(im).build()
    data = im.data()
    head = utf16("LMS_Object_File")
    two = (f"<LMSDataContainerHeader{attrs(Version=2)}>"
           f"{Img('a', 'xy', {'x': 2, 'y': 1}, memory='A').xml(2)}{Img('b', 'xy', {'x': 2, 'y': 1}, memory='B').xml(2)}"
           "</LMSDataContainerHeader>")
    return {
        # the header block's text and nothing after it
        "short": struct.pack("<IIBI", TEST, len(head) + 5, MAGIC, len(head) // 2) + head,
        "magic": lof_bytes(data, Lof(im).text(), magic=0x2B),
        # a memory block of more bytes than the file has
        "data_past_end": good[:54] + struct.pack("<Q", 1 << 40) + good[62:],
        "xml_block": lof_bytes(data, Lof(im).text(), xml_test=0x71),
        # an XML block of more characters than the file has
        "xml_past_end": good[:62 + len(data) + 9] + struct.pack("<I", 1 << 20) + good[62 + len(data) + 13:],
        # an XML block of "<" and a lone high surrogate
        "xml_utf16": lof_bytes(data, "")[:-13] + struct.pack("<IIBI", TEST, 9, MAGIC, 2) + b"<\x00\x00\xd8",
        "two_images": lof_bytes(bytes(4), two),
        # an image that reaches past the block
        "short_block": Lof((Img("plane", "xy", {"x": 4, "y": 3}, memory="A").xml(12), bytes(11))).build(),
        "root": lof_bytes(data, "<Elsewhere/>"),
    }


def rejected() -> dict[str, bytes]:
    two = pair().build()
    first = two.rindex(utf16("MemBlock_1")) - 22  # the first image's block
    block_id = 'MemoryBlockID="MemBlock_1"'
    return {
        "short": struct.pack("<IIB", TEST, 5, MAGIC) + b"\0",
        "test_value": b"\x70\x01" + plane().build()[2:],
        "xml_past_end": (b := plane().build())[:9] + struct.pack("<I", 1 << 20) + b[13:],
        # "<" and a lone high surrogate
        "xml_utf16": struct.pack("<IIBI", TEST, 9, MAGIC, 2) + b"<\x00\x00\xd8",
        "root": Lif("v.lif", [Img("plane", "xy", {"x": 4, "y": 3})], root="LMSDataContainer").build(),
        "version_3": swap(plane(), 'Version="2"', 'Version="3"'),
        "no_memory": swap(plane(), block_id, 'MemoryBlockId="MemBlock_1"'),
        "missing_block": swap(plane(), block_id, 'MemoryBlockID="MemBlock_9"'),
        "bad_block": two[:first] + b"\x71" + two[first + 1:],
        "truncated": two[:-1],
        "duplicate_block": Lif("x.lif", [Img("plane", "xy", {"x": 4, "y": 3})],
                               extra=[("MemBlock_1", bytes(12))]).build(),
        "shared_block": swap(pair(), 'MemoryBlockID="MemBlock_2"', block_id),
        "short_block": plane(dim_attrs={"y": {"NumberOfElements": 4}}).build(),
        "data_type": plane(channel_attrs={0: {"DataType": 2}}).build(),
        "resolution_0": plane(channel_attrs={0: {"Resolution": 0}}).build(),
        "resolution_65": plane(channel_attrs={0: {"Resolution": 65}}).build(),
        "float8": plane(channel_attrs={0: {"DataType": 1}}).build(),
        "mixed_types": Lif("z.lif", [Img("two", "xyc", {"x": 4, "y": 3, "c": 2}, "<u2",
                                         channel_attrs={1: {"DataType": 1}})]).build(),
        "bit_inc": plane(dim_attrs={"x": {"BitInc": 4}}).build(),
        "channel_bit_inc": plane(channel_attrs={0: {"BitInc": 4}}).build(),
        "not_integer": plane(dim_attrs={"y": {"NumberOfElements": "three"}}).build(),
        "zero_elements": plane(dim_attrs={"y": {"NumberOfElements": 0}}).build(),
        "no_x": plane(dim_attrs={"x": {"DimID": 3}}).build(),
        "repeated_dimension": Lif("q.lif", [Img("plane", "xyz", {"x": 4, "y": 3, "z": 2},
                                                 dim_attrs={"z": {"DimID": 2}})]).build(),
        "channels_one_offset": Lif("q.lif", [Img("two", "xyc", {"x": 4, "y": 3, "c": 2},
                                                  channel_attrs={1: {"BytesInc": 0}})]).build(),
        "not_arranged": Lif("q.lif", [Img("gap", "cxy", {"c": 2, "x": 4, "y": 3},
                                          dim_attrs={"x": {"BytesInc": 3}, "y": {"BytesInc": 12}})]).build(),
        # 2^30 time points of BytesInc 0: within the block, but more planes than references
        "planes": Lif("q.lif", [Img("plane", "xyt", {"x": 4, "y": 3, "t": 2},
                                    dim_attrs={"t": {"NumberOfElements": 1 << 30, "BytesInc": 0}})]).build(),
        "rows_overlap": plane(dim_attrs={"y": {"BytesInc": 3}}).build(),
        "no_dimensions": plane().build().replace(utf16("DimensionDescription"), utf16("DimensionDescript0n")),
    }


def liffile_check(path: Path, lif: Lif | Lof | Lifext) -> str:
    """liffile's reading of every image: its array, as the writer laid it out."""
    if isinstance(lif, Lof):
        want = [lif.item] if isinstance(lif.item, Img) else []
    elif isinstance(lif, Lifext):
        want = next(iter(lif.children.values()))  # liffile reads the first ChildrenOf alone
    else:
        want = [it for it in lif.items if isinstance(it, Img)]
    with liffile.LifFile(path) as f:
        images = list(f.images)
        if len(images) != len(want):
            return f"{len(images)} images, not {len(want)}"
        for im, img in zip(images, want, strict=True):
            _ = im.sizes  # before asarray, which reads padded rows only once sizes has found them
            got = np.asarray(im.asarray())
            exp = img.array().squeeze()
            if img.tags and img.tags[:2] == [3, 2] and img.order[0] == "c":
                exp = exp[..., ::-1]  # liffile gives BGR samples as RGB
            if got.shape != exp.shape or not np.array_equal(got, exp, equal_nan=True):
                return f"{img.name}: {got.shape} != {exp.shape}"
    return "ok"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for suffix in ("lif", "lof", "lifext"):
        for old in OUT.glob(f"*.{suffix}"):
            old.unlink()
    for suffix, files in (("lif", accepted()), ("lof", accepted_lof()), ("lifext", accepted_lifext())):
        for name, lif in files.items():
            path = OUT / f"{name}.{suffix}"
            path.write_bytes(lif.build())
            print(f"{name}: liffile {liffile_check(path, lif)}")
    for name, data in rejected().items():
        (OUT / f"edge_reject_lif_{name}.lif").write_bytes(data)
    for name, data in rejected_lof().items():
        (OUT / f"edge_reject_lif_lof_{name}.lof").write_bytes(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
