"""Walk an HDF4 file's data descriptors and report its byte layout: tags, special elements
(compressed, chunked, linked blocks, external), number types, Vdata headers. Reads only the
bytes the descriptors name. Big-endian throughout (HDF4's file format)."""

import struct
import sys
import traceback
from collections import Counter
from pathlib import Path

TAGS = {1: "NULL", 20: "LINKED", 30: "VERSION", 40: "COMPRESSED", 60: "CHUNKED", 61: "CHUNK",
        100: "FID", 101: "FD", 104: "DIL", 105: "DIA", 106: "NT", 107: "MT", 300: "ID", 301: "LUT",
        302: "RI", 306: "RIG", 701: "SDD", 702: "SD", 703: "SDS", 704: "SDL", 705: "SDU",
        706: "SDF", 707: "SDM", 708: "SDC", 709: "SDT", 720: "NDG", 731: "CAL", 732: "FV",
        1962: "VH", 1963: "VS", 1965: "VG"}
SPECIAL = {1: "LINKED", 2: "EXT", 3: "COMP", 4: "VLINKED", 5: "CHUNKED", 6: "BUFFERED",
           7: "COMPRAS"}
COMP = {0: "NONE", 1: "RLE", 2: "NBIT", 3: "SKPHUFF", 4: "DEFLATE", 5: "SZIP", 7: "JPEG"}
NT = {3: "uchar8", 4: "char8", 5: "float32", 6: "float64", 20: "int8", 21: "uint8", 22: "int16",
      23: "uint16", 24: "int32", 25: "uint32", 26: "int64", 27: "uint64"}


def nt_name(t):
    base = t & 0xFFF
    flags = []
    if t & 0x1000:
        flags.append("native")
    if t & 0x4000:
        flags.append("little")
    if t & 0x2000:
        flags.append("custom")
    return NT.get(base, f"nt{base}") + ("/" + "+".join(flags) if flags else "")


class File:
    def __init__(self, path):
        self.b = Path(path).read_bytes()
        assert self.b[:4] == b"\x0e\x03\x13\x01", "not HDF4"
        self.dd = {}
        self.blocks = 0
        off = 4
        while True:
            self.blocks += 1
            ndd, nxt = struct.unpack_from(">hI", self.b, off)
            for i in range(ndd):
                tag, ref, o, n = struct.unpack_from(">HHII", self.b, off + 6 + 12 * i)
                if tag == 1:
                    continue
                self.dd[(tag, ref)] = (o, n)
            if nxt == 0:
                break
            off = nxt

    def u(self, fmt, o):
        return struct.unpack_from(">" + fmt, self.b, o)

    def special(self, tag, ref):
        o, n = self.dd[(tag, ref)]
        (code,) = self.u("H", o)
        kind = SPECIAL.get(code, code)
        if kind == "COMP":
            ver, ulen, dref, model, ctype = self.u("HIHHH", o + 2)
            co = self.dd.get((40, dref))
            extra = ""
            if ctype == 4:
                (lvl,) = self.u("H", o + 14)
                extra = f" level {lvl}"
            elif ctype == 5:
                extra = f" szip params {self.u('IIIII', o + 14)}"
            elif ctype == 2:
                extra = f" nbit {self.u('IHHII', o + 14)}"
            elif ctype == 3:
                extra = f" skphuff {self.u('II', o + 14)}"
            sub = None
            if co is None:
                # the compressed data may itself be linked blocks
                for t in (40 | 0x4000,):
                    if (t, dref) in self.dd:
                        sub = self.special(t, dref)
            return {"kind": "COMP", "ulen": ulen, "comp": COMP.get(ctype, ctype) + extra,
                    "data": co, "sub": sub, "model": model}
        if kind == "LINKED":
            blocks = self.linked_blocks(o)
            length, blk_len, num_blk = self.u("III", o + 2)
            return {"kind": "LINKED", "length": length, "blk_len": blk_len, "per_link": num_blk,
                    "blocks": len(blocks), "sizes": dict(Counter(n for _, n in blocks)),
                    "sum": sum(n for _, n in blocks)}
        if kind == "EXT":
            length, eoff, flen = self.u("III", o + 2)
            name = self.b[o + 14:o + 14 + flen].decode(errors="replace")
            return {"kind": "EXT", "length": length, "offset": eoff, "file": name}
        if kind == "CHUNKED":
            hlen, ver, flag, tot, csize, ntsize, ttag, tref, stag, sref, ndims = (
                self.u("IBIIIIHHHHI", o + 2))
            dims = [self.u("III", o + 2 + 33 + 12 * i) for i in range(ndims)]
            fo = o + 2 + 33 + 12 * ndims
            (flen,) = self.u("I", fo)
            # chunk table: a Vdata of rows (origin[ndims] int32, chk_tag u16, chk_ref u16)
            data = self.element(1963, tref)
            nvert = self.vh(tref)["nvert"]
            rsz = 4 * ndims + 4
            rows = [struct.unpack_from(">" + "I" * ndims + "HH", data, k * rsz) for k in range(nvert)]
            chunk_kinds = Counter()
            comps = Counter()
            for r in rows:
                ct, cr = r[-2], r[-1]
                if (ct, cr) in self.dd:
                    chunk_kinds["plain"] += 1
                elif (ct | 0x4000, cr) in self.dd:
                    s = self.special(ct | 0x4000, cr)
                    chunk_kinds[s["kind"]] += 1
                    comps[s.get("comp")] += 1
                else:
                    chunk_kinds["missing"] += 1
            return {"kind": "CHUNKED", "flag": flag, "elem_tot": tot, "chunk_size": csize,
                    "nt_size": ntsize, "dims(flag,len,chunk)": dims, "fill": self.b[fo + 4:fo + 4 + flen],
                    "table": (ttag, tref), "chunks": len(rows), "chunk_kinds": dict(chunk_kinds),
                    "comps": dict(comps)}
        return {"kind": kind}

    def linked_blocks(self, o):
        """The (offset, length) of each block of a linked-block element whose header is at o:
        special code, length, block length, blocks per link table, the first link table's ref."""
        length, blk_len, num_blk = self.u("III", o + 2)
        (link_ref,) = self.u("H", o + 14)
        blocks = []
        while link_ref:
            lo, ln = self.dd[(20, link_ref)]
            (link_ref,) = self.u("H", lo)
            refs = self.u("H" * num_blk, lo + 2)
            blocks += [self.dd[(20, r)] for r in refs if r]
        return blocks

    def element(self, tag, ref):
        """An element's bytes, whether plain or linked blocks."""
        if (tag, ref) in self.dd:
            o, n = self.dd[(tag, ref)]
            return self.b[o:o + n]
        o, n = self.dd[(tag | 0x4000, ref)]
        (code,) = self.u("H", o)
        assert code == 1, f"special {code}"
        length = self.u("I", o + 2)[0]
        return b"".join(self.b[bo:bo + bn] for bo, bn in self.linked_blocks(o))[:length]

    def vh(self, ref):
        o, n = self.dd[(1962, ref)]
        interlace, nvert, ivsize, nfields = self.u("HIHH", o)
        p = o + 10
        types = self.u("H" * nfields, p); p += 2 * nfields
        isize = self.u("H" * nfields, p); p += 2 * nfields
        offs = self.u("H" * nfields, p); p += 2 * nfields
        order = self.u("H" * nfields, p); p += 2 * nfields
        names = []
        for _ in range(nfields):
            (ln,) = self.u("H", p)
            names.append(self.b[p + 2:p + 2 + ln].decode(errors="replace"))
            p += 2 + ln
        (ln,) = self.u("H", p)
        name = self.b[p + 2:p + 2 + ln].decode(errors="replace")
        p += 2 + ln
        (ln,) = self.u("H", p)
        cls = self.b[p + 2:p + 2 + ln].decode(errors="replace")
        p += 2 + ln
        extag, exref, version = self.u("HHH", p)
        return {"interlace": interlace, "nvert": nvert, "ivsize": ivsize,
                "fields": list(zip(names, [nt_name(t) for t in types], order, offs, isize)),
                "name": name, "class": cls, "version": version}


def report(path):
    f = File(path)
    print(f"\n######## {Path(path).name}: {len(f.dd)} descriptors in {f.blocks} DD blocks")
    tags = Counter()
    for (t, r) in f.dd:
        base = t & ~0x4000
        tags[(TAGS.get(base, base), "special" if t & 0x4000 else "")] += 1
    print("  tags:", ", ".join(f"{k[0]}{'*' if k[1] else ''}={v}" for k, v in sorted(tags.items(), key=str)))
    # number types used by NT elements
    nts = Counter()
    for (t, r), (o, n) in f.dd.items():
        if t == 106:
            ver, typ, width, cls = f.b[o:o + 4]
            nts[(nt_name(typ), width, cls)] += 1
    print("  NT elements:", dict(nts))
    # special elements
    sp = Counter()
    for (t, r) in sorted(f.dd):
        if t & 0x4000:
            s = f.special(t, r)
            base = TAGS.get(t & ~0x4000, t & ~0x4000)
            key = (base, s["kind"], s.get("comp"))
            sp[key] += 1
            if sp[key] <= 2:
                print(f"  special {base}#{r}: {s}")
    print("  special elements:", dict(sp))
    # SD data elements: contiguous plain SDs
    plain_sd = [(r, f.dd[(702, r)]) for (t, r) in f.dd if t == 702]
    print(f"  plain SD elements: {len(plain_sd)}")
    # vdata interlace and field types
    inter = Counter()
    ftypes = Counter()
    linked_vs = 0
    for (t, r) in f.dd:
        if t == 1962:
            h = f.vh(r)
            inter[(h["interlace"], h["class"])] += 1
            for name, typ, order, off, isz in h["fields"]:
                ftypes[typ] += 1
            if (1963 | 0x4000, r) in f.dd:
                linked_vs += 1
    print("  vdata (interlace, class):", dict(inter))
    print("  vdata field types:", dict(ftypes))
    print("  vdata storage that is special (linked):", linked_vs)
    for (t, r) in f.dd:
        if t == 1962:
            h = f.vh(r)
            if h["class"] not in ("Attr0.0", "DimVal0.1", "DimVal0.0", "_HDF_CHK_TBL_0", "Var0.0"):
                print(f"  VH#{r} {h}")


if __name__ == "__main__":
  for p in sys.argv[1:]:
    try:
        report(p)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        print(f"!! {p}: {type(e).__name__}: {e}")
