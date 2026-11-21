# Building an output from the IR

`refkv.build` gives a Python caller the tools refkv's own outputs are built with, so that an output
refkv does not write (one TIFF made of several, an OME-Zarr 0.6 image) can be built from what refkv
read, and checked as refkv checks its own. It is the first step of the design in
[d-v-b/refkv#278](https://github.com/d-v-b/refkv/pull/278) (design/proposals/profile-plugins.md there): reading an input,
values that name the bytes they read, and `seal`, which checks an output and makes it a store. A
format's description, its plan and the routines that build on them come in later steps. One part of
the description is there already: the IR describes each array the source stores
([spec/stored-array.md](../spec/stored-array.md)), as `p.ir.stored_arrays()` and `p.ir.chunk_map()`
give them.

## Reading an input

`refkv.build.read(location)` reads a file or a store as `zarrify` does, with the same sniffing,
files beside it, pins, reader policy and object stores, and returns a `Parsed`. It parses and
stops: it makes no output, and reads nothing that only an output needs.

A `Parsed` holds the input open until it is closed: a local file, and any file read beside it, stays
mapped, so that the built-in output can still be made of it (`project`, below). Use it as a context
manager, `with read(...) as p:` or `async with await read_async(...) as p:`, or call `p.close()`.
One that is collected unclosed releases the input then, with a `ResourceWarning`. A closed `Parsed`
keeps its IR, its sources' sizes and pins, and the values made of it, so it can still be sealed
without checksums.

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
  parser kept them and otherwise read from the source, several together, within `max_bytes`
  (64 MiB by default); an element in a derived space (inside a gzip stream, say) gives the
  inflated bytes the parser kept, or raises where it kept none, or kept a decoded value
  (`p.ir.value(i)`) instead; a range of no source of the input, or a negative one, is a
  `KeyError`, and one past its source a `ValueError`. It blocks: call it from a thread, not
  from refkv's own event loop;
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

`cut(value, start, stop)` and `concat(*values, merge=False)` make new values; `fits(value)` gives
why `seal` might refuse a value's payload (more than `MAX_PAYLOAD`, 65519 bytes, whatever its
sources are numbered, with the checksums its inputs were read with), or None.

`Array` is a Zarr v3 array (shape, data type, chunk shape, codecs, chunks by coordinates, fill
value, dimension names, attributes, chunk key encoding), and `array.entries(to=...)` its entries;
a fill value left None is the data type's zero. `Written` is an output's entries
with their placement: `at_open`, the bytes entries placed explicitly (True, read at open by a vzip
reader; False, kept for later; any other follows refkv's default, a Zarr document read at open), and
`stored`, those kept uncompressed. It merges with `|` and `|=`, keeping placements.

## Sealing

`refkv.build.seal(written, inputs, *, policy=None, registry=None, info=None)` checks an output and
returns a `refkv.ReferenceStore`, read under `policy` and `registry` (the inputs' by default) with
`info` as its own:

- the source table is numbered by first use over the references in key order, the url sources
  first, then the data sources;
- every key is one the archive holds, every `Ref` lies within its source as `read` found it,
  every payload is at most 65519 bytes (with checksums, when the inputs were read with them, each
  counted at its largest, 6 bytes, so that whether an output is sealed never depends on its
  bytes);
- the inputs go together: one reader policy and registry, all read with checksums or none, and a
  file read twice with the same pins;
- the store's reader policy allows it: its url sources (rules 1 to 3), and no more sources or
  format-entry bytes than the policy's limits.

How many reads or bytes one value takes is not `seal`'s to check: it is each reader's own limit
(`max_reads` and `max_get_bytes`, [spec/refkv.md §6.7](../spec/refkv.md#67-reader-policy) rule 5), which a reader of a trusted store may
raise. A value that needs more than a reader allows fails that `get`, and only that one.

A failure is a `SealError`, whose `code` (one of `CODES`) says which rule. The numbering and the
checks of keys, ranges and payloads are the core's (`refkv._core.seal`), the same function
every output of a core run passes through, from Python, the CLI or the browser (every output of
`zarrify` but the DICOM store form's); `seal` adds what
only Python knows: the caller's objects, the inputs, the policy's URL rules, the placements and
the archive. A layout plugin's output is sealed the same way
([docs/layout-plugins.md](layout-plugins.md)): `refkv.build.apply(plan(layout), [p])` is what
`zarrify(..., layout=layout)` does with the input once it is parsed.

`refkv.build.project(p)` is the output `zarrify` writes for the input, as values:
`seal(project(p).written, [p])` reads what `zarrify` writes. `read` did not make it: the first
`project(p)` does, from the parse `p` keeps, and reads then what the built-in output alone needs
(an edge chunk it copies, a chunk its `omero` windows sample). So `p` must still be open the
first time; the output is kept, and a later `project(p)` builds on it, after `p` is closed too.
`project` blocks, and `project_async` is the same on a caller's event loop.

What `project` returns (a `Projected`) is refkv's own output to build on: `written`, every entry
with its placement; `keys()`, `nodes()`, `document(key)`, `attributes(path)` (without refkv's
declarations) and `source_metadata(path)`; `array(path)`, an array as an `Array`, and
`array_entries(path)`, its document and chunk entries; `entries(prefix)`, the entries below a
prefix as a `Written`; and `elements(path)`, the IR elements each chunk of an array reads. It is
one projection of the input, and nothing else in `refkv.build` needs it.

## Arrays as the source stores them

The IR describes each array the source stores (`p.ir.stored_arrays()`), and the units it holds
(`p.ir.units(k)`, the chunk map's rows of the array at position `k`). `stored_array` is the plainest
projection of one: a Zarr array with the description's shape and grid, a chunk for each unit, its
bytes referenced where they lie.

```python
from refkv.build import Written, read, seal, stored_array
from refkv.zarrify.coding import npy_array_type

with read("weights.npz") as p:
    described = p.ir.stored_arrays().arrays
    position = {a.element: k for k, a in enumerate(described)}
    out = Written({"zarr.json": b'{"zarr_format":3,"node_type":"group","attributes":{}}'})
    for entry in p.description["arrays"]:          # what the NumPy parser found
        k = position[entry["stored"]]
        if "deflate" in entry or described[k].grid is None:
            continue                               # see below
        t = npy_array_type(entry["header"])        # the header's declaration, as the profile reads it
        array = stored_array(p, k, t["data_type"], endian=t["endian"],
                             inner_shape=[t["trailing"]] if t["trailing"] else (),
                             fill_value=t["fill_value"])
        out |= array.entries(to=entry["path"])
    store = seal(out, [p])
```

A description stops at the bytes of an element: the data type, its byte order and the fill value
are what the source declares, which a profile's routine reads (`npy_array_type` for an NPY
header), and the caller gives them. Nothing here asks for refkv's own output. `p.description` is
the profile's description of the input, where its parser returns one: NumPy's
([npy.md §8.6](../spec/zarrify/npy.md#86-the-description)), FITS's
([fits.md §9.6](../spec/zarrify/fits.md#96-the-description)), HDF5's, of a file read as a file
([hdf5.md §8.7](../spec/zarrify/hdf5.md#87-the-description)), netCDF3's
([netcdf3.md §7.7](../spec/zarrify/netcdf3.md#77-the-description)), ENVI's
([envi.md §8.7](../spec/zarrify/envi.md#87-the-description)), Zarr v2's
([zarr2.md §7.3](../spec/zarrify/zarr2.md#73-the-description)), Zarr v3's
([zarr3.md §7.6](../spec/zarrify/zarr3.md#76-the-description)), TIFF's and QPTIFF's
([tiff.md §7.6](../spec/zarrify/tiff.md#76-the-description)), and NDPI's
([ndpi.md §7.1](../spec/zarrify/ndpi.md#71-the-description)) do, and the other profiles' is None
until their projections read the descriptions too. `stored_array` raises `ValueError` for an array it
cannot write so: one whose storage the IR does not model (a description with no grid, such as a
NumPy `a<n>` array's, which the profile still presents from its header), whose elements are not
whole bytes, whose units are encoded with a codec `zarr_codecs` has no Zarr codec after `bytes`
for (it translates the codecs numcodecs decodes, and a Zarr store's own codecs that take bytes;
not a Zarr filter, nor HDF5's own framings), whose
units lie in the members of a run of elements (an ND2 file's frames), or whose units lie in a
derived space: a deflated `.npz` member's data is a range of what its stream inflates to, which no
range of the file holds. For such an array a caller references the stream itself,
`p.encoded(p.ir.space(entry["stored"]))`, and writes the codecs that decode it (`gzip_member` and
`gzip_codecs`, below).

## The pieces of refkv's own output

What refkv's own projections share is public too, each routine the binding of the function they
call, so that a caller's output can keep any piece of refkv's convention without restating it:

| routine | gives |
|---|---|
| `grid(span, shape, item_size, limit=)` | contiguous values cut into chunks as [conventions §7](../spec/conventions.md#7-source-values-as-arrays) cuts them: the chunk shape, and each chunk's range of `span`, an edge chunk padded with `Zeros`, or a `Copy` where the padded reference would not fit |
| `filled(value)` | a value with a `Copy` as its bytes, read through the input; any other value as it is |
| `gzip_member(stream, crc32=, size=)` | a raw deflate stream (a ZIP member's) framed as the gzip member the `gzip` codec reads, the stream referenced where it lies |
| `mirror(parsed)` | the IR mirror's entries under `refkv_source` ([conventions §8](../spec/conventions.md#8-the-ir-mirror)), with their placement: the table, the view, and the keys a store's or an archive's listing ignored, which the convention records there. A store's objects under `refkv_source/objects` are its projection's to write |
| `fits_arrays(parsed, index)`, `fits_cards(parsed, index)` | a FITS HDU's arrays as the FITS convention writes them (an image, a table's `rows` and `heap`, a tile-compressed image), but for refkv's declarations, and its header's cards as that convention writes them in JSON; the `refkv.virtualizarr` layout presents FITS files with them |
| `hdf5_hierarchy(parsed)` | an HDF5 input's hierarchy as the HDF5 convention presents it, a file or a store of one with the files it names: its nodes, each node's attributes without refkv's declarations and its source metadata, each array's document and chunks (an edge chunk refkv copies a `Copy`, for `filled`), each array's storage element of the IR (`element`), and the URL the root names the source by (`url`, a store's named file), made from the parser's description by the function refkv's own projection calls; `refkv.virtualizarr`, `xarray-v2` and `hdf5-rechunk` present HDF5 files with it, and python/tests/test_rebuild_hdf5.py rebuilds refkv's own output with it |
| `netcdf3_hierarchy(parsed)` | a netCDF3 file's hierarchy as the netCDF3 convention presents it ([netcdf3.md §2](../spec/zarrify/netcdf3.md#2-the-hierarchy)): the root, then an array per variable it presents, each node's attributes without refkv's declarations and its source metadata, and each array's document and chunks (an edge chunk refkv copies a `Copy`, for `filled`), made from the parser's description by the function refkv's own projection calls; the `refkv.virtualizarr` layout presents netCDF3 files with it. `ValueError` for an input that is not a netCDF3 file |
| `envi_image(parsed)` | an ENVI raster's image as the ENVI convention presents it, read as a store of its header and data files: the root's source metadata (`source_metadata()`), the array's document with no attributes (`document()`), its chunks by coordinates, each a value of its range of the data file (`chunks()`), the data file's key (`data_key`) and the URL the root names the source by (`url`, the header file's), made from the parser's description by the function refkv's own projection calls; `refkv.virtualizarr` presents ENVI rasters with it |
| `zarr_hierarchy(parsed)` | a Zarr v2 store's (and an OME-Zarr store's) or a Zarr v3 store's hierarchy, listed or zipped, as the Zarr convention presents it: its nodes, each node's source metadata, and each array's document (a Zarr v2 node's with no attributes but an OME group's `ome`, a Zarr v3 node's with the attributes it keeps in place) and chunks, made from the parser's description by the function refkv's own projection calls; `refkv.virtualizarr` presents Zarr stores with it |
| `ome_image(parsed)` | the OME-Zarr 0.5 image refkv's convention writes at the root of a TIFF, QPTIFF, NDPI or IMS input, a file or a TIFF's image across files: the root's `ome` attributes without refkv's declarations and its source metadata, each level's document and chunks (sharded as the convention shards strips), and the URL the root names the source by (`url`, an image across files' named file), made for the TIFF family from the parser's description through refkv's plan, by the function refkv's own projection calls; it reads the chunks the `omero` windows sample. `ome-zarr-06` presents these images with it, and python/tests/test_rebuild_tiff.py rebuilds refkv's own TIFF output with it |
| `declaration(parsed, path, own=, attributes=, url=)` | a node's attributes with refkv's convention declared ([conventions §2](../spec/conventions.md#2-attributes)): at the root, the profile, its version and the source, by `url` (the input's by default) |

```python
from refkv.build import Array, Written, declaration, filled, grid, mirror, read, seal
from refkv.zarrify.layouts.hierarchy import group

with read("volume.npy") as p:
    span = p.span(p.ir.index("data"))                      # the range that holds the values
    chunk_shape, chunks = grid(span, [512, 512, 300], 2, limit=1 << 20)
    array = Array([512, 512, 300], "uint16", chunk_shape, [{"name": "bytes", "configuration": {"endian": "little"}}],
                  {coords: filled(v) for coords, v in chunks.items()})
    out = Written({"zarr.json": group(declaration(p, own=p.description["own"]))})
    out |= array.entries(to="array")
    out |= mirror(p)
    store = seal(out, [p])
```

A declaration is a claim: an output that keeps it says its subtree follows refkv's convention.
`seal` does not check that; `refkv.zarrify.layouts.hierarchy.check` does.

`python/tests/test_rebuild_npy.py` is the test of these tools: a NumPy projection written with
them, and with `npy_array_type` and `gzip_codecs`, writes what `zarrify` writes for every NumPy
fixture, with the same source table, IR mirror, declarations and placement, and without ever
asking for refkv's own output.

## A TIFF made of several

The tests (`python/tests/test_build.py`, `stack`) build one classic TIFF of the pages of several
files from their IRs alone: each page's tags are copied from the IR's bytes (`ir.value` of a tag's
entry, `p.bytes` of its value), the strip and tile tables rewritten, and the strips referenced as
`Ref`s; the new header and IFDs are one `Data` part. Until the TIFF profile's plan lands, `read`
still refuses a file its image layout refuses, such as a stack of pages with neither OME-XML nor
an ImageJ description.
