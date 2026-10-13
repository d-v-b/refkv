"""Sparse local copies of the NDPI corpus slides from the caching proxy's 64 KiB blocks
(written at their offsets in a file of the slide's full size)."""
import hashlib, os, sys
from pathlib import Path
CACHE = Path("/tmp/vzip-proxy-cache")
out = Path(sys.argv[1])
names = ["CMU-1.ndpi", "Hamamatsu-1.ndpi", "Hamamatsu-2.ndpi"]
hashes = {hashlib.sha256(("https://openslide.cs.cmu.edu/download/openslide-testdata/Hamamatsu/" + n).encode()).hexdigest(): n for n in names}
blocks = {h: [] for h in hashes}
for e in os.scandir(CACHE):
    h, _, i = e.name.partition(".")
    if h in blocks and i.isdigit():
        blocks[h].append(int(i))
for h, n in hashes.items():
    size = int((CACHE / f"{h}.size").read_text())
    with open(out / n, "wb") as f:
        f.truncate(size)
        for i in sorted(blocks[h]):
            f.seek(i << 16)
            f.write((CACHE / f"{h}.{i}").read_bytes())
    print(n, size, len(blocks[h]), "blocks", (len(blocks[h]) << 16) / 1e6, "MB")
