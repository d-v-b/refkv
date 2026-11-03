# Proposal: store operators and key bounds

refkv builds a `ReferenceStore` from one input: a file, a store, or a
VirtualiZarr dataset. Combining stores is left to the caller. The TIFF-stack
example ([docs/tiff-stack.md](../../docs/tiff-stack.md)) does it by hand, and
in doing so it uses four operations that are general:

1. **Rename keys.** Each file's chunk `0/c/y/x` becomes `0/c/i/y/x`.
2. **Merge source tables.** References index their store's source table, so
   joining stores joins the tables and moves each reference's index.
3. **Union.** The renamed stores have disjoint keys, and are joined.
4. **Overlay.** The stack's new `zarr.json` documents replace the files'.

This note proposes those as operators, and a property of a store that makes
the union and the overlay well-defined: its **bounds**, the keys it answers
for. It is a design note, not normative. A prototype of every operator here
was run against the TIFF stack and the cases in
[Checked by a prototype](#checked-by-a-prototype). TensorStore has close
counterparts at two levels; [Prior art: TensorStore](#prior-art-tensorstore)
compares them.

## What a union does beyond a dict merge

- **Sources.** Each operand's relative source URLs are resolved against that
  operand's base URL (`store.url`) first, since the operands' bases differ.
  Equal sources (URL and pins) become one, and every `Range` is moved to its
  source's new index. Index 0 should go to the most-used source, as
  `from_virtualizarr` does, since it costs nothing on the wire.
- **Internal sources.** A `Source(key=k)` names an entry of the same store.
  A rename must rename `k` too, and a union must refuse one whose target is
  answered by another operand.
- **Store properties.** `read_at_open` and `compressed` are the union of the
  operands', filtered to the keys that survive. `page_size`, `url`,
  `registry` and `policy` come from the call, or else from the first operand.
  `info` is empty unless the call gives one, because no operand's summary
  describes the result.

## A union of keys is wrong for Zarr

A union at the level of keys, in which the later operand `b` wins every key
both hold, can build a hierarchy that decodes wrongly. If `b` holds
`0/zarr.json` and `a` holds the chunks `0/c/...`, `a`'s chunks are decoded
with `b`'s codecs, data type and shape. Here and below, `x ⊕ y` is such a
union, with `y` winning.

**First attempt: shadow by node.** The unit is the Zarr node: an array's
metadata and its chunks win or lose together, and a group's metadata wins
while its children merge. The prototype found two faults.

- **It is not associative when node kinds differ.** Let `A` have a group `p`
  with an array `p/q`, `B` an array `p`, and `C` a group `p` with an array
  `p/r`. Then `C ⊕ (B ⊕ A)` keeps `p/r`: `A`'s group wins `p`, and `B`'s array
  no longer shadows anything. But `(C ⊕ B) ⊕ A` loses `p/r`, because `B`'s
  array shadowed `C`'s subtree before `A` was applied. Coverage was inferred
  from metadata, and the metadata that implied it lost.
- **A header cannot add metadata to someone else's chunks.** A store holding
  only the stack's `0/zarr.json` describes an array, so it shadows every
  chunk under `0/`, including the planes' chunks it was meant to describe.

Both faults have the same cause. Whether a store speaks for a key is inferred
from what it holds. It should instead be stated.

## Bounds

A store's **bounds** are the keys it answers for.

- **Inside its bounds**, a key the store does not hold is *absent*. For a
  chunk, that means the fill value.
- **Outside its bounds**, the store has nothing to say about the key, and a
  union asks the next operand.

The two cases look the same today: `get` returns None for both.

A bound is one of:

- `key(k)`: one key;
- `range(lo, hi)`: every key `k` with `lo ≤ k < hi`, compared as UTF-8
  bytes, as TensorStore's `kvstack` layers take `inclusive_min` and
  `exclusive_max`;
- `prefix(p)`: every key under `p/`. It is `range("p/", "p0")`, since `0`
  follows `/`, so `0/c/1/` does not take in `0/c/10/`;
- `box(path, lo, hi)`: the chunks of the array at `path` whose coordinates
  `c` satisfy `lo ≤ c < hi`.

A box is needed because chunk keys do not sort in index order. Under the
default chunk key encoding, `0/c/10` sorts before `0/c/2`, so no range of
keys is "chunks 2 to 10". Keys, ranges and prefixes need no knowledge of
Zarr; a box reads the array's `chunk_key_encoding` to map a key to
coordinates.

**The default bounds** are derived from what the store holds:
- a `prefix` over each array's subtree;
- a `key` for each group's metadata;
- a `key` for every other key.

That is the node shadowing above. A builder states bounds when the default
is wrong:
- a header states only its metadata keys;
- a plane of a stack states its box;
- a store that deletes something states the region it empties.

## Operators

All of them return a new store; stores stay immutable.

- **`map_keys(store, f)`.** Renames each key to `f(key)`, or drops it where
  `f` returns None. Internal sources and bounds follow. Two keys mapping to
  one raises.
- **`mount(store, path)`.** `map_keys` under `path/`, plus a group at each
  ancestor that lacks a node.
- **`map_chunks(store, array, f)`.** Moves the chunks of `array` to
  coordinates `f(c)`. It reads the array's chunk key encoding both ways and
  leaves the metadata alone. Box bounds move with it.
- **`overlay(*stores)`.** For each key, the last operand whose bounds contain
  it answers, with a value or with absence. The result's bounds are the union
  of the operands'. This is a priority lookup, like `ChainMap` with stated
  membership but the later store winning, as a later layer wins in
  TensorStore's `stack` and `kvstack`. It is associative, idempotent and has
  the empty store as its identity, but is not commutative. The `A`, `B`, `C`
  case above gives the same keys however it is nested.
- **`union(*stores)`.** `overlay`, but where two operands' bounds share a key,
  both must answer the same (equal values, or both absent), or it raises
  naming the key. On disjoint bounds it is commutative as well, and it is the
  operator for building a stack or a mosaic.
- **`normalize(store)`.** Drops keys that no valid node chain owns, such as a
  chunk under a group, which an `overlay` can leave: in the `A`, `B`, `C` case,
  `B`'s chunk `p/c/0` sits under `A`'s group `p`. It also adds missing ancestor
  groups. It runs once, at the end, so the operators above stay associative.

An `overlay` with stated bounds also covers two cases that no union of keys
can:
- **A patch.** `overlay(stack, patch)`, where the patch has the bounds of one
  chunk, replaces that chunk and leaves the rest.
- **A tombstone.** `overlay(stack, gone)`, where `gone` has empty contents and
  the bounds of a region, turns that region into fill value.

## The TIFF stack on the operators

```python
stores = [zarrify(str(p)) for p in paths]
meta = stacked_metadata(stores[0], n=len(stores))   # shape, chunks, z axis
header = ReferenceStore(
    {"zarr.json": root_group, "0/zarr.json": meta},
    bounds=[key("zarr.json"), key("0/zarr.json")],
)
planes = [
    map_chunks(s, "0", lambda c, i=i: (i, *c)).with_bounds([box("0", (i, 0, 0), (i + 1, *grid))])
    for i, s in enumerate(stores)
]
stack = union(header, *planes)
```

Each operand states what it answers for, none overlaps another, and `union`
checks that. The by-hand version in
[docs/examples/tiff_stack.py](../../docs/examples/tiff_stack.py) merges
source tables, moves indexes and builds keys itself. It also has to check by
hand that the files' layouts agree. Here, `union` would raise if two planes'
boxes overlapped, and the remaining check, that every file's `0/zarr.json` is
the same, belongs in `stacked_metadata`.

## Bounds in a saved archive

Stated bounds must survive `save`, or a reopened patch reverts to its
default bounds and shadows what it should not. Two options:

- **A format entry.** Write the bounds beside the source table, under the
  binding's reserved prefix, as `__vz__/` is in
  [the vzip binding](../../spec/bindings/vzip.md). A reader that ignores it
  still reads the store correctly on its own. Only a combination of stores
  depends on it.
- **A store-model property.** Put bounds in
  [Part 1 of spec/refkv.md](../../spec/refkv.md#part-1-reference-values),
  so every binding carries them. This is the cleaner choice if lazy
  composition (below) is wanted.

**Stated bounds versus observed range.** A vzip archive's page index already
records the first key of each page, which gives the range of keys the archive
*holds*. That is a different fact from the keys it *answers for*, and an
optimization rather than a meaning.

**Lazy composition.** With bounds in each archive, an `overlay` of opened
archives need not read either one's directory up front:
- `get(k)` goes to the last archive whose bounds contain `k`;
- `list` merges the sorted listings of each archive, each filtered to the keys
  no later archive's bounds contain;
- two archives can be checked for overlap by comparing their bounds alone.

## Checked by a prototype

A prototype of `map_keys`, `mount`, `map_chunks`, `overlay` and `union` with
the four kinds of bound was run on the 24-plane stack of the TIFF example.
It found:

- **The stack.** `union(header, *planes)` reads as a `(24, 256, 256)` array
  whose planes equal their files.
- **Order.** The reversed operand order gives the same keys.
- **Associativity.** `overlay` over the `A`, `B`, `C` case gives the same keys
  however it is nested.
- **A patch.** One chunk's bounds replace that chunk and leave the other 95.
- **A tombstone.** Plane 5's box over an empty store reads plane 5 as zeros
  and plane 6 unchanged.
- **Prefixes as ranges.** `range("0/c/1/", "0/c/10")` contains the same
  keys as `prefix("0/c/1")`, and not `0/c/10/0/0`.
- **Without bounds.** Node shadowing gave different results for
  `C ⊕ (B ⊕ A)` and `(C ⊕ B) ⊕ A`. A union with the header's default bounds
  refused the stack, because the header claimed every chunk under `0/`.

## Prior art: TensorStore

[TensorStore](https://google.github.io/tensorstore/) composes stores at two
levels. Its key-value level is close to this proposal; its array level does
more, at a cost this proposal avoids.

**Keys: the [`kvstack`](https://google.github.io/tensorstore/kvstore/kvstack/index.html)
driver.**
- Each layer maps an `exact` key, a `prefix`, or a range (`inclusive_min`,
  `exclusive_max`) to an underlying kvstore. Those are this proposal's `key`,
  `prefix` and `range` bounds.
- `strip_prefix` removes a prefix from the keys a layer passes on, the
  inverse of `mount`.
- "If the key ranges of the layers overlap, the last layer takes
  precedence": `overlay`'s rule.
- Every range is stated by the user, and there is no box. The defaults
  derived from Zarr nodes are this proposal's own, since only a store that
  knows it holds Zarr can derive them.
- The documentation does not say whether a key missing inside a layer's
  range falls through to an earlier layer. Here it does not: inside its
  bounds, a store's answer is final.

**Arrays: the [`stack`](https://google.github.io/tensorstore/driver/stack/index.html)
driver** (`ts.overlay`, `ts.stack`, `ts.concat`).
- Each layer is placed in a common index domain by an index transform, so
  its domain is a box bound in array coordinates. "The last layer that
  contains any given position within the domain takes precedence."
- The domain is the hull of the layers' domains, unless one is given.
- "Reading or writing at a position that has no backing layer results in an
  error." There is no fill value.
- Layers must share a data type, but each keeps its own chunks and codecs:
  the stack has no chunk layout or codecs of its own.

**The difference: when the combining happens.** TensorStore keeps its layers
apart and assembles values when they are read, so a reader needs TensorStore
and the JSON spec of the layers. These operators build one Zarr hierarchy in
one store, whose every chunk is, byte for byte, a chunk of an operand.

- **Wider reach.** Any Zarr reader with refkv's store opens the result, and a
  read is one lookup in one flat key table rather than a search through
  layers.
- **Stricter inputs.** The operands of a stack need equal codecs and chunk
  shapes, and a join along an existing axis must fall on whole chunks.
  TensorStore's index transforms translate, stride and relabel any region;
  `map_chunks` moves whole chunks only.
- **Absence.** Inside a store's bounds a missing chunk is its fill value,
  which a tombstone relies on. TensorStore's `stack` driver has no such
  case: an uncovered position is an error.

[Lazy composition](#bounds-in-a-saved-archive) of opened archives, routed by
their bounds, is what `kvstack` does. Where a join cannot fall on whole
chunks, refkv could zarrify each file as its own array and write a
TensorStore `stack` spec over them, rather than refuse the join.

## Open questions

- **One URL, two pins.** Two operands that pin the same URL differently were
  built from different versions of the object. Keep both sources, so the
  reader fails the stale one, or refuse the union?
- **Zarr v2 nodes.** `.zarray` and `.zgroup` nodes, and the `.` separator of
  v2 chunk keys, for `map_chunks` and boxes.
- **Joining along an existing axis.** This needs each operand's extent along
  the axis to be a whole number of chunks. Zarr's regular chunk grid allows
  no partial chunk in the middle, and a rectilinear chunk grid would remove
  that limit.
- **Group attributes.** `overlay` replaces a group's whole `zarr.json`; a
  merge of attributes, such as the stack's `multiscales`, is the caller's.
- **Opened archives as operands.** `refkv.open` returns a `VZipStore`, not a
  `ReferenceStore`. The operators either take any `ReferenceReader` or first
  read the store whole.
