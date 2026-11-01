# Zarrifying image files and stores

refkv **zarrifies** an image file, or a chunked array store: it presents it
as an OME-Zarr dataset without converting or copying its pixels. A **zarrifier** reads
only the input's structure (headers, tile tables, metadata documents) and
writes a small vzip archive ([spec/bindings/vzip.md](../../spec/bindings/vzip.md)): a ZIP file
whose values are bytes or references. Its metadata entries are OME-Zarr 0.5
(Zarr v3) documents, and its chunk entries are references to byte ranges of
the original file, or to the store's own chunk objects. A Zarr reader that understands refkv then reads the pixels
straight from where they already are.

Each input format has a **profile**: a specification, written for
implementers, of exactly which archive a zarrifier produces from which
input ([spec/zarrify.md](../../spec/zarrify.md)). The READMEs below are for users:
who each one is for, what you get, and how to run it.

## Formats

<!-- profiles:docs-table -->
| format | for | README | profile |
|---|---|---|---|
| CZI | Zeiss CZI files | [czi](czi/README.md) | [czi.md §7](../../spec/zarrify/czi.md#7-czi-profile) |
| DICOM | DICOM files, including whole-slide images | [dicom](dicom/README.md) | [dicom.md §7](../../spec/zarrify/dicom.md#7-dicom-profile) |
| FITS | FITS images and tables (`.fits`, `.fz`) | [fits](fits/README.md) | [fits.md §9](../../spec/zarrify/fits.md#9-fits-profile) |
| FLAC | FLAC audio files, as a sharded array of samples (`.flac`) | [flac](flac/README.md) | [flac.md §8](../../spec/zarrify/flac.md#8-flac-profile) |
| GRIB | GRIB2 files of gridded weather and climate fields (`.grib2`), as labeled arrays with CF coordinates | [grib](grib/README.md) | [grib.md §8](../../spec/zarrify/grib.md#8-grib-profile) |
| HDF5 | HDF5 files (`.h5`, `.hdf5`), and NWB files (`.nwb`), which pynwb's `NWBZarrIO` opens | [hdf5](hdf5/README.md) | [hdf5.md §8](../../spec/zarrify/hdf5.md#8-hdf5-profile) |
| IMS | Imaris `.ims` files | [ims](ims/README.md) | [ims.md §7](../../spec/zarrify/ims.md#7-ims-profile) |
| LIF | Leica LIF files (`.lif`) | [lif](lif/README.md) | [lif.md §7](../../spec/zarrify/lif.md#7-lif-profile) |
| MP3 | MP3 audio files, as an array of samples (`.mp3`) | [mp3](mp3/README.md) | [mp3.md §8](../../spec/zarrify/mp3.md#8-mp3-profile) |
| MP4 | MP4 and QuickTime video and audio, as arrays of planes and samples (`.mp4`, `.mov`) | [mp4](mp4/README.md) | [mp4.md §8](../../spec/zarrify/mp4.md#8-mp4-profile) |
| MRC | MRC2014 maps, image stacks, volumes and volume stacks (`.mrc`, `.map`) | [mrc](mrc/README.md) | [mrc.md §8](../../spec/zarrify/mrc.md#8-mrc-profile) |
| N5 | N5 containers on S3 | [n5](n5/README.md) | [n5.md §8](../../spec/zarrify/n5.md#8-n5-profile) |
| ND2 | Nikon NIS-Elements ND2 files | [nd2](nd2/README.md) | [nd2.md §7](../../spec/zarrify/nd2.md#7-nd2-profile) |
| NDPI | Hamamatsu NDPI whole-slide images | [ndpi](ndpi/README.md) | [ndpi.md §7](../../spec/zarrify/ndpi.md#7-ndpi-profile) |
| NIfTI | NIfTI-1 and NIfTI-2 volumes (`.nii`) | [nifti](nifti/README.md) | [nifti.md §7](../../spec/zarrify/nifti.md#7-nifti-profile) |
| NumPy | NumPy arrays: `.npy` files, and the arrays of `.npz` archives, stored or deflated | [npy](npy/README.md) | [npy.md §8](../../spec/zarrify/npy.md#8-numpy-profile) |
| NRRD | NRRD volumes, attached or detached, raw or gzip (`.nrrd`, `.nhdr`) | [nrrd](nrrd/README.md) | [nrrd.md §8](../../spec/zarrify/nrrd.md#8-nrrd-profile) |
| OME-Zarr | OME-Zarr 0.4 hierarchies, migrated to OME-Zarr 0.5 in place | [ome-zarr](ome-zarr/README.md) | [ome-zarr.md §11](../../spec/zarrify/ome-zarr.md#11-ome-zarr-profile) |
| QPTIFF | Akoya (PerkinElmer) Vectra multiplex scans and inForm component data | [qptiff](qptiff/README.md) | [qptiff.md §7](../../spec/zarrify/qptiff.md#7-qptiff-profile) |
| Sentinel-2 SAFE | Sentinel-2 Level-1C and Level-2A products, as a `.SAFE` directory or a `.SAFE.zip` | [safe](safe/README.md) | [safe.md §8](../../spec/zarrify/safe.md#8-safe-profile) |
| TIFF | OME-TIFF, plain TIFF (tiled or in strips), and JPEG-tiled slides such as Aperio SVS | [tiff](tiff/README.md) | [tiff.md §7](../../spec/zarrify/tiff.md#7-tiff-profile) |
| VSI | Olympus cellSens whole-slide images (`.vsi` with its tile files) | [vsi](vsi/README.md) | [vsi.md §9](../../spec/zarrify/vsi.md#9-vsi-profile) |
| Zarr v2 | Zarr v2 hierarchies on S3 | [zarr2](zarr2/README.md) | [zarr2.md §7](../../spec/zarrify/zarr2.md#7-zarr-v2-profile) |
<!-- /profiles -->

A format that reads a file takes a URL, or a local path. One that reads a
store takes a URL ending in `/` that answers the S3 `ListObjectsV2` operation
anonymously, such as a public S3 bucket. SAFE reads either: a product's
`.SAFE` directory as a store, or its `.SAFE.zip` as a file. VSI is named by
its `.vsi` file and reads the tile files beside it by name, without listing
the directory, and NRRD by its header file, a detached header's data file
read beside it by name. MP3 and FLAC are not images: their archives hold a
plain Zarr array of audio samples, decoded from the file's frames when a
chunk is read ([mp3](mp3/README.md), [flac](flac/README.md)).

The SAFE and CZI pages are stubs so far, with what the browser reads and an
example script but no account of the output; their conventions describe it. A Sentinel-2 product becomes a GeoZarr hierarchy (one group per
resolution, one array per band, one chunk per JPEG 2000 tile, with the
zarr-conventions `proj` and `spatial` metadata) rather than OME-Zarr, and
keeps every file of the product. A CZI file becomes a bioformats2raw layout
of OME-Zarr 0.5 images, one per series (scene, block, ...), whose chunks are
the file's subblocks (uncompressed, JPEG, JPEG XR or zstd); subblocks that
are in no pyramid level of an image are kept as tile arrays under `tiles/`.
Their public test inputs are listed in [corpus_safe.txt](../../conformance/zarrify/corpus_safe.txt) and
[corpus_czi.txt](../../conformance/zarrify/corpus_czi.txt).

**Numbers in these pages.** The outputs, timings, request counts and archive
sizes quoted in the format pages were measured at spec/zarrify.md revision 16
([spec/history/virtualize/REVISIONS.md](../../spec/history/virtualize/REVISIONS.md)).
Since then, the archives also keep the
source's whole metadata (below), so they are larger than the sizes quoted.
TIFF and ND2 are now read through a read planner, so their request counts
differ too. Where a summary line or an attribute path has changed since
then, the pages show its current form, and say so where a value could not
be measured again.

## Two ways to run it

**The Python command line.** `pip install refkv` installs the `refkv` command
([installing](../README.md#installing)):

```bash
refkv zarrify <url or path> out.vzip
```

In a clone of this repository, after `uv sync`, `uv run refkv zarrify <url or
path> out.vzip` or `just zarrify <url or path> out.vzip` runs the clone's code.

Every format is zarrified by refkv's Rust core,
[rust/refkv-ir](../../rust/refkv-ir/), part of the refkv
distribution: the wheels on PyPI hold it as the Python module `refkv._core`
(a build from source, `uv sync` in a clone included, compiles it, which needs
a Rust toolchain). The core parses the file into an intermediate representation (IR),
plans the range requests, projects the IR onto the format's convention, and
writes the source's IR mirror ([below](#the-sources-metadata-and-where-it-came-from-refkv_zarrified)).

The command prints a one-line JSON summary. It exits with status 3 if the
profile rejects the input, and with status 1 if the reader policy refuses the
source, or a source the archive would name, such as a `--url` on a private
host (below); it then writes no archive. The format is chosen by the file's first bytes, or for a
store by the objects at its root. Options:

- `--url <url>`: the URL to record as the source, for a local file or
  directory that will be served from that URL (a local directory needs it;
  for a VSI slide, the `.vsi` file's URL, whose directory the tile files are
  named under, and for a NRRD input the header file's);
- `--checksums`: also record the CRC-32C of every referenced range
  ([spec/refkv.md §4.2](../../spec/refkv.md#42-range)), which reads every byte the
  archive references (off by default);
- `--allow-private-hosts`: let a file or store at an http(s) URL be read
  from a loopback, private or link-local host (below; unsafe);
- `--connections N` and `--byte-cost SECONDS_PER_MB`: for a file input,
  how many requests the read planner runs at a time (default 8), and how
  much wall time it charges for a megabyte read beyond those asked (default
  0.1 s);
- `--workers N`: how many of a store's metadata documents are read at a time
  (default 16).

Every `url` source records the size of the object it names. The input
file's source also records its ETag when every response gave the same
strong one ([spec/refkv.md §5.1](../../spec/refkv.md#51-pins)). A reader then notices a
source that has changed since the archive was written.

**Reader policy.** A file or a store at an http(s) URL is read under the
reader policy of [spec/refkv.md §6.7](../../spec/refkv.md#67-reader-policy) (a store's
listing, every object read and every redirect). A host that is, or
resolves to, a loopback, private, link-local or other special address is
refused, and so is a request that would go through a proxy the reader cannot
check. To zarrify a file or store served from your own machine or network (for
example `http://127.0.0.1:8000/...`), pass `--allow-private-hosts`. Reading
an archive applies the same policy to its sources, so an archive that refers
to such a host opens only with `refkv.zarr.open(path,
policy=Policy(allow_private_hosts=True))` (`from refkv.policy import
Policy`). A local file or directory needs neither.

Read the archive in Python with `refkv.zarr` and zarr-python
(`pip install 'refkv[zarr]'`, or `refkv[images]` for the JPEG, JPEG 2000,
JPEG XR and LZW decoders and TIFF's horizontal predictor). zarr-python finds the codecs that some formats need by
the names the archive gives: refkv's own (`refkv.stream`, `refkv.grib`, `refkv.fits_tiles`,
`refkv.tiff_lzw`, `refkv.skip`, and `n5_default`) through refkv's `zarr.codecs` entry points,
with no `import refkv.zarr` in your code (zarr imports the codec modules, which are
under `refkv.zarr`, itself), imagecodecs-zarr's (`imagecodecs_jpeg`,
`imagecodecs_jpeg2k`, `imagecodecs_jpegxr`, `imagecodecs_ljpeg`, `imagecodecs_delta`), which `refkv[images]` installs, and
zarr-python's own (`numcodecs.zlib`, `numcodecs.shuffle`, `numcodecs.delta`). So a
copy of an archive's arrays (in a directory, or in another store) reads with zarr
alone, except that an array on the zarr-extensions `rectilinear` chunk grid (a
clipped CZI level, an MP4 video plane whose chunks differ in length) also needs
`zarr.config.set({"array.rectilinear_chunks": True})`. Importing `refkv.zarr` sets
it, and so does loading one of refkv's codecs, but zarr checks an array's chunk
grid before it loads its codecs: set it yourself before opening such an array.
`refkv.zarr` opens the archive itself:

```python
import zarr
import refkv.zarr

g = zarr.open_group(refkv.zarr.open("out.vzip"), mode="r")
```

**From Python.** The command is a thin wrapper over
`refkv.zarrify.zarrify`, which returns the result as a
`refkv.ReferenceStore` instead of writing a file. The store reads right away,
in memory; `store.save(path)` writes the archive, the same bytes the command
writes; and `store.info` holds the format and the summary the command prints:

```python
import zarr
import refkv.zarr
from refkv.zarrify import zarrify

store = zarrify("https://openslide.cs.cmu.edu/download/openslide-testdata/Aperio/CMU-1-Small-Region.svs")
store.info["format"]                                   # "tiff"
g = zarr.open_group(refkv.zarr.store(store), mode="r")  # no archive yet
store.save("CMU-1-Small-Region.vzip")
```

`zarrify` takes the input's local path or URL, and the command's options as
keyword arguments: `url` (the URL the archive names the source by),
`checksums`, `policy`, `connections`, `byte_cost` (in seconds per byte, not
per MB) and `workers`. It raises `refkv.zarrify.Rejected` when no profile
accepts the input, `refkv.errors.ResolutionError` when the reader policy
refuses the input or a source (such as a URL on a private host), and `OSError`
when the input cannot be read (`FileNotFoundError` for a missing local file,
an HTTP error status, or a host that cannot be reached). The store reads its sources under the `policy` it was
given. `save` checks the sources against the policy the archive's readers will
use, `Policy()` unless it is given one, and raises `ResolutionError` for a
source a reader under it could not read, so for sources on a private host pass
that one too: `store.save(path, policy=policy)`.

**Naming the input's format.** Given a string, `zarrify` decides what the input
is. It is a store when it is a directory or a URL ending in `/`, and otherwise a
file whose first bytes, not its name, select the profile. A source or store class
names the profile instead
([spec](../../spec/zarrify.md#17-named-inputs-and-layouts)). The input must then
be in that format, and a store's URL needs no trailing `/`. There is one class
per profile in the registry. `<Name>Source` is for a file (`TiffSource`,
`Mp4Source`, `NdpiSource`, ...). `<Name>Store` is for a store (`Zarr2Store`,
`OmeZarrStore`, `N5Store`). SAFE has both, `SafeSource` for a `.SAFE.zip` file
and `SafeStore` for a `.SAFE` directory. The input's read options belong to the
class (`url`, `checksums`, `policy`, and `connections` and `byte_cost` for a
file, or `workers` and `max_objects` for a store), so `zarrify` raises
`TypeError` when one is also passed beside it:

```python
from refkv.zarrify import zarrify, NdpiSource, Zarr2Store, MrcSource, Image

zarrify(NdpiSource("https://example.org/slide", checksums=True))  # an NDPI slide, or rejected
zarrify(Zarr2Store("https://example.org/data.zarr"))  # a store, though its URL has no trailing "/"
zarrify(MrcSource("emd_1234.map"), layouts=[Image()])  # the image, without the dataset layout
```

A named input skips sniffing, so the profile's parser reads the input and
rejects it with its own message when the input is not in that format.
`TiffSource` accepts the TIFF variant the parser finds (`ndpi`, `qptiff`).
`NdpiSource` and `QptiffSource` reject a TIFF file that is not that variant.
`OmeZarrStore` rejects a Zarr v2 store with no OME-NGFF 0.4 layout at its root,
and `Zarr2Store` reads either kind. `layouts` lists the layouts the hierarchy
holds. It must contain `Image()`, the convention's hierarchy at the root.
`Dataset()` adds the format's [dataset layout](../../spec/conventions.md#9-dataset-layouts)
next to the image (MRC's `dataset` group). By default every layout the format
has is written, so `layouts=[Image()]` leaves the dataset layout out.
`zarrify_store` and a string `location` take `layouts` too.

**From async code.** `zarrify_async` and `zarrify_store_async` are coroutines that
take the same arguments and give the same store, or raise the same errors, as
`zarrify` and `zarrify_store`, which wait for them. While an input's reads are in
flight the event loop is free, so one process can zarrify several inputs at once,
or zarrify inside a notebook, a server or a VirtualiZarr pipeline. The reads still
block, each on a thread of the run's own (`connections` of them), so the reader
policy holds as it does for `zarrify`:

```python
import asyncio
from refkv.zarrify import zarrify_async

async def main(urls):
    return await asyncio.gather(*(zarrify_async(u) for u in urls))

stores = asyncio.run(main(["https://example.org/a.nd2", "https://example.org/b.czi"]))
```

In a notebook, `await zarrify_async(url)` in a cell. The blocking `zarrify` works
there too, since it runs on refkv's own event loop, but called from a coroutine on
that loop (a coroutine refkv runs) it raises `RuntimeError`: await `zarrify_async`
there.

**The browser.** The same zarrifiers run in a service worker
([js/](../../js/README.md)): the Rust core, built for WebAssembly. The
service worker serves the
result to any Zarr reader on the page's origin as a plain HTTP Zarr store. The
live demo, <https://d-v-b.github.io/refkv-demo/image-to-zarr/>, takes a URL
(`?url=<url>`), shows the levels, opens them in a Neuroglancer fork
(<https://github.com/d-v-b/neuroglancer>, branch `vzip`) and offers the
archive for download. That Neuroglancer link reads through the service
worker, so it opens only on the demo's site, in a browser where the demo has
zarrified the file. To share a view, host the downloaded archive and open its
URL in the demo: the link then names the archive,
`<archive url>|vzip:<path>|zarr3:`, which any deployment of the fork opens
with no service worker, reading the original's bytes by the archive's
references. The archive pins each source's size; the fork checks a pin when
the response shows its header, and skips it when a cross-origin server hides
it ([spec/refkv.md §6.7](../../spec/refkv.md#67-reader-policy), rule 7).
Two limits apply:

- The demo page has an example of every format. MP3 and FLAC archives
  hold audio, which the page lists and offers for download but does not
  display. Of a hierarchy that is not an OME-Zarr image (an HDF5 file, a
  Zarr v2 or N5 store without one), the page shows the arrays Neuroglancer
  can: two or more axes, a data type it has (not `float64` or `int64`) and
  codecs the fork decodes (not `numcodecs.zlib`, an HDF5 file's deflated
  datasets); an HDF5 file's images first. A local build of the demo (`just
  js::build && node demo/serve.mjs 8080`; the build compiles the Rust core
  to WebAssembly first, `dist/refkv_ir.wasm`, so it needs a Rust toolchain
  with the `wasm32` target) shows a change before it is deployed.
- The host must allow cross-origin reads (CORS), and so must the host of a
  shared archive. zenodo.org, ftp.ebi.ac.uk,
  raw.githubusercontent.com, OpenOrganelle's S3 bucket and the IDR's Embassy
  S3 do; openslide.cs.cmu.edu, downloads.openmicroscopy.org and the NCI
  Imaging Data Commons bucket on storage.googleapis.com do not, so their
  files can be zarrified only from Python. When a read fails, the demo says
  why, where the browser can tell: the host does not allow this site to read
  it (CORS), it cannot be reached, or it is an `http://` URL read from the
  `https://` page (mixed content). The page also shows, for every input, a
  Python script (PEP 723, for `uv run`) that builds the same hierarchy with
  refkv, which CORS does not restrict.

The browser code also runs under Node:
`node js/conformance/zarrify.ts <url> out.vzip [--checksums]
[--allow-private-hosts]` writes an archive equivalent to the Python one. It
runs the same Rust core as Python, built for wasm32 (`just js::wasm`).

**Store reads.** Listing a store is sequential (S3 returns 1000 keys per
request). Its metadata documents (`attributes.json`, `.zgroup`, `.zarray`,
`.zattrs`) are then read concurrently, 16 requests at a time by default
(`--workers N` in Python, `concurrency` in the browser's `openHttpStore`),
holding at most 64 MiB of documents ahead of the zarrifier.

## The source's metadata, and where it came from: `refkv_zarrified`

A zarrified hierarchy looks like any other Zarr, so it says what it is. Each
format has a [Zarr convention](https://github.com/zarr-conventions/zarr-conventions-spec)
of its own, specified in [spec/conventions.md](../../spec/conventions.md): the
hierarchy's whole Zarr layout, and the translation of the source's header
into JSON. Every output's root records, under the key `refkv_zarrified`,
the profile, the convention's version and the source. The nodes also hold what the source says about
them, under the same key. Metadata that is large, or that grows with the
data, is on the group `refkv_source`, a child of the root that OME-NGFF
readers ignore and whose documents are fetched only when it is opened. Where
each format's metadata is:

- **Every file format:** `refkv_source` is the source's **IR
  mirror** ([conventions §8](../../spec/conventions.md#8-the-ir-mirror)).
  It is a table under `refkv_source/ir/` that accounts for every byte of the
  file, so that, with the source's bytes, the file is rebuilt byte for byte
  with no knowledge of the format, and a JSON view of it under
  `refkv_source/tree`: every TIFF tag of every IFD, every ND2 chunk, every CZI
  segment, every HDF5 object header, attribute and link of an IMS file. Values of more than 1024 bytes, such as a long OME-XML, are not
  copied into the view; it points to them in the source. The root keeps a
  little: a TIFF's byte order and BigTIFF flag, an ND2 file's decoded
  metadata chunks (attributes, experiment, picture metadata, text info) that
  fit its budget, and a CZI file's header.
- **NDPI:** the IR mirror on `refkv_source`, as for TIFF: every IFD, tag,
  high word, strip and tile. The root keeps a fluorescence slide's filter,
  wavelength and exposure time.
- **DICOM:** the IR mirror on `refkv_source`, as for TIFF: the file's data
  elements themselves, layout, padding and dead space included. The root
  keeps the File Meta Information and the dataset in the DICOM JSON Model,
  without the members that would make it large
  ([dicom §5](../../spec/zarrify/dicom.md#5-source-metadata)).
- **NIfTI:** the IR mirror on `refkv_source`: the header, the extender, each
  extension and the voxel data. The root keeps the header, field by field,
  the scaling and the affine.
- **MRC:** the IR mirror on `refkv_source`: the header, the extended header
  and the data. The root keeps the header, field by field, the kind of file,
  the data type, the axis map and the voxel size. The archive also holds the
  file as a dataset for xarray under `dataset/`, declared by its own
  convention ([conventions §9](../../spec/conventions.md#9-dataset-layouts)).
- **LIF:** the IR mirror on `refkv_source`: the XML header, every memory
  block (an image's or not) and any bytes after them. The root keeps the
  file's version; each image keeps its element of the XML (its name, its path
  in the file's tree), its memory block, and every attribute of its dimensions
  and channels, as text, and a tile of a tile scan its stage position.
- **NRRD:** the IR mirror on `refkv_source`: the header file, line by line,
  and the data (in the header file, or in a detached header's data file): the
  files the chunks reference described in place, and a detached header's file
  held, its bytes in the archive. The root keeps every field as written, every
  key/value pair and comment, and what the layout derives from them (the data
  type, the encoding, each axis's image axis, whether the orientation is
  applied).
- **VSI:** the IR mirror on `refkv_source`: the `.vsi` file (its metadata
  tree field by field, and the IFDs inside it) and every tile file (its
  headers, tile directory and tiles), each described in place, so that the
  mirror rebuilds each file from the slide's own bytes. The root keeps the
  metadata tree as JSON, the list of stacks and the file's size; each
  image keeps its stack's properties (scale, origin, channels, creation
  time, magnification) and its tile file's headers.
- **MP3:** the IR mirror on `refkv_source`: the ID3v2 tags, the Xing or
  Info frame, every audio frame, and the ID3v1 and APE tags, so that the
  file is rebuilt from the archive. The root keeps the stream's parameters
  (version, sample rate, channels, frames, bitrate), the info frame's
  counts and LAME fields, and the ID3v1 tag's fields.
- **FLAC:** the IR mirror on `refkv_source`: the ID3v2 tags, every metadata
  block and every frame, and the ID3v1 tag, so that the file is rebuilt from
  the archive. The root keeps the stream's parameters, STREAMINFO (with its
  MD5), the Vorbis comments, the cue sheet, the pictures' fields and the tags'
  fields; each picture's data is an array of bytes under `pictures`.
- **N5, Zarr v2 and OME-Zarr:** each node's own attributes and the layout
  members its `zarr.json` does not reproduce, on the node, as
  `{"attributes": ..., "metadata": ...}` (with `"unversioned"` for an
  OME-Zarr group); every other object of the store, kept whole under
  `refkv_source/objects/`.
- **SAFE:** the product's identity on the root, each band's radiometric
  values on its array, and every file of the product kept on `refkv_source`.

So a reader who needs, say, an ND2 file's objective or a DICOM series'
acquisition parameters finds them in the hierarchy. For the NIfTI example
in [nifti/](nifti/README.md):

```python
v = g.attrs["refkv_zarrified"]
print({k: v[k] for k in ("profile", "version", "source")})
print(v["nifti"]["header"]["descrip"], v["nifti"]["scaling"])
```

```
{'profile': 'nifti', 'version': 0, 'source': {'url': 'https://raw.githubusercontent.com/spm/spm/main/canonical/avg152T1.nii'}}
NIFTI-1 Image {'slope': 0.003921568859368563, 'inter': 0.0}
```

The record stays with the hierarchy if it is copied out of the archive. The
profile and version name the rules that produced it, so it
can be regenerated from the source and checked. Until refkv 1.0,
every convention is at version 0 and promises nothing: any change may
change a layout, in a 0.x release too. From
1.0, conventions start at version 1, and a convention's version increases
only when its layout changes for some source
([conventions §1](../../spec/conventions.md#1-conventions)).
The record names the source by its URL only; the archive's source table
pins the sources (above). Each convention has a JSON Schema next to it,
`spec/zarrify/<format>/schema.json`.

## The implementations agree

`conformance/zarrify/compare.py` runs three zarrifiers on synthetic
files of every profile (including inputs each must reject) and on the public
files and stores listed in
[conformance/zarrify/](../../conformance/zarrify/) (`corpus_*.txt`),
and checks that their outputs are equivalent ([spec/zarrify.md §1.1](../../spec/zarrify.md#11-output-and-equivalence)):

- `ref`, the reference (`conformance/zarrify/reference/cli.py`): the
  Python profiles refkv shipped before the formats moved to the Rust core,
  frozen (a format added since, such as MRC, has none, and compares `py`
  with `web` alone; the registry's `reference` says which);
- `py`, `python -m refkv.zarrify`;
- `web`, the browser code under Node.

`py` and `web` run one core, so the independent check
is `ref`, compared with both everywhere but `refkv_source`. `py` and `web` are
compared entirely, the mirror included, which shows that the two hosts drive
the core alike. The mirror has no second implementation. Instead,
`compare.py` checks every mirror on its own: it holds the mirror's
invariants, it describes a source of the size the archive pins, and it is
canonical. The
verify scripts check that the source rebuilt from it is the file, byte for
byte ([HARNESS.md](../../conformance/zarrify/HARNESS.md)). The
pixels are checked separately, per format, against an independent reader;
each README says which.
