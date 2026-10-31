# Implementation plan: the GRIB profile

This plan adds a GRIB convention and profile (`spec/zarrify/grib.md`, Part 1
and Part 2 of one document, as [fits.md](../../spec/zarrify/fits.md) is) and
an array-to-bytes codec, `refkv.grib`, that decodes one GRIB message into one
chunk. They are implemented in the Rust core and reached from Python, the
browser, the CLI and the Neuroglancer fork, with fixtures, a verifier, a
public corpus and a docs page. It is a design note, not normative; the
convention, the profile and the codec's section of
[conventions.md](../../spec/conventions.md) will state the decisions.

The input is **one GRIB2 file**. Each message (each field of a message,
[§2.2](#22-variables-which-fields-share-an-array)) becomes one chunk, and
that chunk's reference is the message's byte range, unchanged: no bytes are
rewritten and no literal ranges are needed. The fields are arranged into
labeled arrays that xarray opens as a dataset, with CF time, level and grid
coordinates and GeoZarr georeferencing. A forecast archive made of many
files (reference time × step), the case with the most value, comes after
this profile ([§11](#11-open-questions), item 8). It builds on this profile's
output.

**Status.** Implemented, but for the Neuroglancer fork and the demo (step 5):
[spec/zarrify/grib.md](../../spec/zarrify/grib.md) and
[conventions §12](../../spec/conventions.md#12-grib-messages) state what was decided. The
implementation departs from this plan in these places:

- **Point order.** The codec keeps the file's row order and directions, normalizing only the
  scanning mode's bits `0x10` (alternate rows reversed) and `0x20` (points consecutive along `j`).
  The coordinates follow the file, so a grid stored north to south has a descending latitude.
  Grids that differ only in those two bits are one grid.
- **Decoding as ecCodes does.** A field of bit width 0 is `R`, not `R × 10^−D`: ecCodes, NCEP's
  g2c and gribberish all read it so, and the encoders write it so. `10^−D` is `D` successive
  divisions, as ecCodes computes it. The decoder matches ecCodes 2.50 bit for bit on every
  fixture and sample.
- **Level dimensions are named by level type** (`isobaric`, `heightAboveGround`, …), not one
  `level`. A grid's group holds variables of several level types, and xarray needs each
  dimension to have one coordinate.
- **Names.** Short names come from a table generated from ecCodes 2.50 (`spec/tools/grib_names.py`).
  Probability, percentile and derived-ensemble products add a qualifier (`problt152p4`, `p90`,
  `ensmean`): NBM's fields otherwise collided.
- **Messages of several fields.** A field's chunk is a single-field message made by reference: a
  literal section 0, the field's sections, a literal `7777`. The arrays' codec then needs no
  `field` member, though the codec still accepts one.
- **Indexes.** NOAA's `n.k:offset` lines, for the fields of one message, are read too. NAM's
  index uses them.
- **Reads.** The store run now puts one object's spans in requests of at most 16, which the hosts
  make concurrently.
  Before, the hosts read one request's spans in sequence, so GFS's 743 heads took 110 s; now
  they take 17 s. This applies to every store format.
- **Determinism.** The Lambert projection uses the pure-Rust `libm` crate, so the native and
  wasm builds compute the same bits: the core's first new dependency for a profile.
- **CCSDS** is a port of gribberish's pure-Rust AEC decoder (item 7), with two fixes from libaec.
- **JPEG 2000** stays unpresented (item 12). The Neuroglancer fork's JPEG 2000 decoder is
  hayro-jpeg2000 (pure Rust), which the core could use for 5.40 later.
- **The corpus** reads ECMWF from its Google Cloud mirror: the AWS mirror answered 503 to this
  machine (item 11).
- **Not done: the demo.** Neuroglancer has no float64 data type, so the fork cannot show the
  arrays as they are. That needs float64 support in the fork's Zarr reader, or a GRIB map view.

## 1. Research summary

Sources read:

- **The format:** WMO-No. 306, *Manual on Codes*, Volume I.2, FM 92 GRIB
  edition 2, and its code and template tables
  ([wmo-im/GRIB2](https://github.com/wmo-im/GRIB2)).
- **Readers:** the source of [gribberish](https://github.com/mpiannucci/gribberish)
  1.9.0 (its Zarr codec, `gribberish/src/zarr.rs` and
  `python/gribberish/zarr/codec.py`), and VirtualiZarr's
  [GRIB parser docs](https://virtualizarr.readthedocs.io/en/latest/api/parsers/grib.html).
- **Indexers:** kerchunk's [`grib2.py`](https://fsspec.github.io/kerchunk/_modules/kerchunk/grib2.html),
  [gribscan](https://gribscan.readthedocs.io/en/latest/_sources/getting_started.md)
  and [grib-index-kerchunk](https://gh.hhj520.top/icpac-igad/grib-index-kerchunk).

Every message of eight public operational files was surveyed in October
2026 (the 2026-10-29 00z runs) with a script that reads each message's head
over HTTP range requests and walks its sections ([§1.2](#12-what-public-files-hold)).
Two messages were decoded with both eccodes 2.50 and gribberish 1.9.0:

- GFS precipitation (template 5.3);
- ECMWF 2 m temperature (template 5.42).

The two decoders agreed exactly, value for value, in float64. Findings
marked **[surveyed]** were checked against those files.

### 1.1 The format in one page

A GRIB2 file is a sequence of **messages**, with nothing between them
(though readers tolerate padding). Each message is self-contained:

| section | holds |
|---|---|
| 0, indicator (16 bytes) | `GRIB`, the discipline (octet 7), the edition (octet 8, `2`) and the message's total length (octets 9–16) |
| 1, identification | the originating center, the tables' versions, the reference time, the production status and the type of data |
| 2, local use (optional) | the center's own bytes |
| 3, grid definition | the number of points and a grid template (3.0 regular lat/lon, 3.30 Lambert conformal, …), with the shape of the Earth and the scanning mode |
| 4, product definition | a product template (4.0 analysis or forecast at a point in time, 4.1 its ensemble member, 4.8 a statistic over a time interval, …): the parameter (category and number, code table 4.2), the forecast time and its unit, the fixed surfaces of the level (type, scale factor and scaled value, code table 4.5) |
| 5, data representation | the number of packed values and a packing template: 5.0 simple, 5.2 complex, 5.3 complex with spatial differencing, 5.4 IEEE floating point, 5.40 JPEG 2000, 5.41 PNG, 5.42 CCSDS (AEC), and others (5.50/5.51 spectral, 5.61 simple with a logarithm, 5.200 run length) |
| 6, bitmap | the bitmap indicator (0: a bitmap follows, 254: the message's previous bitmap, 255: none), then one bit per grid point |
| 7, data | the packed values, for the points the bitmap marks |
| 8, end | `7777` |

Sections 2–7 may repeat within one message, so one message can hold several
**fields**. Sections 3–7 repeat, or only 4–7, or only 5–7, and each repeat
inherits the sections it does not restate. For every packing that stores
scaled integers `X`, a value is `Y = (R + X × 2^E) × 10^−D`, with:

- the reference value `R`, an IEEE binary32;
- the binary scale factor `E` and the decimal scale factor `D`, both signed
  integers in sign-and-magnitude form.

GRIB edition 1 differs in every section's layout and is not in this plan
([§11](#11-open-questions), item 9).

### 1.2 What public files hold

Every message of one forecast file of each of eight feeds, the 2026-10-29
00z runs **[surveyed]**:

| feed | messages | grid | packing | bitmaps | scanning mode | CORS |
|---|---|---|---|---|---|---|
| GFS 0.25° (`noaa-gfs-bdp-pds`) | 743 | 3.0, 1440 × 721 | 5.3 (740), 5.0 (3) | 34 | `0x00` | `*` |
| HRRR CONUS surface (`noaa-hrrr-bdp-pds`) | 173 | 3.30, 1799 × 1059 | 5.3 (161), 5.0 (12) | 10 | `0x40` | `*` |
| GEFS 0.5° member (`noaa-gefs-pds`) | 85 | 3.0, 720 × 361 | 5.3 | 4 | — | `*` |
| ECMWF IFS 0.25° (`data.ecmwf.int`) | 191 | 3.0, 1440 × 721 | 5.42 | 4 | `0x00` | `*` |
| NBM CONUS (`noaa-nbm-grib2-pds`) | 208 | 3.30, 2345 × 1597 | 5.3 (207), 5.2 (1) | 0 | `0x50` (193), `0x40` (15) | `*` |
| RAP 13 km (`noaa-rap-pds`) | 314 | 3.30, 451 × 337 | 5.40 | 8 | `0x40` | `*` |
| NAM (`noaa-nam-pds`) | 404 | 3.30 | 5.3 | 1 | — | `*` |
| ECMWF AIFS (AWS mirror) | — | — | — | — | — | — |

The AWS mirror of ECMWF's data answered `503 Slow Down` to the survey's
parallel reads; the same files on `data.ecmwf.int` were read instead. "—" in
the scanning mode column means the survey did not read it.

### 1.3 Prior art

- **VirtualiZarr** has read GRIB since v2.7 (June 2026) through
  gribberish's `GribberishParser`. Each GRIB message becomes one chunk, and
  reading a chunk "decodes the referenced message bytes through
  gribberish's registered zarr codec". Fields that do not fit one
  hypercube go to `open_virtual_datatree`. GRIB1 complex and spherical
  harmonic packing are unsupported.
- **gribberish** (Rust, MIT) decodes GRIB1 and GRIB2, with a Python
  binding, a JS/WASM build and Zarr v3 codecs for zarr-python and zarrs.
  - The codec is `{"name": "gribberish", "configuration": {"var",
    "adjust_longitude_range", "north_up"}}`, and its chunk is a whole
    message.
  - It decodes to float64 (or float32 when the array's type is float32).
    With `var` set to `latitude` or `longitude`, it computes that coordinate
    from the message instead.
  - JPEG 2000 comes through openjpeg-sys and PNG through `png` (both default
    features).
  - CCSDS comes through its own pure-Rust AEC decoder
    (`gribberish/src/utils/ccsds.rs`, about 30 KB), or through libaec-sys
    as an optional feature.
  - Its Python wheel decoded the survey's CCSDS message exactly as eccodes
    did.
- **kerchunk** (`scan_grib`) scans a file with eccodes and references whole
  messages, decoded by `kerchunk.codecs.GRIBCodec` with eccodes. Its
  `_grib_idx` module builds references from NOAA's `.idx` files instead of
  scanning, which is the approach of grib-index-kerchunk too.
- **gribscan** writes a JSON Lines index per file and reads it through a
  `rawgrib` codec.

Each of these chunks by whole message and decodes with a GRIB library.
None specifies the decoding apart from its implementation.

### 1.4 Findings that shaped the design

- **One field per message [surveyed].** No message of the 2,118 read holds
  more than one field. The format allows it, so the design handles it
  ([§2.2](#22-variables-which-fields-share-an-array)), but no public feed
  needs it now.
- **The header is small [surveyed].** Sections 0–6, up to the data section,
  are a median 0.03%–0.4% of a message. A whole-message chunk costs a
  reader almost nothing more than a data-section chunk. In exchange, the
  chunk carries its own grid, scanning mode, scaling and bitmap, so the
  codec needs no per-array configuration of them.
- **Scaling is per message.** `R`, `E`, `D` and the bit width differ from
  message to message of one array (GFS's most common widths run from 9 to 17 bits).
  Zarr codec configurations are per array, so the decoder must read them
  from the chunk. That is the main reason the chunk is the whole message
  rather than section 7.
- **Scanning modes vary [surveyed].** HRRR, RAP and NAM scan rows south to
  north (`0x40`). Most NBM fields also reverse every other row (`0x50`,
  boustrophedon). The decoder must put points in a fixed order, so it
  needs section 3, which is in the message.
- **Packing: four templates cover every feed [surveyed]:** 5.3 for NOAA
  (with 5.0 and 5.2), 5.42 for ECMWF, and 5.40 for RAP. Complex packing's
  missing-value management (octet 23 of 5.2/5.3) is in use: 21 GFS and 185
  NBM fields mark missing values in the packed data, without a bitmap.
- **Bitmaps:** 61 of the 2,118 fields carry one (land/sea-masked fields).
  None uses indicator 254, which only a multi-field message can.
- **Duplicates [surveyed].** GFS's f006 file holds `APCP` and `ACPCP`
  (accumulated precipitation, 0–6 h) twice each. The two messages of each
  pair have byte-identical sections 1–5, and eccodes decodes identical
  values from them. At later steps the pairs differ: one is the 6-hour
  bucket, the other is accumulated since the run started. A cell of a
  hypercube cannot hold both, so the arrangement must handle a repeated key
  ([§2.2](#22-variables-which-fields-share-an-array)).
- **Index files exist and are untrusted.** NOAA publishes `<file>.idx`
  (`n:offset:d=…:VAR:level:step:`). ECMWF publishes `<file>.index` (JSON
  Lines with `_offset` and `_length`). Either gives every message's offset
  in one request, where walking the file takes one sequential request per
  message. They are a hint: the profile verifies each offset by reading
  the message head it points to ([§4.2](#42-finding-the-messages)).
- **CORS [surveyed].** Every NOAA bucket and `data.ecmwf.int` answered
  with `Access-Control-Allow-Origin: *`, so the browser demo can read the
  files directly.
- **Compressed files can't be referenced.** MRMS (`.grib2.gz`) and DWD's
  open data (`.grib2.bz2`) are compressed as whole files. Their messages
  have no byte ranges, so they are out of scope, as any compressed file is.

## 2. The convention (Part 1 of `spec/zarrify/grib.md`)

### 2.1 Declaration and layout

The root declares the `grib` convention as every format's root does. As
with [SAFE](../../spec/zarrify/safe.md), the hierarchy is not an OME-Zarr
image: it is labeled arrays with GeoZarr georeferencing.

- **Root:** holds the source metadata (`refkv_source`,
  [§2.6](#26-source-metadata)).
- **One group per grid,** named `grid<k>` in the order each grid first
  appears. Most files have one grid; files that mix resolutions or domains
  have more.
- **In each grid group:**
  - its coordinate variables ([§2.3](#23-dimensions-and-coordinates));
  - its georeferencing ([§2.4](#24-grids-and-georeferencing));
  - one array per variable ([§2.2](#22-variables-which-fields-share-an-array)).

  xarray opens a grid group as a dataset, and the hierarchy as a DataTree.

[spec/zarrify.md §1.7](../../spec/zarrify.md#17-named-inputs-and-layouts)
says every output has the image layout at its root. GRIB has no image
layout, so the convention states the exception as SAFE's does
([§11](#11-open-questions), item 1).

### 2.2 Variables: which fields share an array

Two fields belong to one array when they share all of the following:

- **The grid:** byte-identical section 3, without its length and section
  number.
- **The product:** byte-identical section 4, except the octets of the
  dimensions in [§2.3](#23-dimensions-and-coordinates). Those are the
  forecast time, the fixed surfaces' scaled values, the ensemble member's
  number and a time interval's end. The template number is part of the
  product, so analysis and accumulation fields never share an array.
- **The discipline and section 1**, without the reference time.

This rule is generic. It needs no table of parameters to group fields, and
two fields that differ in any octet it does not name as a dimension are
never stacked together. A file whose fields all differ by one forecast step
or one pressure level stacks as cfgrib's would.

**Names.** An array is named by the parameter's short name, then the level
type, as `t_isobaricInhPa` or `2t_heightAboveGround`:

- **Short names** come from a table generated from eccodes' GRIB2
  definitions (Apache 2.0, the names cfgrib and ECMWF's tools use).
- **Level types** use eccodes' `typeOfLevel` names, from the same
  definitions.
- **A parameter or level type not in the table** is named by its numbers,
  as `d0_c1_p8_l1`.
- **Names that collide** take `_2`, `_3`, … in the order of their first
  message.

The table is pinned with the convention's version, and the name only labels
the array. The numbers that identify the parameter are always in its
attributes ([§2.6](#26-source-metadata)).

**Repeated keys.** A field whose array and dimension coordinates equal an
earlier field's can't share that cell, as with GFS's two `APCP` fields
([§1.4](#14-findings-that-shaped-the-design)). The second goes into a second
array, named as the first with `_2` added, and so on. No field is dropped,
and an array never mixes fields of different identity. A separate array is
better than a `copy` dimension: the copies are often different quantities
at later steps (a bucket and a running total), which a dimension would
present as one.

**Multi-field messages.** Each field is its own chunk. Its reference is the
whole message, and the codec's `field` member says which field
([§3](#3-the-refkvgrib-codec)).

### 2.3 Dimensions and coordinates

Every variable has the same dimensions, in this order:

`(time, step, level, number, y, x)`

| dimension | from | coordinate |
|---|---|---|
| `time` | section 1's reference time | seconds since 1970-01-01T00:00:00Z, `int64`, CF `units` and `calendar: "proleptic_gregorian"` |
| `step` | section 4's forecast time in its unit (code table 4.4); for a statistic (4.8, 4.11, …) the interval's start, with the interval's length an attribute, since it is part of the identity | seconds, `int64` |
| `level` | the first fixed surface's value, `scaled value × 10^−scale factor`; a layer (two fixed surfaces) gets `level_bounds` | float64, in the level type's unit (`Pa`, `m`, …) |
| `number` | the ensemble member's number (4.1, 4.11), for a variable whose template has one | `int64` |
| `y`, `x` | the grid ([§2.4](#24-grids-and-georeferencing)) | as [conventions §9](../../spec/conventions.md#9-dataset-layouts)'s coordinate variables |

Two rules keep the dimensions repeatable:

- **A dimension a variable's templates don't have is left out.** There is no
  `level` for a surface with no value, and no `number` outside ensembles.
- **A dimension the templates do have is kept, even at length 1.**

The second rule makes one file's output, and its neighbor's, stack along
`time` and `step` without reshaping. That is what the many-file layout
needs ([§11](#11-open-questions), item 8). It is not what cfgrib does:
cfgrib drops a length-1 dimension to a scalar coordinate.

**Chunks.** The chunk shape is `[1, 1, 1, 1, ny, nx]`, trimmed to the
variable's dimensions: one field per chunk. A dimension coordinate is
sorted ascending. A cell of the hypercube with no field, such as a level
missing at one step, has no chunk, and reads as the fill value.

**`valid_time`.** Every variable gets this coordinate over
`(time, step)`, holding `time + step`, as cfgrib's has.

### 2.4 Grids and georeferencing

| grid template | in version 0 | `y`, `x` coordinates | CRS |
|---|---|---|---|
| 3.0 regular lat/lon | yes | `latitude`, `longitude` in degrees, regular steps | geographic, on the Earth shape of code table 3.2 |
| 3.30 Lambert conformal | yes | `y`, `x` in meters from the projection's origin, regular steps | LCC from section 3, on that Earth shape |
| 3.1 rotated lat/lon, 3.10 Mercator, 3.20 polar stereographic | later, as the corpus needs them | as 3.30 | from section 3 |
| 3.40 regular Gaussian | later | irregular latitudes, regular longitudes | geographic |
| reduced grids (3.40 with a row list, 3.101 unstructured) | not presented ([§2.7](#27-what-is-not-presented)) | — | — |

- **Coordinates:** written in conventions §9's regular-steps form: a
  `uint32` index with `scale_factor` and `add_offset`, a few kilobytes
  whatever the grid's size.
- **Georeferencing:** each grid group carries SAFE's GeoZarr members:
  - `proj:wkt2` for the CRS (GRIB's Earth shapes are rarely an EPSG code:
    GFS's is a sphere of radius 6,371,229 m);
  - `spatial:transform`, the affine map from `(y, x)` indexes to the CRS.
- **2-D latitude and longitude for projected grids** are not written: they
  would be 30 MB of computed values for HRRR. A reader computes them from
  the CRS, as rioxarray does ([§11](#11-open-questions), item 5).

The codec outputs points in the grid's own order:

- `x` increasing along `+i` and `y` along `+j`;
- the scanning mode's reversed directions undone;
- alternate rows (`0x10`) re-reversed;
- adjacent-in-`j` storage (`0x20`) transposed.

The coordinates are written for that order, so a grid stored north to south
has a descending `latitude`. Reordering to north-up is a reader's choice,
as gribberish's `north_up` is ([§11](#11-open-questions), item 6).

### 2.5 Values, fill, bitmaps and missing values

- **Data type:** `float64` by default. It is what eccodes and gribberish
  decode to, so an output compares exactly with both. `R` is binary32, and
  the scaling `(R + X × 2^E) × 10^−D` is computed in binary64. Data type is
  open question [§11](#11-open-questions) item 3.
- **Fill value:** NaN.
- **Bitmaps:** a point that a field's bitmap leaves out is NaN.
- **Missing values in complex packing:** with missing-value management
  (5.2/5.3 octet 23 is `1` or `2`), the primary and secondary missing
  values both decode to NaN. Which one a point held is not kept
  ([§11](#11-open-questions), item 4).
- **Constant fields:** a field with a bit width of 0 is `R × 10^−D` at
  every point (or every point its bitmap marks).

### 2.6 Source metadata

Every byte of every message's sections 1–6 is kept, in the source's IR
mirror ([conventions §8](../../spec/conventions.md#8-the-ir-mirror)). The
mirror is one table, with one row per field holding:

- the message's offset and length, and the field's index within it;
- the discipline and edition;
- each section's template number and decoded octets (`schema/grib.json`).

Section 2's local-use bytes are kept as bytes. Section 6's bitmap is kept
as a range, not copied: it is in the chunk.

Each variable's attributes are:

- CF `units` and `long_name`, from the same eccodes-derived table as its
  name;
- `GRIB_discipline`, `GRIB_parameterCategory`, `GRIB_parameterNumber`,
  `GRIB_typeOfFirstFixedSurface` and `GRIB_productDefinitionTemplateNumber`.

These are the numbers that identify it, under cfgrib's `GRIB_` attribute
names, so that tools written for cfgrib's output find them.

### 2.7 What is not presented

A field the profile can't present is recorded in the mirror and left out of
the arrays. The file is not rejected, as the HDF5 profile does for
per-object gaps. Such a field is one with:

- a packing other than 5.0, 5.2, 5.3, 5.4, 5.41 or 5.42: spectral
  5.50/5.51, 5.61 and 5.200, and 5.40 until its decoder is decided
  ([§11](#11-open-questions), item 12);
- a grid not in [§2.4](#24-grids-and-georeferencing)'s version 0;
- a bitmap indicator from 1 to 253 (a predefined bitmap, which only the
  originating center can supply);
- a product template whose dimensions the convention doesn't name yet.

The summary counts them by reason.

## 3. The `refkv.grib` codec

A new section of [conventions.md](../../spec/conventions.md), beside
[§10's `refkv.stream`](../../spec/conventions.md#10-coded-streams).

**The codec.** `{"name": "refkv.grib", "configuration": C}` is an
array-to-bytes codec, with these members:

| member | type | presence | value |
|---|---|---|---|
| `field` | integer | optional | the field of the message to decode, from 0 in the order of the message's section 7s; 0 when absent |

There are no other members. The array's last two dimensions are the grid's
`(ny, nx)`, and its other dimensions have length 1 in the chunk.

**A chunk's bytes** are one GRIB edition 2 message: sections 0 to 8, as the
file holds them.

**Decoding a chunk:**

1. **Find the field.** Walk the sections, and take the `field`-th field's
   sections 3, 5, 6 and 7, each the last of its number at or before that
   field's section 7. A message that does not parse, or has too few
   fields, is an error.
2. **Unpack** the `n` values of section 5, by its template:
   - **5.0** simple;
   - **5.2** complex;
   - **5.3** complex with spatial differencing, order 1 or 2;
   - **5.4** IEEE binary32 or binary64, which is the value itself;
   - **5.40** JPEG 2000: section 7 is a JPEG 2000 code stream of one
     component, whose samples are `X`;
   - **5.41** PNG: section 7 is a PNG image, whose samples are `X`;
   - **5.42** CCSDS: section 7 is a CCSDS 121.0-B AEC stream with
     section 5's flags, block size and reference sample interval, whose
     samples are `X`.

   Apart from 5.4, each value is `(R + X × 2^E) × 10^−D` in binary64.
3. **Place.** With a bitmap (indicator 0, or 254 for the message's earlier
   one), the `n` values fill the bitmap's set points in order, and the
   others are NaN. Without one, `n` must equal the grid's point count.
4. **Order** the grid's points as [§2.4](#24-grids-and-georeferencing) says,
   then convert to the array's data type: `float64` as is, `float32`
   rounded to nearest.

A template the codec does not decode is an error. The profile never
references such a field ([§2.7](#27-what-is-not-presented)), so a reader
meets the error only in a hand-made archive.

The codec has no encoder.

**Decoders:**

- **Rust core:** `rust/refkv-ir/src/grib/decode.rs`, behind the existing
  `vz_*_decode` wasm export pattern and the Python codec class, as
  `refkv.fits_tiles` is.
  - The core depends on nothing but `miniz_oxide` and `serde_json`, and
    builds for wasm with no C toolchain. The decoder keeps it that way.
  - Simple and complex packing are a few hundred lines.
  - PNG is grayscale samples behind zlib, which `miniz_oxide` inflates.
  - CCSDS needs a pure-Rust AEC decoder ([§11](#11-open-questions), item 7).
  - JPEG 2000 has no decoder in the core. Its other JPEG 2000 chunks (TIFF,
    SAFE, DICOM) are decoded by the hosts' `imagecodecs_jpeg2k` codecs
    ([§11](#11-open-questions), item 12).
- **Neuroglancer fork:** gets `src/datasource/zarr/codec/refkv_grib/`, as it
  has `refkv_fits_tiles/`, calling the same wasm.

**Why not `gribberish`.** An archive could name gribberish's codec instead,
and VirtualiZarr's output would then interoperate with refkv's. Against
that:

- the codec is specified only by its implementation;
- its configuration describes presentation (`north_up`,
  `adjust_longitude_range`), not the bytes;
- it has no browser decoder that the fork can call without depending on
  gribberish's JS build.

Because refkv's chunk is the same whole message, a gribberish decoder can
still read refkv's references with a one-line change of codec name. The
verifier does that ([§8](#8-verifier)).

## 4. The profile (Part 2 of `spec/zarrify/grib.md`)

### 4.1 Choosing the profile

The file starts with `GRIB`, and octet 8 is `2`. A file whose first message
is edition 1 is rejected in version 0. An edition 1 message after an edition
2 message is recorded and not presented.

### 4.2 Finding the messages

- **With an index.** If `<url>.idx` (NOAA's form) or, for a `.grib2` URL,
  the same URL with `.index` in place of `.grib2` (ECMWF's form) exists, its
  offsets are read in one request. The probe of each is one request, and a
  missing index is not an error.
- **Without one.** The profile walks the file: read section 0, then jump by
  its total length, one request per message, in sequence.
- **Each message is verified, either way.**
  - Its head is read: the first 16 KiB, with more when sections 1–6 run
    past that.
  - It must start with `GRIB` and give a length that ends at the next
    message's offset, or the file's end.
  - Its last 4 bytes must be `7777`. Each head read starts 4 bytes before
    its message, so it holds the previous message's end. The last message's
    end is one more read.

An index that disagrees with the file is ignored and the file is walked,
so the output never depends on the index. Reading 16 bytes past each `7777`
tolerates padding between messages up to a limit; anything else between
them is an error.

### 4.3 Reading each message

From each head: sections 1–5 in full, and section 6's first 6 bytes. A
long bitmap is not read (it is in the chunk). From each field: its identity
([§2.2](#22-variables-which-fields-share-an-array)), its dimension
coordinates ([§2.3](#23-dimensions-and-coordinates)) and its grid
([§2.4](#24-grids-and-georeferencing)).

### 4.4 Checks and rejection

The file is rejected for:

- a message that doesn't parse, or whose sections overrun its length;
- an edition 1 file;
- an empty file;
- going over a budget: messages (2^20), fields per message (2^10), a
  grid's points (2^31), the dimensions' cells (2^32).

A field is left unpresented, and recorded, for the reasons of
[§2.7](#27-what-is-not-presented).

### 4.5 Chunks and the source table

One source, the file, with its pin from the first response's validators. A
chunk's reference is one `Range`: the message's offset and length. The
coordinate chunks are bytes entries (conventions §9). There are no literal
ranges.

### 4.6 Cost

To zarrify a file, the profile reads:

- the index (1 request) or the file's walk (`m` sequential requests);
- `m` heads, in parallel;
- the ends that the heads did not reach.

For GFS 0.25° that is about 745 requests and about 12 MB of the file's 528
MB. For ECMWF IFS it is about 193 requests and 3 MB of 98 MB. A reader of
one field reads one message. For GFS 0.25° messages, the median is 749 KB
and the largest is 1.6 MB; for ECMWF IFS, the median is 510 KB and the
largest is 1.1 MB. A point time series reads
every message it crosses, since no packing here can be read in parts
([§11](#11-open-questions), item 10).

## 5. Modules

- **`rust/refkv-ir/src/grib/`** (Rust core):
  - `sections.rs`: section and template parsing;
  - `walk.rs`: index probing, walking and verification;
  - `decode.rs`: the codec;
  - `tables.rs`: the generated names table;
  - `project/grib.rs`: the hierarchy;
  - `schema/grib.json`: the mirror's schema;
  - `profiles/grib.json`: the profile.
- **The names table:** generated by `spec/tools/grib_names.py` from a pinned
  eccodes release's `definitions/grib2/*.def` (short name, name and units
  per parameter, and `typeOfLevel` per surface type). It is checked in as
  generated Rust, with the eccodes version in a comment.
- **Hosts:**
  - registry entries;
  - detection in both hosts;
  - `python/src/refkv/zarr/codecs/grib.py` and its `zarr.codecs` entry
    point;
  - the wasm export and `js/src/embed.ts`;
  - the CLI;
  - the Neuroglancer fork's `refkv_grib` codec.
- **Docs:** `docs/zarrify/grib/README.md`, with an xarray example on GFS and
  one on HRRR. It also covers the browser demo, through the map viewer
  SAFE uses.

## 6. Tests

- **Rust, the decoder:** one test over every packing × bitmap × scanning
  mode fixture, which compares with values decoded by eccodes and checked
  in. Then one test per error:
  - a truncated message;
  - a bad `7777`;
  - a `field` past the last field;
  - an unsupported template;
  - a bitmap count that disagrees with `n`;
  - a 5.3 order other than 1 or 2.
- **Rust, the walk:** one test over files with and without an index, with
  padding, and with multi-field messages, checking that the offsets are
  equal. Then one test per rejection of [§4.4](#44-checks-and-rejection).
- **Python (`python/tests/ir/test_grib.py`):** one test that opens every
  fixture's output with xarray and compares each variable with cfgrib's
  values, at the same coordinates. Then one test per unpresented reason,
  checking the mirror's record.
- **JS:** the wasm decoder against the same expected values.

## 7. Fixtures

Written by `fixtures/generators/grib/write_fixtures.py` with eccodes, small
(16 × 12 grids), each changing one thing:

- the packings 5.0, 5.2, 5.3 (orders 1 and 2, missing-value management 0,
  1 and 2), 5.4, 5.40, 5.41 and 5.42;
- a bitmap, a bitmap reused through 254, and a constant field (bit width 0);
- scanning modes `0x00`, `0x40`, `0x80`, `0x50` and `0x20`;
- grids 3.0 and 3.30, and two grids in one file;
- a three-field message (sections 4–7 repeated, then 5–7);
- duplicate keys, as GFS's `APCP`;
- an ensemble (4.1) and a statistic (4.8);
- rejects: truncated, a bad length, edition 1, and padding past the limit.

A slice of each corpus feed's messages (3–5 messages, taken by range,
under 2 MB) is checked in too. They test the walk and the decoder on real
encoders' output, which eccodes' encoder alone doesn't cover.

## 8. Verifier

`conformance/zarrify/verify_grib.py`, for every corpus file:

- every presented variable's every chunk, decoded by refkv, must equal
  eccodes' values for that message, NaN where eccodes reports missing;
- the same chunk bytes, decoded by gribberish's codec, must give the same
  values;
- the dataset must equal cfgrib's
  (`xr.open_dataset(engine="cfgrib", backend_kwargs={"indexpath": ""})`),
  per `typeOfLevel`, up to the dimension order and length-1 dimensions.

eccodes installs from wheels (`eccodes` with `eccodeslib`, 2.50.0 on
CPython 3.11–3.14, not on free-threaded builds) as a verifier dependency.
It is never a runtime one.

## 9. Corpus

`conformance/zarrify/corpus_grib.txt`, one file per feed of
[§1.2](#12-what-public-files-hold), pinned by URL. The operational buckets
keep only recent runs, so the corpus names files from the archives that
keep them: NOAA's buckets, and ECMWF's AWS mirror
([§11](#11-open-questions), item 11).

## 10. Order of work

1. **Decoder:** `sections.rs` and `decode.rs` for 5.0, 5.2, 5.3 and 5.4,
   the scanning modes and bitmaps, and the fixture generator. Compared with
   eccodes. This is the riskiest part, and it is self-contained.
2. **CCSDS and PNG:** 5.42 (the AEC decoder), which gives ECMWF, and
   5.41. 5.40, which RAP needs, follows item 12's decision.
3. **Walk and projection:** index probing, verification, grouping, the
   dimensions and coordinates, duplicates, the mirror and the summary.
   `test_grib.py` against cfgrib.
4. **Selection and hosts:** registry, detection, CLI, the Python codec and
   entry point, the wasm export, `just ir-wasm`, `identical.py`.
5. **Neuroglancer and the demo:** the fork's `refkv_grib` codec, and a GFS
   and an HRRR example on the demo page, viewed on the map.
6. **Spec, schema, docs:** `spec/zarrify/grib.md`, the codec's section of
   conventions.md, `just profiles`, `just sections`, the docs page.
7. **Corpus run,** with the verifier, request counts and archive sizes
   recorded.

Steps 1–3 give xarray users GFS and HRRR. Step 2 adds ECMWF. Step 5 puts
them in the browser.

## 11. Open questions

These are recommended answers. The convention states whatever is decided.

1. **No image layout.** zarrify.md says every output's root is the image
   layout. **Recommendation:** GRIB states the exception as SAFE does: a
   GeoZarr hierarchy. The Neuroglancer fork opens its arrays as plain Zarr
   v3 arrays with dimension names.
2. **Fixed dimensions or cfgrib's.** **Recommendation:** fixed per template
   ([§2.3](#23-dimensions-and-coordinates)), length 1 kept, so that files
   of one feed stack. The verifier compares with cfgrib up to squeezing.
3. **`float64` or `float32`.** eccodes and gribberish decode to `float64`,
   and cfgrib presents `float32`. The packed values carry at most about 24
   bits, so `float32` keeps them, but the scaling's
   rounding then differs from eccodes in the last bit.
   **Recommendation:** `float64`, which compares exactly. The chunks are
   computed, not stored, so the doubled size costs memory on read only.
4. **Primary and secondary missing values.** Both become NaN, and which
   one a point held is lost. No surveyed field uses the secondary
   (management `2`). **Recommendation:** NaN for both in version 0. If a
   corpus file has secondaries, a `missing` member of the codec's
   configuration can select a second output, a mask array.
5. **2-D latitude and longitude for projected grids.** **Recommendation:**
   not written ([§2.4](#24-grids-and-georeferencing)). If readers need them,
   the codec can gain a `coordinate` member that computes them from the
   message's section 3, as gribberish's `var` does. That needs no stored
   bytes.
6. **North-up and −180–180° longitudes.** **Recommendation:** the grid's
   own order. A reordering codec, or a reader's flip, can present it
   otherwise. The data stays as the file has it, so "lose no scientific
   information" needs no proof.
7. **The AEC decoder.** The options are:
   - libaec (C, BSD-2), which would be the core's first C dependency, and
     its wasm build's;
   - gribberish's pure-Rust decoder (MIT), vendored with its notice;
   - one written from CCSDS 121.0-B.

   **Recommendation:** vendor gribberish's decoder, trimmed to decoding,
   and test it against eccodes on every fixture and corpus field. It
   already decodes ECMWF's fields as eccodes does.
8. **Many files: the forecast archive.** The cube with the most value is
   reference time × step over a prefix of NOAA's buckets.
   **Recommendation:** after this profile, as a store input profile
   ([spec/zarrify.md §1.4](../../spec/zarrify.md#14-store-inputs)) that
   lists the prefix and stacks each file's arrays along `time` and `step`.
   The question that profile must answer is cost: verifying every head of
   every file is this profile's cost times the number of files, and trusting
   the index files instead gives up the check. This plan's fixed dimensions
   are its precondition.
9. **GRIB edition 1.** None of the surveyed feeds use it; older archives
   do. **Recommendation:** a later step with its own sections parser, sharing
   the decoder's unpacking (edition 1's simple packing is the same
   formula).
10. **Point time series.** A whole-field chunk makes them read every field.
    Complex packing with spatial differencing decodes sequentially, so no
    subset of it can be read alone. CCSDS's reference sample intervals
    could be read in parts in principle.
    **Recommendation:** out of scope. Rechunking is a copy, not a
    reference.
11. **Corpus permanence.** `data.ecmwf.int` keeps only the last few days
    (a run from 5 days before the survey was gone). Its AWS mirror,
    `ecmwf-forecasts`, still holds 2024's runs, and NOAA's HRRR bucket holds
    runs back to 2016. **Recommendation:** pin the corpus to those archives,
    and copy one message per ECMWF fixture into `fixtures/grib/` under ECMWF's
    open data license (CC BY 4.0).
12. **JPEG 2000 (5.40).** Of the surveyed feeds, only RAP uses it. The
    options are:
    - a pure-Rust JPEG 2000 decoder in the core;
    - letting `refkv.grib` call the host's JPEG 2000 decoder (imagecodecs
      in Python, the demo's openjpeg wasm in the browser), which makes the
      codec's decoding depend on the host;
    - for 5.40 fields only, referencing section 7 alone with a JPEG 2000
      codec, then a scaling codec that reads `R`, `E` and `D` from a literal
      range. That breaks the one-codec rule and needs a scaling codec that
      takes per-chunk parameters.

    **Recommendation:** leave 5.40 unpresented in version 0, and decide
    when a corpus feed other than RAP needs it.

    **Decided** (after version 0): the first, hayro-jpeg2000, for the
    reversible 5/3 codestreams of up to 20 bits that RAP, CFSv2, GFS-Wave,
    GEFS-Wave and the NAM Puerto Rico and Hawaii nests write (JasPer, lossless,
    at most 19 bits on 2026-10-29); [conventions §12](../../spec/conventions.md#12-grib-messages) says why 9/7 is not
    decoded.
