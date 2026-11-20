# Writing a layout plugin

refkv's profiles read a file and write its Zarr hierarchy in one layout, refkv's. A **layout
plugin** is a Python package that writes the whole hierarchy of the output instead: an OME-Zarr
0.6 image of a TIFF, a TIFF's every page, an HDF5 file re-tiled into 2-D chunks, a Zarr v2
hierarchy that xarray opens. refkv parses the file as it always does, hands the plugin what it
parsed, and writes exactly what the plugin returns. A plugin's layout is a projection of the
file's IR, as refkv's own layout is: refkv builds no output of its own for a plugin, unless the
plugin asks for it. The rules are [spec/layout.md](../spec/layout.md).

refkv ships five plugins as examples, in
[refkv.zarrify.layouts.examples](../python/src/refkv/zarrify/layouts/examples/): `bare`,
`ome_zarr_06`, `tiff_pages`, `hdf5_rechunk` and `xarray_v2`. Each imports only refkv's public API,
so the quickest start is to copy the one closest to what you want into a package of your own.

```sh
pip install "refkv[layouts]"   # zarr-metadata, for copy, array, group and check
python -m refkv.zarrify image.ome.tif out.vzip --layout refkv.example.ome-zarr-06
python -m refkv.zarrify ophys.nwb out.vzip --layout refkv.example.ome-zarr-06 \
    --layout-options '{"images": [{"name": "movie", "array": "stimulus/templates/movie/data",
                       "dims": ["t", "x", "y"], "order": ["t", "y", "x"],
                       "axes": [{"name": "t", "type": "time"}, {"name": "y", "type": "space"},
                                {"name": "x", "type": "space"}]}]}'
```

## Supported layouts

Beside the examples, refkv supports one layout, which it keeps working from release to release:

- **`refkv.virtualizarr`**
  ([refkv.zarrify.layouts.virtualizarr](../python/src/refkv/zarrify/layouts/virtualizarr.py))
  presents an HDF5, netCDF-4, FITS or zipped Zarr v3 file as VirtualiZarr's own parsers
  (`HDFParser`, `FITSParser`, `ZippedZarrParser`) present it, so that code that selects variables
  and reads attributes from those parsers' output reads refkv's alike. Its chunk references are
  refkv's own layout's; it drops what `HDFParser` hides (`CLASS`, `NAME`, `REFERENCE_LIST`,
  `_Netcdf4Dimid`, `_NCProperties`, ...), names each FITS HDU by its `EXTNAME` (`PRIMARY`, `SCI`,
  ...) with its header cards as attributes, and writes no refkv declarations and no
  `refkv_source` mirror. It runs without the `layouts` extra. Its option `{"nonfinite": "text"}`
  writes a NaN or infinite attribute as text (`"NaN"`) rather than the bare token zarr-python
  writes. The module's docstring lists where it keeps refkv's presentation because VirtualiZarr's
  is a defect, such as `phony_dim_<axis>` names that give one dimension two sizes.

```sh
python -m refkv.zarrify data.nc out.vzip --layout refkv.virtualizarr
```

## A plugin

A plugin is an object with a `name`, the `formats` it reads, `options(given)` and
`write(inputs, options)`:

```python
from refkv.build import project


class Bare:
    """refkv's image layout, without refkv's declarations, the IR mirror or dataset layouts."""

    name = "acme.bare"  # <distribution>.<layout>
    formats = "*"  # every file and store profile; or {"tiff": {0}, "hdf5": {0}, "zarr2": {0}}

    def options(self, given):
        return {}  # the options in canonical form; raise ValueError to refuse them

    def write(self, inputs, options):
        [parsed] = inputs  # a zarrify call gives one input
        return project(parsed).entries(declarations=False, mirror=False, datasets=False)
```

`write` returns the output's entries, keys from its root: bytes (documents among them) and
references, each a value of `refkv.build` that names the bytes it reads
([docs/build.md](build.md)). The output has exactly those keys. A plugin may return a
`refkv.build.Written` instead of a plain mapping, to say which documents a reader is given when
it opens the archive (`at_open`), and which entries stay uncompressed (`stored`).

Register it under the entry-point group `refkv.layouts`, the key its name:

```toml
[project.entry-points."refkv.layouts"]
"acme.bare" = "acme:Bare"
```

and ask for it by name, `zarrify("image.tif", layout=Layout("acme.bare"))` or `--layout acme.bare`.
A notebook or a test can skip the entry point: `Layout(Bare())`.

## What a plugin is given

`inputs` holds the call's inputs as refkv parsed them, each a `refkv.build.Parsed`
([docs/build.md](build.md)). refkv opens and closes them around `write`.

- **The IR.** `parsed.ir` is every structure the parser read, with its byte extent, type and value
  (a TIFF's tags, an HDF5 file's object headers), and the arrays the source stores
  (`parsed.ir.stored_arrays()`, `parsed.ir.chunk_map()`). `parsed.reference(i, member)` is the
  value that reads an element's bytes as refkv's profile would (a JPEG tile with its file's tables
  spliced in), and `cut` and `concat` build values of parts of them. A plugin makes references of
  its own this way: re-tiled chunks, an IFD refkv's TIFF layout leaves out. The `tiff-pages`
  example is built from the IR alone.
- **refkv's own output, when asked for.** `pr = project(parsed)` is the output refkv writes
  without a plugin, with every layout the format has: `pr.keys()`, `pr.document(key)`,
  `pr.array_entries(path)` (a document and its chunks), `pr.entries(prefix)`,
  `pr.attributes(path)` (without refkv's declarations) and `pr.source_metadata(path)` (what refkv
  read of the node's header). `copy(pr, path, order=...)` copies an array, transposed if you ask;
  `pr.entries("refkv_source/")` keeps the IR mirror, so that the source can be rebuilt from the
  output; `pr.elements(path)` says which IR elements an array's chunks read. refkv makes this
  output the first time a plugin asks, and not at all for one that never does.
- **The routines** of `refkv.zarrify.coding` turn a format's coding parameters into the codecs and
  data type refkv writes, by the code refkv's profiles call: `tiff_codecs(tiff_tags(parsed, i),
  ...)` gives a TIFF IFD's codecs, predictors and packed samples included, so that a plugin does
  not redo the step most likely to go silently wrong.

Every reference refkv writes is checked to lie within a source refkv opened.

`pr.entries(...)` gives a `Written`, which carries the placement of each entry it keeps (the IR
mirror's documents are not read at open, say). Build the output in a `Written`
(`out = Written(); out |= pr.entries("refkv_source/")`) to keep them; a plain `dict` loses them,
and every Zarr document in it is read at open.

## What refkv checks

refkv seals what a plugin returns (`refkv.build.seal`): it writes the output only when every key
is one the archive can hold and every reference reads bytes of the inputs' sources, within them,
with a payload a reader takes; anything else, a Zarr v3 document that is not one included, is the
plugin's to get right. `check(output, inputs)`, in `refkv.zarrify.layouts.hierarchy`, is how a
plugin's tests find out: it returns `Problem(key, code, message)` for what `zarrify` refuses (the
first problem `seal` finds, under its code: `key`, `value`, `range`, `payload`, `checksums`,
`inputs`, `open`, `archive`) and for what a reader would trip on (`document`, `orphan`, `chunk`,
`json`, `zarr`, `consolidated`, `parent`, `copy`, `size`, `refkv`). A test reads a file with
`refkv.build.read`:

```python
from refkv.build import read
from refkv.zarrify.layouts.hierarchy import check


def test_bare():
    with read("tests/data/image.tif") as parsed:
        assert check(Bare().write([parsed], {}), [parsed]) == []
```

## When a layout is refused

| what | error | `python -m refkv.zarrify` |
|---|---|---|
| a name no plugin has, a plugin that fails to load, options it refuses, `layout=` beside `layouts=` | `LayoutUsageError` (a `ValueError`) | exit 2 |
| an input of a profile the plugin does not read, a store of DICOM files (which keeps no IR), a `Rejected` its `write` raises, refkv's own output's among them | `LayoutRejected` (a `Rejected`) | exit 3 |
| any other exception from `write`, or an output refkv does not seal | `LayoutFailed` | exit 1, with the plugin's traceback |

No output is written in any of them.

## Determinism

Two runs with the same refkv release, input, options and plugin release write the same archive
only if the plugin's output depends on nothing else: not on set or dict iteration order that
`PYTHONHASHSEED` changes, nor on the time or the machine. The examples' tests run `write` twice in
processes of different hash seeds and compare the outputs.
