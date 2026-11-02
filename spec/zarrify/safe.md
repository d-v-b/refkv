# SAFE

The Zarr v3 layout of a Sentinel-2 Level-1C or Level-2A product in the SAFE
format (a `.SAFE` directory, or the `.SAFE.zip` archive of one), and the
translation of its metadata into JSON. What all of refkv's conventions share
is in [spec/conventions.md](../conventions.md), cited here as "conventions §n".
How refkv produces this layout as a reference store is the SAFE profile,
[Part 2](#part-2-the-profile) below.

Convention version: 0 (until release, [conventions §1](../conventions.md#1-conventions)) · UUID: `ef81346c-19e8-42ad-93b0-a279ccaf44c1` ·
Schema: [schema.json](safe/schema.json)

Changed 2026-10-26, with no change to any output: the layout is tabulated, its entries in
[Layout](#layout) and its types in type tables
([conventions §11.5](../conventions.md#115-layout-tables)), the GeoZarr attributes of the root
and the resolution groups among them.

Changes before 2026-10-22 are recorded in
[the revision log of VIRTUALIZE.md](../history/virtualize/REVISIONS.md), this document's earlier
name, up to
[revision 29](../history/virtualize/REVISIONS.md#revision-29-no-recorded-revision-the-profile-registry).

This document has two parts ([conventions §11.1](../conventions.md#111-parts)).

# Part 1. The convention

This convention gives a layout only to the products that meet its
requirements. Where it says that "the input is rejected", or that
something MUST hold, a product that fails has no layout under this
convention, and the profile rejects it.

A SAFE product is a tree of files. Its pixels are JPEG 2000 files (JP2),
one per band and resolution, each a grid of independently coded tiles. Its
metadata is XML: the product metadata `MTD_MSIL1C.xml` or `MTD_MSIL2A.xml`,
the tile metadata `GRANULE/<g>/MTD_TL.xml`, the datastrip metadata, quality
reports and the SAFE manifest. The output is a GeoZarr hierarchy: one group
per resolution (`r10m`, `r20m`, `r60m`), one array per band, and one chunk
per JP2 tile. Each chunk is a standalone JPEG 2000 codestream that is
rebuilt from byte ranges of the band's file, plus a header that is shared
by the band's chunks. The groups declare their coordinate reference system
and affine transform with the zarr-conventions `proj` and `spatial`
conventions ([§5](#5-georeferencing-geozarr)). The source's values are recorded but never applied: the
radiometric offsets and the quantification values are metadata of each band
([§6.2](#62-band-arrays)). Every file of the product is kept: `refkv_source` is the product's IR
mirror, which lists every object, holds the XML documents it reads, describes
each band file's boxes, codestream and tile-parts, and references every other
object whole ([§6.3](#63-the-source-metadata-node)).

Unlike the other conventions, the hierarchy has no OME-NGFF metadata. It is
geospatial data, and its target formats are Zarr v3 and the GeoZarr
conventions of [§5](#5-georeferencing-geozarr). This section extends [conventions §2](../conventions.md#2-attributes) ("Nothing else"): the
attributes of this convention's nodes may hold, besides `zarr_conventions`
and `refkv_zarrified`, the members that [§5](#5-georeferencing-geozarr) gives them (`proj:code`,
`proj:wkt2`, `spatial:*` and `multiscales`), and only on the groups that [§5](#5-georeferencing-geozarr)
names.

## Layout

The hierarchy's entries ([conventions §11.5](../conventions.md#115-layout-tables)):

| prefix | type | structure | description |
|---|---|---|---|
| `zarr.json` | group | [`SafeGroup`](#type-safegroup) | the root ([§3](#3-the-hierarchy)); its attributes are a [`SafeRootAttributes`](#type-saferootattributes) ([§5.3](#53-the-root)) |
| `zarr.json#/attributes/zarr_conventions` | attribute | array of [`ConventionMetadata`](../conventions.md#type-conventionmetadata) or [`SafeGeoCmo`](#type-safegeocmo) | this convention's CMO ([§1](#1-declaration)), then, for Level-2A, the multiscales CMO, then the proj and spatial CMOs ([§5](#5-georeferencing-geozarr)) |
| `zarr.json#/attributes/refkv_zarrified` | attribute | [`RootProperty`](../conventions.md#type-rootproperty) | `profile` `"safe"`, `version` `0`, `source.url` the input's URL: a `.SAFE` directory's store URL, ending in `/`, or a `.SAFE.zip` file's URL ([§1](#1-declaration)) |
| `zarr.json#/attributes/refkv_zarrified/safe` | attribute | [`SafeRoot`](#type-saferoot) | the root's source metadata: the product's identity ([§6.3](#63-the-source-metadata-node)) |
| `zarr.json#/attributes/proj:code` | attribute | string | the CRS code ([§5.3](#53-the-root)) |
| `zarr.json#/attributes/proj:wkt2` | attribute | string | the CRS's WKT2, for a WGS 84 / UTM CRS code ([§5.2](#52-resolution-groups)) |
| `zarr.json#/attributes/spatial:bbox` | attribute | array of number | the bounding box of the first resolution group ([§5.3](#53-the-root)) |
| `zarr.json#/attributes/spatial:dimensions` | attribute | `["y", "x"]` | Level-2A only ([§5.3](#53-the-root)) |
| `zarr.json#/attributes/spatial:registration` | attribute | `"pixel"` | Level-2A only ([§5.3](#53-the-root)) |
| `zarr.json#/attributes/multiscales` | attribute | [`SafeMultiscales`](#type-safemultiscales) | Level-2A only: one layout item per resolution group ([§5.3](#53-the-root)) |
| `r<r>m/zarr.json` | group | [`SafeGroup`](#type-safegroup) | a resolution group, one per resolution of a band file ([§3](#3-the-hierarchy)); its attributes are a [`SafeResolutionAttributes`](#type-saferesolutionattributes) ([§5.2](#52-resolution-groups)) |
| `r<r>m/zarr.json#/attributes/zarr_conventions` | attribute | array of [`SafeGeoCmo`](#type-safegeocmo) | the proj and spatial CMOs ([§5.2](#52-resolution-groups)) |
| `r<r>m/zarr.json#/attributes/proj:code` | attribute | string | the CRS code ([§5.2](#52-resolution-groups)) |
| `r<r>m/zarr.json#/attributes/proj:wkt2` | attribute | string | the CRS's WKT2, for a WGS 84 / UTM CRS code ([§5.2](#52-resolution-groups)) |
| `r<r>m/zarr.json#/attributes/spatial:dimensions` | attribute | `["y", "x"]` | [§5.2](#52-resolution-groups) |
| `r<r>m/zarr.json#/attributes/spatial:transform` | attribute | array of number | the transform of resolution `r` ([§5.1](#51-the-grid-of-a-resolution)) |
| `r<r>m/zarr.json#/attributes/spatial:shape` | attribute | array of integer | the shape of resolution `r` ([§5.1](#51-the-grid-of-a-resolution)) |
| `r<r>m/zarr.json#/attributes/spatial:bbox` | attribute | array of number | the bounding box of resolution `r` ([§5.1](#51-the-grid-of-a-resolution)) |
| `r<r>m/zarr.json#/attributes/spatial:registration` | attribute | `"pixel"` | [§5.2](#52-resolution-groups) |
| `r<r>m/<band>/zarr.json` | array | [`SafeArray`](#type-safearray) | a band array, one per band file of resolution `r` ([§4](#4-arrays)) |
| `r<r>m/<band>/zarr.json#/attributes/zarr_conventions` | attribute | array of [`ConventionMetadata`](../conventions.md#type-conventionmetadata) | `[C]`, `C` this convention's CMO ([§1](#1-declaration)) |
| `r<r>m/<band>/zarr.json#/attributes/refkv_zarrified` | attribute | [`NodeProperty`](../conventions.md#type-nodeproperty) | the band's property ([§1](#1-declaration)) |
| `r<r>m/<band>/zarr.json#/attributes/refkv_zarrified/safe` | attribute | [`SafeBand`](#type-safeband) | the band's source metadata ([§6.2](#62-band-arrays)) |
| `r<r>m/<band>/c/<v>/<u>` | chunk | [`SafeChunk`](#type-safechunk) | the tile `t = v × nx + u` of a band file of one component; every chunk is present ([§4](#4-arrays), [§8.6](#86-chunks)) |
| `r<r>m/<band>/c/0/<v>/<u>` | chunk | [`SafeChunk`](#type-safechunk) | the tile `t = v × nx + u` of a band file of three components; every chunk is present ([§4](#4-arrays), [§8.6](#86-chunks)) |
| `refkv_source/**` | subtree | [the mirror's entries](../conventions.md#table-mirror) | the IR mirror of the product, a store IR in the directory form and a file IR in the zip form ([§6.3](#63-the-source-metadata-node)); its elements are [§6.3](#63-the-source-metadata-node)'s |
| `refkv_source/zarr.json#/attributes/refkv_zarrified/safe` | attribute | [`SafeMirror`](#type-safemirror) | `refkv_source`'s source metadata ([§6.3](#63-the-source-metadata-node)) |
| `refkv_source/objects/<k>` | object | the source range `(i, 0, size)` of the object's url source `i` | directory form only: an object the IR references, whole ([§6.3](#63-the-source-metadata-node), [§8.7](#87-other-objects)); the mirror's entries list it as a store convention's `objects/**` |

| placeholder | value |
|---|---|
| `<r>` | a resolution `r` of a band file, in metres, in decimal ([§2.5](#25-band-files)); the groups are in ascending order of `r` |
| `<band>` | a band file's band name ([§2.5](#25-band-files)) |
| `<v>`, `<u>` | the tile's row `v` (`0 ≤ v < ny`) and column `u` (`0 ≤ u < nx`) in the band file's grid of tiles, in decimal ([§4](#4-arrays)) |
| `<k>` | an object's key, escaped as [Zarr v2 convention §5](zarr2.md#5-other-objects) says; it stands for one or more segments |

## 1. Declaration

The root declares the convention
([conventions §11.2](../conventions.md#112-declaration)), with the input's URL (the
store URL, ending in `/`, of a directory, or the URL of a zip file) and its source
metadata ([§6.1](#61-values)) as the member `"safe"`. Each band
array and `refkv_source` declare it with their own source metadata ([§6.2](#62-band-arrays),
[§6.3](#63-the-source-metadata-node)). The arrays under `refkv_source` have none. Its CMO is:

```json
{
  "uuid": "ef81346c-19e8-42ad-93b0-a279ccaf44c1",
  "schema_url": "https://raw.githubusercontent.com/d-v-b/refkv/refs/heads/main/spec/zarrify/safe/schema.json",
  "spec_url": "https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safe.md",
  "name": "refkv_zarrified",
  "description": "The Zarr layout of a Sentinel-2 SAFE source zarrified by refkv, and the source's metadata"
}
```

A node's `zarr_conventions` lists the CMOs of the conventions it uses, in
this order: this convention's, then `multiscales`, `proj` and `spatial`
([§5](#5-georeferencing-geozarr)), each only when the node uses it.

## 2. The source

### 2.1 Objects

The product is a set of **objects**, each with a **key** (a relative path
whose segments are separated by `/`) and a **size**. The profile says how
it finds them: by listing a store, or from the central directory of a zip
file. Keys compare as UTF-8 byte strings. The objects are classified as
follows:

| kind | objects |
|---|---|
| the **band files** | the objects that the product metadata lists as images in `IMG_DATA` ([§2.3](#23-image-files)) |
| the **XML documents** | every other object whose key is `manifest.safe`, or ends in `.xml` or `.xsd` |
| the **other objects** | every other object. Among them are the quality masks (GML files before processing baseline 04.00, JP2 files from 04.00), the preview image (`QI_DATA/*_PVI.jp2`), the L2A cloud and snow probabilities (`QI_DATA/MSK_CLDPRB_*.jp2`, `MSK_SNWPRB_*.jp2`, and before processing baseline 02.07 `QI_DATA/*_CLD_*.jp2`, `*_SNW_*.jp2`), the auxiliary data (`AUX_DATA/*`), the `HTML` folder and the quicklook images that some mirrors add (the Google Cloud mirror's `<product>-ql.jpg`) |

In this version the masks, the probabilities and the preview image are kept
whole ([§6.3](#63-the-source-metadata-node)), not presented as arrays.

An object of size 0 is an **empty object**. It is none of the three kinds,
and only its key is kept ([§6.3](#63-the-source-metadata-node)).

A **folder marker** is an object whose key is a nonempty `d` followed by
`_$folder$`. Mirrors that emulate directories in a flat bucket write them:
the Google Cloud mirror writes one of 6 bytes (`folder`) for every
directory, including the empty ones (such as the root's `AUX_DATA` of a
Level-2A product of processing baseline 02.06 or 02.07). A folder marker is
layout of the mirror, as a zip file's directory entries are layout of the
zip: it is none of the kinds above (not an empty object either), it is not
read, and nothing of it is kept but the directory it names. The kinds above
are those of the other objects.

The **named directories** are the `d` of every folder marker, and those
that the profile names otherwise (a zip file's directory entries, a
listing's empty objects whose keys end in `/`). A named directory is
**empty** when no other key, of an object, of a folder marker or of a named
directory, starts with `d/`. The empty directories are kept ([§6.3](#63-the-source-metadata-node)), so that
the rebuilt product has them ([§6.4](#64-reconstruction)); the others are the directories of its
objects.

**Reading XML.** The product metadata and the tile metadata ([§2.2](#22-the-product-metadata), [§2.4](#24-the-tile-geocoding))
are read as the subset of XML 1.0 of
[spec/zarrify.md §1.5](../zarrify.md#15-listing-a-store). They MUST be
at most 2^24 bytes, and the input is rejected if they are not in that
subset. In this convention, an element is named by its **local name**: its
name after the last `:` (so `n1:General_Info` is `General_Info`). A name
below also matches its alias in the product specification (PSD) 14.2, which
the Level-2A products of processing baselines 02.04–02.06 use:

| name | PSD 14.2 alias |
|---|---|
| `Product_Info`, `Product_Organisation`, `Product_Image_Characteristics` | `L2A_` followed by the name |
| `IMAGE_FILE`, `PRODUCT_URI`, `TILE_ID` | the name followed by `_2A` |
| `QUANTIFICATION_VALUES_LIST` | `L1C_L2A_Quantification_Values_List` |
| `BOA_QUANTIFICATION_VALUE`, `AOT_QUANTIFICATION_VALUE`, `WVP_QUANTIFICATION_VALUE` | `L2A_` followed by the name |
| `Scene_Classification_List`, `Scene_Classification_ID`, `SCENE_CLASSIFICATION_TEXT`, `SCENE_CLASSIFICATION_INDEX` | `L2A_` followed by the name |

A **path** `A/B/C` names the elements `C` that are children of a `B` that is a child of
an `A`. An element's **text** is
[§1.5's](../zarrify.md#15-listing-a-store), with leading and trailing
whitespace ([§1.3](../zarrify.md#13-arithmetic)) removed. XML documents that this convention does not read
are not parsed.

### 2.2 The product metadata

The root MUST have exactly one of the objects `MTD_MSIL1C.xml` (the product
is **Level-1C**) and `MTD_MSIL2A.xml` (**Level-2A**), and the object
`manifest.safe`. (A product in the format used before December 2016, whose
product metadata is named `S2A_OPER_MTD_SAFL1C_….xml` and which may hold
several granules, has neither, and is rejected: this version does not
support that format.) The product metadata's
root element MUST be named `Level-1C_User_Product` or
`Level-2A_User_Product`, as the level requires.

The **granules** are the elements `General_Info/Product_Info/Product_Organisation/Granule_List/Granule`
under the root. Their `IMAGE_FILE` children, in document order, are the
**image files**, of which there MUST be at least one. (A product has one
granule. Processing baselines 02.04–02.07 list it three times, one
`Granule_List` per resolution, each with the image files of that
resolution: their image files are concatenated. That they name one granule
is checked by [§2.3](#23-image-files).)

### 2.3 Image files

Each image file's text `F` MUST have the form `GRANULE/<g>/<d>/<rest>`,
where `<g>` and `<d>` are segments, `<g>` the same for every image file,
and `<rest>` is one or more segments, and the image files MUST all differ.
The image files with `<d>` = `IMG_DATA` are the bands, of which there MUST
be at least one. For each, the key `F` followed by `.jp2` MUST be an object
of the product, the image file's **band file**, whose **basename** `b` is
the last segment of `F`. The others (PSD 14.2 lists its DEM under
`AUX_DATA`, which the products do not hold, and its cloud and snow
probabilities under `QI_DATA`) are not bands: their objects, if any, are
other objects ([§2.1](#21-objects)).

`<g>` names the granule's directory. The **tile metadata** is the object
`GRANULE/<g>/MTD_TL.xml`, which MUST exist, and whose root element MUST be
named `Level-1C_Tile_ID` or `Level-2A_Tile_ID`, as the level requires.

### 2.4 The tile geocoding

The element `Geometric_Info/Tile_Geocoding` under the tile metadata's root
MUST be present once. It MUST have these children:

| child | how many | content |
|---|---|---|
| `HORIZONTAL_CS_CODE` | one | its text is `EPSG:` followed by one or more digits: the **CRS code** |
| `Size` | one or more; no two have the same `r` | an attribute `resolution` whose value is one or more digits without a leading zero, at most 2^32 − 1, the resolution `r` in metres, and one child `NROWS` and one child `NCOLS`, each of whose texts is a positive decimal integer (digits, without a leading zero), at most 2^32 − 1 |
| `Geoposition` | for each `Size` of resolution `r`, exactly one whose attribute `resolution` is the same text | one child each of `ULX`, `ULY`, `XDIM` and `YDIM`, whose texts are decimal numbers ([§6.1](#61-values)), finite as binary64 values ([spec/zarrify.md §1.3](../zarrify.md#13-arithmetic)), with `XDIM` and `YDIM` not zero. (`ULX` and `ULY` are the coordinates of the outer corner of the top-left pixel; `XDIM` and `YDIM` are the pixel's width and height, `YDIM` negative for a north-up grid.) |

Other children of `Tile_Geocoding` (`HORIZONTAL_CS_NAME`) are not checked.

### 2.5 Band files

Each band file is a JP2 file whose codestream the profile reads and checks
([§8.3](#83-band-files)). It gives:

| of the band file | value |
|---|---|
| its **image size** `W × H` | the SIZ marker's `Xsiz` and `Ysiz` |
| its **tile size** `T × U` | `XTsiz` and `YTsiz` |
| its number of tiles | `nx = ceil(W / T)` across and `ny = ceil(H / U)` down |
| its number of **components** `C` | MUST be 1 (a band) or 3 (a true-color image) |
| their **precision** `p` | the same for every component, from 1 to 16, unsigned and not subsampled |
| its **tile-parts** | one per tile, in raster order |

The band file's **resolution** is the `r` of the one `Size` ([§2.4](#24-the-tile-geocoding)) whose
`NCOLS` is `W` and whose `NROWS` is `H`; if none or several match, the input
is rejected. Its **band name** is the basename `b`, with the suffix `_<r>m`
removed if `b` ends with it, then the part of it after its last `_` (all of
it if it has no `_`). The band name MUST be one or more ASCII letters and
digits (`B02`, `B8A`, `TCI`, `AOT`, `WVP`, `SCL`). No two band files with the
same resolution may have the same band name.

So in a Level-1C product `T32TNS_20240103T101329_B02` is the band `B02` at
10 m, and in a Level-2A product `T32TNS_20240103T101329_B02_20m` is the band
`B02` at 20 m.

## 3. The hierarchy

The hierarchy is (its entries are in [Layout](#layout)):

| node | path | what |
|---|---|---|
| the root | `""` | a group ([§5.3](#53-the-root)) |
| a resolution group | `r<r>m` (for example `r10m`) | for each resolution `r` of a band file, in ascending order, a group, a child of the root ([§5.2](#52-resolution-groups)) |
| a band array | `r<r>m/<band>` | in the group `r<r>m`, one array per band file of that resolution, named by its band name ([§4](#4-arrays)) |
| `refkv_source` | `refkv_source` | a group, a child of the root, holding the source's other metadata ([§6.3](#63-the-source-metadata-node)) |

A product whose bands have the resolutions 10, 20 and 60 m, as every
product does, has the groups `r10m`, `r20m` and `r60m`. A Level-1C product
has each band at its own resolution (`r10m` has `B02`, `B03`, `B04`, `B08`
and `TCI`; `r20m` has `B05`, `B06`, `B07`, `B8A`, `B11` and `B12`; `r60m`
has `B01`, `B09` and `B10`). A Level-2A product has most bands at several
resolutions, and adds `AOT`, `WVP` and `SCL`.

Every group's `zarr.json` is
`{"zarr_format": 3, "node_type": "group", "attributes": A}`, a
<a id="type-safegroup"></a>**`SafeGroup`**:

| member | type | presence | value |
|---|---|---|---|
| `zarr_format` | integer | required | `3` |
| `node_type` | string | required | `"group"` |
| `attributes` | [`SafeRootAttributes`](#type-saferootattributes), [`SafeResolutionAttributes`](#type-saferesolutionattributes) or object | required | `A` as this document gives it: a `SafeRootAttributes` on the root ([§5.3](#53-the-root)), a `SafeResolutionAttributes` on a resolution group ([§5.2](#52-resolution-groups)), `refkv_source`'s as [conventions §8.2](../conventions.md#82-the-table) gives them, or `{}` for a group it gives no attributes |

## 4. Arrays

A band file of `C` components, precision `p`, image size `W × H` and tile
size `T × U` is an array whose `zarr.json` is as in
[conventions §3](../conventions.md#3-arrays), a
<a id="type-safearray"></a>**`SafeArray`**: an
[`ArrayDocument`](../conventions.md#type-arraydocument) (its `fill_value` 0, as there) with
these members:

| member | type | presence | value |
|---|---|---|---|
| `shape` | array of integer | required | `[H, W]` when `C = 1`, and `[3, H, W]` when `C = 3` |
| `data_type` | string | required | `uint8` when `p ≤ 8`, and `uint16` otherwise. (A Sentinel-2 reflectance band has `p = 15`: its values fill 15 bits of a `uint16`. The true-color image `TCI` and the scene classification `SCL` have `p = 8`.) |
| `chunk_grid` | object | required | regular, its `chunk_shape` `[U, T]` when `C = 1`, and `[3, U, T]` when `C = 3` |
| `codecs` | array of object | required | `[{"name": "imagecodecs_jpeg2k"}]` when `C = 1`, and `[{"name": "transpose", "configuration": {"order": [1, 2, 0]}}, {"name": "imagecodecs_jpeg2k"}]` when `C = 3` (the codestream decodes to `[y, x, c]`) |
| `dimension_names` | array of string | required | `["y", "x"]` when `C = 1`, or `["c", "y", "x"]` when `C = 3` |
| `attributes` | [`Declaration`](../conventions.md#type-declaration) | required | the declaration of [§1](#1-declaration) with the band's source metadata, a [`SafeBand`](#type-safeband) ([§6.2](#62-band-arrays)) |

**Chunks.** The chunk at `<group>/<band>/c/<v>/<u>` (`<group>/<band>/c/0/<v>/<u>`
when `C = 3`), for `0 ≤ u < nx` and `0 ≤ v < ny`, is the tile `t = v × nx + u`
of the band file. It holds one JPEG 2000 codestream (ISO/IEC 15444-1),
which decodes to `U` rows of `T` samples per component. Its top-left
`h × w` samples are the tile's samples, where `w = min(T, W − u × T)` and
`h = min(U, H − v × U)`. The other samples are outside the array at its
right and bottom edges, and are unspecified. Every chunk is present.

The codestream is the band file's tile, coded as the file codes it, placed
on the JPEG 2000 reference grid where the file places it, and it is the
only tile with data:

| of the codestream | what it is |
|---|---|
| its main header | the band file's, with its own SIZ marker. The image area is `[x0, x0 + T) × [y0, y0 + U)`, where `x0 = u × T` and `y0 = v × U`. The tiles are `w × h`, with their origin at `(x0, y0)` |
| its tile 0 | the band file's tile `t`. Its area, `[x0, x0 + w) × [y0, y0 + h)`, is the area that tile has in the file, so the wavelet decomposition, the precincts and the code-blocks are the file's, and so are its packets |
| each of its other tiles (when `w < T` or `h < U`) | lies outside the array, and holds only empty packets. They decode to the value `2^(p − 1)` (the DC level shift) and are never read |

The profile says how these codestreams are made of byte ranges of the band
file and a few bytes of their own: a chunk is a [`SafeChunk`](#type-safechunk)
([§8.6](#86-chunks)).

The band file's other bytes are kept in the source metadata: its JP2 boxes,
main header and tile-part headers are elements of the IR mirror
([§6.3](#63-the-source-metadata-node)), and the original SIZ marker segment is
also in the band's own source metadata ([§6.2](#62-band-arrays)). The rest of the main
header, its coding style, quantization and comments, is in every chunk.

## 5. Georeferencing (GeoZarr)

The groups declare their coordinate reference system and their grid with
two conventions of the [zarr-conventions](https://github.com/zarr-conventions)
organization, which the GeoZarr specification builds on. Both are at their
tag `v0.1`, whose members are prefixed keys at the top level of
`attributes`:
[`proj`](https://github.com/zarr-conventions/proj/blob/v0.1/README.md) and
[`spatial`](https://github.com/zarr-conventions/spatial/blob/v0.1/README.md). A Level-2A root
also uses
[`multiscales`](https://github.com/zarr-conventions/multiscales/blob/v0.1/README.md)
([§5.3](#53-the-root)). Each has a CMO, a <a id="type-safegeocmo"></a>**`SafeGeoCmo`**:

| member | type | presence | value |
|---|---|---|---|
| `uuid` | string | required | the convention's, below |
| `schema_url` | string | required | the convention's, below |
| `spec_url` | string | required | the convention's, below |
| `name` | string | required | the convention's, below |
| `description` | string | required | the convention's, below |

| member | [`proj`](https://github.com/zarr-conventions/proj/blob/v0.1/README.md) | [`spatial`](https://github.com/zarr-conventions/spatial/blob/v0.1/README.md) | [`multiscales`](https://github.com/zarr-conventions/multiscales/blob/v0.1/README.md) |
|---|---|---|---|
| `uuid` | `"f17cb550-5864-4468-aeb7-f3180cfb622f"` | `"689b58e2-cf7b-45e0-9fff-9cfc0883d6b4"` | `"d35379db-88df-4056-af3a-620245f8e347"` |
| `schema_url` | `"https://raw.githubusercontent.com/zarr-conventions/proj/refs/tags/v0.1/schema.json"` | `"https://raw.githubusercontent.com/zarr-conventions/spatial/refs/tags/v0.1/schema.json"` | `"https://raw.githubusercontent.com/zarr-conventions/multiscales/refs/tags/v0.1/schema.json"` |
| `spec_url` | `"https://github.com/zarr-conventions/proj/blob/v0.1/README.md"` | `"https://github.com/zarr-conventions/spatial/blob/v0.1/README.md"` | `"https://github.com/zarr-conventions/multiscales/blob/v0.1/README.md"` |
| `name` | `"proj"` | `"spatial"` | `"multiscales"` |
| `description` | `"Coordinate reference system information for geospatial data"` | `"Spatial coordinate information"` | `"Multiscale layout of zarr datasets"` |

Each CMO is written exactly as above, with these five members, in this order.

### 5.1 The grid of a resolution

For the resolution `r`, with the `Size` `NROWS`, `NCOLS` and the
`Geoposition` `ULX`, `ULY`, `XDIM`, `YDIM` of [§2.4](#24-the-tile-geocoding) (as binary64 values,
[spec/zarrify.md §1.3](../zarrify.md#13-arithmetic)):

| of the grid | value |
|---|---|
| the **transform** | `[XDIM, 0, ULX, 0, YDIM, ULY]`: the affine coefficients `[a, b, c, d, e, f]` of the spatial convention, which maps the index `(col, row)` of a pixel's top-left corner to `(a × col + b × row + c, d × col + e × row + f)`. (This is GDAL's geotransform `[ULX, XDIM, 0, ULY, 0, YDIM]` in the spatial convention's order.) |
| the **shape** | `[NROWS, NCOLS]` |
| the **bounding box** | `[min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)]`, where `x0 = ULX`, `x1 = ULX + XDIM × NCOLS`, `y0 = ULY` and `y1 = ULY + YDIM × NROWS` (the product first) |

The grid's position is pixel registration: `ULX` and `ULY` are a corner, not
a pixel center (the GML in each band file gives the center of the top-left
pixel, half a pixel in from it).

### 5.2 Resolution groups

The attributes of the group `r<r>m` are:

```json
{
  "zarr_conventions": [P, Q],
  "proj:code": "EPSG:32632",
  "proj:wkt2": "PROJCRS[\"WGS 84 / UTM zone 32N\",BASEGEOGCRS[…]]",
  "spatial:dimensions": ["y", "x"],
  "spatial:transform": [10.0, 0.0, 499980.0, 0.0, -10.0, 5200020.0],
  "spatial:shape": [10980, 10980],
  "spatial:bbox": [499980.0, 5090220.0, 609780.0, 5200020.0],
  "spatial:registration": "pixel"
}
```

(with `proj:wkt2` shortened here), a
<a id="type-saferesolutionattributes"></a>**`SafeResolutionAttributes`**:

| member | type | presence | value |
|---|---|---|---|
| `zarr_conventions` | array of [`SafeGeoCmo`](#type-safegeocmo) | required | `[P, Q]`, where `P` and `Q` are the CMOs of `proj` and `spatial` ([§5](#5-georeferencing-geozarr)) |
| `proj:code` | string | required | the CRS code ([§2.4](#24-the-tile-geocoding)) as written |
| `proj:wkt2` | string | optional | as below: present, right after `proj:code`, when the CRS code is one of the WGS 84 / UTM zones |
| `spatial:dimensions` | array of string | required | `["y", "x"]` |
| `spatial:transform` | array of number | required | the transform of resolution `r` ([§5.1](#51-the-grid-of-a-resolution)) |
| `spatial:shape` | array of integer | required | the shape of resolution `r` ([§5.1](#51-the-grid-of-a-resolution)) |
| `spatial:bbox` | array of number | required | the bounding box of resolution `r` ([§5.1](#51-the-grid-of-a-resolution)) |
| `spatial:registration` | string | required | `"pixel"` |

The proj convention applies a group's CRS
to its direct child arrays, and the spatial convention its properties, so
the band arrays carry neither. Every band array's `shape` ends with the
group's `spatial:shape` ([§2.5](#25-band-files)), and its dimension names include `y` and
`x`.

**`proj:wkt2`.** When the CRS code is `EPSG:` followed by one of the
numbers 32601 to 32660 or 32701 to 32760, written without a leading zero
(the 120 WGS 84 / UTM zones, which are every CRS Sentinel-2 products use),
the group also has the member `proj:wkt2`, right after `proj:code`. Its
value is the template below, with these placeholders:

| placeholder | value |
|---|---|
| `<c>` | the number |
| `<z>` | the zone: `c − 32600` or `c − 32700`, without a leading zero |
| `<h>` | `N` for 32601–32660 and `S` for 32701–32760 |
| `<lon>` | the decimal integer `6 × z − 183` (with a `-` when negative) |
| `<fn>` | `0` for `N` and `10000000` for `S` |

all on one line, with no whitespace but the spaces shown inside the quoted names:

```
PROJCRS["WGS 84 / UTM zone <z><h>",BASEGEOGCRS["WGS 84",ENSEMBLE["World Geodetic System 1984 ensemble",
MEMBER["World Geodetic System 1984 (Transit)"],MEMBER["World Geodetic System 1984 (G730)"],
MEMBER["World Geodetic System 1984 (G873)"],MEMBER["World Geodetic System 1984 (G1150)"],
MEMBER["World Geodetic System 1984 (G1674)"],MEMBER["World Geodetic System 1984 (G1762)"],
MEMBER["World Geodetic System 1984 (G2139)"],MEMBER["World Geodetic System 1984 (G2296)"],
ELLIPSOID["WGS 84",6378137,298.257223563,LENGTHUNIT["metre",1]],ENSEMBLEACCURACY[2.0]],
PRIMEM["Greenwich",0,ANGLEUNIT["degree",0.0174532925199433]],ID["EPSG",4326]],
CONVERSION["UTM zone <z><h>",METHOD["Transverse Mercator",ID["EPSG",9807]],
PARAMETER["Latitude of natural origin",0,ANGLEUNIT["degree",0.0174532925199433],ID["EPSG",8801]],
PARAMETER["Longitude of natural origin",<lon>,ANGLEUNIT["degree",0.0174532925199433],ID["EPSG",8802]],
PARAMETER["Scale factor at natural origin",0.9996,SCALEUNIT["unity",1],ID["EPSG",8805]],
PARAMETER["False easting",500000,LENGTHUNIT["metre",1],ID["EPSG",8806]],
PARAMETER["False northing",<fn>,LENGTHUNIT["metre",1],ID["EPSG",8807]]],
CS[Cartesian,2],AXIS["(E)",east,ORDER[1],LENGTHUNIT["metre",1]],
AXIS["(N)",north,ORDER[2],LENGTHUNIT["metre",1]],ID["EPSG",<c>]]
```

(The line breaks above are for reading: the value has none.) This is the
WKT2 (ISO 19162:2019) that PROJ 9.8 writes for these codes from the EPSG
database, without its `USAGE` (the scope and area of use, which is not part
of the definition). It is fixed here, rather than taken from a CRS database
at run time, so that every producer writes the same string whatever its
database's version. For any other code the group has no `proj:wkt2`: the
EPSG code is what the product gives.

### 5.3 The root

The root's attributes are the declaration of [§1](#1-declaration) and the GeoZarr members, a
<a id="type-saferootattributes"></a>**`SafeRootAttributes`**:

| member | type | presence | value |
|---|---|---|---|
| `zarr_conventions` | array of [`ConventionMetadata`](../conventions.md#type-conventionmetadata) or [`SafeGeoCmo`](#type-safegeocmo) | required | this convention's CMO, then, for a Level-2A product, the multiscales CMO, then the CMOs of `proj` and `spatial` ([§1](#1-declaration)) |
| `refkv_zarrified` | [`RootProperty`](../conventions.md#type-rootproperty) | required | the declaration's property ([§1](#1-declaration)), its `"safe"` member a [`SafeRoot`](#type-saferoot) |
| `proj:code` | string | required | the CRS code |
| `proj:wkt2` | string | optional | present, right after `proj:code`, when the CRS code is one of the WGS 84 / UTM zones; as [§5.2](#52-resolution-groups) gives it |
| `spatial:bbox` | array of number | required | the bounding box of the first resolution group ([§5.1](#51-the-grid-of-a-resolution)), so that a reader finds where the product is without opening a group |
| `spatial:dimensions` | array of string | optional | for a Level-2A product: `["y", "x"]` |
| `spatial:registration` | string | optional | for a Level-2A product: `"pixel"` |
| `multiscales` | [`SafeMultiscales`](#type-safemultiscales) | optional | for a Level-2A product |

<a id="type-safemultiscales"></a>**`SafeMultiscales`**, the root's `multiscales`:

| member | type | presence | value |
|---|---|---|---|
| `layout` | array of [`SafeLayoutItem`](#type-safelayoutitem) | required | one layout item per resolution group, in the order of [§3](#3-the-hierarchy) |

<a id="type-safelayoutitem"></a>**`SafeLayoutItem`**:

| member | type | presence | value |
|---|---|---|---|
| `asset` | string | required | the group's name |
| `spatial:shape` | array of integer | required | the group's `spatial:shape` |
| `spatial:transform` | array of number | required | the group's `spatial:transform` |

For example, `multiscales` is

```json
{"layout": [
  {"asset": "r10m", "spatial:shape": [10980, 10980], "spatial:transform": [10.0, 0.0, 499980.0, 0.0, -10.0, 5200020.0]},
  {"asset": "r20m", "spatial:shape": [5490, 5490], "spatial:transform": [20.0, 0.0, 499980.0, 0.0, -20.0, 5200020.0]},
  {"asset": "r60m", "spatial:shape": [1830, 1830], "spatial:transform": [60.0, 0.0, 499980.0, 0.0, -60.0, 5200020.0]}
]}
```

A Level-2A product presents most of its bands at 10, 20 and 60 m. Its
coarser groups are resampled versions of the finer ones, made by the
processor, and a reader that wants a band at lower resolution can choose
among them. The layout items have no `derived_from` and no `transform`.
The product metadata does not say how the coarser bands were made, or from
which resolution, so the hierarchy does not claim it. A Level-1C product
has each band at only one resolution, so its groups are not levels of one
image, and its root has no `multiscales`.

In this version, the JPEG 2000 resolution levels inside each band file (4
wavelet decompositions) are not presented as multiscales levels, and the
`proj` and `spatial` members are on the groups only: the band arrays carry
neither, since the conventions apply a group's members to its child
arrays.

## 6. Source metadata

### 6.1 Values

The values this section copies from the XML are translated as follows (in
place of [conventions §6](../conventions.md#6-source-metadata-as-json)'s rules for
binary fields, which do not apply):

| of the XML | in JSON |
|---|---|
| an element's **value** | its text ([§2.1](#21-objects)). If it is a **decimal number** (an optional `-` or `+`, one or more digits, optionally `.` and one or more digits, and optionally `e` or `E`, an optional sign and one or more digits), it is a JSON number: an integer when it has neither a fraction nor an exponent ([conventions §6](../conventions.md#6-source-metadata-as-json): the string of its digits beyond 2^53 − 1), and otherwise its binary64 value (a [`Float`](../conventions.md#type-float), so one that overflows binary64 is `"Infinity"` or `"-Infinity"`). A `+` and leading zeros are not kept. Otherwise the value is the JSON string of the text. Its type is number or string |
| a **measure**, an element with an attribute `unit` | the object `{"value": V, "unit": T}`, a [`SafeMeasure`](#type-safemeasure) |
| a measure without the attribute `unit` | its value alone |
| a field that is missing, or found more than once where one is named | absent |

**Source metadata never rejects**: only what [§2](#2-the-source) reads for the
layout is checked.

<a id="type-safemeasure"></a>**`SafeMeasure`**:

| member | type | presence | value |
|---|---|---|---|
| `value` | number or string | required | `V`, the element's value |
| `unit` | string | required | `T`, the attribute's value as a string |

The XML documents themselves are kept whole ([§6.3](#63-the-source-metadata-node)), and they are the
record of the product's metadata. What follows is a **derived
convenience**: a fixed set of fields copied from them into JSON, not a
translation of the XML. It holds what a reader needs to
interpret a band's values, kept on the band's array, and the product's
identity, kept on the root. TECHNIQUES.md's criteria decide what goes in
it. Each value is small, typed and semantic, and is wanted by nearly every
reader of the band, who would otherwise have to fetch and parse the
product metadata. The rest (the angle grids, the datastrip's
per-detector tables, the quality reports) is large, or a column of
numbers, or rarely wanted. It stays in the XML, which is kept whole and
read only by a reader who wants it. A general translation of XML into JSON
is not attempted: the product metadata alone holds spectral response curves
of hundreds of numbers per band, and the datastrip metadata is 15–25 MB.

### 6.2 Band arrays

Let `X` be the product metadata's element
`General_Info/Product_Image_Characteristics` under its root. A band whose
band name is `B` followed by two digits `dd`, or `B8A`, is a **spectral
band**, whose physical band name `PB` is `B` followed by `dd` without a
leading `0` (`B02` is `B2`), or `B8A`. Its **band id** `i` is the attribute
`bandId` of the element `X/Spectral_Information_List/Spectral_Information`
whose attribute `physicalBand` is `PB`, read as an integer, when exactly one
such element has a `bandId` of one to nine digits. Below, an element "whose
`bandId` (or `band_id`) is `i`" is one whose attribute of that name is one
to nine digits of the value `i`.

A band's source metadata `S` is an object with these members, in this
order, each present only when its value is, a
<a id="type-safeband"></a>**`SafeBand`** (a value and a measure are [§6.1](#61-values)'s):

| member | type | presence | value |
|---|---|---|---|
| `IMAGE_FILE` | string | required | the image file's text ([§2.3](#23-image-files)) |
| `bandId` | integer | optional | for a spectral band with band id `i`: `i` |
| `physicalBand` | string | optional | for a spectral band with band id `i`: `PB` |
| `RESOLUTION` | number or string | optional | for a spectral band with band id `i`: the value of `RESOLUTION` under its `Spectral_Information` |
| `Wavelength` | [`SafeWavelength`](#type-safewavelength) | optional | for a spectral band with band id `i`: an object of the measures `MIN`, `MAX` and `CENTRAL` under its `Spectral_Information/Wavelength`, those present |
| `PHYSICAL_GAINS` | number or string | optional | for a spectral band with band id `i`: the value of the element `X/PHYSICAL_GAINS` whose `bandId` is `i` |
| `SOLAR_IRRADIANCE` | [`SafeMeasure`](#type-safemeasure), number or string | optional | for a spectral band with band id `i`: the measure `X/Reflectance_Conversion/Solar_Irradiance_List/SOLAR_IRRADIANCE` whose `bandId` is `i` |
| `QUANTIFICATION_VALUE` | [`SafeMeasure`](#type-safemeasure), number or string | optional | Level-1C, for a spectral band with band id `i`: the measure `X/QUANTIFICATION_VALUE` |
| `RADIO_ADD_OFFSET` | number or string | optional | Level-1C, for a spectral band with band id `i`: the value of `X/Radiometric_Offset_List/RADIO_ADD_OFFSET` whose `band_id` is `i` |
| `BOA_QUANTIFICATION_VALUE` | [`SafeMeasure`](#type-safemeasure), number or string | optional | Level-2A, for a spectral band with band id `i`: the measure `X/QUANTIFICATION_VALUES_LIST/BOA_QUANTIFICATION_VALUE` |
| `BOA_ADD_OFFSET` | number or string | optional | Level-2A, for a spectral band with band id `i`: the value of `X/BOA_ADD_OFFSET_VALUES_LIST/BOA_ADD_OFFSET` whose `band_id` is `i` |
| `AOT_QUANTIFICATION_VALUE` | [`SafeMeasure`](#type-safemeasure), number or string | optional | for the band `AOT` (Level-2A): the measure `X/QUANTIFICATION_VALUES_LIST/AOT_QUANTIFICATION_VALUE` |
| `WVP_QUANTIFICATION_VALUE` | [`SafeMeasure`](#type-safemeasure), number or string | optional | for the band `WVP` (Level-2A): the measure `X/QUANTIFICATION_VALUES_LIST/WVP_QUANTIFICATION_VALUE` |
| `Scene_Classification_List` | array of [`SafeSceneClassification`](#type-safesceneclassification) | optional | for the band `SCL` (Level-2A): one object per element `X/Scene_Classification_List/Scene_Classification_ID`, in order (and absent when there are more than 64 such elements, or none; a product has 12) |
| `siz` | [`Base64`](../conventions.md#type-base64) | required | the base64 ([conventions §6](../conventions.md#6-source-metadata-as-json)) of the band file's SIZ marker segment, from its `FF 51` marker to its end, which the chunks replace with their own |

<a id="type-safewavelength"></a>**`SafeWavelength`**, present when it has a member:

| member | type | presence | value |
|---|---|---|---|
| `MIN` | [`SafeMeasure`](#type-safemeasure), number or string | optional | the measure `MIN` under `Spectral_Information/Wavelength` |
| `MAX` | [`SafeMeasure`](#type-safemeasure), number or string | optional | the measure `MAX` under `Spectral_Information/Wavelength` |
| `CENTRAL` | [`SafeMeasure`](#type-safemeasure), number or string | optional | the measure `CENTRAL` under `Spectral_Information/Wavelength` |

<a id="type-safesceneclassification"></a>**`SafeSceneClassification`**, an element
`Scene_Classification_ID`, with the values of its children, those present:

| member | type | presence | value |
|---|---|---|---|
| `SCENE_CLASSIFICATION_TEXT` | number or string | optional | the value of its child `SCENE_CLASSIFICATION_TEXT` |
| `SCENE_CLASSIFICATION_INDEX` | number or string | optional | the value of its child `SCENE_CLASSIFICATION_INDEX` |

So the band `B04` of a Level-2A product of processing baseline 05.10 has:

```json
{
  "IMAGE_FILE": "GRANULE/L2A_T32TNS_A035655_20240103T101328/IMG_DATA/R10m/T32TNS_20240103T101329_B04_10m",
  "bandId": 3,
  "physicalBand": "B4",
  "RESOLUTION": 10,
  "Wavelength": {"MIN": {"value": 646, "unit": "nm"}, "MAX": {"value": 685, "unit": "nm"}, "CENTRAL": {"value": 665, "unit": "nm"}},
  "PHYSICAL_GAINS": 4.76278246,
  "SOLAR_IRRADIANCE": {"value": 1512.79, "unit": "W/m²/µm"},
  "BOA_QUANTIFICATION_VALUE": {"value": 10000, "unit": "none"},
  "BOA_ADD_OFFSET": -1000,
  "siz": "/1EAKQAAAAAq5AAAKuQAAAAAAAAAAAAABAAAAAQAAAAAAAAAAAAAAQ4BAQ=="
}
```

**The values are not applied.** The arrays hold the digital numbers the
band files hold. Since processing baseline 04.00, a Level-1C reflectance is
`(DN + RADIO_ADD_OFFSET) / QUANTIFICATION_VALUE`, and a Level-2A reflectance
is `(DN + BOA_ADD_OFFSET) / BOA_QUANTIFICATION_VALUE`. Earlier baselines have
no offset, and their products have no offset member. The convention writes
no `scale_factor`, `add_offset` or `_FillValue` attributes, which a reader
would apply.

### 6.3 The source metadata node

The root's source metadata `S` is the object

```json
{
  "PRODUCT_URI": "S2B_MSIL2A_20240103T101329_N0510_R022_T32TNS_20240103T113848.SAFE",
  "PROCESSING_LEVEL": "Level-2A",
  "PRODUCT_TYPE": "S2MSI2A",
  "PROCESSING_BASELINE": "05.10",
  "Special_Values": [{"SPECIAL_VALUE_TEXT": "NODATA", "SPECIAL_VALUE_INDEX": 0}, {"SPECIAL_VALUE_TEXT": "SATURATED", "SPECIAL_VALUE_INDEX": 65535}],
  "U": 1.03421885250175,
  "TILE_ID": "S2B_OPER_MSI_L2A_TL_2BPS_20240103T113848_A035655_T32TNS_N05.10",
  "SENSING_TIME": "2024-01-03T10:18:03.604815Z",
  "HORIZONTAL_CS_NAME": "WGS84 / UTM zone 32N",
  "HORIZONTAL_CS_CODE": "EPSG:32632"
}
```

a <a id="type-saferoot"></a>**`SafeRoot`**, with the members, in this order, each present only
when its value is (`X` is [§6.2](#62-band-arrays)'s):

| member | type | presence | value |
|---|---|---|---|
| `PRODUCT_URI` | string | optional | the value of that child of `General_Info/Product_Info` in the product metadata, as a string (below) |
| `PROCESSING_LEVEL` | string | optional | the value of that child of `General_Info/Product_Info` in the product metadata, as a string |
| `PRODUCT_TYPE` | string | optional | the value of that child of `General_Info/Product_Info` in the product metadata, as a string |
| `PROCESSING_BASELINE` | string | optional | the value of that child of `General_Info/Product_Info` in the product metadata, as a string |
| `Special_Values` | array of [`SafeSpecialValue`](#type-safespecialvalue) | optional | one object per element `X/Special_Values`, in order (and absent when there are more than 64 such elements, or none; a product has 2) |
| `U` | number or string | optional | the value of `X/Reflectance_Conversion/U`, the Earth–Sun distance correction |
| `TILE_ID` | string | optional | the text of that child of `General_Info` in the tile metadata, as a string |
| `SENSING_TIME` | string | optional | the text of that child of `General_Info` in the tile metadata, as a string |
| `HORIZONTAL_CS_NAME` | string | optional | the text of that child of `Tile_Geocoding`, as a string |
| `HORIZONTAL_CS_CODE` | string | required | the text of that child of `Tile_Geocoding`, as a string: the CRS code, which [§2.4](#24-the-tile-geocoding) requires |

(`PROCESSING_BASELINE` is the string `"05.10"`, not a number: it is a version, but by
[§6.1](#61-values) its text is a decimal number. As an exception, the first four are always
strings.)

<a id="type-safespecialvalue"></a>**`SafeSpecialValue`**, an element `Special_Values`, with
the values of its children, those present:

| member | type | presence | value |
|---|---|---|---|
| `SPECIAL_VALUE_TEXT` | number or string | optional | the value of its child `SPECIAL_VALUE_TEXT` |
| `SPECIAL_VALUE_INDEX` | number or string | optional | the value of its child `SPECIAL_VALUE_INDEX` |

**`refkv_source`.** The group `refkv_source` (the source metadata node of
[conventions §2](../conventions.md#2-attributes)) is always present. It is the
product's **IR mirror** ([conventions §8](../conventions.md#8-the-ir-mirror)): its
table, from which the product is rebuilt ([§6.4](#64-reconstruction)), and its view,
`refkv_source/tree`. Its source metadata `S` is the object

```json
{"ir": I, "empty_dirs": [...], "ignored": [...]}
```

a <a id="type-safemirror"></a>**`SafeMirror`**: a
[`Mirror`](../conventions.md#type-mirror) with these members, each but `ir` present only
when it is not empty:

| member | type | presence | value |
|---|---|---|---|
| `ir` | [`IrDescription`](../conventions.md#type-irdescription) | required | `I`, the mirror's description ([conventions §8.2](../conventions.md#82-the-table)) |
| `empty_dirs` | array of string | optional | the empty directories ([§2.1](#21-objects)), ascending, each without a final `/` |
| `ignored` | array of string | optional | the keys of the objects that the listing recorded but did not use ([spec/zarrify.md §1.4](../zarrify.md#14-store-inputs), and for a zip file its directory entries that are recorded likewise, [§8.4](#84-the-zip-form)), ascending |

(Neither is an object of the product, so the IR has neither: a listing's
empty `d/` keys and ignored keys are not objects of a store IR,
[conventions §8.9](../conventions.md#89-stores).)

The IR is the product's in each form:

- **The directory form** is a store input, and its IR a **store IR**
  ([conventions §8.9](../conventions.md#89-stores)): every listed object is an
  object of the IR, its key its path, folder markers and empty objects
  included. Of these objects:

  | object | in the IR | its bytes |
  |---|---|---|
  | the **XML documents read whole** ([§8.2](#82-objects-and-reading)): the product metadata, the tile metadata, and every other XML document of at most 65536 bytes | **held**: each has one child, a value `xml` of type `ascii[n]` spanning it | the table holds them |
  | the **band files** | **described in place**: each has the band file's elements below, its leaves partitioning it | its own (it is the url source its chunks reference, [§8.8](#88-the-source-table)) |
  | every **other object** (the other XML documents, such as the datastrip metadata, 15–25 MB, and the other objects of [§2.1](#21-objects)) | **referenced**: it has no children | the hierarchy references it whole at the archive's key `refkv_source/objects/<k>`, with `k` the object's key escaped as [Zarr v2 convention §5](zarr2.md#5-other-objects) says (the directories between `refkv_source` and the objects are not nodes) |
  | a **folder marker** | it has no children and is not referenced | the mirror's layout ([§2.1](#21-objects)); its key is in the listing |
  | an **empty object** | it has no children | none; its key is in the listing |

- **The zip form** is a file input, and its IR a **file IR** of the
  `.SAFE.zip` file ([conventions §8.1](../conventions.md#81-elements)): its leaves
  partition the zip file, which the mirror rebuilds byte for byte from
  source 0. Its elements are the zip file's own structures, and, for each
  object of the product that is not empty, its zip entry: its local header,
  and its data, which is a band file's elements below, an XML document, or
  another object. A deflated XML document is a derived element of transform
  `deflate` (the raw deflate stream of the entry, which a reader inflates
  to the document): its space holds nothing.

**Elements.** Paths are relative to the IR's root, whose path is `""`
([conventions §8.1](../conventions.md#81-elements)); `<i>`, `<k>`, `<m>` are
decimal name indexes. In the directory form, a band file's elements are below
its object (whose path is its key, [conventions §8.9](../conventions.md#89-stores)), at
offsets from its start; in the zip form, below `entries/<i>/data` of its zip
entry, at offsets in the zip file. Types are those of
[conventions §8.6](../conventions.md#86-types).

| path | kind | type | extent: what |
|---|---|---|---|
| `""` | struct | | the root: the store's root directory (no extent), or the zip file, `(0, size)` |
| `<directory>` | struct | | directory form: a directory of the listing, no extent |
| `<key>` | object | | directory form: the object of that key, `(0, size)` (held, described in place, or referenced, above) |
| `<key>/xml` | value | `ascii[n]` | an XML document read whole: all of it |
| `…/boxes/<i>` | struct | | a band file's box `i` ([§8.3](#83-band-files)), from offset 0, the `jp2c` box last |
| `…/boxes/<i>/header` | value | `{lbox:>u4,tbox:ascii[4]}`, or `{lbox:>u4,tbox:ascii[4],xlbox:>u8}` | its `LBox`, `TBox` and `XLBox` |
| `…/boxes/<i>/contents` | value | `bytes[n]` | the contents of a box other than `jp2c` (when not empty) |
| `…/boxes/<i>/codestream` | struct | | the `jp2c` box's contents: the codestream `[c0, n)` |
| `…/codestream/soc` | value | `>u2` | SOC |
| `…/codestream/siz` | value | `{marker:>u2,lsiz:>u2,rsiz:>u2,xsiz:>u4,ysiz:>u4,xosiz:>u4,yosiz:>u4,xtsiz:>u4,ytsiz:>u4,xtosiz:>u4,ytosiz:>u4,csiz:>u2,components:{ssiz:u1,xrsiz:u1,yrsiz:u1}[C]}` | the SIZ segment |
| `…/codestream/markers/<m>` | struct | | marker segment `m` of the main header rest `R`, in order |
| `…/markers/<m>/header` | value | `{marker:>u2,length:>u2}` | its marker and length `L` |
| `…/markers/<m>/body` | value | `bytes[L − 2]` | the rest of the segment (when not empty) |
| `…/codestream/tiles/<k>` | struct | | the tile-part of tile `k`, `[s_k, s_k + Psot)` |
| `…/tiles/<k>/sot` | value | `{marker:>u2,lsot:>u2,isot:>u2,psot:>u4,tpsot:u1,tnsot:u1}` | its SOT segment |
| `…/tiles/<k>/body` | data | | its body `[s_k + 12, s_k + Psot)`; form `{"codec":"jpeg2000-tile-part","recipe":[["src",0,null]]}` |
| `…/codestream/eoc` | value | `>u2` | EOC |
| `end` | value | `{signature:<u4,disk:<u2,cd_disk:<u2,entries_here:<u2,entries:<u2,cd_size:<u4,cd_offset:<u4,comment_length:<u2}` | zip form: the EOCD record |
| `end_comment` | value | `ascii[n]` | zip form: its comment (when not empty) |
| `zip64_locator` | value | `{signature:<u4,cd_disk:<u4,record_offset:<u8,disks:<u4}` | zip form: the ZIP64 EOCD locator (ZIP64 only) |
| `zip64_end` | value | `{signature:<u4,size:<u8,version_made:<u2,version_needed:<u2,disk:<u4,cd_disk:<u4,entries_here:<u8,entries:<u8,cd_size:<u8,cd_offset:<u8}` | zip form: the ZIP64 EOCD record's first 56 bytes (ZIP64 only) |
| `central_directory` | struct | | zip form: the central directory (when not empty) |
| `central_directory/entries/<i>` | struct | | its file header `i` |
| `…/entries/<i>/header` | value | `{signature:<u4,version_made:<u2,version_needed:<u2,flags:<u2,method:<u2,time:<u2,date:<u2,crc32:<u4,compressed_size:<u4,size:<u4,name_length:<u2,extra_length:<u2,comment_length:<u2,disk:<u2,internal:<u2,external:<u4,local_header_offset:<u4}` | its 46 bytes |
| `…/entries/<i>/name`, `…/comment` | value | `ascii[n]` | its name and comment (when not empty) |
| `…/entries/<i>/extra` | value | `bytes[n]` | its extra field (when not empty) |
| `entries/<i>` | struct | | zip form: the local header and data of central directory entry `i`, for an object of the product that is not empty, `[lho, ds + cs)` |
| `entries/<i>/local_header` | value | `{signature:<u4,version:<u2,flags:<u2,method:<u2,time:<u2,date:<u2,crc32:<u4,compressed_size:<u4,size:<u4,name_length:<u2,extra_length:<u2}` | its local file header's 30 bytes |
| `entries/<i>/name` | value | `ascii[n]` | the local header's name (when not empty) |
| `entries/<i>/extra` | value | `bytes[n]` | the local header's extra field (when not empty) |
| `entries/<i>/data` | struct | | a band file's data: its elements |
| `entries/<i>/data` | value | `ascii[n]` | a stored XML document |
| `entries/<i>/data` | derived | | a deflated XML document: form `{"transform":"deflate"}` |
| `entries/<i>/data` | value | `bytes[n]` | another object's data |
| `gaps/<offset>` | gap | | bytes no other element claims (in the zip form: the local headers of empty objects and directory entries, data descriptors, padding) |

A value whose type is a record, a number or `ascii[n]` of at most 1024
bytes is shown by the view ([conventions §8.7](../conventions.md#87-the-view)); in
the directory form, its bytes are read from the object that holds it, or,
for a band file, from the band file itself.

**Equivalence.** Hierarchies of one product in one form are compared entry for
entry, `refkv_source` included ([conventions §11.4](../conventions.md#114-equivalence)).

### 6.4 Reconstruction

The product can be rebuilt from the hierarchy:

| form | rebuilt |
|---|---|
| **the zip form** | the zip file, byte for byte, from the mirror's leaves and source 0 ([conventions §8.4](../conventions.md#84-reading-the-source-back)); each object is an entry of it (a deflated XML document inflated) |
| **the directory form** | the listing (every object's key and size) and every held object, byte for byte, from the mirror alone ([conventions §8.9](../conventions.md#89-stores)); each band file, described in place, from its own source (the url source its chunks reference, whose size the archive pins), its structure from its elements; each other object from `refkv_source/objects/<k>`; each empty directory from `empty_dirs` (every other directory is one of an object's) |

What is not kept is the directory form's layout: the order of the listing,
the mirror's own metadata (generations and storage class), and the bytes of
its folder markers ([§2.1](#21-objects)), whose keys the listing keeps. (A folder
marker's 6 bytes are not kept, but its directory is.)

## 7. Example

The resolution group `r20m` of the Level-2A product
`https://storage.googleapis.com/gcp-public-data-sentinel-2/L2/tiles/32/T/NS/S2B_MSIL2A_20240103T101329_N0510_R022_T32TNS_20240103T113848.SAFE/`:

```json
{
  "zarr_format": 3,
  "node_type": "group",
  "attributes": {
    "zarr_conventions": [
      {"uuid": "f17cb550-5864-4468-aeb7-f3180cfb622f", "schema_url": "https://raw.githubusercontent.com/zarr-conventions/proj/refs/tags/v0.1/schema.json", "spec_url": "https://github.com/zarr-conventions/proj/blob/v0.1/README.md", "name": "proj", "description": "Coordinate reference system information for geospatial data"},
      {"uuid": "689b58e2-cf7b-45e0-9fff-9cfc0883d6b4", "schema_url": "https://raw.githubusercontent.com/zarr-conventions/spatial/refs/tags/v0.1/schema.json", "spec_url": "https://github.com/zarr-conventions/spatial/blob/v0.1/README.md", "name": "spatial", "description": "Spatial coordinate information"}
    ],
    "proj:code": "EPSG:32632",
    "proj:wkt2": "PROJCRS[\"WGS 84 / UTM zone 32N\",BASEGEOGCRS[…]]",
    "spatial:dimensions": ["y", "x"],
    "spatial:transform": [20.0, 0.0, 499980.0, 0.0, -20.0, 5200020.0],
    "spatial:shape": [5490, 5490],
    "spatial:bbox": [499980.0, 5090220.0, 609780.0, 5200020.0],
    "spatial:registration": "pixel"
  }
}
```

(with `proj:wkt2` shortened). Its array `B05`, from `T32TNS_20240103T101329_B05_20m.jp2` (5490 × 5490,
15-bit, tiles of 640 × 640: 9 × 9 tiles, the last row and column 370
pixels):

```json
{
  "zarr_format": 3,
  "node_type": "array",
  "shape": [5490, 5490],
  "data_type": "uint16",
  "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": [640, 640]}},
  "chunk_key_encoding": {"name": "default", "configuration": {"separator": "/"}},
  "fill_value": 0,
  "codecs": [{"name": "imagecodecs_jpeg2k"}],
  "dimension_names": ["y", "x"],
  "attributes": {
    "zarr_conventions": [{"uuid": "ef81346c-19e8-42ad-93b0-a279ccaf44c1", "schema_url": "https://raw.githubusercontent.com/d-v-b/refkv/refs/heads/main/spec/zarrify/safe/schema.json", "spec_url": "https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safe.md", "name": "refkv_zarrified", "description": "The Zarr layout of a Sentinel-2 SAFE source zarrified by refkv, and the source's metadata"}],
    "refkv_zarrified": {"safe": {
      "IMAGE_FILE": "GRANULE/L2A_T32TNS_A035655_20240103T101328/IMG_DATA/R20m/T32TNS_20240103T101329_B05_20m",
      "bandId": 4, "physicalBand": "B5", "RESOLUTION": 20,
      "Wavelength": {"MIN": {"value": 694, "unit": "nm"}, "MAX": {"value": 714, "unit": "nm"}, "CENTRAL": {"value": 703.8, "unit": "nm"}},
      "PHYSICAL_GAINS": 5.17211547,
      "SOLAR_IRRADIANCE": {"value": 1425.78, "unit": "W/m²/µm"},
      "BOA_QUANTIFICATION_VALUE": {"value": 10000, "unit": "none"},
      "BOA_ADD_OFFSET": -1000,
      "siz": "/1EAKQAAAAAVcgAAFXIAAAAAAAAAAAAAAoAAAAKAAAAAAAAAAAAAAQ4BAQ=="
    }}
  }
}
```

The chunk `r20m/B05/c/8/8` is the codestream of tile 80, whose area is
`[5120, 5490) × [5120, 5490)`. Its image area is `[5120, 5760)²`, its tiles
are 370 × 370, and its tiles 1, 2 and 3 hold empty packets.

# Part 2. The profile

The SAFE profile of [spec/zarrify.md](../zarrify.md) (revision 26, draft) is [§8](#8-safe-profile) of this document. (Conformance is spec/zarrify.md's [Appendix A](../zarrify.md#appendix-a-conformance).)
What every profile's Part 2 shares is in
[spec/zarrify.md, Profiles](../zarrify.md#profiles), with the changes this document
states to its [§1](../zarrify.md#1-introduction) and [§2](../zarrify.md#2-the-zarr-layout).

## 8. SAFE profile

A Sentinel-2 Level-1C or Level-2A product in the SAFE format is read in one
of two forms:

| form | input | `U` | read |
|---|---|---|---|
| **the directory form** | a store input ([§1.4](../zarrify.md#14-store-inputs)) | the URL of the `.SAFE` directory, ending in `/` | it is listed as [§1.5](../zarrify.md#15-listing-a-store) says, and the profile is chosen there ([§8.1](#81-choosing-the-profile)) |
| **the zip form** | a file input ([§1.2](../zarrify.md#12-input)) | the URL of a `.SAFE.zip` file | its entries are the product's objects ([§8.4](#84-the-zip-form)) |

The two forms give the same hierarchy for the same product, but for
`source.url`, the source table and `refkv_source` (the IR mirror of a store
or of a file, [the convention §6.3](#63-the-source-metadata-node)): every other document is the same,
and every chunk holds the same bytes.

### 8.1 Choosing the profile

**Directory form.** [spec/zarrify.md §1.4](../zarrify.md#14-store-inputs)'s table gains a row, after the N5
row:

| root keys | |
|---|---|
| `manifest.safe` | SAFE profile ([§8](#8-safe-profile)) |

**Zip form.** The zip form's **signature** ([spec/zarrify.md §1.2](../zarrify.md#12-input), tested at the precedence of
[profiles/safe.json](profiles/safe.json)): bytes 0–3 of `H` are
`50 4B 03 04`, a ZIP local file header.

A zip file that is not a SAFE product is rejected by [§8.4](#84-the-zip-form) or by the
convention.

### 8.2 Objects and reading

The product's objects are, in the directory form, the store's objects
([§1.4](../zarrify.md#14-store-inputs)), and in the zip form its entries ([§8.4](#84-the-zip-form)). The profile reads:

- the product metadata and the tile metadata
  ([the convention §2](#2-the-source)), whole;
- in the directory form, every other XML document of at most 65536 bytes,
  whole: these and the product and tile metadata are the IR's held objects
  ([the convention §6.3](#63-the-source-metadata-node));
- in the zip form, every other deflated XML document, whole, to inflate
  and check it ([§8.4](#84-the-zip-form));
- of each band file, its boxes and codestream main header ([§8.3](#83-band-files)) and the
  first 12 bytes of each tile-part (its SOT marker segment), as the walk of
  [§8.3](#83-band-files) reaches them: from offset 0, in blocks of 64 KiB (each block from
  the first byte the walk needs that the blocks read so far lack); then each SOT segment, the first in a block of 64 KiB (unless a block read for the main header holds it), each later one in 12 bytes, or in a block of 64 KiB after a tile-part of at most 16384 bytes; then the 2 bytes of EOC. The band files are walked
  together: each round reads the next range of every band file whose walk
  is not done.

It reads no other bytes (but, in the zip form, those of [§8.4](#84-the-zip-form), and
the values the view shows that the walk did not read, [conventions §8.7](../conventions.md#87-the-view)).
It does not read the folder markers
([the convention §2.1](#21-objects)), the other objects, or the other XML
documents. The reads of a band file include no coded data
but what the blocks a reader fetches happen to hold (**Structure only**,
[§1.2](../zarrify.md#12-input)). In the directory form, this replaces [§1.4](../zarrify.md#14-store-inputs)'s rule that an object is
read whole, and the listing's empty objects whose keys end in `/` (which
[§1.4](../zarrify.md#14-store-inputs) ignores without recording them) name directories (the convention
[§2.1](#21-objects)): a band file is read in ranges, with HTTP range requests. A
response that is not of the requested length is a failure.

### 8.3 Band files

A band file of `n` bytes is read as a JP2 file (ISO/IEC 15444-1, Annex I).
All integers are big-endian.

**Boxes.** A box at offset `o` is a `u32` `LBox` and a 4-byte type `TBox`,
then, when `LBox` is 1, a `u64` `XLBox`. Its length is `LBox`, or `XLBox`
when `LBox` is 1 (which MUST then be at least 16), or, when `LBox` is 0,
`n − o`. A length other than these, below the box's header (8 or 16 bytes),
or reaching beyond `n`, rejects the input. The boxes are read from offset 0,
one after another (superboxes are not entered). The first MUST be
`LBox = 12`, `TBox = "jP  "` (`6A 50 20 20`), content `0D 0A 87 0A`, and the
second MUST have `TBox = "ftyp"`. The boxes are read until the first whose
`TBox` is `"jp2c"`, which MUST be within the first 1024 boxes, MUST end at
`n`, and whose contents (after its header), `[c0, c1)` with `c1 = n`, are
the **codestream**. The bytes `[0, c0)` are the band file's **JP2 header**.

**Main header.** All markers below must lie within the codestream, or the
input is rejected.

- At `c0`: SOC, `FF 4F`.
- At `c0 + 2`: the SIZ marker `FF 51` and its segment of length `Lsiz`
  (the 2-byte field after the marker, counting itself): `Rsiz` (`u16`),
  `Xsiz`, `Ysiz`, `XOsiz`, `YOsiz`, `XTsiz`, `YTsiz`, `XTOsiz`, `YTOsiz`
  (`u32`), `Csiz` (`u16`), then `Csiz` triples `Ssiz`, `XRsiz`, `YRsiz`
  (`u8`). `Lsiz` MUST be `38 + 3 × Csiz`. Bits 14 and 15 of `Rsiz` (High
  Throughput, Part 2 extensions) MUST be 0. `XOsiz`, `YOsiz`, `XTOsiz` and
  `YTOsiz` MUST be 0, and `Xsiz`, `Ysiz`, `XTsiz` and `YTsiz` at least 1.
  `Csiz` MUST be 1 or 3. Every `Ssiz` MUST be the same, with bit 7 (signed)
  0, giving the precision `p = (Ssiz & 0x7F) + 1`, which MUST be at most
  16. Every `XRsiz` and `YRsiz` MUST be 1. The SIZ **segment** is the
  `2 + Lsiz` bytes from its marker.
- Then marker segments, each a marker `FF xx` followed by a `u16` length
  `L ≥ 2` and `L − 2` more bytes, up to the first SOT marker `FF 90`. Their
  markers MUST be among COD `FF 52` (exactly one), COC `FF 53`, QCD `FF 5C`
  (exactly one), QCC `FF 5D`, RGN `FF 5E`, POC `FF 5F`, CRG `FF 63` and COM
  `FF 64`. Any other (TLM `FF 55`, PLM `FF 57`, PPM `FF 60`, CAP `FF 50`,
  CPF `FF 59`, or one this list does not name) rejects the input: TLM and
  PLM describe the file's tile-parts and would be wrong in a chunk, and PPM
  moves the packet headers out of the tiles. The bytes from the end of the
  SIZ segment to the first SOT are the **main header rest** `R`, which is
  at least the COD and QCD segments, and MUST be at most 65536 bytes (it is
  a data source, [§8.6](#86-chunks), copied into the output; Kakadu's is 148 bytes).
- **COD**: `Scod` (`u8`), which MUST be 0 or 1 (no SOP or EPH markers, and
  default precinct anchors), the progression order (`u8`), the number of
  layers `Lay` (`u16`, at least 1), the multiple component transform
  (`u8`), the number of decomposition levels `N` (`u8`, at most 32), the
  code-block width and height exponents, the code-block style and the
  transform (`u8` each), and, when `Scod` is 1, `N + 1` precinct size bytes
  `PP_r` (for `r = 0 … N`): `PPx_r = PP_r & 0x0F` and `PPy_r = PP_r >> 4`.
  When `Scod` is 0, every `PPx_r` and `PPy_r` is 15. The segment's length
  MUST be `12`, or `13 + N` when `Scod` is 1.
- **COC**: `Ccoc` (`u8`, since `Csiz < 257`), which MUST be less than
  `Csiz`, with at most one COC per component, then `Scoc` (`u8`, 0 or 1)
  and the same fields as COD's from `N` on. A component with a COC has its
  `N` and `PP_r`; any other has COD's.

**Tile-parts.** The number of tiles is `nt = nx × ny`, with
`nx = ceil(Xsiz / XTsiz)` and `ny = ceil(Ysiz / YTsiz)`, and it MUST be at
most 65535. The tile-parts are read from the first SOT, at `s_0`: for
`k = 0, 1, …, nt − 1`, the 12 bytes at `s_k` MUST be `FF 90`, `Lsot = 10`,
`Isot = k` (`u16`), `Psot` (`u32`) at least 14, `TPsot = 0` and `TNsot = 1`,
with `s_k + Psot ≤ c1 − 2`; then `s_{k+1} = s_k + Psot`. The two bytes at
`s_nt` MUST be EOC `FF D9`, and `s_nt + 2` MUST be `c1`. So each tile has
one tile-part, in raster order, and the codestream ends there. The
tile-part of tile `k` is the bytes `[s_k, s_k + Psot)`. Its body (the rest
of its header, its PLT markers among them, and its packets) is
`[s_k + 12, s_k + Psot)`.

### 8.4 The zip form

The file is a ZIP archive (PKWARE APPNOTE 6.3), read from its end. All
integers are little-endian.

- **End of central directory.** The EOCD record is the last occurrence of
  `50 4B 05 06` in the file's last `min(n, 65557)` bytes for which the 22
  bytes of the record and its comment (of the length its last field gives)
  end exactly at the end of the file. If there is none, the input is
  rejected. Its disk numbers MUST be 0, and its two entry counts equal.
  When the entry count is `FFFF`, or the central directory's size or offset
  is `FFFFFFFF`, the 20 bytes before the EOCD MUST be a ZIP64 EOCD locator
  (`50 4B 06 07`, disk 0, one disk), and the record it locates a ZIP64
  EOCD record (`50 4B 06 06`), which gives the entry count, the size and
  the offset (`u64`). The central directory MUST lie within the file, and
  end where the ZIP64 record or the EOCD starts.
- **Central directory.** It is exactly `count` file headers
  (`50 4B 01 02`), filling its size. Of each the profile reads the flags,
  the method, the CRC-32, the compressed size `cs`, the uncompressed size
  `us`, the name, extra and comment lengths, the disk number (which MUST
  be 0) and the local header offset `lho`. For the fields that are
  `FFFFFFFF`, the ZIP64 extended information extra field (`0x0001`) MUST be
  present and give them, in the order `us`, `cs`, `lho`. Flag bit 0
  (encrypted) and bit 6 (strong encryption) MUST be 0. The name MUST be
  UTF-8 (the zip flag for UTF-8 is not consulted). No two entries may have
  the same name.
- **The product.** Every name MUST start with the same segment `D` followed
  by `/`, where `D` ends in `.SAFE`. An entry's key is its name without
  `D/`. An entry whose key is empty or ends in `/` is a directory entry. It
  is ignored, and its key is recorded unless its size is 0 (as [§1.4](../zarrify.md#14-store-inputs) does
  for the listing). Its key, if not empty, names a directory (the
  convention [§2.1](#21-objects)). Otherwise a key MUST NOT contain an empty segment, `.`
  or `..`, nor a `\`. The product's objects are the other entries, with
  size `us`.
- **Empty objects.** An entry of size `us = 0` that is not a directory
  entry is an empty object: its method, its sizes and its local header are
  not read or checked.
- **Local headers.** For each other object, the 30 bytes at `lho` MUST be a
  local file header (`50 4B 03 04`), whose name length `nn` and extra
  length `ne` (bytes 26–29) give the object's data start `ds = lho + 30 + nn + ne`,
  and `ds + cs` MUST be at most the central directory's offset. The local
  header's other fields are not read: the central directory's are used. The
  objects' ranges `[lho, ds + cs)` MUST NOT overlap: entries that share
  bytes (several central entries naming one local entry, a zip bomb) are
  rejected.
- **Methods.** An object with method 0 (stored) MUST have `cs = us`, and
  its bytes are `[ds, ds + us)` of the file, which the output's chunks
  reference.
  An object with method 8 (deflate) MUST be an XML document
  ([the convention §2.1](#21-objects)), with
  `us` at most 2^26 (a datastrip metadata is 15–25 MB), and the `us` of all
  of them together MUST be at most 2^27; both are checked before anything is
  inflated. Each is read whole and inflated once, one at a time, not all at
  once (the product and tile metadata when they are parsed, the others
  after them). The raw deflate stream
  MUST end exactly at `cs` bytes and give exactly `us` bytes, whose CRC-32
  MUST be the entry's. The IR describes it as a derived element
  ([the convention §6.3](#63-the-source-metadata-node)), and the output holds no copy of its bytes:
  the mirror rebuilds the zip file. Any other
  method, and method 8 for any other object, rejects the input. (The
  pixels can only be referenced in place when they are stored. ESA's SAFE
  zips store every entry.)

The local headers and the deflated XML documents are read, and the reads of
[§8.2](#82-objects-and-reading) apply to the stored XML documents and band files, at their data
start.

### 8.5 Rejection and limits

The input is rejected when a check of [§1.2](../zarrify.md#12-input) (zip form) or [§1.4](../zarrify.md#14-store-inputs)–[§1.6](../zarrify.md#16-json-documents)
(directory form), [§8.3](#83-band-files) or [§8.4](#84-the-zip-form) fails, and when the convention gives it no
layout: wherever it says that the input is rejected, or that something MUST
hold and it does not. In particular:

- a product in the format used before December 2016, which has no
  `MTD_MSIL1C.xml` or `MTD_MSIL2A.xml`, or image files in more than one
  granule directory;
- a band file that is not a JP2 file, whose codestream has a TLM, PLM or
  PPM marker, SOP or EPH markers, more than one tile-part for some tile,
  tile-parts out of order, a nonzero image or tile origin, or a component
  that is signed, subsampled or of more than 16 bits;
- a band file whose size matches no resolution of the tile geocoding;
- a band file whose chunks would need empty tiles of more than 4096 bytes
  in one chunk, or more than the band file's size in all ([§8.6](#86-chunks));
- an XML document that the convention reads whose elements nest more than
  256 deep ([§1.5](../zarrify.md#15-listing-a-store));
- in the zip form, an encrypted entry, a band file or other object that is
  compressed, entries whose bytes overlap, a deflated XML document of more
  than 2^26 bytes, or deflated XML documents of more than 2^27 bytes
  together.

**Limits.** Every reference entry's payload MUST be at most 65519 bytes
([§1.2](../zarrify.md#12-input)). A chunk's payload is about 100 bytes for an interior tile, and
grows with the empty tiles of an edge chunk ([§8.6](#86-chunks)), which are bounded
there: at most 4096 bytes per chunk, and at most the band file's size for
all its chunks, before they are made. Every Sentinel-2 tiling is well
within these: the most measured is 1439 bytes, the corner chunk of a
Level-2A 60 m true-color image (1830 pixels in tiles of 256: an edge of 38,
so 48 empty tiles), and a band's are at most 4.3 kB in all, for band files
of 0.2–160 MB. An output key MUST be at most 65535 bytes and
MUST NOT start with `__vz__/` ([§1.4](../zarrify.md#14-store-inputs)). The browser implementation fails, as
for the other store profiles, on a listing of more than 100000 objects
([Appendix A](../zarrify.md#appendix-a-conformance)). A product has 60–1000 objects.

### 8.6 Chunks

The chunk of band array `<group>/<band>` at tile `t = v × nx + u`
([the convention §4](#4-arrays)), with
`T = XTsiz`, `U = YTsiz`, `x0 = u × T`, `y0 = v × U`,
`w = min(T, Xsiz − x0)`, `h = min(U, Ysiz − y0)`, `a = ceil(T / w)` and
`b = ceil(U / h)`, is the concatenation of these ranges, a
<a id="type-safechunk"></a>**`SafeChunk`** (its parts are numbered as pieces 1 to 5; its
ranges are [`Range`](../zarrify.md#type-range)s):

| part | range | presence | value |
|---|---|---|---|
| 1. the main header's start | a literal range | required | `FF 4F` (SOC), then the SIZ segment with the fields below |
| 2. the main header rest `R` | the source range `(d, 0, n_R)` of a data source `d`, `n_R` the length of `R` | required | a shared byte string ([§1.2](../zarrify.md#12-input)): the whole of the data source that holds it |
| 3. the tile-part's SOT | a literal range | required | the SOT segment `FF 90 00 0A`, `Isot = 0`, the tile-part's `Psot`, `00 01` |
| 4. the tile-part's body | in the zip form, the source range `(0, ds + s_t + 12, Psot − 12)`; in the directory form, `(f, s_t + 12, Psot − 12)`, `f` the band file's url source ([§8.8](#88-the-source-table)) | required | the tile-part's body `[s_t + 12, s_t + Psot)`, a range of the band file (in the zip form, offset by its data start `ds`) |
| 5. the **tail** | a literal range, or the source range `(d, 0, l)` of a data source `d` of the tail's length `l` | required | for `k = 1, …, a × b − 1`, an empty tile-part for the tile `k` (`i = k mod a` across, `j = k div a` down): `FF 90 00 0A`, `Isot = k`, `Psot = 14 + e_k`, `00 01`, then SOD `FF 93`, then `e_k` bytes `00`; then EOC `FF D9`. An interior chunk's tail is `FF D9`, a literal. An edge chunk's (`a × b > 1`) is a shared byte string ([§1.2](../zarrify.md#12-input)), like piece 2 |

The SIZ segment of piece 1 is the band file's with these fields:

| field | value |
|---|---|
| `Rsiz` | `0` |
| `Xsiz` | `x0 + T` |
| `Ysiz` | `y0 + U` |
| `XOsiz` | `x0` |
| `YOsiz` | `y0` |
| `XTsiz` | `w` |
| `YTsiz` | `h` |
| `XTOsiz` | `x0` |
| `YTOsiz` | `y0` |
| `Csiz` and the component triples | unchanged |

(`x0 + T` and `y0 + U` MUST be at most 2^32 − 1. `Rsiz` is 0 because a profile's
restrictions on the tile size may not hold for the new tiles. The file's own is kept in the
segment `siz` of the source metadata.)

**Bounds.** A tail is `2 + Σ_k (14 + e_k)` bytes. Before it is made, its
length is computed (first `2 + 14 × (a × b − 1)`, then with each `e_k`),
and it MUST be at most 4096 bytes. The tails of all the chunks of a band
file MUST be at most the band file's size `n` in all, which is checked
before any chunk of it is made. (So an `e_k` above 2^32 − 15, whose `Psot`
would not fit, is rejected, and the output's literals and data sources stay
within the input's size.)

**Empty tiles.** The tile `k` covers `[ex0, ex1) × [ey0, ey1)`, with
`ex0 = x0 + i × w`, `ex1 = min(x0 + (i + 1) × w, x0 + T)`, and `ey0`,
`ey1` likewise from `y0`, `j`, `h` and `U`. Its `e_k` empty packets, each
the single byte `00` (a packet header whose first bit says the packet is
empty), are one per layer, component, resolution and precinct. With
`N_c` and `PPx_r`, `PPy_r` those of component `c` ([§8.3](#83-band-files)):

```
e_k = Lay × Σ_c Σ_{r=0}^{N_c} nprec(ex0, ex1, N_c − r, PPx_r) × nprec(ey0, ey1, N_c − r, PPy_r)
nprec(z0, z1, d, e) = ceil(ceil(z1 / 2^d) / 2^e) − floor(ceil(z0 / 2^d) / 2^e)   when ceil(z1 / 2^d) > ceil(z0 / 2^d),
                      0 otherwise
```

(ISO/IEC 15444-1 B.5 and B.6: the resolution `r` of a tile-component
covers `[ceil(z0 / 2^(N_c − r)), ceil(z1 / 2^(N_c − r)))` and is cut into
precincts of `2^PP` anchored at 0.) This is the number of packets that
Kakadu writes for a tile of that area: it equals the number of packet
lengths in the PLT markers of every tile of the files examined in the
design of this profile, edge tiles included. Since Sentinel-2's tile sizes
are multiples of `2^N`, the empty tiles have the same structure as the
file's own edge tiles. The pixels of an empty tile decode to
`2^(p − 1)`, lie outside the array, and are not read.

So the chunk is a valid codestream with `a × b` tiles, of which tile 0 is
the file's tile `t` with its own coding unchanged. Its area on the
reference grid is the one it has in the file, so its packets decode
identically. An interior tile (`w = T`, `h = U`) has `a = b = 1` and no
empty tile. Chunk keys are `<group>/<band>/c/<v>/<u>`, or
`<group>/<band>/c/0/<v>/<u>` for a 3-component band.

**Data sources.** Piece 2 is the same in every chunk of a band, and in
every band file written with the same parameters (all the 15-bit bands of
one resolution, in the products examined). It is held once, in a data
source, so that reading a chunk reads only its tile-part from the band
file. Piece 2 sits at the start of the file, far from most tiles, so as a
range of the file it would cost a reader a separate request per chunk. An
edge chunk's tail is the same along an edge of a band, and in every band
of the same tiling and coding (2–5 distinct tails per band, in the products
examined), so it is held once too. The data sources are the distinct main
header rests and edge tails, numbered as [§1.2](../zarrify.md#12-input) says. They are COD, QCD and
COM marker segments (coding parameters and the encoder's comments), and
empty tile-parts, not pixel data ([§1.2](../zarrify.md#12-input), **Structure only**).

### 8.7 Other objects

In the directory form, each object that the IR references
([the convention §6.3](#63-the-source-metadata-node): an XML document
not read whole, and every other object but the band files, the folder
markers and the empty objects) is the entry `refkv_source/objects/<k>`, which
references the whole object, the range `(i, 0, size)` of its own source.
In the zip form there is no such entry: the mirror rebuilds the zip file,
every entry included.

The documents under `refkv_source/` are deflated and, like the chunks, not
among the documents a reader fetches when it opens the archive
([conventions §2](../conventions.md#2-attributes)).

### 8.8 The source table

References are listed in ascending order of their entries' keys (as UTF-8
byte strings), and within a reference in order of its ranges. This order
numbers the sources ([§1.2](../zarrify.md#12-input)'s order of first use).

| form | sources |
|---|---|
| **Zip form** | Source 0 is `U` ([§1.2](../zarrify.md#12-input)), and the data sources follow, from 1 |
| **Directory form** | The sources are first one `url` source per object that some reference uses (every band file, and every object [§8.7](#87-other-objects) references), with a `size` pin, the object's listed size, and the object's URL ([§1.4](../zarrify.md#14-store-inputs)), in order of first use, numbered from 0. The data sources follow, numbered from the count `m` of url sources in order of first use ([§1.2](../zarrify.md#12-input)'s rule, starting at `m` rather than 1) |

In the directory form, this replaces [§1.4](../zarrify.md#14-store-inputs)'s rule that a store
input's sources are one per entry and that it has no data sources: a band file is used by many
chunks, and by nothing else.

### 8.9 Summary

This section is informative. Both implementations print a one-line JSON
summary, with these members:

| member | value |
|---|---|
| `level` | `"L1C"` or `"L2A"` |
| `form` | `"store"` or `"zip"` |
| `groups` | the resolution groups |
| `bands` | the band arrays |
| `chunks` | the number of chunks |
| `edgeChunks` | the chunks with empty tiles |
| `dataSources` | the data sources |
| `objects` | the product's objects, folder markers included |
| `folderMarkers` | the folder markers |
| `emptyDirs` | the empty directories |
| `heldXml` | the XML documents held (directory form) |
| `otherObjects` | the objects referenced whole (directory form) |
| `emptyObjects` | the empty objects |
| `tileParts` | the SOT segments read |
| `bandReads` | the reads of band files |
| `batches` | the parser's rounds of reads |
| `listingRequests` | the listing's requests (directory form) |

It also has the run's or the read planner's counts, the IR's element count and what the
mirror's table folded.

### 8.10 Cost

This section is informative. With no TLM marker in the band files (Kakadu
writes none for Sentinel-2), the tile-parts are found by following each
`Psot` to the next: one 12-byte read per tile, each depending on the one
before. A Level-1C product has about 3100 tiles (121 for each 10 m band,
81 for each 20 m band, 100 for each 60 m band, and 1849 in the 256 × 256
tiles of its 10 m TCI). A Level-2A product has about 3300 (its 36 band files
include every band at 60 m, and its TCI has 1024 × 1024 tiles). The band files
are independent, so a zarrifier reads them concurrently. A 64 KiB read
block holds several tile headers only when the tiles are small (masks,
mostly empty tiles). The output is about 3100–3300 chunk entries, 10–40 data
sources (main header rests and edge tails), and the product's 60–150 other entries.

Measured on the 14 accepted products of `corpus_safe.txt` (revision 26,
through the harness's caching proxy): a product in the directory form takes
346–3217 range requests and 5.3–22 MB of reads (the reads after a small
tile-part fetch a 64 KiB block, which holds the next headers; after a larger
one, 12 bytes), the same requests and bytes as revision 25's; the zip form
2508–3197 requests and 2.5–22 MB. The archive is 1.04–1.22 MB in the
directory form (5–12% more than revision 25's: the held XML documents and the
mirror's table) and 1.01–1.06 MB in the zip form (1% more). Level-1C products
take longest: their true-color image's 1849 tile-part headers are read one
after another.
