# Building an output from the IR

`refkv.build` gives a Python caller the tools refkv's own outputs are built with, so that an output
refkv does not write (one TIFF made of several, an OME-Zarr 0.6 image) can be built from what refkv
read, and checked as refkv checks its own. It is the first step of the design in
[design/proposals/profile-plugins.md](../design/proposals/profile-plugins.md): reading an input,
values that name the bytes they read, and `seal`, which checks an output and makes it a store. A
format's description, its plan and the routines that build on them come in later steps.

## Reading an input

`refkv.build.read(location)` reads a file or a store as `zarrify` does, with the same sniffing,
files beside it, pins, reader policy and object stores, and returns a `Parsed`:

```python
from refkv.build import read

with read("image.tif") as p:
    p.profile, p.version        # "tiff", 0
    p.ir                        # the IR (refkv.ir.Ir): at, index, children, value, raw, ...
    p.sources                   # the sources it read, each with its size as found
```

The IR's elements become values through the `Parsed`:

- `p.reference(i)`: element `i`'s bytes as refkv's profile reads them, its recipe applied (a JPEG
  tile's tables spliced in);
- `p.span(i)`: its own bytes, as one `Ref`;
- `p.bytes(i)`, `p.bytes((source, offset, length))`: the bytes themselves, from the IR where the
  parser kept them and otherwise read from the source, several at once, within `max_bytes`
  (64 MiB by default);
- `p.members(i)`, `p.form(i)`, `p.element_at(source, offset)`, `p.source_of(i)` and `p.gaps()`.

A structure refkv's model of the format does not interpret is still an element, whose typed value
(`p.ir.value(i)`) and bytes a caller decodes itself.

## Values

A value is a sequence of parts, each naming what it reads, so that the source table is numbered
only when the output is sealed:

| part | reads |
|---|---|
| `Ref(parsed, source, offset, length)` | bytes of one of an input's sources |
| `Shared(data)` | a byte string many references share, made a data source |
| `Data(data, offset, length)` | a range of a data source of the caller's own |
| `Zeros(n)` | zero bytes, of the zero data source |
| `Literal(data)` | bytes in the reference itself |
| `Copy(ref)` | bytes to be copied into the output, filled with `parsed.bytes` |

`cut(value, start, stop)` and `concat(*values, merge=False)` make new values; `fits(value)` says
whether a value's payload stays within 65519 bytes whatever its sources are numbered.

`Array` is a Zarr v3 array (shape, data type, chunk shape, codecs, chunks by coordinates, fill
value, dimension names), and `array.entries(to=...)` its entries. `Written` is an output's entries
with their placement: which bytes entries a vzip reader reads at open, and which are stored
uncompressed.

## Sealing

`refkv.build.seal(written, inputs)` checks an output and returns a `refkv.ReferenceStore`:

- the source table is numbered by first use over the references in key order, the url sources
  first, then the data sources;
- every key is one the archive holds, every `Ref` lies within its source as `read` found it,
  every payload is at most 65519 bytes (with checksums, when the inputs were read with them), and
  a value reads at most 1024 ranges, counted as readers merge them;
- the inputs go together: one reader policy and registry, all read with checksums or none, and a
  file read twice with the same pins.

A failure is a `SealError`, whose `code` says which rule. A chunk refkv's own output holds is
**checked**: it carries the rule of the convention it was built under, which `seal` holds it to
where it is written (as built, or under a leading `transpose` codec), and not the read rule a
caller's own values meet.

`refkv.build.project(p)` is the output `zarrify` writes for the input, as checked values:
`seal(project(p).written, [p])` reads what `zarrify` writes.

## A TIFF made of several

The tests (`python/tests/test_build.py`, `stack`) build one classic TIFF of the pages of several
files from their IRs alone: each page's tags are copied from the IR's bytes (`ir.value` of a tag's
entry, `p.bytes` of its value), the strip and tile tables rewritten, and the strips referenced as
`Ref`s; the new header and IFDs are one `Data` part. Until the TIFF profile's plan lands, `read`
still refuses a file its image layout refuses, such as a stack of pages without OME-XML.
