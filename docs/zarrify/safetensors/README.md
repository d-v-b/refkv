# safetensors

refkv turns a file into a Zarr hierarchy by writing a small archive of references to the
file's bytes, without copying its values ([overview](../README.md)). This page is about
safetensors files, the format of most model weights on the Hugging Face Hub
(`model.safetensors`).

## Who this is for

A model's weights are often gigabytes, and inspecting one tensor, comparing two checkpoints,
or reading a layer from a notebook usually means downloading the whole file first. A
safetensors file is a short JSON header and then every tensor's values back to back,
uncompressed, so refkv reads the header (usually one range request) and writes an archive in
which every Zarr chunk is a run of at most 1 MiB of a tensor. A reader then fetches only the
chunks it slices, from the file where it already is. GPT-2's 548 MB file becomes a 216 KB
archive in under a second.

## What you get

A Zarr group at the archive root (not an OME-Zarr image: a tensor has no axes to name), with
one array per tensor:

- **Paths:** each tensor at its name, kept whole: `model.layers.0.mlp.up_proj.weight` is one
  array at the root. A `/` in a name separates groups, as in Zarr (`encoder/conv/kernel`).
- **Data types:** every type the library defines. The integers, `F16`, `F32`, `F64`, `C64` and
  `BOOL` are Zarr's own; `BF16` is `bfloat16`, and `F8_E5M2`, `F8_E4M3FNUZ`, `F8_E5M2FNUZ` and
  `F8_E8M0` the zarr-extensions types of those names. `F8_E4M3`, PyTorch's `float8_e4m3fn` (no
  infinities, up to 448), is `refkv.float8_e4m3fn`, since zarr-extensions' `float8_e4m3` is a
  different type. The packed types `F4`, `F6_E2M3` and `F6_E3M2` are `float4_e2m1fn`,
  `float6_e2m3fn` and `float6_e3m2fn`, decoded by the `packbits` codec.
- **Chunks:** the values as the file stores them, cut into equal runs of at most 1 MiB along
  the outermost axis that allows it. A scalar is one chunk; an empty tensor has none.
- **Dimension names:** `dim_0`, `dim_1`, …, as xarray names unnamed axes.
- **Source metadata:** under `refkv_zarrified.safetensors`, the header's length, its
  `__metadata__` (often `{"format": "pt"}`), and every tensor as the header writes it (name,
  `dtype`, shape, `data_offsets`), with its path. The whole file is described on
  `refkv_source` by the Rust core's IR mirror, from which it is rebuilt byte for byte.

**Not presented.** A tensor whose name is not a Zarr path (an empty part, `.`, `..`, a part
starting with `__`, or `zarr.json`; `name`), whose path starts with `refkv_source`
(`reserved`), or whose path is another tensor's group (`a` beside `a/b`; `path`) is left out,
and listed with its reason. Its bytes stay in the mirror.

**A model's shards as one group.** A store of safetensors files (a local directory, or an S3
bucket or prefix) is zarrified as one group: each `.safetensors` file is a group at its key,
holding its tensors as above, and the root's source metadata lists the shards, checks each
`*.safetensors.index.json` against the shards' headers, and keeps each `config.json` beside them as
written. Every other file (the tokenizer, a README, code) is referenced whole under
`refkv_source/objects/`. A shard whose header refkv cannot read is listed with the reason, and the
rest of the store is still zarrified. Reading a Hugging Face Hub repository this way needs the Hub
listing, which is not in refkv yet; a downloaded copy of one works as a local directory.

**Not supported.** A model's tensors are not also gathered by name across its shards: the index,
which the store keeps, says which shard holds each.
[spec/zarrify/safetensors.md §7](../../../spec/zarrify/safetensors.md#7-what-the-layout-does-not-express)
lists what the layout does not express.

## Reading the data types

zarr-python has no `bfloat16`, `float8`, `float6` or `float4` types and no `packbits` codec.
refkv's `zarr` extra adds them, through [ml_dtypes](https://pypi.org/project/ml-dtypes/): zarr
finds them by name once refkv is installed, and an array reads as a NumPy array of the
`ml_dtypes` type (`ml_dtypes.bfloat16`, `ml_dtypes.float8_e4m3fn`, ...). TensorStore reads
`bfloat16`, the zarr-extensions `float8` types and `float4_e2m1fn` (but not with `packbits`); it
names `refkv.float8_e4m3fn`'s type `float8_e4m3fn`. The demo's viewer shows `float32` and
integer tensors of two or more axes, not these.

## Try it

[example.py](example.py) zarrifies GPT-2's weights on the Hugging Face Hub and plots its
position embeddings, read through Zarr. Its dependencies are in its header (PEP 723), so it runs
on its own (uv builds refkv from GitHub, which needs a Rust toolchain, until a release on PyPI has
the safetensors profile; refkv 0.2.0 does not):

```bash
uv run docs/zarrify/safetensors/example.py
```

An 8-bit checkpoint, from [corpus_safetensors.txt](../../../conformance/zarrify/corpus_safetensors.txt):

```bash
uv run refkv zarrify https://huggingface.co/RedHatAI/Qwen2.5-0.5B-Instruct-FP8-dynamic/resolve/67c5c3a4f39629d3653b715527f363d86929841e/model.safetensors qwen-fp8.vzip
```

```
{"format": "safetensors", "tensors": [{"path": "lm_head.weight", "dtype": "BF16", "dataType": "bfloat16", "shape": [151936, 896], "chunkShape": [414, 896], "chunks": 367}, ...], "unpresented": {}, ...}
```

Read it:

```python
import zarr
import refkv.zarr

g = zarr.open_group(refkv.zarr.open("qwen-fp8.vzip"), mode="r")
w = g["model.layers.0.mlp.down_proj.weight"]
print(w.dtype, w.shape, w.chunks)
print(w[0, :6])
```

```
float8_e4m3fn (896, 4864) (128, 4864)
[-22 22 80 18 -22 -30]
```

The archive is 585 KB, for a 903 MB file.

A directory of shards, such as a downloaded model, is zarrified as a store, served at the URL
given (the URL each chunk's references name):

```bash
uv run refkv zarrify ./DeepSeek-V4.1-Flash deepseek.vzip --url https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash-1017/resolve/1bae0e1088993b9b82de08f59de5be5a34e42170/
```

Its 48 shards (510 GB, 96,085 tensors, 93 reads of their headers) become 596,317 chunks; the
archive is 218 MB, most of it the ZIP entries of that many chunks.

## How it's verified

`python/tests/ir/test_safetensors.py` zarrifies every synthetic file in
[fixtures/safetensors/](../../../fixtures/safetensors/) (written by the safetensors library, and
by hand where it cannot write a case,
[fixtures/generators/safetensors/write_fixtures.py](../../../fixtures/generators/safetensors/write_fixtures.py))
and compares every tensor with the library's reading (`safetensors.deserialize`), bit for bit,
in its shape and data type; it checks the names it leaves out and their reasons, the groups, the
chunk shapes and each data type's codecs. `js/test/safetensors/verify.py` does the same for the
browser code. Every archive's mirror rebuilds its file, and the inputs the profile rejects are
listed with their messages in
[conformance/zarrify/rejects/safetensors.json](../../../conformance/zarrify/rejects/safetensors.json).

- Profile: [spec/zarrify/safetensors.md](../../../spec/zarrify/safetensors.md#part-2-the-profile)
- Rust core: [rust/refkv-ir/src/safetensors.rs](../../../rust/refkv-ir/src/safetensors.rs)
- Data types and codec: [python/src/refkv/zarr/dtypes.py](../../../python/src/refkv/zarr/dtypes.py),
  [python/src/refkv/zarr/codecs/packbits.py](../../../python/src/refkv/zarr/codecs/packbits.py)
