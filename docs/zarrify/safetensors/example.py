# /// script
# requires-python = ">=3.12"
# dependencies = ["refkv[zarr]", "matplotlib"]
#
# [tool.uv.sources]
# refkv = { git = "https://github.com/d-v-b/refkv", subdirectory = "python" }
# ///
"""GPT-2's weights, a 548 MB safetensors file on the Hugging Face Hub, zarrified over HTTP
without downloading it: refkv reads the file's header and presents each tensor as a Zarr array,
each chunk a reference to the file's own bytes. Plotting the position embeddings reads 3 MB of
the file.

Run with `uv run docs/zarrify/safetensors/example.py`; the archive is saved as gpt2.vzip, and
the figure as safetensors.png.
"""

import matplotlib.pyplot as plt
import numpy as np
import zarr

import refkv.zarr
from refkv.zarrify import zarrify

URL = ("https://huggingface.co/openai-community/gpt2/resolve/607a30d783dfa663caf39e06633721c8d4cfcd7e/"
       "model.safetensors")

store = zarrify(URL)
store.save("gpt2.vzip")
root = zarr.open_group(refkv.zarr.store(store), mode="r")
meta = root.attrs["refkv_zarrified"]  # attributes are JSON: check the parts read
assert isinstance(meta, dict)
own = meta["safetensors"]
assert isinstance(own, dict)
assert isinstance(own["tensors"], list)
print(len(own["tensors"]), "tensors;", own["metadata"])
wpe = root.get_array("wpe.weight")
print(wpe.shape, wpe.dtype, wpe.chunks)

fig, ax = plt.subplots(figsize=(10, 5))
ax.imshow(np.asarray(wpe[:, :256]).T, aspect="auto", cmap="RdBu", vmin=-0.3, vmax=0.3)
ax.set_xlabel("position")
ax.set_ylabel("embedding dimension (first 256)")
ax.set_title("GPT-2 position embeddings (wpe.weight)")
fig.tight_layout()
fig.savefig("safetensors.png", dpi=100)
plt.show()
