# /// script
# requires-python = ">=3.12"
# dependencies = ["refkv[zarr]>=0.2.0", "pynwb>=4.2", "hdmf-zarr>=0.14", "matplotlib"]
# ///
"""An NWB file of the DANDI Archive (dandiset 000054, a two-photon session of 161 ROIs, 81 MB),
zarrified without copying it: refkv reads the file's HDF5 structure and presents it as Zarr in
hdmf-zarr's form, each chunk a reference to the file's own bytes, and pynwb's NWBZarrIO reads it
as an NWB file, fetching the traces only when they are plotted.

Run with `uv run docs/zarrify/hdf5/example.py`; the figure is saved as hdf5.png.
"""

import warnings

import matplotlib.pyplot as plt
import numpy as np
from hdmf_zarr.nwb import NWBZarrIO

import refkv.zarr
from refkv.zarrify import zarrify

URL = "https://dandiarchive.s3.amazonaws.com/blobs/5ca/ea3/5caea343-caf7-4914-aa51-6379f9524c3b"

store = zarrify(URL)
print({k: store.info[k] for k in ("format", "objects", "arrays", "unpresented", "nwb")})
warnings.simplefilter("ignore")  # pynwb's notes on the session's cached namespaces
with NWBZarrIO(refkv.zarr.store(store), "r") as io:
    nwb = io.read()
    series = nwb.processing["ophys"]["dF"]["dF"]
    print(nwb.session_description, "|", series.data.shape, series.rate, "Hz")
    rows = np.asarray(series.data[:3000, :8])  # 3000 frames of 8 ROIs: two of the trace's 1 MiB chunks
    t = np.arange(rows.shape[0]) / series.rate
fig, ax = plt.subplots(figsize=(8, 4))
ax.plot(t, rows + np.arange(rows.shape[1]) * np.nanmax(rows), linewidth=0.5)
ax.set_xlabel("seconds")
ax.set_yticks([])
ax.set_title("dandiset 000054, sub-F3, testing day 8: dF of 8 ROIs")
fig.tight_layout()
fig.savefig("hdf5.png", dpi=100)
plt.show()
