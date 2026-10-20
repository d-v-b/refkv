# NDPI

The Zarr layout of a Hamamatsu NDPI slide, and its source model: the
elements of its IR, which `refkv_source` holds as the IR mirror. NDPI is a variant of TIFF; this convention builds on the
[TIFF convention](tiff.md). What all of refkv's conventions share is
in [spec/conventions.md](../conventions.md), cited here as "conventions §n".
How refkv produces this layout as a reference store is the NDPI profile,
[Part 2](#part-2-the-profile) below.

Convention version: 0 (until release, [conventions §1](../conventions.md#1-conventions)) · UUID: `6cac71ef-dbb2-4acd-b60c-00389aa4238a` ·
Schema: [schema.json](ndpi/schema.json)

This document has two parts. [Part 1](#part-1-the-convention) is the NDPI
convention: the Zarr layout, cited as "the convention §n". [Part 2](#part-2-the-profile)
is the NDPI profile: how refkv reads the source, which inputs it rejects, and how
each chunk references the source. The profile keeps its numbering as a section
of [spec/virtualize.md](../virtualize.md), and is cited as spec/virtualize.md §n.

# Part 1. The convention

This convention gives a layout only to the files that meet its
requirements. Where it says that "the input is rejected", or that
something MUST hold, a file that fails has no layout under this
convention, and the profile rejects it.

## 1. Declaration

The root declares the convention by [conventions §2](../conventions.md#2-attributes),
with `"profile": "ndpi"`, `"version": 0`, `"revision": 28` ([conventions §1](../conventions.md#1-conventions)) and the file's URL as `source.url`;
it has no source metadata member ([§5](#5-source-metadata)). Its CMO is:

```json
{
  "uuid": "6cac71ef-dbb2-4acd-b60c-00389aa4238a",
  "schema_url": "https://raw.githubusercontent.com/d-v-b/refkv/refs/heads/main/spec/virtualize/ndpi/schema.json",
  "spec_url": "https://github.com/d-v-b/refkv/blob/main/spec/virtualize/ndpi.md",
  "name": "refkv_virtualized",
  "description": "The Zarr layout of a NDPI source virtualized by refkv, and the source's metadata"
}
```

`refkv_source` declares it too, with the IR mirror's description ([§5](#5-source-metadata),
[conventions §8](../conventions.md#8-the-ir-mirror)), and so does each group of
the mirror's view that has a member; the mirror's arrays have no
attributes.

## 2. The source

An NDPI file is a little-endian classic TIFF (magic 42) whose IFDs have
64-bit offsets ([spec/virtualize.md §4](#4-ndpi-profile)
says how they are read). Its **IFDs** are those of its main chain, in
order. Of duplicate tags in an IFD, the first is used.

The layout uses these tags, besides ImageWidth (256), ImageLength (257), BitsPerSample (258), Compression (259), PhotometricInterpretation (262) and SamplesPerPixel (277), whose defaults are the [TIFF convention's](tiff.md#2-the-source); field types and counts are as the profile checks them:

| tag | name | kind | type | default |
|---|---|---|---|---|
| 273, 279 | StripOffsets, StripByteCounts | scalar | integer | required |
| 282, 283 | XResolution, YResolution | scalar | RATIONAL (5) | absent |
| 296 | ResolutionUnit | scalar | integer | none |
| 65420 | NDPI format flag | scalar | integer | required |
| 65421 | Magnification | scalar | FLOAT (11) or DOUBLE (12) | required |
| 65422, 65423 | X, YOffsetFromSlideCenter | scalar | SHORT, LONG, SSHORT, SLONG (3, 4, 8, 9) | absent |
| 65426 | McuStarts | array | integer | absent |
| 65432 | McuStartsHighBytes | array | integer | absent |


## 3. Levels

**Levels.** The levels are the main-chain IFDs whose Magnification is
positive, in chain order. Each MUST have Compression 7,
PhotometricInterpretation 6, SamplesPerPixel 3, BitsPerSample 8 and one
strip (StripOffsets and StripByteCounts of one value each), which MUST lie
within the file and be at least 4 bytes long (a JPEG stream's SOI and EOI),
and each MUST be strictly smaller in both width and length than the
one before. If two levels have the same Magnification (focal planes), the
input is rejected. Other IFDs (the macro image, the slide map) are ignored.
The array has axes `c`, `y`, `x`, data type `uint8`, and codecs
`transpose` and `imagecodecs_jpeg` ([conventions §3](../conventions.md#3-arrays)).


## 4. The image

The hierarchy is one image at the root ([conventions §4](../conventions.md#4-images)),
without a name, with one array per level at path `"<level>"`.

**Scale.** With XResolution `rx` and a ResolutionUnit tag of 3
(centimeter), let `p = 10000 / rx`; of 2 (inch), `p = 25400 / rx`. If `p`
is less than 25.4, `x` has unit `micrometer` and level 0 scale `p`;
otherwise, and without a ResolutionUnit tag, no unit and scale 1, as in the
TIFF convention [§4.4](tiff.md#44-arrays). `y` likewise from YResolution.
Level `L`'s scale is level 0's times `(W0 / WL)` (x) or `(H0 / HL)` (y), as
in [the TIFF convention §4.4](tiff.md#44-arrays).

**Position.** When `x` and `y` have units and level 0's IFD has
XOffsetFromSlideCenter (65422) and YOffsetFromSlideCenter (65423), in
nanometres, the image's centre is at `(X / 1000, Y / 1000)` micrometres,
which gives its translation ([conventions §5](../conventions.md#5-units)).

**Chunks.** A level's strip is a JPEG stream. Without McuStarts, the
level is one chunk, the whole image (chunk shape `[3, H, W]`), which holds
the strip. With McuStarts, the strip is a sequence of restart
**intervals**, each `R` MCUs of `mw × mh` pixels wide and one MCU tall, in
row-major order, `q` per row and `r` rows (the profile defines them from
the strip's JPEG header); a chunk is `a × b` intervals, `a` =
`min(q, max(1, floor(1024 / (R × mw))))` across and `b` down, so the chunk
shape is `[3, b × mh, a × R × mw]`. (`b` is chosen by the profile, as the
largest that keeps every chunk's reference within refkv's limits.) Chunk
`(u, v)` (row `u`, column `v`), at `<level>/c/0/<u>/<v>`, holds a complete
JPEG stream of those intervals, which decodes to the chunk's pixels; at the
image's bottom and right edges, the pixels outside the array are
unspecified. Every chunk is present.

A chunk's stream holds the strip's header with a frame header (SOF0) of its
own size, its intervals, and restart markers and an EOI of its own, so:
- the chunk's width `a × R × mw` and height `b × mh` MUST each be at most
  65535, the largest a frame header holds, else the input is rejected;
- the strip's SOF0 segment, which every chunk replaces, is kept where it
  is: the strip is an element of the source model ([§5](#5-source-metadata)), so the mirror
  rebuilds it byte for byte;
- the strip's own restart markers and final EOI are referenced by no chunk:
  each chunk puts the standard sequence that every valid JPEG stream has.
  The mirror keeps the strip's own, whatever they are.

## 5. Source metadata

The root has no source metadata member. Everything else the file holds is on
`refkv_source`, which is the file's **IR mirror**
([conventions §8](../conventions.md#8-the-ir-mirror)): its table, from which the
file is rebuilt byte for byte, and its view, `refkv_source/tree`. This
section is the NDPI **source model**: the elements of an NDPI's IR. It is the
[TIFF source model](tiff.md#5-source-metadata) read in NDPI's layout. Nothing is
left out, layout and dead space included (they are elements too, the dead
space as gaps): a level's strip, its JPEG header, restart markers and EOI
included, is one element, which the chunks of [§4](#4-the-image) cut along McuStarts.

### 5.1 Elements

Paths are relative to the IR's root, whose path is `""`; `<i>`, `<j>`,
`<tag>` are decimal name indexes ([conventions §8.1](../conventions.md#81-elements)). Types are those of [conventions §8.6](../conventions.md#86-types);
an NDPI is little-endian.

| path | kind | type | extent: what |
|---|---|---|---|
| `""` | struct | | the root: the whole file, `(0, size)` |
| `header` | struct | | the header, bytes 0 to 12 |
| `header/byte_order` | value | `ascii[2]` | `II` |
| `header/magic` | value | `<u2` | 42 |
| `header/first_ifd` | value | `<u8` | the first IFD's offset `V` ([§4](#4-ndpi-profile)) |
| `ifds/<i>` | struct | | main-chain IFD `i` ([§2](#2-the-source)): its entry count, entries, next-IFD offset and high words, `2 + 16 n + 8` bytes for `n` entries |
| `…/entry_count` | value | `<u2` | the entry count `n` |
| `…/tags/<tag>` | struct | | the first entry of tag `<tag>` (12 bytes); a later one of the same tag is `…/tags/<tag>~<k>`, `k` = 1, 2, … |
| `…/tags/<tag>/entry` | value | record | the entry's tag, field type and count (`{tag:<u2,type:<u2,count:<u4}`), with `offset:<u4` (the low 32 bits of the value's offset) when the value is out of line, or `field:bytes[4]` when the field type is unknown |
| `…/tags/<tag>/value` | value | by field type | the value: in the entry's field (the bytes it uses; the rest of the field is a gap), or at its offset `low + high × 2^32` when it lies within the file; its type as in the [TIFF source model](tiff.md#51-elements) (`<u4[n]`, `<u4[n,2]`, `<f4[n]`, …) |
| `…/tags/<tag>/high` | value | `<u4` | the entry's **high word**, after the next-IFD offset: the high 32 bits of an out-of-line value's offset, and of an in-line LONG or IFD of count 1 |
| `…/next_ifd` | value | `<u8` | the next-IFD offset |
| `…/strips` | struct | | an IFD's strips (by StripOffsets, with high words, and StripByteCounts), when it has both tables |
| `…/strips/<j>` | data | | strip `j`, when its byte count is not 0 and it lies within the file; its form's geometry is `{"shape": [rows, columns], "samples": s, "bits": b}`, its codec `{"compression": c}`, and its recipe its bytes: a level's strip is a whole JPEG stream |
| `…/subifds/<j>`, `…/exif`, `…/gps`, `…/interoperability`, `…/ifd_<tag>` | struct | | the IFD a pointer tag ([§5.2](#52-which-ifds)) leads to (`…/<name>/<j>` when the tag has several values), as an IFD |
| `gaps/<offset>` | gap | | bytes no other element claims (padding, unused space, bytes 12 to 16 when no element claims them) |

An IFD whose strip tables are at the offsets of an earlier IFD's, with the
same counts and codec, names that IFD's `strips` by an alias. An element
whose bytes an earlier element claims (an IFD read twice through a pointer,
a strip at another's offset) is an alias of that element.

### 5.2 Which IFDs

The IFDs of [§2](#2-the-source), the main chain, at depth 0 (`ifds/<i>`); then the IFDs that
pointer tags lead to, as the [TIFF source model §5.2](tiff.md#52-which-ifds)
says, with these differences:
- the main-chain IFDs are the only IFDs of the tree: an NDPI's SubIFDs tag
  (330) is followed like the other pointer tags;
- the other pointer tags of type IFD or IFD8 are those that are neither in
  the table of [§2](#2-the-source) nor layout tags, McuStarts (65426) and McuStartsHighBytes
  (65432) among the latter;
- every IFD is read in NDPI's layout ([§4](#4-ndpi-profile)): one a pointer leads to is read
  when its offset is at least 16 and its entry count, entries, next-IFD
  offset and high words lie within the file, has at least one entry, and
  overlaps no IFD already read; an in-line LONG or IFD of count 1 leads to
  `low + high × 2^32`, and an out-of-line value is at its 64-bit offset.

### 5.3 Values

An entry's value is an element when it lies within the file. Its bytes are
in the source; the view shows it when it is a number, a record or text
(ASCII) of at most 1024 bytes ([conventions §8.7](../conventions.md#87-the-view)), and a producer reads
every such value. A value element holds the bytes of its extent: an in-line
LONG or IFD of count 1 shows its low 32 bits, and its `high` sibling the
rest. Every high word is shown.

### 5.4 Equivalence

The elements are determined by the file, and the mirror by its elements
([conventions §8.8](../conventions.md#88-canonical-form): one order, one folding, one encoding; NDPI has no
named array rule): hierarchies of one file are compared entry for entry,
`refkv_source` included ([spec/virtualize.md §1.1](../virtualize.md#11-output-and-equivalence)).

## 6. Example

A slide at `https://example.org/slide.ndpi`, the 207,434-byte
`ndpi_levels.ndpi` of refkv's test fixtures: three levels, of which the first
two have McuStarts. The root's property:

```json
{
  "profile": "ndpi",
  "version": 0,
  "revision": 28,
  "source": {
    "url": "https://example.org/slide.ndpi"
  }
}
```

`refkv_source` is the IR mirror ([§5](#5-source-metadata)). Its property:

```json
{
 "ndpi": {
  "ir": {
   "version": 2,
   "size": 207434,
   "elements": 292,
   "rows": 146,
   "names": ["", "byte_order", "entry", "entry_count", "first_ifd", "gaps/", "header", "high", "ifds/", "magic",
             "next_ifd", "strips", "tags/", "value"],
   "types": ["", "<f4[1]", "<i4[1]", "<u2", "<u2[1]", "<u2[3]", "<u4", "<u4[1,2]", "<u4[1500]", "<u4[1]",
             "<u4[60]", "<u8", "ascii[2]", "{tag:<u2,type:<u2,count:<u4,offset:<u4}", "{tag:<u2,type:<u2,count:<u4}"],
   "forms": ["{\"geometry\":{\"shape\":[1200,1280],\"samples\":3,\"bits\":8},\"codec\":{\"compression\":7},\"recipe\":[[\"src\",0,null]]}",
             "…"]
  }
 }
}
```

(abridged: four forms, one per strip shape). The view, `refkv_source/tree`,
is the root's document, and begins:

```json
{
 "ndpi": {
  "header": {"byte_order": "II", "magic": 42, "first_ifd": 198686},
  "ifds/0": {
   "strips": {},
   "entry_count": 16,
   "tags/256": {"entry": {"tag": 256, "type": 4, "count": 1}, "value": [1280], "high": 0},
   "tags/273": {"entry": {"tag": 273, "type": 4, "count": 1}, "value": [12], "high": 0},
   "tags/282": {"value": [[5477, 1]], "entry": {"tag": 282, "type": 5, "count": 1, "offset": 192670}, "high": 0},
   "tags/65421": {"entry": {"tag": 65421, "type": 11, "count": 1}, "value": [20.0], "high": 0},
   "tags/65426": {"value": {"$vz": "element", "id": 89},
                  "entry": {"tag": 65426, "type": 4, "count": 1500, "offset": 192686}, "high": 0},
   "next_ifd": 202450
  }
 }
}
```

(abridged: the other tags are like these). Level 0's strip, `ifds/0/strips/0`,
starts at byte 12, before its IFD, so `strips` comes first; its 1,500
McuStarts values (6,000 bytes) are more than the view shows, so the view
names the element by its row, and a reader reads them from the source.

# Part 2. The profile

The NDPI profile of [spec/virtualize.md](../virtualize.md) (revision 16), numbered
as its [§4](#4-ndpi-profile). It builds on the TIFF profile ([§3](tiff.md#3-tiff-profile), [tiff.md](tiff.md#part-2-the-profile)). [§1](../virtualize.md#1-introduction) and [§2](../virtualize.md#2-the-zarr-layout)
are in spec/virtualize.md and apply here.

The output has the convention's layout for the input
([spec/virtualize.md §2](../virtualize.md#2-the-zarr-layout)); this profile says
how the file is read, which inputs are rejected, and how each chunk
references the file. "The convention §n" below is a section of Part 1.

## 4. NDPI profile

Hamamatsu NDPI files are little-endian classic TIFFs (magic 42) with a 64-bit
extension and pyramids stored as single JPEG strips. They are read by [§3.1](tiff.md#31-reading)
([tiff.md](tiff.md#31-reading)) with the changes below, with the tags of
[the convention §2](#2-the-source) as the
checked tags.

**Detection.** Let `V` be the little-endian `u64` at bytes 4–11 of the
header. The file is NDPI if it is little-endian with magic 42, `16 ≤ V`,
`V + 2 ≤` the file's size, and the IFD at `V`, read as below, has tag 65420
among its first `n` entries (its entry count `n` and the `12 n` bytes of
entries MUST lie within the file for this test; if they do not, the file is
not NDPI). Otherwise it is a TIFF ([§3](tiff.md#3-tiff-profile)). This test never rejects.

**IFDs.** The main chain starts at `V`. An IFD at `o` is a `u16` entry count
`n`, `n` entries of 12 bytes as in TIFF, then a `u64` next-IFD offset, then
`n` `u32` **high words**, one per entry. An entry's value field is its 4
bytes plus its high word `h`: an out-of-line value is at offset
`low + h × 2^32`, and an inline value of count 1 and type LONG (4) or IFD
(13) is `low + h × 2^32`. Other inline values ignore `h`. Only the main
chain is read for the layout: an NDPI's SubIFDs, like the IFDs of its other
pointer tags, are read in this layout for the IR only, and
never reject the input ([the convention
§5](#5-source-metadata)). The limits and checks
of [§3.1](tiff.md#31-reading) apply, with these values read ([§3.1](tiff.md#31-reading), **Values read**): the
Magnification of every main-chain IFD, and every tag of the table of each
level ([the convention §3](#3-levels)); of a
scalar tag, its first value, of McuStarts, McuStartsHighBytes and
BitsPerSample, every value. (A LONG or IFD value of count 1, with its high
word, can exceed 2^53 − 1 too.) No other value is read for the layout, so
the other IFDs' tags have only their field types, counts and places
checked.

**Rejection.** The input is rejected when a check fails, and when the
convention gives it no layout: wherever the convention says that the input
is rejected, or that something MUST hold and it does not.

**Strips.** A level's strip is the JPEG stream `S` = bytes
`[StripOffsets, StripOffsets + StripByteCounts)` of the file, which MUST
lie within the file. StripByteCounts MUST be at least 4, the SOI and EOI
markers of the shortest JPEG stream.

- **Without McuStarts,** the level is one chunk, the whole image (chunk
  shape `[3, H, W]`), with the single range of `S`.
- **With McuStarts,** let `M[i]` be its values, plus `McuStartsHighBytes[i]
  × 2^32` when that tag is present (it MUST then have as many values); they
  are offsets in `S`. `S`'s **header** is its first `M[0]` bytes: a JPEG SOI,
  then marker segments up to and including SOS, which MUST end exactly at
  `M[0]`. The header MUST have exactly one SOF0 (`FF C0`) segment and a DRI
  (`FF DD`) segment with restart interval `R > 0` (in MCUs). The SOF0
  segment's sampling factors give the MCU size `mw × mh`: 8 times the largest
  horizontal and vertical factors. The strip is then a sequence of
  **intervals**, each `R` MCUs wide and one MCU tall, in row-major order:
  `q = ceil(W / (R × mw))` per row and `r = ceil(H / mh)` rows, and McuStarts
  MUST have `q × r` values, increasing, with `M[q × r − 1] < len(S)`.
  Interval `i` is the bytes `[M[i], M[i + 1] − 2)` of `S` (`[M[i], len(S) −
  2)` for the last), dropping the 2-byte restart marker or EOI after it; each
  MUST be non-empty.

**Chunks of a strip with McuStarts.** A chunk is `a × b` intervals: `a` =
`min(q, max(1, floor(1024 / (R × mw))))` intervals across and `b` intervals
down, so the chunk shape is `[3, b × mh, a × R × mw]`. `a × R × mw` MUST be
at most 65535. The 2 bytes of `S` before `M[i]` (for `i ≥ 1`) and its last
2 bytes, its restart markers and EOI, are not referenced: the chunks put
their own, the standard sequence `FF D0 + (t mod 8)` and `FF D9`, and the
mirror keeps the strip's ([the convention §5](#5-source-metadata)).
`b` is the largest
number from 1 to `min(r, floor(1024 / mh))` (at least 1) for which every
chunk's payload is at most 65519 bytes ([§1.2](../virtualize.md#12-input)); `b × mh` MUST be at most
65535 (it always is, since `mh ≤ 120`). Chunk `(u, v)` (row `u`,
column `v`) is the JPEG stream:

1. the header up to its SOF0 segment, a shared byte string ([§1.2](../virtualize.md#12-input)): a range
   of the data source that holds it;
2. a literal SOF0 segment: the header's, with its height set to `b × mh` and
   its width to `a × R × mw`;
3. the rest of the header after the SOF0 segment, a shared byte string: a
   range of the data source that holds it;
4. for `y` from 0 to `b − 1` and, within it, `x` from 0 to `a − 1`: the
   interval at row `min(u × b + y, r − 1)` and column `min(v × a + x, q − 1)`
   (a range of the file), followed, except after the last, by the literal
   restart marker `FF D0 + (t mod 8)`, where `t` counts the intervals placed
   so far from 0;
5. the literal EOI `FF D9`.

Clamping repeats the last row or column of intervals in the chunks at the
image's bottom and right edges; those pixels lie outside the array and are
not read. (Every interval decodes on its own after a restart marker.)
Chunk keys are `<level>/c/0/<u>/<v>`. The references are listed (for
[§1.2](../virtualize.md#12-input)'s order of first use) level by level, and within a level by `u`, then
`v`.

**Data sources.** Pieces 1 and 3 are the same in every chunk of a level,
and are held once, in data sources, so that reading a chunk reads only its
intervals from the file. They sit at the start of the strip, far from most
intervals, so as ranges of the file they would cost a reader a separate
request per chunk. By [§1.2](../virtualize.md#12-input)'s order of first use, the first McuStarts level's
pieces 1 and 3 are sources 1 and 2; a later level adds a source only for a
piece whose bytes differ from every earlier one. (In CMU-1.ndpi every level
shares piece 1, its quantization and Huffman tables, and piece 3, DRI and
SOS, differs by its restart interval.) Both are JPEG markers and tables, not
pixel data ([§1.2](../virtualize.md#12-input), **Structure only**).
