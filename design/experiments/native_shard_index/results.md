# Results: the shard index stored three ways

Run on 2026-10-27, refkv at `63288330` (PR 141's head, which adds `refkv.phkv`), zarr-python 3.4.2 and numpy 2.5.4 (the script's lock file), against
`https://s3.janelia.org/shroff-lab/s0_hdf5/` (30 shards, 24 + 6 along the last two axes of a
6368 x 1600 x 12000 uint8 array). The command, and its output, which `--verify` ends:

```
uv run --script --locked design/experiments/native_shard_index/compare.py OUT_DIR --verify
```

```
30 shards, 29 full, 1 partial (chunks allocated: [256, 512]); entries identical to Zarr's in 29

binding                   index          bytes  per shard   vs inline  vs internal
vzip                      inline       496,660     16,555          +0           +0
vzip                      internal     111,106      3,704    -385,554           +0
vzip                      native         5,616        187    -491,044     -105,490
vzip (mirror_refs=False)  inline       250,332      8,344          +0           +0
vzip (mirror_refs=False)  internal     110,628      3,688    -139,704           +0
vzip (mirror_refs=False)  native         4,810        160    -245,522     -105,818
phkv                      inline       248,526      8,284          +0           +0
phkv                      internal     106,421      3,547    -142,105           +0
phkv                      native         2,777         93    -245,749     -103,644

full shard c/0/0/0       identical across 9 archives: True  ['288ba504f3']
full shard c/0/0/5       identical across 9 archives: True  ['09b1515d3d']
partial shard c/0/0/23   identical across 9 archives: True  ['29dc707994']
shard c/0/1/0            identical across 9 archives: True  ['417ff3bd2a']
```

The sizes are the archives' bytes. `vs inline` and `vs internal` are differences from the same binding's
row of that index. The decode check reads four regions through `refkv.zarr.open` and zarr-python (the
Blosc-compressed inner chunks of three full shards and of the partial shard `c/0/0/23`), and compares their
bytes across the nine archives.
