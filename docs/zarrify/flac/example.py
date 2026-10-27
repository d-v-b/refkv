# /// script
# requires-python = ">=3.12"
# dependencies = ["refkv[zarr,media]", "matplotlib"]
#
# [tool.uv.sources]
# refkv = { git = "https://github.com/d-v-b/refkv", subdirectory = "python" }
# ///
"""A file of the IETF's FLAC decoder testbench (CC0): five seconds of stereo music and its front
cover, zarrified without copying it: refkv reads the file's structure and presents it as Zarr, the
samples sharded, each frame a reference to the file's own bytes, read only when plotted.

Run with `uv run docs/zarrify/flac/example.py`; the figure is saved as flac.png.
"""

import io

import matplotlib.image
import matplotlib.pyplot as plt
import numpy as np
import zarr

import refkv.zarr
from refkv.zarrify import zarrify

URL = "https://raw.githubusercontent.com/ietf-wg-cellar/flac-test-files/main/subset/57%20-%20PNG%20PICTURE.flac"

root = zarr.open_group(refkv.zarr.store(zarrify(URL)), mode="r")
meta = root.attrs["refkv_zarrified"]  # attributes are JSON: check the parts read
assert isinstance(meta, dict)
flac = meta["flac"]
assert isinstance(flac, dict)
rate, bits = flac["sample_rate"], flac["bits_per_sample"]
assert isinstance(rate, int)
assert isinstance(bits, int)
# [time, channel], int16: each frame an inner chunk of a shard, decoded as FLAC, exactly
samples = np.asarray(root.get_array("samples")[:, 0]) / 2 ** (bits - 1)
cover = np.asarray(root.get_array("pictures/0")[...]).tobytes()  # the PICTURE block's PNG
t = np.arange(len(samples)) / rate
fig = plt.figure(figsize=(12, 6))
top, bottom = fig.add_subplot(2, 2, 1), fig.add_subplot(2, 2, 3)
top.plot(t, samples, linewidth=0.3)
top.set_ylabel("amplitude")
top.set_title("IETF FLAC testbench, file 57 (left channel)")
bottom.specgram(samples, Fs=rate, NFFT=2048, noverlap=1536, cmap="magma")
bottom.set_xlabel("seconds")
bottom.set_ylabel("Hz")
side = fig.add_subplot(1, 2, 2)
side.imshow(matplotlib.image.imread(io.BytesIO(cover), format="png"))
side.set_title("its front cover, from pictures/0")
side.axis("off")

fig.tight_layout()
fig.savefig("flac.png", dpi=100)
plt.show()
