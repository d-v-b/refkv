# Writing a layout plugin

refkv's profiles read a file and write its Zarr hierarchy in one layout, refkv's. A **layout
plugin** is a Python package that writes the whole hierarchy of the output instead: an OME-Zarr
0.6 image of a TIFF, a TIFF's every page, an HDF5 file re-tiled into 2-D chunks, a Zarr v2
hierarchy that xarray opens. refkv reads the file as it always does, hands the plugin what it
read, and writes exactly what the plugin returns. The rules are [spec/layout.md](../spec/layout.md).

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
  presents an HDF5, netCDF-4, netCDF3, FITS, ENVI, Zarr v2 or zipped Zarr v3 file as
  VirtualiZarr's own parsers (`HDFParser`, `NetCDF3Parser`, `FITSParser`, `ENVIParser`,
  `ZarrParser`, `ZippedZarrParser`) present it, so that code that selects variables and reads
  attributes from those parsers' output reads refkv's alike. Its chunk references are
  refkv's own layout's; it drops what `HDFParser` hides (`CLASS`, `NAME`, `REFERENCE_LIST`,
  `_Netcdf4Dimid`, `_NCProperties`, ...), names each FITS HDU by its `EXTNAME` (`PRIMARY`, `SCI`,
  ...) with its header cards as attributes, presents an ENVI raster in its file's axis order
  (`band`, `y`, `x` as the interleave orders them) under its data file's name, gives a Zarr v2
  array its `_ARRAY_DIMENSIONS` as `dimension_names`, and writes no refkv declarations and no
  `refkv_source` mirror. It runs without the `layouts` extra. Its option `{"nonfinite": "text"}`
  writes a NaN or infinite attribute as text (`"NaN"`) rather than the bare token zarr-python
  writes. The module's docstring lists where it keeps refkv's presentation because VirtualiZarr's
  is a defect, such as `phony_dim_<axis>` names that give one dimension two sizes.

```sh
python -m refkv.zarrify data.nc out.vzip --layout refkv.virtualizarr
```

## A plugin

A plugin is an object with a `name`, the `formats` it reads, `options(given)` and
`write(base, options)`:

```python
from refkv.zarrify.layouts import Base, Entries


class Bare:
    """refkv's image layout, without refkv's declarations, the IR mirror or dataset layouts."""

    name = "acme.bare"  # <distribution>.<layout>
    formats = "*"  # every file and store profile; or {"tiff": {0}, "hdf5": {0}, "zarr2": {0}}

    def options(self, given):
        return {}  # the options in canonical form; raise ValueError to refuse them

    def write(self, base: Base, options) -> Entries:
        return base.entries(declarations=False, mirror=False, datasets=False)
```

`write` returns the output's entries, keys from its root: bytes (documents among them) and
reference values (`refkv.Range`, `refkv.Concat`). The output has exactly those keys. A plugin may
instead return `Written(entries, read_at_open=..., stored=...)` to say which documents a reader is
given when it opens the archive, and which entries stay uncompressed.

Register it under the entry-point group `refkv.layouts`, the key its name:

```toml
[project.entry-points."refkv.layouts"]
"acme.bare" = "acme:Bare"
```

and ask for it by name, `zarrify("image.tif", layout=Layout("acme.bare"))` or `--layout acme.bare`.
A notebook or a test can skip the entry point: `Layout(Bare())`.

## What a plugin is given

`base` is refkv's own output with every layout the format has, and the IR it was built from
(`refkv.zarrify.layouts.Base`):

- **The output.** `base.keys()`, `base.document(key)`, `base.array(path)` (a document and its
  chunks), `base.entries(prefix)`, `base.attributes(path)` (without refkv's declarations) and
  `base.source_metadata(path)` (what refkv read of the node's header). `copy(base, path,
  order=...)` copies an array, transposed if you ask; `base.entries("refkv_source/")` keeps the
  IR mirror, so that the source can be rebuilt from the output.
- **The IR.** `base.ir` is every structure the parser read, with its byte extent, type and value
  (a TIFF's tags, an HDF5 file's object headers). `base.reference(i, member)` is the reference that
  reads an element's bytes as refkv's profile would (a JPEG tile with its file's tables spliced
  in); `base.elements(path)` says which elements a base array's chunks read; `cut` and `concat`
  build references of parts of them. A plugin makes references of its own this way: re-tiled
  chunks, an IFD refkv's TIFF layout leaves out.
- **The routines** of `refkv.zarrify.coding` turn a format's coding parameters into the codecs and
  data type refkv writes, by the code refkv's profiles call: `tiff_codecs(tiff_tags(base, i),
  ...)` gives a TIFF IFD's codecs, predictors and packed samples included, so that a plugin does
  not redo the step most likely to go silently wrong.

A plugin reads no bytes of the source itself: what the parser read is structure, and every
reference refkv writes is checked to lie within a source refkv opened.

## What refkv checks

refkv writes the output only when every key is one the archive can hold and every reference reads
bytes of the base's sources, within them, with a payload a reader takes; anything else, a Zarr v3
document that is not one included, is the plugin's to get right. `check(base, output)`, in
`refkv.zarrify.layouts.hierarchy`, is how a plugin's tests find out: it returns `Problem(key, code,
message)` for what `zarrify` refuses (`key`, `reference`, `open`, `archive`) and for what a reader
would trip on (`document`, `orphan`, `chunk`, `json`, `zarr`, `consolidated`, `parent`, `copy`, `size`,
`refkv`). A test
gets a base from a file with `layouts.base(path)`:

```python
from refkv.zarrify.layouts import base
from refkv.zarrify.layouts.hierarchy import check


def test_bare():
    b = base("tests/data/image.tif")
    assert check(b, Bare().write(b, {})) == []
```

## When a layout is refused

| what | error | `python -m refkv.zarrify` |
|---|---|---|
| a name no plugin has, a plugin that fails to load, options it refuses, `layout=` beside `layouts=` | `LayoutUsageError` (a `ValueError`) | exit 2 |
| a base the plugin does not read, a store of DICOM files (which has no base), a `Rejected` its `write` raises | `LayoutRejected` (a `Rejected`) | exit 3 |
| any other exception from `write`, or an output refkv does not write | `LayoutFailed` | exit 1, with the plugin's traceback |

No output is written in any of them.

## Determinism

Two runs with the same refkv release, input, options and plugin release write the same archive
only if the plugin's output depends on nothing else: not on set or dict iteration order that
`PYTHONHASHSEED` changes, nor on the time or the machine. The examples' tests run `write` twice in
processes of different hash seeds and compare the outputs.
