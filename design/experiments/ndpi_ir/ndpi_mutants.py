"""NDPI mutants: mutate.py's byte mutations, plus mutations aimed at NDPI's own structure
(the IFDs, high words, McuStarts and the strips' JPEG headers). Seeded."""
import random, struct, sys
from pathlib import Path
sys.path.insert(0, "conformance/virtualize")
from mutate import mutate

out = Path(sys.argv[1]); count = int(sys.argv[2]); rng = random.Random(int(sys.argv[3]))
out.mkdir(parents=True, exist_ok=True)
for p in sorted(Path("fixtures/ndpi").glob("*.ndpi")):
    data = p.read_bytes()
    v = struct.unpack("<Q", data[4:12])[0]
    # the IFD chain's extents, and the strips' first bytes
    spots = []
    o = v
    while 16 <= o < len(data) - 2 and len(spots) < 64:
        n = struct.unpack("<H", data[o:o + 2])[0]
        end = o + 2 + 16 * n + 8
        if end > len(data):
            break
        spots.append((o, end))
        for i in range(n):
            tag, typ, cnt, low = struct.unpack("<HHII", data[o + 2 + 12 * i:o + 14 + 12 * i])
            if tag in (273, 65426) and typ == 4:
                at = low if tag == 273 and cnt == 1 else low
                if at < len(data):
                    spots.append((at, min(len(data), at + 600)))
        o = struct.unpack("<Q", data[o + 2 + 12 * n:o + 10 + 12 * n])[0]
    for k in range(count):
        d = bytearray(data)
        if k % 3 == 0 or not spots:
            d = bytearray(mutate(bytes(d), rng))
        else:
            a, b = rng.choice(spots)
            for _ in range(rng.choice((1, 1, 2, 4))):
                i = rng.randrange(a, b)
                d[i] = rng.choice((0, 1, 2, 0xFF, 0x7F, 0x80, d[i] ^ (1 << rng.randrange(8)), rng.randrange(256)))
        (out / f"{p.stem}.n{k}.ndpi").write_bytes(bytes(d))
print(len(list(out.iterdir())))
