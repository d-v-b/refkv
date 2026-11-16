"""How HDF4 stores text: each char8 Vdata field (attributes, dimension names, tables), its padding,
whether a value holds several strings, its encoding, and whether the bytes sit in one range."""

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from layout import File  # noqa: E402


def classify(raw: bytes):
    """(how it ends, how many NUL-separated texts, encoding)"""
    stripped = raw.rstrip(b"\x00")
    pad = "nul-padded" if len(stripped) < len(raw) else "exact"
    if stripped.endswith(b" ") and pad == "exact":
        pad = "space-padded"
    inner = stripped.count(b"\x00")
    try:
        stripped.decode("ascii")
        enc = "ascii"
    except UnicodeDecodeError:
        try:
            stripped.decode("utf-8")
            enc = "utf-8"
        except UnicodeDecodeError:
            enc = "latin-1?"
    return pad, inner, enc


for path in sys.argv[1:]:
    f = File(path)
    stats = Counter()
    biggest = []
    multi = []
    for (t, r) in sorted(f.dd):
        if t != 1962:
            continue
        h = f.vh(r)
        fields = h["fields"]
        char_fields = [x for x in fields if x[1].startswith("char8") or x[1].startswith("uchar8")]
        if not char_fields:
            continue
        linked = (1963 | 0x4000, r) in f.dd
        data = f.element(1963, r) if ((1963, r) in f.dd or linked) else b""
        for name, typ, order, off, isize in char_fields:
            for rec in range(h["nvert"]):
                raw = data[rec * h["ivsize"] + off: rec * h["ivsize"] + off + order]
                pad, inner, enc = classify(raw)
                stats[(h["class"], pad, "multi" if inner else "one", enc, "linked" if linked else "one-range")] += 1
                biggest.append((len(raw), h["name"], h["class"], linked))
                if inner and len(multi) < 4:
                    multi.append((h["name"], h["class"], raw[:120]))
    print(f"\n#### {Path(path).name}")
    for k, v in sorted(stats.items(), key=str):
        print(f"  {v:4d} {k}")
    biggest.sort(reverse=True)
    print("  largest:", biggest[:4])
    for m in multi:
        print("  several texts in one value:", m)
    # the Calibration table, raw
    for (t, r) in sorted(f.dd):
        if t == 1962 and f.vh(r)["name"] == "Calibration":
            h = f.vh(r)
            data = f.element(1963, r)
            print("  Calibration record bytes:", len(data), "interlace", h["interlace"])
            for name, typ, order, off, isize in h["fields"]:
                print(f"    {name} {typ}[{order}] @{off}: {data[off:off + min(isize, 64)]!r}")
