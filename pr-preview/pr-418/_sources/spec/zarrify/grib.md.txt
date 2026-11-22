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
Schema: [schema.json](grib/schema.json)

Changes: [changes/grib/](changes/grib/), one file per change.

Added 2026-10-30.

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
| `zarr.json#/attributes/refkv_zarrified` | attribute | [`RootProperty`](../conventions.md#type-rootproperty) | `profile` `"grib"`, `version` `0`, `source.url` the GRIB file's object URL ([spec/zarrify.md §1.8](../zarrify.md#18-files-read-as-stores)) |
| `zarr.json#/attributes/refkv_zarrified/grib` | attribute | [`GribRoot`](#type-gribroot) | the root's source metadata ([§5.1](#51-the-root)) |
| `grid<g>/zarr.json` | group | [`GribGridGroup`](#type-gribgridgroup) | grid `g`'s group, its georeferencing ([§4](#4-georeferencing)) |
| `grid<g>/<variable>/zarr.json` | array | [`GribVariable`](#type-gribvariable) | a variable ([§3.1](#31-variables)) |
| `grid<g>/<variable>/c/<coords>` | chunk | [`GribChunk`](#type-gribchunk) | a field ([§8.5](#85-chunks)) |
| `grid<g>/<coordinate>/zarr.json`, `grid<g>/<coordinate>/c/<k>` | array, chunk | [`GribCoordinate`](#type-gribcoordinate) | a coordinate ([§3.3](#33-coordinates)) |
| `refkv_source/**` | subtree | [the mirror's entries](../conventions.md#table-mirror) | the IR mirror, a store IR ([§5.2](#52-the-ir-mirror)): its elements are [§5.3](#53-elements)'s; the index files, held, are under `refkv_source/ir/held/**` |

| placeholder | value |
|---|---|
| `<g>` | the grid's number, from 0, in the order of the grids' first presented fields |
| `<variable>` | a variable's name ([§3.2](#32-names)) |
| `<coordinate>` | `time`, `step`, `valid_time`, `number`, a level dimension's name and its `_bounds`, the grid's dimensions that have coordinates, and a reduced grid's `latitude` and `longitude` ([§3.3](#33-coordinates)) |
| `<coords>` | the field's coordinates along the variable's dimensions, in decimal, one key segment each, each of the grid's always `0` |

## 1. Declaration

The root and `refkv_source` declare the convention
([conventions §11.2](../conventions.md#112-declaration)), the root with the GRIB file's object URL as
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

A field is presented only when section 4's template is one of these (otherwise it is not
presented, `product`, [§2.6](#26-what-is-not-presented)), each with octets 10–34 of template 4.0 (the
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
- **level**: the first surface's value, `value × 10^−factor` (the factor a signed octet, its
  high bit the sign, the value 4 unsigned octets; in binary64, `value / 10^factor` for a factor
  of at least 0 and `value × 10^−factor` otherwise, the power of 10 a product of tens, each
  multiplication rounded; none when the type is 255, the factor `0xff` or the value all ones),
  and the second's, as the level's bound;
- **number**: the ensemble member's number, for 4.1 and 4.11.

A field whose statistic has more than one time range (its count, not 1) is not presented
(`statistic`); a statistic's length is part of the variable's name ([§3.2](#32-names)).

### 2.4 Grids

A field is presented only when its section 3 defines its grid by a template (octet 6, 0) of one
of these, whose `Ni` × `Nj` is its number of points (octets 7–10), with no list of points per row
(octet 11, 0) unless it is a reduced grid below; otherwise it is not presented (`grid_source`,
`reduced`, `grid`, `grid_size`, [§2.6](#26-what-is-not-presented)). Two grids are one when their
sections 3 are equal from octet 6 but the scanning mode's bits 3 and 4 (`0x20`, `0x10`), which
change the order of the points in the file, not in the decoded field:

| template | grid | `Ni`, `Nj` | scanning mode | the first point, the increments |
|---|---|---|---|---|
| 3.0 | regular latitude/longitude | octets 31–34, 35–38 | octet 72 | `La1`, `Lo1` (47–50, 51–54); `Di`, `Dj` (64–67, 68–71) when flag table 3.3's bits 3 and 4 (octet 55) say they are given, else from the last point (`La2`, `Lo2`, 56–59, 60–63) |
| 3.1 | rotated latitude/longitude | octets 31–34, 35–38 | octet 72 | as 3.0, in the rotated frame; the southern pole's latitude and longitude (73–76, 77–80), and the angle of rotation in degrees (81–84, an IEEE binary32) |
| 3.40 | Gaussian latitude/longitude | octets 31–34, 35–38 | octet 72 | `La1`, `Lo1` (47–50, 51–54), `Lo2` (60–63), and `N`, the parallels between a pole and the equator (68–71) |
| 3.30 | Lambert conformal | octets 31–34, 35–38 | octet 65 | `La1`, `Lo1` (39–42, 43–46); `Dx`, `Dy` in mm (56–59, 60–63); `LaD`, `LoV` (48–51, 52–55), `Latin1`, `Latin2` (66–69, 70–73) |
| 3.20 | polar stereographic | octets 31–34, 35–38 | octet 65 | `La1`, `Lo1` (39–42, 43–46); `Dx`, `Dy` in mm (56–59, 60–63); `LaD`, `LoV` (48–51, 52–55); the projection's pole (octet 64, bit 1: the south pole) |
| 3.10 | Mercator | octets 31–34, 35–38 | octet 60 | `La1`, `Lo1` (39–42, 43–46); `Di`, `Dj` in mm (65–68, 69–72); `LaD` (48–51) |
| 3.101 | general unstructured | — | — | the number of the grid used (16–18), its number in the reference (19), and its UUID (20–35); a grid of at least one point, which section 3 does not place |

Angles are in 10^−6 degree, or for 3.0, 3.1 and 3.40 in units of the basic angle and its
subdivisions (octets 39–46) when both are given. The scanning mode's bit 1 (`0x80`) runs `i` from
east to west, and bit 2 (`0x40`) runs `j` from south to north; an increment takes the sign of its
direction. A 3.1, 3.10, 3.20 or 3.30 field whose Earth (code table 3.2, octet 15) is not of a shape
of the table below, or a shape whose octets 16–30 it needs are missing (a scale factor `0xff` or a
value of all ones), or a 3.20 or 3.30 field whose projection is bipolar (octet 64, bit 2), is not
presented (`earth`). A scaled value of octets 16–30 is its 4 octets, unsigned, scaled by the signed
octet before them as a level's value is ([§2.3](#23-dimensions)).

A 3.20 field whose `LaD` is not in its pole's hemisphere (above 0 and at most 90 for the north pole,
below 0 and at least −90 for the south), a 3.10 field whose `LaD` is not between −90 and 90, or
either with an increment that is 0 or missing, is not presented (`projection`). Octets 61–64 of 3.10,
the orientation of the grid, are not read: NOAA's NBM writes a central meridian there, on a grid
whose `i` runs along the parallels, and a Mercator grid with another orientation would be read as
if it did too.

**Gaussian rows.** A 3.40 grid's rows are of the `2N` latitudes of [§4](#4-georeferencing), numbered
from 0 north to south, and `Nj` of them may be presented: its first row is the one nearest `La1`,
and the rows that follow run south, or north with the scanning mode's bit 2. Such a field whose `N`
is 0 or more than 8192, a bound on the time its rows take to compute, whose `La1` is not within `9 / N` degrees (a tenth of the rows' spacing) of a row, or whose rows run past a pole,
is not presented (`gaussian`). Its longitude increment comes from its corners, not from `Di`, which
producers round (CFSv2 writes 0.938 for 0.9375, which puts its last point 0.19° off): `360 / Ni` when
`(Lo2 − Lo1) + 360 / Ni` is within a tenth of `360 / Ni` of 360, else `(Lo2 − Lo1) / (Ni − 1)`, with
360 added or subtracted so that the span runs `i`'s way, and 0 when `Ni` is 1.

**Reduced grids.** A 3.0 or 3.40 grid with a list of the points along each parallel (octet 11, the
width in octets of each number, at most 8; octet 12, 1) is presented as a sequence of points, when:
`Ni` is missing (all ones); the list, from octet 73, holds a number for each of its `Nj` rows,
which add up to its number of points; its scanning mode has none of bits 1, 3 and 4 set; and its rows
go round the Earth: `(Lo2 − Lo1) + 360 / m`, with 360 added when `Lo2 − Lo1` is negative and `m` its
longest row, is within a tenth of `360 / m` of 360. The points of a row of `c` are at `Lo1 + i × 360 / c`,
`i` from 0, and the rows' latitudes are a 3.40's above (`N` and `La1`, within its rules) or a
3.0's (`La1` and `Dj`, or from `La2`). Any other grid with a list is not presented (`reduced`),
including a sub-area of a reduced grid, whose rows do not go round the Earth.

| shape | the Earth |
|---|---|
| 0 | a sphere of radius 6367470 m |
| 1 | a sphere of the radius of octets 16–20, in m |
| 2 | an ellipsoid of semi-major axis 6378160 m and inverse flattening 297 |
| 3 | an ellipsoid of the semi-major and semi-minor axes of octets 21–25 and 26–30, in km (× 1000 m), its inverse flattening `a / (a − b)` |
| 4 | an ellipsoid of semi-major axis 6378137 m and inverse flattening 298.257222101 (GRS80) |
| 5 | an ellipsoid of semi-major axis 6378137 m and inverse flattening 298.257223563 (WGS 84) |
| 6 | a sphere of radius 6371229 m |
| 7 | as shape 3, its axes in m |
| 8 | a sphere of radius 6371200 m |

### 2.5 Values

A field's values are those `refkv.grib` decodes from its message
([conventions §12](../conventions.md#12-grib-messages)): binary64, NaN where its bitmap or its
packing says a value is missing, in the grid's row order. A field whose packing (section 5's
template) is not one the codec decodes (5.0, 5.2, 5.3, 5.4, 5.40, 5.41 or 5.42) is not
presented (`packing`). The profile reads no more of section 7 than its header, so a 5.40 field
whose codestream the codec does not decode (the 9/7 transform, more than 20 bits) is presented,
and reading its chunk is a decode error
([conventions §12](../conventions.md#12-grib-messages)).

### 2.6 What is not presented

A field that this convention cannot present is recorded in the root's source metadata
([§5.1](#51-the-root)) with its reason, and is not in any variable. Its bytes stay in the
mirror.

| reason | the field's |
|---|---|
| `grid_source` | grid is not defined by a template (section 3 octet 6) |
| `reduced` | grid lists the points of each row (section 3 octet 11), and is not a reduced grid [§2.4](#24-grids) presents |
| `grid` | grid template is not one of [§2.4](#24-grids)'s |
| `grid_size` | `Ni` × `Nj` is not its number of points, or a 3.101 grid has none |
| `gaussian` | Gaussian grid's `N` is 0 or more than 8192, its `La1` is not a row, or its rows run past a pole |
| `earth` | rotated, Lambert, polar stereographic or Mercator grid's Earth is not of shapes 0–8, or its Lambert or polar projection is bipolar |
| `projection` | polar stereographic or Mercator grid's `LaD` is out of its range, or an increment is 0 or missing |
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
- the grid's: `y` and `x` for 3.10, 3.20 and 3.30; `latitude` and `longitude` for 3.0 and 3.40;
  `rlat` and `rlon` for 3.1; and `point` alone for a reduced grid and 3.101, whose length is the
  grid's number of points.

Its shape is its group's coordinates' lengths along its dimensions, its chunk shape 1 along each
but the grid's, which it takes whole; its codecs are `[{"name": "refkv.grib"}]`. A chunk is a
field at its coordinates; a cell no field has has no chunk, and reads as NaN.

**Repeated fields.** A field whose cell another field of its identity already holds (a file may
hold two fields with the same sections 1 to 5, as NCEP's GFS does) goes to the next **copy**: the
first field of each cell is in the variable, the second in a second variable named as the first
with `_2`, and so on. No field is dropped.

A variable's attributes are `long_name` and `units`, when [§3.2](#32-names)'s table names its
parameter; `coordinates`, `"valid_time"`, or on a reduced grid `"valid_time latitude longitude"`;
and `GRIB_centre`, `GRIB_discipline`,
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
4.10, by the table [grib/statistics.tsv](grib/statistics.tsv): `avg`, `accum`, `max`, `min`,
`diff`, `rms`, `sd`, `cov`, `diffStartEnd`, `ratio`, `anomaly`, `sum`, else `stat<n>`) and its
length (`<h>h`, `<m>min` or `<s>s`, the first that
divides it), as `t_isobaric`, `tp_surface_accum6h`. A name an earlier variable or coordinate of
the group has takes `_2`, `_3`, … .

- **Short names** are those of the table [grib/parameters.tsv](grib/parameters.tsv), part of
  this convention, by the centre, the discipline, the category and the number (its columns
  `centre`, `discipline`, `category`, `number`, `short_name`, `name` and `units`, tab-separated,
  one row per parameter): the centre's own row (NCEP's, 7, and ECMWF's, 98, where ecCodes names
  a parameter otherwise than WMO), else WMO's, the row of centre 0. A parameter it does not name
  is `d<discipline>_c<category>_p<number>`. Its `name` and `units` are the variable's
  `long_name` and `units` ([§3.1](#31-variables)). The table was generated by
  `spec/tools/grib_names.py` from ecCodes 2.50.0's definitions (Apache 2.0); regenerating it
  from another release is a change of this convention.
- **Qualifiers** follow the level type for a product that is a probability (4.5, 4.9:
  `prob` and, by code table 4.9, `lt<lower>`, `gt<upper>`, `<lower>to<upper>`, `gt<lower>` or
  `lt<upper>`, a limit's shortest decimal with `p` for its point and `m` for a minus), a
  percentile (4.6, 4.10: `p<percentile>`) or a derived forecast (4.2, 4.12, code table 4.7:
  `ensmean`, `enswmean`, `enssd`, `enssdn`, `ensspread`, `enslai`, `enscmean`, `ensiqr`, `ensmin`,
  `ensmax`, else `ens<code>`).
- **Level types** are named by the table [grib/surfaces.tsv](grib/surfaces.tsv), part of this
  convention, of code table 4.5 (its columns `centre`, `type`, `name` and `units`, the units
  empty for a type whose values have none): `surface`, `isobaric` (in Pa), `heightAboveGround`,
  `depthBelowLand`, and so on, the rows of centre 0 for every centre, and NCEP's local types,
  the rows of centre 7, for its centre alone; another is `level<type>`. A level dimension's
  `units` ([§3.3](#33-coordinates)) are its type's. A layer of two surfaces of one type is the type's name with `Layer`; of two
  types, the first's name, `To`, and the second's, capitalized.

### 3.3 Coordinates

A grid's group holds these coordinates, each a 1-D array named as its dimension but
`valid_time` and the bounds:

| coordinate | values | data type | attributes |
|---|---|---|---|
| `time` | the group's fields' reference times, ascending, each once | `float64` | `units` `"seconds since 1970-01-01T00:00:00Z"`, `calendar` `"proleptic_gregorian"`, `standard_name` `"forecast_reference_time"`, `long_name` `"initial time of forecast"` |
| `step` | their steps, ascending, each once | `float64` | `units` `"seconds"`, `standard_name` `"forecast_period"`, `long_name` `"time since forecast_reference_time"` |
| `valid_time` | `time + step`, over `(time, step)` | `float64` | `units` and `calendar` as `time`'s, `standard_name` `"time"`, `long_name` `"time"` |
| `number` | the members' numbers, ascending, when a variable has them | `float64` | `units` `"1"`, `standard_name` `"realization"`, `long_name` `"ensemble member numerical id"` |
| a level dimension | the first surfaces' values of the variables that have it, ascending by the first value then the second, each pair once | `float64` | `units` (Pa, m, K, …) when the level type has one, `long_name` the dimension's name, and `bounds` for a layer |
| `<level>_bounds` | for a layer, `[first, second]` of each value, over (the level, `bounds`) | `float64` | — |
| `latitude`, `longitude`, `y`, `x`, `rlat` or `rlon`, the grid's evenly spaced dimensions | the grid's positions ([§4](#4-georeferencing)) | `uint32` | `units`, `standard_name`, `scale_factor`, `add_offset` |
| `latitude` of a 3.40 grid | its rows' latitudes ([§4](#4-georeferencing)), in the file's order | `float64` | `units` `"degrees_north"`, `standard_name` `"latitude"` |
| `latitude`, `longitude` of a reduced grid | each point's position ([§2.4](#24-grids)), in the file's order, over `point` | `float64` | `units` `"degrees_north"` and `"degrees_east"`, `standard_name` `"latitude"` and `"longitude"` |

The `float64` coordinates are one chunk each, little-endian and deflated (gzip, level 6), but a
reduced grid's two, which are in chunks of at most 2^17 values
([conventions §9](../conventions.md#9-dataset-layouts)). The grid's evenly spaced dimensions are
the index of [conventions §9](../conventions.md#9-dataset-layouts)'s regular steps, whose
`scale_factor` and `add_offset` give the positions: `units` `"degrees_north"`, `"degrees_east"` and
`standard_name` `"latitude"`, `"longitude"` for 3.0 and 3.40; `units` `"degrees"` and
`standard_name` `"grid_latitude"`, `"grid_longitude"` for 3.1; and `units` `"m"` and `standard_name`
`"projection_y_coordinate"`, `"projection_x_coordinate"` for 3.10, 3.20 and 3.30. A 3.101 grid's
`point` has no coordinate: section 3 names the grid that holds its positions
([§4](#4-georeferencing)).

<a id="type-gribcoordinate"></a>**`GribCoordinate`**: an [`ArrayDocument`](../conventions.md#type-arraydocument)
of the table's row, a `float64` coordinate with the fill value `"NaN"`.

## 4. Georeferencing

A grid's group carries the GeoZarr `proj` and `spatial` conventions' members, as SAFE's
resolution groups do ([spec/zarrify/safe.md §5](safe.md#5-georeferencing-geozarr)), but a reduced
grid's, which has `proj` alone, and a 3.101 grid's, which has neither.

<a id="type-gribgridgroup"></a>**`GribGridGroup`**, its attributes:

| member | value |
|---|---|
| `zarr_conventions` | the CMOs of `proj` and `spatial`; of `proj` alone for a reduced grid, when it has `proj:wkt2`; absent for 3.101 |
| `proj:wkt2` | the CRS, when the grid's Earth is of a shape [§2.4](#24-grids) names: for 3.0, 3.40 and a reduced grid, a geographic CRS on the grid's Earth; for 3.1, a geographic CRS on it derived by a `Pole rotation (GRIB convention)` conversion of the southern pole and the angle of rotation; for 3.30, a Lambert Conic Conformal (2SP) CRS on it, its false origin at (`Latin1`, `LoV`), its standard parallels `Latin1` and `Latin2`; for 3.20, a Polar Stereographic (variant B) CRS on it, its standard parallel `LaD`, its longitude of origin `LoV`, or `LoV + 180°` in [−180, 180) for the south pole, as NCEP's south-polar grids require (PROJ's `y` runs away from the south pole along its longitude of origin); for 3.10, a Mercator (variant B) CRS on it, its first standard parallel `LaD`, its natural origin's longitude 0; absent for 3.101 |
| `spatial:dimensions` | the grid's dimensions ([§3.1](#31-variables)), but for a reduced grid and 3.101 |
| `spatial:transform` | `[dx, 0, x0, 0, dy, y0]`: for 3.0 and 3.1, `dx` the longitude's increment, `x0` `Lo1`, `dy` the latitude's increment and `y0` `La1`, in degrees; for 3.10, 3.20 and 3.30, `Dx` and `Dy` in meters and `(x0, y0)` the first point projected, by **The Lambert projection**, **The polar stereographic projection** and **The Mercator projection** below. Absent for 3.40, whose latitudes are not evenly spaced, a reduced grid and 3.101 |
| `spatial:shape` | `[Nj, Ni]`, but for a reduced grid and 3.101 |
| `spatial:registration` | `"node"`: the positions are grid points; but for a reduced grid and 3.101 |
| `GRIB_numberOfGridUsed`, `GRIB_numberOfGridInReference`, `GRIB_uuidOfHGrid` | for 3.101 alone: its section 3's octets 16–18 and 19, and its octets 20–35 in lowercase hexadecimal, 32 digits and no hyphens, which name the grid file that gives the points' positions |

**The Lambert projection.** `(x0, y0)` is the Lambert conformal conic projection of the first
point, (`La1`, `Lo1`), with its origin at (`Latin1`, `LoV`) (Snyder, *Map Projections: A
Working Manual*, 1987, equations 15-1 to 15-10 and 14-15). It is computed in binary64
([spec/zarrify.md §1.3](../zarrify.md#13-arithmetic)), each operation in the order written,
with musl's `sin`, `cos`, `tan`, `log`, `pow` and `sqrt` (as the Rust crate `libm` 0.2
implements them), so that every implementation computes the same bits. `π` is the binary64
value nearest π, `r(d) = d × (π / 180)` (the quotient rounded first), and the angles are in
degrees ([§2.4](#24-grids)):

- the Earth ([§2.4](#24-grids)): for a sphere, `a` its radius and `e = 0`; for an ellipsoid,
  `a` its semi-major axis, `f = 1 / inverse flattening` and `e = sqrt(f × (2 − f))`;
- `φ = r(La1)`, `φ1 = r(Latin1)`, `φ2 = r(Latin2)`;
- `m(x) = cos(x) / sqrt(1 − e × e × sin(x) × sin(x))`;
- `t(x) = tan(π/4 − x / 2) / pow((1 − e × sin(x)) / (1 + e × sin(x)), e / 2)`, `π/4` the
  binary64 value nearest π/4;
- `n = sin(φ1)` when `φ1 = φ2`, else `n = (log(m(φ1)) − log(m(φ2))) / (log(t(φ1)) − log(t(φ2)))`;
- `F = m(φ1) / (n × pow(t(φ1), n))`;
- `ρ = a × F × pow(t(φ), n)` and `ρ0 = a × F × pow(t(φ1), n)`;
- `θ = n × r(L)`, where `L = ((Lo1 − LoV) + 180) mod 360 − 180`, and `x mod 360` is the
  exact remainder of `x / 360` with the sign of `x` (C's `fmod`), plus 360 when it is negative
  (Rust's `f64::rem_euclid`);
- `x0 = ρ × sin(θ)` and `y0 = ρ0 − ρ × cos(θ)`.

**The polar stereographic projection.** `(x0, y0)` is the polar stereographic projection of
(`La1`, `Lo1`), true to scale at `LaD` (Snyder, equations 21-33 to 21-36, 14-15 and 15-9), computed
the same way with `a`, `e`, `m` and `t` above, and `s` = −1 when the projection's pole is the south
pole, else 1:

- `ρ = 2 × a × t(r(s × La1)) / sqrt(pow(1 + e, 1 + e) × pow(1 − e, 1 − e))` when `LaD` is 90 or −90;
- else `ρ = a × m(c) × t(r(s × La1)) / t(c)`, where `c = r(s × LaD)`;
- `θ = r(L)`, where `L` is `Lo1 − LoV` in [−180, 180) as above;
- `x0 = s × ρ × sin(θ)` and `y0 = −ρ × cos(θ)`: whichever the pole, `y` runs towards it along `LoV`.

That is how NCEP's south-polar grids place their points: the first point of its sea ice analysis
(`seaice.t00z.south12psg.grib2`), 36.866°S 139.806°E with `LoV` 100°, scanning `j` northward, puts
the pole inside the grid and its ice in the Weddell, Ross and Amundsen Seas only so. ecCodes 2.50
runs `x` and `y` the other way, off Antarctica, and FM 92's note that latitude increases with `y`
along `LoV` holds only of a north pole.

**The Mercator projection.** `(x0, y0)` is the Mercator projection of (`La1`, `Lo1`), true to scale
at `LaD`, its central meridian Greenwich (Snyder, equations 7-6, 7-7, 14-15 and 15-9), computed the
same way with `a`, `e`, `m` and `t` above: `k = a × m(r(LaD))`, `x0 = k × r(L)`, where `L` is `Lo1`
in [−180, 180) as above, and `y0 = −k × log(t(r(La1)))`. Its grid may run past 180°: its `x` then
exceeds the projection's range, as `Lo1 + i × dx` does.

**The Gaussian latitudes.** The `2N` latitudes of a 3.40 grid are, from north to south, the zeros
of the Legendre polynomial `P` of degree `2N`, computed in binary64 with musl's `cos` and `asin`,
each operation in the order written, by Newton's method, 5 times from an asymptotic estimate. For
row `k`, from 0, the latitude is `−(the latitude of row 2N − 1 − k)` when `k` is at least `N`, and
otherwise:

- `x = cos(π × (k + 0.75) / (2N + 0.5))`, then 5 times:
  - `p0 = 1`, `p = x`, then for `j` from 2 to `2N`, `q = ((2 × j − 1) × x × p − (j − 1) × p0) / j`,
    `p0 = p`, `p = q`;
  - `d = 2N × (p0 − x × p) / (1 − x × x)` and `x = x − p / d`;
- the latitude is `asin(x) × (180 / π)` degrees, `180 / π` the quotient rounded first.

The row nearest `La1` ([§2.4](#24-grids)) is `k`, the integer nearest
`(90 − La1) × (2N + 0.5) / 180 − 0.75`, halves away from zero, which must lie from −0.5 to below
`2N`.

The grid's evenly spaced positions are its `y` and `x` coordinates: `y0 + j × dy` and `x0 + i × dx`.
The values are in the file's order ([§2.5](#25-values)): a grid that scans north to south has a
descending latitude.

**Unstructured grids.** A 3.101 field's points are placed by a separate grid file, which the
group's `GRIB_uuidOfHGrid` names and the file does not hold. The values are presented by their
index in that file.

## 5. Source metadata

### 5.1 The root

<a id="type-gribroot"></a>**`GribRoot`**, the root's member `"grib"`:

| member | type | value |
|---|---|---|
| `messages` | integer | the file's messages |
| `fields` | integer | their fields |
| `index` | string or null | the key of the index file whose offsets the run used, when the file agreed with it to the end ([§8.2](#82-finding-the-messages)); else null |
| `unpresented` | array of [`GribUnpresented`](#type-gribunpresented) | the fields not presented, in the file's order |

<a id="type-gribunpresented"></a>**`GribUnpresented`**: `message` and `field` (each from 0), and
`reason`, one of [§2.6](#26-what-is-not-presented)'s.

<a id="type-gribrootgroup"></a>**`GribRootGroup`**: the root group, its attributes
`zarr_conventions` and `refkv_zarrified` alone.

### 5.2 The IR mirror

The source's IR is a store IR ([conventions §8.9](../conventions.md#89-stores)): the GRIB file,
described in place, and every index file the run read ([§8.2](#82-finding-the-messages)),
held. Every message's sections 0 to 6, and their bytes, are in it.

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
- 2-D latitudes and longitudes of a projected or rotated grid: a reader computes them from the CRS.
- A 3.101 grid's positions, which are in another file ([§4](#4-georeferencing)).
- A field's position in a file of many fields as a dimension of its own: the many files of a
  forecast run, stacked along `time` and `step`, are a later profile's.
- Which of complex packing's two missing values a missing value was: both are NaN.

# Part 2. The profile

The GRIB profile reads a GRIB edition 2 file, and the index beside it when there is one, and
writes the layout of Part 1.

## 8. GRIB profile

A GRIB file is a file read as a store
([spec/zarrify.md §1.8](../zarrify.md#18-files-read-as-stores)): the store URL is the GRIB
file's directory, its objects are the GRIB file, the input, at its key, and the index files the
run probes for beside it ([§8.2](#82-finding-the-messages)), and the root's `source.url` is the
GRIB file's object URL.

### 8.1 Choosing the profile

A file whose first 4 bytes are `GRIB` and whose eighth is 2 is GRIB's (the registry's `sniff`
order, [spec/zarrify.md §1.2](../zarrify.md#12-input)).

### 8.2 Finding the messages

The run probes for two index files beside the GRIB file, in this order: `<key>.idx` (NOAA's:
lines `n:offset:…`, or `n.k:offset:…` for each field of a message of several) and, when the key
ends in `.grib2`, `.grb2`, `.grib` or `.grb`, the key with that suffix replaced by `.index`
(ECMWF's: JSON lines with `_offset`). It reads every index found that is not empty and at most 16 MiB
(2^24 bytes) long, whole, and the mirror holds each one read ([§5.2](#52-the-ir-mirror)). The run uses
the first index, in that order, that lists offsets, the first 0 and all below the file's size.
With one, the run reads the first 16 KiB of each listed message, from 4 bytes before it, at
once; with none, or from the first offset where the file and the index disagree (a listed offset
at which no message starts, or none the checks of [§8.4](#84-checks-and-rejection) accept, or that
lies inside a message), it walks the file: 64 KiB from where the last message ended (more, where a
section is longer), a message's length taking it to the next. Every
message is read and checked ([§8.4](#84-checks-and-rejection)) whichever way it was found, so an
index changes the reads and not the output, but for [§5.1](#51-the-root)'s `index` and the
mirror's held files. The index files are optional siblings
([spec/zarrify.md §1.9](../zarrify.md#19-sources-beside-the-input)): one outside the input's scope is
absent.

Bytes where a message is due that do not start with `GRIB` are padding: the bytes to the next
`GRIB`, or to the file's end, which MUST be at most 4096 bytes ([§8.4](#84-checks-and-rejection)).

### 8.3 Reading each message

Of each message, the run reads sections 0 to 5 whole, the first 6 octets of each section 6, and
the first 5 of each section 7, reading again where a head did not hold them.

### 8.4 Checks and rejection

A zarrifier MUST reject the input, with the message given (`{at}` the message's offset in the
file, in decimal), when:

| check | message |
|---|---|
| the file is shorter than 16 bytes | `file too short for a GRIB message` |
| a message's indicator (section 0, 16 octets) does not lie within the file | `the file ends inside the GRIB message at byte {at}` |
| its edition (octet 8) is not 2 | `a GRIB edition {edition} message at byte {at}: only edition 2 is read` |
| its length (octets 9–16) is less than 21, or the message does not end within the file | `the GRIB message at byte {at} has the length {length}, past the {end}-byte file`, `{end}` the file's size |
| its last 4 octets are not `7777` | `the GRIB message at byte {at} does not end with 7777` |
| a section (its length, the first 4 octets, and its number, the fifth) is shorter than 5 octets, or ends after the message's section 8 begins | `the section {number} at byte {offset} of the GRIB message at byte {at} has the length {length}, past the message's end` |
| a section 1 to 5 is longer than 1 MiB (2^20 octets) | `the section {number} of the GRIB message at byte {at} has {length} octets, more than the profile reads` |
| the sections are not in FM 92's order (section 1 first; after a section 7, the message may restate sections 2, 3 or 4 on, for the next field; sections 5, 6 and 7 always follow 4, 5 and 6), or the last before section 8 is not a section 7 | `the GRIB message at byte {at}: its section {n} follows section {m}`, or `…: it ends after section {m}, not section 7` |
| a field's section 1 is shorter than 21 octets, its section 3 than 14 (for 3.0, 3.10 and 3.40, 72; for 3.1, 84; for 3.20, 65; for 3.30, 81; for 3.101, 35), its section 4 than 9 (for 4.0, 4.2, 4.5 and 4.6, 34; 4.1, 36; 4.8, 58; 4.9, 71; 4.10, 59; 4.11, 61; 4.12, 60), its section 5 than 11, or its section 6 than 6 | `the GRIB message at byte {at}: a GRIB section {n} …` |
| padding ([§8.2](#82-finding-the-messages)) is longer than 4096 bytes, or is not followed by a message or the file's end | `the bytes at byte {at} are not a GRIB message` |
| the file holds no message | `no GRIB message` |
| no field is presented | `none of the {fields} GRIB fields can be presented: {reasons}`, the reasons ([§2.6](#26-what-is-not-presented)) in UTF-8 order, comma-separated |

A field that is not presented ([§2.6](#26-what-is-not-presented)) does not reject the file.
The checks are those of the profile's rules, `rust/refkv-ir/schema/grib.json`, and of the
section reader they name.

### 8.5 Chunks

<a id="type-gribchunk"></a>**`GribChunk`**: a field's chunk is a message that holds that field
alone:

- the field's message, one range of the GRIB file, when it holds no other field;
- else a message made of the field's sections: a literal section 0 (`GRIB`, two zero octets, the
  discipline, `2`, and the new length), the ranges of its sections 1 to 7 (a reused bitmap's
  section 6 in place of its own), and a literal `7777`.

The output's one source is the GRIB file's object URL, with a `size` pin
([spec/zarrify.md §1.8](../zarrify.md#18-files-read-as-stores)); an index is no source.

### 8.6 Summary

The run's summary: `messages`, `fields`, `grids`, `arrays` (each variable's `path`, `shape` and
count of `fields`), `chunks`, `unpresented` (the count of each reason), `index`, and the store
run's counts.
