# GRIB

The Zarr layout of a GRIB edition 2 file: the gridded fields of a weather or climate model's
output, as labeled arrays with CF coordinates and GeoZarr georeferencing. What all of refkv's
conventions share is in [spec/conventions.md](../conventions.md), cited here as "conventions §n".
How refkv produces this layout as a reference store, which files it reads and which inputs it
rejects, is the GRIB profile, [Part 2](#part-2-the-profile) below. The format is WMO's FM 92
GRIB edition 2, in WMO-No. 306, *Manual on Codes*, Volume I.2, and its code and template tables
([wmo-im/GRIB2](https://github.com/wmo-im/GRIB2)), cited here as "FM 92"; a template is named by
its number, as 3.30 (grid definition template 30), and an octet by its number in its section,
from 1.

Convention version: 0 (until release, [conventions §1](../conventions.md#1-conventions)) · UUID: `c9bbe039-8dfa-49f9-a655-756f822a42d6` ·
Schema: [schema.json](grib/schema.json) · Added 2026-10-30.

This document has two parts ([conventions §11.1](../conventions.md#111-parts)).

Like MP3, HDF5 and NumPy, GRIB's fields are not images: the hierarchy holds no OME-NGFF metadata.
As with [SAFE](safe.md), it is georeferenced with the GeoZarr conventions. The values stay in the
file: each chunk is one field's message, decoded by the codec `refkv.grib`
([conventions §12](../conventions.md#12-grib-messages)).

# Part 1. The convention

## Layout

The hierarchy's entries ([conventions §11.5](../conventions.md#115-layout-tables)):

| prefix | type | structure | description |
|---|---|---|---|
| `zarr.json` | group | [`GribRootGroup`](#type-gribrootgroup) | the root ([§3](#3-the-hierarchy)) |
| `zarr.json#/attributes/zarr_conventions` | attribute | array of [`ConventionMetadata`](../conventions.md#type-conventionmetadata) | the CMO of [§1](#1-declaration) |
| `zarr.json#/attributes/refkv_zarrified` | attribute | [`RootProperty`](../conventions.md#type-rootproperty) | `profile` `"grib"`, `version` `0`, `source.url` the GRIB file's URL (the object URL of its key in the store, [spec/zarrify.md §1.4](../zarrify.md#14-store-inputs)) |
| `zarr.json#/attributes/refkv_zarrified/grib` | attribute | [`GribRoot`](#type-gribroot) | the root's source metadata ([§5.1](#51-the-root)) |
| `grid<g>/zarr.json` | group | [`GribGridGroup`](#type-gribgridgroup) | grid `g`'s group, its georeferencing ([§4](#4-georeferencing)) |
| `grid<g>/<variable>/zarr.json` | array | [`GribVariable`](#type-gribvariable) | a variable ([§3.1](#31-variables)) |
| `grid<g>/<variable>/c/<coords>` | chunk | [`GribChunk`](#type-gribchunk) | a field ([§8.5](#85-chunks)) |
| `grid<g>/<coordinate>/zarr.json`, `grid<g>/<coordinate>/c/<k>` | array, chunk | [`GribCoordinate`](#type-gribcoordinate) | a coordinate ([§3.3](#33-coordinates)) |
| `refkv_source/**` | subtree | [the mirror's entries](../conventions.md#table-mirror) | the IR mirror, a store IR ([§5.2](#52-the-ir-mirror)): its elements are [§5.3](#53-elements)'s; an index file, held, is under `refkv_source/ir/held/**` |

| placeholder | value |
|---|---|
| `<g>` | the grid's number, from 0, in the order of the grids' first presented fields |
| `<variable>` | a variable's name ([§3.2](#32-names)) |
| `<coordinate>` | `time`, `step`, `valid_time`, `number`, a level dimension's name and its `_bounds`, and the grid's two dimensions ([§3.3](#33-coordinates)) |
| `<coords>` | the field's coordinates along the variable's dimensions, in decimal, one key segment each, the grid's two always `0/0` |

## 1. Declaration

The root and `refkv_source` declare the convention
([conventions §11.2](../conventions.md#112-declaration)), the root with the GRIB file's URL as
`source.url` and its source metadata of [§5.1](#51-the-root) as the member `"grib"`. Its CMO is:

```json
{
  "uuid": "c9bbe039-8dfa-49f9-a655-756f822a42d6",
  "schema_url": "https://raw.githubusercontent.com/d-v-b/refkv/refs/heads/main/spec/zarrify/grib/schema.json",
  "spec_url": "https://github.com/d-v-b/refkv/blob/main/spec/zarrify/grib.md",
  "name": "refkv_zarrified",
  "description": "The Zarr layout of a GRIB source zarrified by refkv, and the source's metadata"
}
```

## 2. The source

### 2.1 Messages and fields

A GRIB file is a sequence of **messages**. A message is section 0 (16 octets: `GRIB`, two
reserved octets, the discipline, the edition, `2`, and the message's length, octets 9–16), the
sections 1 to 7, and section 8 (`7777`). Each section starts with its length (4 octets) and its
number (1 octet). A message describes one or more **fields**: after a field's section 7, the
message may restate sections 2 to 7, 3 to 7 or 4 to 7 for the next (FM 92 regulation 92.1.3).
A field's sections are the last of each number at or before its section 7.

| section | what this convention reads of it |
|---|---|
| 1, identification | the reference time (octets 13–19); the rest identifies the field |
| 2, local use | nothing (it is kept in the mirror) |
| 3, grid definition | the grid ([§2.4](#24-grids)) |
| 4, product definition | the parameter, the surfaces, the step, the ensemble member and the statistic ([§2.2](#22-identity), [§2.3](#23-dimensions)) |
| 5, data representation | the number of values and the packing template |
| 6, bitmap | the bitmap indicator (octet 6) |
| 7, data | nothing (the codec decodes it) |

### 2.2 Identity

A field's **identity** is the octets that say what it is, all but those that say where it lies
along the dimensions of [§2.3](#23-dimensions): the discipline; section 1 from octet 6, but the
reference time's octets; and section 4 from octet 6, with the octets of the unit and forecast
time (18–22), the surfaces' scale factors and values (24–28, 30–34), an ensemble member's
number, and a statistic's end time and its count of missing values set to 0. A variable's
fields have one identity and one grid, and each of the two surfaces either has a value in every
field or in none ([§3.1](#31-variables)).

### 2.3 Dimensions

Section 4's template must be one of these, each with octets 10–34 of template 4.0 (the
parameter's category and number, octets 10–11; the unit of time, 18; the forecast time, 19–22;
the first surface's type, scale factor and value, 23–28; the second's, 29–34):

| template | the member's number | the statistic: its end time, count of time ranges, missing values, process, unit and length |
|---|---|---|
| 4.0, 4.2, 4.5, 4.6 | — | — |
| 4.1 | octet 36 | — |
| 4.8 | — | 35–41, 42, 43–46, 47, 49, 50–53 |
| 4.9 | — | 48–54, 55, 56–59, 60, 62, 63–66 |
| 4.10 | — | 36–42, 43, 44–47, 48, 50, 51–54 |
| 4.11 | octet 36 | 38–44, 45, 46–49, 50, 52, 53–56 |
| 4.12 | — | 37–43, 44, 45–48, 49, 51, 52–55 |

A field's coordinates are:

- **time**: the reference time, in seconds since 1970-01-01T00:00:00Z;
- **step**: for a template without a statistic, the forecast time in seconds (its unit, code
  table 4.4: a minute, an hour, a day, 3, 6 or 12 hours, or a second); for one with a statistic,
  the statistic's end less the reference time, in seconds;
- **level**: the first surface's value, `value × 10^−factor` (the factor a signed octet, the
  value 4 unsigned octets; none when the type is 255, the factor `0xff` or the value all ones),
  and the second's, as the level's bound;
- **number**: the ensemble member's number, for 4.1 and 4.11.

A statistic must have one time range (its count, 1), and its length is part of the variable's
name ([§3.2](#32-names)).

### 2.4 Grids

Section 3 must define its grid by a template (octet 6, 0) with no list of points per row (octet
11, 0), of one of these templates, whose `Ni` × `Nj` is its number of points (octets 7–10). Two
grids are one when their sections 3 are equal from octet 6 but the scanning mode's bits 3 and 4
(`0x20`, `0x10`), which change the order of the points in the file, not in the decoded field:

| template | grid | `Ni`, `Nj` | scanning mode | the first point, the increments |
|---|---|---|---|---|
| 3.0 | regular latitude/longitude | octets 31–34, 35–38 | octet 72 | `La1`, `Lo1` (47–50, 51–54); `Di`, `Dj` (64–67, 68–71) when flag table 3.3's bits 3 and 4 (octet 55) say they are given, else from the last point (`La2`, `Lo2`, 56–59, 60–63) |
| 3.30 | Lambert conformal | octets 31–34, 35–38 | octet 65 | `La1`, `Lo1` (39–42, 43–46); `Dx`, `Dy` in mm (56–59, 60–63); `LaD`, `LoV` (48–51, 52–55), `Latin1`, `Latin2` (66–69, 70–73) |

Angles are in 10^−6 degree, or for 3.0 in units of the basic angle and its subdivisions (octets
39–46) when both are given. The scanning mode's bit 1 (`0x80`) runs `i` from east to west, and
bit 2 (`0x40`) runs `j` from south to north; an increment takes the sign of its direction. The
Earth (code table 3.2, octets 15–30) must be a sphere or an ellipsoid of shape 0 to 8 for 3.30,
whose projection must not be bipolar (octet 64, bit 2).

### 2.5 Values

A field's values are those `refkv.grib` decodes from its message
([conventions §12](../conventions.md#12-grib-messages)): binary64, NaN where its bitmap or its
packing says a value is missing, in the grid's row order. Its packing (section 5's template)
must be one the codec decodes: 5.0, 5.2, 5.3, 5.4, 5.40, 5.41 or 5.42. The profile reads no
more of section 7 than its header, so a 5.40 field whose codestream the codec does not decode
(the 9/7 transform, more than 20 bits) is presented, and its chunk fails to decode.

### 2.6 What is not presented

A field that this convention cannot present is recorded in the root's source metadata
([§5.1](#51-the-root)) with its reason, and is not in any variable. Its bytes stay in the
mirror.

| reason | the field's |
|---|---|
| `grid_source` | grid is not defined by a template (section 3 octet 6) |
| `reduced` | grid lists the points of each row (section 3 octet 11) |
| `grid` | grid template is not 3.0 or 3.30 |
| `grid_size` | `Ni` × `Nj` is not its number of points |
| `earth` | Lambert grid's Earth is not of shapes 0–8, or its projection is bipolar |
| `product` | product template is not one of [§2.3](#23-dimensions)'s |
| `statistic` | statistic has more than one time range |
| `time` | step is not a whole number of seconds (a month, a year) |
| `packing` | packing template is not one the codec decodes (such as the spectral 5.50 and 5.51, or 5.61, simple packing of logarithms) |
| `bitmap` | bitmap is predefined (indicator 1–253), or reuses a bitmap the message has not given (254) |
| `values` | number of values is not its grid's number of points, with no bitmap |

## 3. The hierarchy

The root has one group per grid, `grid<g>`, of the presented fields of the grids, numbered from
0 in the order of the grids' first fields (two grids are one as [§2.4](#24-grids) says). A grid's
group holds its variables and its coordinates. xarray opens a grid's group as
a dataset (`xr.open_zarr(store, group="grid0")`), and the hierarchy as a DataTree.

### 3.1 Variables

A grid's presented fields are its **variables'**, by identity ([§2.2](#22-identity)), each
variable in the order of its first field. A variable is a `float64` array of NaN fill whose
dimensions are, in order:

- `time` and `step`, always;
- its level dimension ([§3.2](#32-names)), when its fields' first surfaces have values;
- `number`, when its template has an ensemble member;
- the grid's `y` and `x` (`latitude` and `longitude` for 3.0).

Its shape is its group's coordinates' lengths along its dimensions, its chunk shape 1 along each
but the grid's two, which it takes whole; its codecs are `[{"name": "refkv.grib"}]`. A chunk is a
field at its coordinates; a cell no field has has no chunk, and reads as NaN.

**Repeated fields.** A field whose cell another field of its identity already holds (a file may
hold two fields with the same sections 1 to 5, as NCEP's GFS does) goes to the next **copy**: the
first field of each cell is in the variable, the second in a second variable named as the first
with `_2`, and so on. No field is dropped.

A variable's attributes are `long_name` and `units`, when [§3.2](#32-names)'s table names its
parameter; `coordinates`, `"valid_time"`; and `GRIB_centre`, `GRIB_discipline`,
`GRIB_parameterCategory`, `GRIB_parameterNumber`, `GRIB_productDefinitionTemplateNumber`,
`GRIB_typeOfFirstFixedSurface` and `GRIB_typeOfSecondFixedSurface`, the numbers that identify
it, and for a statistic `GRIB_typeOfStatisticalProcessing` and `GRIB_lengthOfTimeRange` (its
length in seconds, or null), under cfgrib's names.

<a id="type-gribvariable"></a>**`GribVariable`**: an [`ArrayDocument`](../conventions.md#type-arraydocument)
with the data type `float64`, the fill value `"NaN"`, the codecs above, the `dimension_names` of
its dimensions and the attributes above.

### 3.2 Names

A variable's name is its parameter's **short name**, then `_` and its level type's name (unless
its first surface's type is 255), then `_` and its qualifier, if any, then, for a statistic, `_`, the process's name (code table
4.10: `avg`, `accum`, `max`, `min`, `diff`, `rms`, `sd`, `cov`, `diffStartEnd`, `ratio`,
`anomaly`, `sum`, else `stat<n>`) and its length (`<h>h`, `<m>min` or `<s>s`, the first that
divides it), as `t_isobaric`, `tp_surface_accum6h`. A name an earlier variable or coordinate of
the group has takes `_2`, `_3`, … .

- **Short names** are those of the table `rust/refkv-ir/src/grib/names_table.rs`, generated by
  `spec/tools/grib_names.py` from ecCodes 2.50.0's definitions (Apache 2.0), by the centre, the
  discipline, the category and the number: the centre's own entry (NCEP's, 7, and ECMWF's, 98,
  where ecCodes names a parameter otherwise than WMO), else WMO's. A parameter it does not name
  is `d<discipline>_c<category>_p<number>`.
- **Qualifiers** follow the level type for a product that is a probability (4.5, 4.9:
  `prob` and, by code table 4.9, `lt<lower>`, `gt<upper>`, `<lower>to<upper>`, `gt<lower>` or
  `lt<upper>`, a limit's shortest decimal with `p` for its point and `m` for a minus), a
  percentile (4.6, 4.10: `p<percentile>`) or a derived forecast (4.2, 4.12, code table 4.7:
  `ensmean`, `enswmean`, `enssd`, `enssdn`, `ensspread`, `enslai`, `enscmean`, `ensiqr`, `ensmin`,
  `ensmax`, else `ens<code>`).
- **Level types** are named by this convention's table of code table 4.5
  (`rust/refkv-ir/src/grib/names.rs`): `surface`, `isobaric` (in Pa), `heightAboveGround`,
  `depthBelowLand`, and so on, and NCEP's local types for its centre, 7; another is
  `level<type>`. A layer of two surfaces of one type is the type's name with `Layer`; of two
  types, the first's name, `To`, and the second's, capitalized.

### 3.3 Coordinates

A grid's group holds these coordinates, each a 1-D array named as its dimension but
`valid_time` and the bounds:

| coordinate | values | data type | attributes |
|---|---|---|---|
| `time` | the group's fields' reference times, ascending, each once | `float64` | `units` `"seconds since 1970-01-01T00:00:00Z"`, `calendar` `"proleptic_gregorian"`, `standard_name` `"forecast_reference_time"` |
| `step` | their steps, ascending, each once | `float64` | `units` `"seconds"`, `standard_name` `"forecast_period"` |
| `valid_time` | `time + step`, over `(time, step)` | `float64` | as `time`'s, `standard_name` `"time"` |
| `number` | the members' numbers, ascending, when a variable has them | `float64` | `standard_name` `"realization"` |
| a level dimension | the first surfaces' values of the variables that have it, ascending by the first value then the second, each pair once | `float64` | `units` (Pa, m, K, …) when the level type has one, `long_name` the dimension's name, and `bounds` for a layer |
| `<level>_bounds` | for a layer, `[first, second]` of each value, over (the level, `bounds`) | `float64` | — |
| `latitude`, `longitude` or `y`, `x` | the grid's positions ([§4](#4-georeferencing)) | `uint32` | `units`, `standard_name`, `scale_factor`, `add_offset` |

The `float64` coordinates are one chunk each, little-endian and deflated (gzip, level 6). The
grid's two are the index of [conventions §9](../conventions.md#9-dataset-layouts)'s regular steps
([conventions §9](../conventions.md#9-dataset-layouts)), whose `scale_factor` and `add_offset`
give the positions.

<a id="type-gribcoordinate"></a>**`GribCoordinate`**: an [`ArrayDocument`](../conventions.md#type-arraydocument)
of the table's row.

## 4. Georeferencing

A grid's group carries the GeoZarr `proj` and `spatial` conventions' members, as SAFE's
resolution groups do ([spec/zarrify/safe.md §5](safe.md#5-georeferencing-geozarr)).

<a id="type-gribgridgroup"></a>**`GribGridGroup`**, its attributes:

| member | value |
|---|---|
| `zarr_conventions` | the CMOs of `proj` and `spatial` |
| `proj:wkt2` | the CRS: for 3.0, a geographic CRS on the grid's Earth; for 3.30, a Lambert Conic Conformal (2SP) CRS on it, its false origin at (`Latin1`, `LoV`), its standard parallels `Latin1` and `Latin2` |
| `spatial:dimensions` | `["latitude", "longitude"]` for 3.0, `["y", "x"]` for 3.30 |
| `spatial:transform` | `[dx, 0, x0, 0, dy, y0]`: for 3.0, `dx` the longitude's increment, `x0` `Lo1`, `dy` the latitude's increment and `y0` `La1`, in degrees; for 3.30, `Dx` and `Dy` in meters and `(x0, y0)` the first point projected (Snyder, *Map Projections: A Working Manual*, 1987, equations 15-1 to 15-10), in binary64 with musl's `sin`, `cos`, `tan`, `log`, `pow` and `sqrt` (as the Rust crate `libm` 0.2 implements them, so that every implementation computes the same bits) in the order of `lambert` in `rust/refkv-ir/src/project/grib.rs` |
| `spatial:shape` | `[Nj, Ni]` |
| `spatial:registration` | `"node"`: the positions are grid points |

The grid's positions are its `y` and `x` coordinates: `y0 + j × dy` and `x0 + i × dx`. The
values are in the file's order ([§2.5](#25-values)): a grid that scans north to south has a
descending latitude.

## 5. Source metadata

### 5.1 The root

<a id="type-gribroot"></a>**`GribRoot`**, the root's member `"grib"`:

| member | type | value |
|---|---|---|
| `messages` | integer | the file's messages |
| `fields` | integer | their fields |
| `index` | string or null | the key of the index file the run read, when the file agreed with it ([§8.2](#82-finding-the-messages)) |
| `unpresented` | array of [`GribUnpresented`](#type-gribunpresented) | the fields not presented, in the file's order |

<a id="type-gribunpresented"></a>**`GribUnpresented`**: `message` and `field` (each from 0), and
`reason`, one of [§2.6](#26-what-is-not-presented)'s.

<a id="type-gribrootgroup"></a>**`GribRootGroup`**: the root group, its attributes
`zarr_conventions` and `refkv_zarrified` alone.

### 5.2 The IR mirror

The source's IR is a store IR ([conventions §8.9](../conventions.md#89-stores)): the GRIB file, described in place, and an index
file the run read, held. Every message's sections 0 to 6, and their bytes, are in it.

### 5.3 Elements

The elements, by path below the GRIB file's object (`<file>`) or an index file's (`<index>`):

| path | kind | type or form | extent |
|---|---|---|---|
| `<file>/messages/<i>` | struct | — | message `i` |
| `<file>/messages/<i>/indicator` | value | `{magic:ascii[4],reserved:bytes[2],discipline:u1,edition:u1,length:>u8}` | section 0 |
| `<file>/messages/<i>/sections/<j>` | value | `{length:>u4,number:u1,body:bytes[n − 5]}` | the message's `j`-th section after section 0, from 0, when it is a section 1 to 5 |
| `<file>/messages/<i>/sections/<j>` | value | `{length:>u4,number:u1,indicator:u1}` | a section 6 with no bitmap |
| `<file>/messages/<i>/sections/<j>/header`, `…/bitmap` | value, data | the header's type above; `{geometry: {bits}, codec: {bitmap: "msb first"}, recipe: [[src, 0, null]]}` | a section 6 with a bitmap |
| `<file>/messages/<i>/sections/<j>` | data | `{geometry: {points, values}, codec: {packing, field}, recipe: [[src, 0, null]]}` | a section 7 |
| `<file>/messages/<i>/end` | value | `ascii[4]` | section 8 |
| `<file>/gaps/<offset>` | gap | — | bytes between messages, or after the last, that are no message ([§8.2](#82-finding-the-messages)) |
| `<index>/text` | value | `ascii[n]` | an index file's text |

## 6. Example

A GFS file of three fields: two `APCP` (total precipitation, accumulated over 6 hours) with the
same sections 1 to 5, and `ICEC`, on the 0.25° grid:

```
zarr.json
grid0/zarr.json            proj:wkt2, spatial:transform [0.25, 0, 0, 0, -0.25, 90]
grid0/time, step, valid_time
grid0/surface              [0.0]
grid0/latitude             721 values, 90 to -90
grid0/longitude            1440 values, 0 to 359.75
grid0/tp_surface_accum6h   (time, step, surface, latitude, longitude) = (1, 1, 1, 721, 1440)
grid0/tp_surface_accum6h_2 the second APCP, a copy
grid0/ci_surface           (1, 1, 1, 721, 1440)
```

## 7. What the layout does not express

- GRIB edition 1, and the fields of [§2.6](#26-what-is-not-presented).
- 2-D latitudes and longitudes of a projected grid: a reader computes them from the CRS.
- A field's position in a file of many fields as a dimension of its own: the many files of a
  forecast run, stacked along `time` and `step`, are a later profile's.
- Which of complex packing's two missing values a missing value was: both are NaN.

# Part 2. The profile

The GRIB profile reads a GRIB edition 2 file, and the index beside it when there is one, and
writes the layout of Part 1.

## 8. GRIB profile

This profile follows no earlier revision of spec/zarrify.md. A GRIB file is read as a store
([spec/zarrify.md §1.4](../zarrify.md#14-store-inputs)): its objects are the GRIB file, the input, and the index files found
beside it.

### 8.1 Choosing the profile

A file whose first 4 bytes are `GRIB` and whose eighth is 2 is GRIB's (the registry's `sniff`
order, [spec/zarrify.md §1.2](../zarrify.md#12-input)).

### 8.2 Finding the messages

The run probes for two index files beside the GRIB file: `<name>.idx` (NOAA's: lines
`n:offset:…`, or `n.k:offset:…` for each field of a message of several) and, when the name ends in `.grib2`, `.grb2`, `.grib` or `.grb`, the name with that
suffix replaced by `.index` (ECMWF's: JSON lines with `_offset`). It reads one found, of at most
16 MiB, whole. When an index lists offsets, the first 0 and all within the file, the run reads the
first 16 KiB of each listed message, from 4 bytes before it, at once; else, or from the first
offset where the file and the index disagree, it walks the file: 64 KiB from where the last
message ended, a message's length taking it to the next. Every message is read and checked
([§8.4](#84-checks-and-rejection)) whichever way it was found, so an index changes the reads and
not the output, but for [§5.1](#51-the-root)'s `index` and the mirror's held file.

Bytes where a message is due that do not start with `GRIB` are padding: the bytes to the next
`GRIB`, or to the file's end, which must be at most 4096 bytes.

### 8.3 Reading each message

Of each message, the run reads sections 0 to 5 whole, the first 6 octets of each section 6, and
the first 5 of each section 7, reading again where a head did not hold them.

### 8.4 Checks and rejection

The schema's roles (`rust/refkv-ir/schema/grib.json`) are checked in this order: the file is at
least 16 bytes; each message's indicator is whole, of edition 2, of a length at least 21 that ends
within the file; it ends with `7777`; each section is at least 5 octets and ends before section
8; a section 1 to 5 is at most 1 MiB; the sections are in FM 92's order, and those the profile
reads parse; padding is as [§8.2](#82-finding-the-messages) says; there is a message; and at least
one field is presented. A field that is not presented ([§2.6](#26-what-is-not-presented)) does
not reject the file.

### 8.5 Chunks

<a id="type-gribchunk"></a>**`GribChunk`**: a field's chunk is a message that holds that field
alone:

- the field's message, one range of the GRIB file, when it holds no other field;
- else a message made of the field's sections: a literal section 0 (`GRIB`, two zero octets, the
  discipline, `2`, and the new length), the ranges of its sections 1 to 7 (a reused bitmap's
  section 6 in place of its own), and a literal `7777`.

The output's one source is the GRIB file ([spec/zarrify.md §1.4](../zarrify.md#14-store-inputs)); an index is no source.

### 8.6 Summary

The run's summary: `messages`, `fields`, `grids`, `arrays` (each variable's `path`, `shape` and
count of `fields`), `chunks`, `unpresented` (the count of each reason), `index`, and the store
run's counts.
