# /// script
# requires-python = ">=3.12"
# dependencies = ["refkv[zarr,media]>=0.2.0", "matplotlib"]
# ///
"""A LibriVox recording (public domain): its first 20 seconds, zarrified without copying it: refkv
reads the file's structure and presents it as Zarr, each chunk a reference to the file's own bytes,
read only when plotted.

Run with `uv run docs/zarrify/mp3/example.py`; the figure is saved as mp3.png.
"""

import matplotlib.pyplot as plt
import numpy as np
import zarr

import refkv.zarr
from refkv.zarrify import zarrify

URL = "https://archive.org/download/public_opinion_1312_librivox/publicopinion_20_lippmann_64kb.mp3"

root = zarr.open_group(refkv.zarr.store(zarrify(URL)), mode="r")
meta = root.attrs["refkv_zarrified"]  # attributes are JSON: check the parts read
assert isinstance(meta, dict)
assert isinstance(meta["mp3"], dict)
rate = meta["mp3"]["sample_rate"]
assert isinstance(rate, int)
samples = np.asarray(root.get_array("samples")[: 20 * rate, 0])  # [time, channel], float32: the chunks decode as MP3
t = np.arange(len(samples)) / rate
fig, (top, bottom) = plt.subplots(2, 1, figsize=(10, 6), sharex=True)
top.plot(t, samples, linewidth=0.3)
top.set_ylabel("amplitude")
bottom.specgram(samples, Fs=rate, NFFT=1024, noverlap=768, cmap="magma")
bottom.set_xlabel("seconds")
bottom.set_ylabel("Hz")
top.set_title("Public Opinion, chapter 20 (LibriVox)")

fig.tight_layout()
fig.savefig("mp3.png", dpi=100)
plt.show()
