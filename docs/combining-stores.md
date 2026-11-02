# Combining stores

`refkv.zarrify` builds one store from one input. `refkv.compose` builds a store
from several: the planes of a z-stack as one array, tiles as a mosaic, a group of
arrays from different files, or a fix to one chunk of an existing store. No data
is copied: the result's references point at the same bytes as its parts'.

## Bounds

Each store answers for a set of keys, its **bounds** (`store.bounds`).

- **Inside its bounds**, a key the store does not hold is *absent*. For a Zarr
  chunk, that means the array's fill value.
- **Outside its bounds**, the store has nothing to say, and a combination asks
  another store.

A bound is one of four kinds, from `refkv.bounds` (also importable from
`refkv.compose`):

| Bound | Keys |
|---|---|
| `Key("a/zarr.json")` | that one key |
| `KeyRange("a", "b")` | every key `k` with `"a" <= k < "b"` (`stop=None`: no upper limit) |
| `Prefix("a/b")` | every key under `a/b/`, not `a/bc`; `Prefix("")` is every key |
| `ChunkBox("0", (2, 0), (5, 4))` | the chunks of array `0` with coordinates from `(2, 0)` up to, not including, `(5, 4)` |

A box is needed because chunk keys do not sort in index order: `0/c/10` sorts
before `0/c/2`, so no key range is "chunks 2 to 10". A box takes the array's
chunk key encoding, `encoding="default"` (Zarr v3: `c/0/1`) or `"v2"` (`0.1`),
and its `separator`, which defaults to the encoding's own (`/` or `.`). A
combination refuses a box whose rank or encoding doesn't match the array's, since
it would take in none of the array's chunks.

A store built without bounds derives them from its Zarr nodes:
- an array answers for its whole subtree;
- a group answers for its own metadata keys;
- every other key answers for itself.

Pass `bounds=` to `ReferenceStore`, or call `store.with_bounds(...)`, to state
others. Bounds change nothing a store reads on its own, and `save` does not write
them: a reopened archive derives its bounds again.

## Operators

All of them return a new store.

- **`overlay(*stores)`.** Each key comes from the last store whose bounds contain
  it, with its value or its absence. As in TensorStore's `stack` and `kvstack`,
  later stores win. `overlay(a, overlay(b, c))` reads the same as
  `overlay(overlay(a, b), c)`.
- **`union(*stores)`.** `overlay`, except that stores whose bounds share a key
  must answer it the same: equal values, or both absent. Otherwise it raises,
  naming the key. On stores with disjoint bounds the order doesn't matter.
- **`shift_chunks(store, array, offset, new_axes=0)`.** Takes the chunks of one
  array alone, with no metadata. It adds `new_axes` new leading axes and moves
  every chunk by `offset`. Only chunks inside the store's bounds move, and the
  bounds move with them: a bound over the whole array becomes the box the moved
  chunks fill.
- **`mount(store, path)`.** Puts the store under `path/`, with its bounds moved
  along.
- **`map_keys(store, f)`.** Renames each key to `f(key)`, or drops it where `f`
  returns None. Derived bounds are derived again, and stated `Key` bounds are
  renamed. A function on keys can't move a range, prefix or box, so a store that
  states one needs `bounds=`. A combination's result states its operands' bounds,
  so to rename one, pass `bounds=`, or call `.with_bounds(None)` to derive them
  again from its keys.
- **`normalize(store)`.** Makes the store a valid hierarchy:
  - an array owns everything below it;
  - any other key not under an array is dropped: a chunk left under a group by an
    overlay, or a stray file at the root;
  - a group is added wherever a node's parent is missing, in that node's Zarr
    version.

A combination merges its stores' source tables. It resolves each store's relative
URLs against that store's own base and keeps one entry for equal sources.

A combination never lets a store read what its own options refuse:
- **Policy and registry.** It takes the stores' reader policy and registry, and
  raises if they differ, unless you pass `policy=` or `registry=`.
- **Base URL.** It decides whether `file:` sources may be read: a store opened
  from a URL may not read local files. When some stores are local and some
  remote, the result takes a base under which no store reads more than before.
  It raises when none exists, unless you pass `url=`. An explicit `url=` is your
  choice, and a local one lets every store read local files. Any operator given
  `url=` first resolves relative source URLs against the store's old base, so
  every reference still reads the same object.

Combine many stores in one call, not a fold of pairwise calls: each call copies
its stores.

## A stack

The planes of a z-stack, each a 2-D TIFF zarrified on its own, become one 3-D
array. A header holds the new metadata and answers only for its own two keys.
Each plane's chunks move to their place on the new `z` axis:

```python
import json

from refkv import ReferenceStore
from refkv.compose import Key, shift_chunks, union
from refkv.zarrify import zarrify

stores = [zarrify(p) for p in paths]  # every plane with the same shape, chunks and codecs
meta = json.loads(stores[0].mapping["0/zarr.json"])
meta["shape"] = [len(stores), *meta["shape"]]
meta["chunk_grid"]["configuration"]["chunk_shape"] = [1, *meta["chunk_grid"]["configuration"]["chunk_shape"]]
meta["dimension_names"] = ["z", *meta["dimension_names"]]
header = ReferenceStore(
    {"zarr.json": b'{"zarr_format":3,"node_type":"group","attributes":{}}',
     "0/zarr.json": json.dumps(meta).encode()},
    bounds=[Key("zarr.json"), Key("0/zarr.json")],
)
stack = union(header, *(shift_chunks(s, "0", (i, 0, 0), new_axes=1) for i, s in enumerate(stores)))
stack.save("stack.vzip")
```

Nothing checks for you that the planes have the same layout. That is up to you,
as is the header's metadata.

## A patch, and a deletion

A store whose bounds cover one chunk replaces that chunk and leaves the rest:

```python
from refkv.compose import ChunkBox, overlay

fixed = ReferenceStore({"0/c/3/0/0": new_bytes}, bounds=[ChunkBox("0", (3, 0, 0), (4, 1, 1))])
patched = overlay(stack, fixed)
```

A store that holds nothing but bounds a region turns the region into fill value:

```python
blank = ReferenceStore({}, bounds=[ChunkBox("0", (5, 0, 0), (6, 4, 4))])
without_plane_5 = overlay(stack, blank)
```

## Limits

- **Whole chunks only.** A combination moves whole chunks and never re-encodes
  them. The stores of a stack need the same codecs and chunk shape. A join along
  an existing axis must fall on whole chunks, because Zarr's regular chunk grid
  allows no partial chunk in the middle.
- **Zarr documents as bytes.** A combination drops consolidated metadata from each
  `zarr.json` it keeps, so it must be able to read one. It refuses to keep a
  `zarr.json` held as a range, even a literal one, though a later store may
  replace it; `normalize` refuses one too.
- **Box lookups.** Chunk boxes are found through a tree of bounding boxes, so a
  lookup checks only the boxes near a chunk. Many boxes crossing at one point
  without holding it (a construction rather than a layout) defeat that, and each
  lookup then checks every box.
- **Only in-memory stores.** The operators take `ReferenceStore`s.
  `refkv.open(path)` returns an archive reader, which they do not take yet.

[design/proposals/store-operators.md](../design/proposals/store-operators.md)
explains the design and its reasons.
