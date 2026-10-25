# Zarrifying image files and stores as OME-Zarr in refkv

Profiles version: 0 (**draft**)

Changed 2026-10-25: the NIfTI, MRC and FITS profiles pad an edge chunk's reference with a range
of the output's one **zero data source**, a data source a reference names a prefix of, in place
of a literal range per reference ([§1.2](#12-input)); an output that still pads with literal
ranges is not equivalent to theirs ([§1.1](#11-output-and-equivalence)).

Changed 2026-10-25: this document, its profiles and the conventions say **zarrify** for what
they called virtualize, and the conventions' property is `refkv_zarrified`, in place of
`refkv_virtualized` ([conventions §2](conventions.md#2-attributes)); nothing else in any output
changes.

Changed 2026-10-24, with no change to any output: each profile's document is numbered on its
own and cited by its name ([Profiles](#profiles)), not as a section of this one, and each file
profile's signature is in its own document, tested in the registry's order
([§1.2](#12-input)).

## 1. Introduction

To **zarrify** a file or store is to present it as Zarr without copying it:
its chunks are references to the input's own bytes. A **zarrifier** reads
the structure of an image file or of a chunked array store (the formats of the table below), and writes a
vzip archive ([spec/bindings/vzip.md](bindings/vzip.md)) that presents the pixels as an OME-Zarr
dataset, or for a store as a Zarr v3 hierarchy (for an OME-Zarr 0.4 store,
an OME-Zarr 0.5 one; for a Sentinel-2 SAFE product, a GeoZarr one; for an
MP3 file, the one format that is not an image, a plain array of audio
samples). The
pixels stay where they are: each Zarr chunk is a reference to byte ranges of
the file, or to a whole object of the store. This document specifies, for
each supported input format (a **profile**), exactly which archive a
zarrifier produces, so that independent zarrifiers produce the same
output from the same input.

This document holds what every profile shares: the input, the output and how
outputs compare ([§1](#1-introduction)), and the common parts of the output ([§2](#2-the-zarr-layout)). Each profile
is a document of its own, `zarrify/<name>.md`, numbered on its own and cited by its name
([Profiles](#profiles)); its registry entry, `zarrify/profiles/<name>.json`, holds its identity.
The table, generated from the registry, lists them:

<!-- profiles:spec-table -->
| profile | input | inputs |
|---|---|---|
| [CZI](zarrify/czi.md#part-2-the-profile) | file | Zeiss CZI files (ZISRAW, file version 1), with uncompressed, JPEG, JPEG XR and Zstd subblocks, mosaics and pyramids |
| [DICOM](zarrify/dicom.md#part-2-the-profile) | file | DICOM Part 10 files with native or JPEG-encapsulated pixel data, including whole-slide images |
| [FITS](zarrify/fits.md#part-2-the-profile) | file | FITS files: images, multi-extension files, binary and ASCII tables |
| [IMS](zarrify/ims.md#part-2-the-profile) | file | Imaris IMS files (HDF5) |
| [MP3](zarrify/mp3.md#part-2-the-profile) | file | MPEG-1, MPEG-2 and MPEG-2.5 Layer III audio files (`.mp3`), with their ID3v2, ID3v1 and APE tags; one array of samples, not an image |
| [MP4](zarrify/mp4.md#part-2-the-profile) | file | MP4 and QuickTime files (`.mp4`, `.m4a`, `.mov`) with H.264 video and AAC or MP3 audio tracks; a group per track, not an image |
| [MRC](zarrify/mrc.md#part-2-the-profile) | file | MRC2014 files (CCP4 maps, image stacks, volumes and volume stacks) |
| [N5](zarrify/n5.md#part-2-the-profile) | store | N5 containers, default-mode blocks |
| [ND2](zarrify/nd2.md#part-2-the-profile) | file | Nikon ND2, format version 3 and later |
| [NDPI](zarrify/ndpi.md#part-2-the-profile) | file | Hamamatsu NDPI slides, a TIFF variant with 64-bit offsets |
| [NIfTI](zarrify/nifti.md#part-2-the-profile) | file | NIfTI-1 and NIfTI-2 single files (`.nii`) |
| [OME-Zarr](zarrify/ome-zarr.md#part-2-the-profile) | store | OME-Zarr 0.4 hierarchies on Zarr v2, migrated to OME-Zarr 0.5 |
| [QPTIFF](zarrify/qptiff.md#part-2-the-profile) | file | Akoya (PerkinElmer) Vectra QPTIFF slides and inForm component data, a TIFF with an XML description per image, in LZW, JPEG or uncompressed tiles or strips |
| [Sentinel-2 SAFE](zarrify/safe.md#part-2-the-profile) | file or store | Sentinel-2 Level-1C and Level-2A products: a `.SAFE` directory (a store) or a `.SAFE.zip` file |
| [TIFF](zarrify/tiff.md#part-2-the-profile) | file | TIFF and BigTIFF, including OME-TIFF and JPEG-tiled slides such as Aperio SVS |
| [VSI](zarrify/vsi.md#part-2-the-profile) | file | Olympus cellSens VSI slides: a `.vsi` file and the tile files beside it, probed by key (uncompressed, JPEG and JPEG 2000 tiles) |
| [Zarr v2](zarrify/zarr2.md#part-2-the-profile) | store | Zarr v2 hierarchies |
<!-- /profiles -->

The table says which profiles read a single file (a **file input**), which a store of many
objects (a **store input**, [§1.4](#14-store-inputs)), and which either; VSI ([vsi.md §9](zarrify/vsi.md#9-vsi-profile)) is named by a file, its `.vsi`, and reads it and the tile files beside it as a store it probes rather than lists. [Appendix A](#appendix-a-conformance) is informative:
the implementations and how they are compared.

The key words MUST, MUST NOT, SHOULD, SHOULD NOT and MAY are to be
interpreted as described in RFC 2119.

### 1.1 Output and equivalence

A zarrifier's **output** is an archive description:

- a **source table**: a list of `url` sources and `data` sources (spec/refkv.md
  [§5](refkv.md#5-source-table)), and
- a set of **entries**: keys, each with either bytes or a list of ranges
  ([spec/refkv.md §2](refkv.md#2-data-model), [§4](refkv.md#4-messages)).

Two outputs are **equivalent** when they have the same source table (the
same sources in the same order: `url` sources with the same URLs, `data`
sources with identical bytes), the same set of keys, and for every key:

- **reference entries:** the same list of ranges, in order. A range is a
  source range `(i, offset, length)` of source `i`, or a **literal** range
  of given bytes ([spec/refkv.md §4.2](refkv.md#42-range)), and two ranges are the same only if both are
  source ranges with equal sources, offsets and lengths, or both are literal
  ranges with identical bytes;
- **JSON documents** (keys ending in `zarr.json`): equal JSON values.
  Objects compare by key and arrays by order; whitespace and key order do
  not matter. Numbers compare by value:
  - a number written as an integer (an optional `-` and digits, with no
    fraction and no exponent) has its exact value, every digit, however
    large, since [§1.6](#16-json-documents) copies such numbers exactly;
  - any other number has its IEEE 754 binary64 value ([§1.3](#13-arithmetic));
  - two numbers are equal when their values are mathematically equal. An
    integer and a binary64 value are equal only if the binary64 value is
    exactly that integer: `3`, `3.0` and `3e0` are equal, and so are
    `9007199254740992` and `9007199254740992.0`, but `9007199254740993` is
    not equal to `9007199254740993.0`, whose binary64 value is
    9007199254740992. `-0`, `0` and `-0.0` are all equal.

  A writer that holds a binary64 value that is an integer beyond 2^53 − 1
  must therefore write it with its exact digits, or with a fraction or an
  exponent: JavaScript's `JSON.stringify` writes 2^64 as
  `18446744073709552000`, an integer literal of another value;
- **other bytes entries:** identical bytes, except a deflated coordinate
  chunk of a dataset layout ([conventions §9](conventions.md#9-dataset-layouts)),
  compared by the values it decodes to.

**IR mirrors.** For a file input, `refkv_source` is the
source's IR mirror ([conventions §8](conventions.md#8-the-ir-mirror)); for
a store input (a SAFE directory included), the store's ([conventions §8.9](conventions.md#89-stores),
whose limits replace those below).
Its table and view are canonical: a function of the source's IR, folded and
encoded one way only ([conventions §8.8](conventions.md#88-canonical-form)).
Mirrors are therefore compared entry for entry, as above, like the rest of the
output. A zarrifier **rejects** a source whose mirror would have more than
`N = 2^22 + floor(size / 4)` rows, the runs expanded and the derived spaces
left out ([conventions §8.3](conventions.md#83-column-runs), limits),
with the message `budget: the mirror would have more than N rows`.

The archive's byte layout (entry order, compression, page index) is the
writer's choice and is not part of the output. Neither are the sources'
pins and the ranges' checksums ([§1.2](#12-input)): an `etag` pin depends on what the
server sent, and checksums are optional. Two zarrifiers conform to
the same profile revision if they produce equivalent outputs for every input
that the profile accepts, and both reject every input it rejects.

### 1.2 Input

A zarrifier is given a URL, `U`. `U` MUST be a valid absolute URI;
zarrifiers do not normalize it. If `U`'s path ends in `/`, the input is a
**store input**, read as [§1.4](#14-store-inputs) says, and the profile is chosen there; the
rest of this section is about **file inputs**, where `U` is the URL of one
file. (An implementation MAY also accept a local file or directory, with `U`
the URL it will be served from: a directory is a store input, listed as
[§1.5](#15-listing-a-store) says.)

For a file input, the output's source table is source 0, a `url` source
whose value is `U` as given, followed by the output's **data sources**, if
its profile defines any. Source 0 carries pins ([spec/refkv.md §5.1](refkv.md#51-pins)):

- a `size` pin, the file's size;
- an `etag` pin when every response the zarrifier received for the file
  (to `HEAD` and to range requests) had an `ETag` header with the same
  strong entity tag; none when a response had none, or a weak one. Two
  responses with different strong tags mean the file changed while it was
  read: the zarrifier fails (it does not reject). A zarrifier that read
  the file from somewhere other than `U` (a local file served at `U`) pins
  no `etag`.

**Checksums.** A zarrifier MAY, when asked to, record on every range of
a `url` source the CRC-32C of its bytes ([spec/refkv.md §4.2](refkv.md#42-range)). That reads every
byte the output references, pixel data included, so it is off by default;
it is the one exception to "Structure only" below, and changes nothing else
in the output. An output whose payload exceeds 65519 bytes only because of
its checksums is a failure, not a rejection. A data source is a `data` source
([spec/refkv.md §5](refkv.md#5-source-table)) holding a byte string that a profile names as **shared**: one
that many references would otherwise each read from the file, or each carry
as a literal, such as a JPEG header. The data sources are the distinct
shared byte strings of the output, numbered from 1 **in order of first
use**: in the order in which the profile lists the references, and within a
reference in order of its ranges, the first range that uses a byte string
not yet in the table adds it as the next source. A range that uses a shared
byte string `d` in data source `i` is `(i, 0, len(d))`, the whole source,
but for a **zero data source**: `n` zero bytes, `n` the most zero padding
any reference of the output holds, of which a reference padded with `p`
zero bytes names the first `p`, `(i, 0, p)`. Every other range of a
reference is a range of source 0 or a literal range. A profile that defines
shared byte strings says so, as the TIFF
([tiff.md §7](zarrify/tiff.md#7-tiff-profile), JPEG tiles) and SAFE ([safe.md §8](zarrify/safe.md#8-safe-profile), JPEG 2000 main headers) profiles do, and as the
NIfTI ([nifti.md §7.2](zarrify/nifti.md#72-chunks)), MRC
([mrc.md §8.2](zarrify/mrc.md#82-chunks)) and FITS
([fits.md §9.4](zarrify/fits.md#94-chunks)) profiles do for a zero data
source; for a profile that defines none, the table is source 0 alone.

**Choosing the profile.** The file's first bytes decide. `H` is the file's first
`min(552, size)` bytes; a test that needs a byte beyond `H` does not match. Each file profile's
document states its **signature**, a test on `H`, and its registry entry
(`zarrify/profiles/<name>.json`) the signature's precedence, `sniff`. The signatures are
tested in ascending precedence, those of equal precedence in order of the profiles' names,
and the first that matches chooses the profile. A file that no signature matches is rejected.
A profile whose `sniff` is null has no signature: the reading of another profile, its registry
entry's `chosen_by`, chooses it (the TIFF profile's chooses NDPI and QPTIFF). The precedences, generated from the registry, by
profile name:

<!-- profiles:sniff-table -->
| profile | `sniff` |
|---|---|
| CZI ([czi.md §7](zarrify/czi.md#7-czi-profile)) | 50 |
| DICOM ([dicom.md §7](zarrify/dicom.md#7-dicom-profile)) | 10 |
| FITS ([fits.md §9](zarrify/fits.md#9-fits-profile)) | 110 |
| IMS ([ims.md §7](zarrify/ims.md#7-ims-profile)) | 70 |
| MP3 ([mp3.md §8](zarrify/mp3.md#8-mp3-profile)) | 100 |
| MP4 ([mp4.md §8](zarrify/mp4.md#8-mp4-profile)) | 85 |
| MRC ([mrc.md §8](zarrify/mrc.md#8-mrc-profile)) | 90 |
| ND2 ([nd2.md §7](zarrify/nd2.md#7-nd2-profile)) | 20 |
| NIfTI ([nifti.md §7](zarrify/nifti.md#7-nifti-profile)) | 60 |
| Sentinel-2 SAFE ([safe.md §8](zarrify/safe.md#8-safe-profile)) | 80 |
| TIFF ([tiff.md §7](zarrify/tiff.md#7-tiff-profile)) | 40 |
| VSI ([vsi.md §9](zarrify/vsi.md#9-vsi-profile)) | 30 |
<!-- /profiles -->

**Rejection.** A zarrifier MUST reject an input the profile does not
accept, producing no output. That includes every input that is not well
formed as this document describes it: a read outside the file, a structure
that is truncated or inconsistent, a value of the wrong type or out of range,
an offset or length above 2^53 − 1. Being unable to read the input (a
network error) is not a rejection; the zarrifier fails instead. An
implementation MAY also fail, rather than reject, when an input exceeds a
resource limit that it states ([Appendix A](#appendix-a-conformance)); a failure is not an output, so it does
not affect equivalence.

**Reader policy.** A zarrifier reads a remote input under a reader policy
([spec/refkv.md §6.7](refkv.md#67-reader-policy)) that the application chooses,
applied to every request it makes for the input: a file's, and a store's
listing and object reads ([§1.4](#14-store-inputs), [§1.5](#15-listing-a-store)), each redirect target checked before it
is requested. A request the policy refuses is a failure, not a rejection. The
implementations in this repository default to the defaults of [spec/refkv.md §6.7](refkv.md#67-reader-policy), so they refuse
loopback, private, link-local and special hosts, and requests through a
proxy, unless the application allows private hosts or unchecked proxies (the
same settings, and `--allow-private-hosts` on their command lines).

**Structure only.** The output MUST NOT depend on the file's pixel data.
(Reading blocks that happen to include pixel bytes is fine.) Coding
parameters are structure, not pixel data, and an output may copy them: the
data sources and literals of the TIFF and NDPI profiles hold JPEG markers
and tables (quantization and Huffman tables, frame and scan headers, restart
intervals), and those of the SAFE profile JPEG 2000 marker segments (SIZ,
COD, QCD, COM, SOT, and empty packet headers), which describe how the pixels
are coded but encode none of them.
Entropy-coded data, from which pixels are decoded, is only ever referenced
as ranges of source 0.

**Evaluation order.** Whether an input is rejected never depends on the
order in which a zarrifier reads or checks things: each profile lists what
is checked, and every listed check applies whether or not its value ends up
in the output.

**References stay in the file.** Every range of source 0 MUST lie within
the file (`offset + length ≤` the file's size; a range of a data source is
the whole source), and every reference entry's
payload MUST be at most 65519 bytes; otherwise the input is rejected. The
payload ([spec/refkv.md §4.4](refkv.md#44-reference-payloads), [§4](refkv.md#4-messages)) of a single range is its `Range` message: for a
source range `(i, o, n)`, `0x08 varint(i)` if `i > 0`, then `0x18 varint(o)`
if `o > 0`, then `0x20 varint(n)` if `n > 0` (fields 1, 3 and 4); for a
literal range of bytes `d`, `0x2A varint(len(d)) d`. The payload of several
ranges is a `Concat` message: for each range, `0x0A varint(len(r)) r`, where
`r` is that range's `Range` message. `varint` is the protobuf base-128
encoding (1 byte below 2^7, 2 below 2^14, and so on).

### 1.3 Arithmetic

Where this document computes a number (a scale, a size), it is computed in
IEEE 754 binary64 arithmetic, in the order written, with each operation
rounded to nearest. Decimal strings are converted to binary64 by correct
rounding. Integers are exact. A number that would be infinite or NaN rejects
the input. In JSON documents, integers (shapes, counts, codec parameters) are
written as integers; a computed number such as a scale MAY be written either
way (`1` or `1.0`), since outputs compare numbers by value ([§1.1](#11-output-and-equivalence)).

**Whitespace** in this document means the XML whitespace characters: space,
tab, CR and LF (U+0020, U+0009, U+000D, U+000A). **Digits** are the ASCII
digits `0`–`9`.

### 1.4 Store inputs

A **store** is a set of **objects**, each a byte string named by a
**key**, as in an S3 bucket or a directory tree. A store input is given by
its **store URL** `U` ([§1.2](#12-input)), whose path ends in `/`; it names the store
whose objects are the objects under `U`.

**The store URL.** `U` MUST have the scheme `http` or `https` (in any
case), an authority without userinfo, and no query or fragment component
(not even an empty `?` or `#`); otherwise the input is rejected. (An
implementation that reads a local directory uses the URL the directory will
be served from, which MUST satisfy the same rules.)

**The objects.** The store is listed as [§1.5](#15-listing-a-store) says. Each listed key `K`
begins with the prefix `P` ([§1.5](#15-listing-a-store)); its **relative key** is `K` without
`P`. A listed object is **ignored** when its relative key is empty, ends in
`/`, contains an empty segment (`//`), or has a segment `.` or `..`
(segments are the parts between `/`). An ignored object is not read, and
its relative key is **recorded** unless its size is 0 and its relative key
is empty or ends in `/` (a folder marker, which some tools write): the
store conventions keep the recorded keys
([the Zarr v2 convention §5](zarrify/zarr2.md#5-other-objects),
[the N5 convention §6](zarrify/n5.md#6-other-objects)), so that
no listed object is dropped without a trace. The store's objects are the
listed objects that are not ignored, each with its relative key and its
**size**, the listed size. From here on "key" means the relative key. A **directory**
is a key prefix that ends just before a `/`: the store's directories are
the root (the empty path) and every `d` such that some key starts with
`d/`. The path of a node of the output is a directory's path.

**Choosing the profile.** The keys at the root (keys without a `/`)
decide, by the first row that matches:

| root keys | |
|---|---|
| `.zarray` or `.zgroup` | OME-Zarr profile ([ome-zarr.md §11](zarrify/ome-zarr.md#11-ome-zarr-profile)) if the store declares OME-NGFF 0.4 (below), else Zarr v2 profile ([zarr2.md §7](zarrify/zarr2.md#7-zarr-v2-profile)) |
| `attributes.json` | N5 profile ([n5.md §8](zarrify/n5.md#8-n5-profile)) |
| `manifest.safe` | SAFE profile ([safe.md §8](zarrify/safe.md#8-safe-profile)), directory form |
| anything else, including an empty store | rejected |

**Declaring OME-NGFF 0.4.** The test reads at most three documents, by
[§1.6](#16-json-documents), in this order; a document that [§1.6](#16-json-documents) rejects rejects the input. In
it, a **relative path** is a string of one or more segments separated by
`/`, none of them empty, `.` or `..`; the **attributes** of a path `p` are
read only if the store has both objects `p/.zgroup` and `p/.zattrs`
(`.zgroup` and `.zattrs` for the root), and are then the document
`p/.zattrs` if it is an object (otherwise there are none). A set of
attributes `A` **declares 0.4 multiscales** if its member `multiscales` is
an array with an element that is an object whose member `version` is the
string `"0.4"`.

1. If the store has an object `.zarray`, or the root has no attributes, the
   store does not declare OME-NGFF 0.4. Otherwise let `A` be the root's
   attributes.
2. The store declares OME-NGFF 0.4 if `A` declares 0.4 multiscales, or `A`'s
   member `plate` or `well` is an object whose member `version` is the
   string `"0.4"`.
3. Otherwise, if `A` has no member `bioformats2raw.layout`, it does not.
   If it has one, the **first image** `q` is found:
   - if `A` has a member `plate`: if `plate` is an object whose member
     `wells` is a nonempty array whose first element is an object whose
     member `path` is a relative path `w`, and the attributes of `w` have a
     member `well` that is an object whose member `images` is a nonempty
     array whose first element is an object whose member `path` is a
     relative path `f`, then `q` is `w/f`; otherwise there is none;
   - otherwise `q` is the first element of the member `series` of the
     attributes of `OME`, if that is a nonempty array whose first element
     is a relative path, and `0` if not.

   The store declares OME-NGFF 0.4 if there is a `q` and its attributes
   declare 0.4 multiscales.

(These are the forms OME-NGFF 0.4 gives an image, a plate, a well and a
bioformats2raw collection at the root. A 0.4 group below a root that does
not declare 0.4 is read by [zarr2.md §7](zarrify/zarr2.md#7-zarr-v2-profile), with its attributes unchanged.)

**Reading an object.** A zarrifier reads an object whole: the bytes
`[0, size)` of the object's URL (below), with HTTP range requests, or from
the local file. A response that does not have the listed size is a
failure, not a rejection (the store changed while it was read). A profile
reads only the metadata documents it names; it never reads a chunk object.
(**Structure only**, §1.2, applies: the output never depends on a chunk's
bytes, only on its key and size.) The SAFE profile is the exception: it
reads its band files in ranges (their boxes, main headers and tile-part
headers), with HTTP range requests, and a response that is not of the
requested length is a failure ([safe.md §8.2](zarrify/safe.md#82-objects-and-reading)).

**Object URLs.** The URL of the object with key `k` is `U` followed by
`k`'s UTF-8 bytes with every byte that is not an unreserved character
(`A`–`Z`, `a`–`z`, `0`–`9`, `-`, `.`, `_`, `~`), a sub-delim
(`!$&'()*+,;=`), `:`, `@` or `/` written as `%` and two uppercase hex
digits (the encoding of [spec/refkv.md §5](refkv.md#5-source-table) for local paths). For example, the key
`a b/0.0` of the store `https://h/x/` has the URL `https://h/x/a%20b/0.0`.

**Source table and chunk references.** A profile names the **chunk
objects** of each array: objects whose keys are chunk keys of the array,
and the other objects it references whole ([ome-zarr.md §11.2](zarrify/ome-zarr.md#112-chunks-and-ome-xml), the only one that does).
Of these, an object of size 0 is not used: it has no entry (so the chunk
reads as the fill value) and no source, and the store conventions keep its
key (with the empty objects' keys, or, for the Zarr v2, N5 and OME-Zarr profiles,
as an object of size 0 of the store's IR mirror,
[the Zarr v2 convention §4.1](zarrify/zarr2.md#41-elements),
[the N5 convention §5.2](zarrify/n5.md#52-elements)). Every other chunk object `k` gives
one entry, whose key is `k` itself, referencing the whole object. Every
other object of the store that the hierarchy doesn't represent (the store
conventions' "Other objects") gives one entry `refkv_source/objects/<k>` (with
the last segment escaped as those conventions say, so that no entry reads as
a Zarr document), referencing it whole in the same way. The
source table is one `url` source per such entry, with a `size` pin, the
object's listed size ([§1.5](#15-listing-a-store)): the URL of its object. The sources are in ascending order of their entries' keys,
compared as UTF-8 byte strings (which is the order of Unicode code points),
and the entry with key `k` at position `i` of that order has the single
range `(i, 0, size)`. No other entry is a reference, and a store input's
output has no data sources ([§1.2](#12-input)). The SAFE profile is the exception: its
url sources are one per object that some reference uses, and data sources
follow them, and its references are ranges within objects
([safe.md §8.8](zarrify/safe.md#88-the-source-table)).

These rules replace [§1.2](#12-input)'s **References stay in the file** (but for the
SAFE profile, whose payloads are checked as [§1.2](#12-input) says): each range lies
within its object by construction, and its payload is far below 65519
bytes. An output key MUST be at most 65535 bytes in UTF-8 and MUST NOT
start with `__vz__/`; otherwise the input is rejected.

**Rejection and failure.** [§1.2](#12-input)'s rejection rule applies to every object a
profile reads and to the listing. A network error, an HTTP status of 408,
429 or 5xx, and an object that is not of its listed size are failures. [§1.5](#15-listing-a-store)
says which listing responses reject the input.

### 1.5 Listing a store

**Local directories.** A directory is listed by walking it: its objects are
its regular files, at any depth, with keys the relative paths joined by `/`
and sizes the file sizes. Symbolic links and files whose names are not
valid UTF-8 are not objects. `P` is empty.

**HTTP stores** are listed with the Amazon S3 `ListObjectsV2` operation,
which S3 and S3-compatible servers answer anonymously for public buckets.
The listing endpoint and the key prefix `P` come from `U`:

- Let `host` be `U`'s host in lower case. `U` is **virtual-hosted** if
  `host` ends in `.amazonaws.com` and, of its labels (the parts between
  `.`), one that is not the first is `s3` or starts with `s3-`. For example
  `janelia-cosem-datasets.s3.amazonaws.com` and
  `bucket.s3.us-east-1.amazonaws.com` are virtual-hosted, while
  `s3.amazonaws.com`, `s3.us-east-1.amazonaws.com` and every other host
  (such as `127.0.0.1` or `storage.googleapis.com`) are **path-style**.
  (A bucket name with a label `s3` or `s3-…` in a path-style URL is
  therefore read as virtual-hosted; buckets so named are not supported.)
- Virtual-hosted: the **endpoint** is `U`'s scheme and authority followed
  by `/`, and the **prefix path** is `U`'s path without its first `/`.
- Path-style: `U`'s path is `/b/r`, where the **bucket** `b` is a nonempty
  segment and `r` is the rest (possibly empty). A path of `/` alone rejects
  the input. The endpoint is `U`'s scheme and authority followed by `/b/`,
  and the prefix path is `r`.
- `P` is the prefix path percent-decoded to bytes, which MUST be valid
  UTF-8 (otherwise the input is rejected). It ends in `/` unless it is
  empty.

For example, `https://janelia-cosem-datasets.s3.amazonaws.com/jrc_hela-2/jrc_hela-2.n5/em/`
has the endpoint `https://janelia-cosem-datasets.s3.amazonaws.com/` and
`P = jrc_hela-2/jrc_hela-2.n5/em/`; the same store as
`https://s3.amazonaws.com/janelia-cosem-datasets/jrc_hela-2/jrc_hela-2.n5/em/`
has the endpoint `https://s3.amazonaws.com/janelia-cosem-datasets/`.

**Requests.** The first request is a `GET` of the endpoint followed by
`?list-type=2&prefix=` and `enc(P)`; each later one appends
`&continuation-token=` and `enc(T)`, where `T` is the previous response's
continuation token. `enc` writes the UTF-8 bytes of its argument with every
byte that is not an unreserved character written as `%` and two uppercase
hex digits. Redirects are followed. A response is then:

- status 200: its body is parsed as below; a body that is not a listing
  rejects the input;
- status 408 or 429, any 5xx status, or no response: a failure;
- any other status: the input is rejected. (A plain web server, which has
  no listing operation, answers with 404 or 403, or with 200 and an HTML
  page, and its store inputs are therefore rejected.)

**Listing bodies.** A body MUST be UTF-8 (a byte sequence that is not
UTF-8 rejects the input; a byte order mark is not allowed) in the following
subset of XML 1.0, where `S` is whitespace ([§1.3](#13-arithmetic)):

- The document is optional `S`, an optional XML declaration (`<?xml`
  followed by `S` and then any text up to the first `?>`), then any mix of
  `S` and comments, one element, and any mix of `S` and comments. A comment
  is `<!--`, then any text not containing `--`, then `-->`.
- An element is `<N`, attributes, optional `S`, then either `/>` (empty) or
  `>`, its content and `</N` with the same name, optional `S` and `>`. A
  name `N` is a letter (`A`–`Z`, `a`–`z`), `_` or `:`, followed by letters,
  ASCII digits, `_`, `:`, `.` and `-`.
- An attribute is `S`, a name, optional `S`, `=`, optional `S`, and a value
  in `"` or `'` quotes holding character data and references but not `<` or
  its quote. Attributes are not otherwise read (namespaces are not
  processed; names compare as written).
- Content is any sequence of character data (text not containing `<` or
  `&`, nor the sequence `]]>`), references, comments and elements.
- Elements MUST NOT nest more than 256 deep (the root element is at depth
  1, its children at 2): a deeper document is rejected, as the JSON reader
  rejects deep nesting ([§1.6](#16-json-documents)).
- A reference is `&lt;`, `&gt;`, `&amp;`, `&quot;`, `&apos;` (standing for
  `<`, `>`, `&`, `"`, `'`), or `&#` and decimal ASCII digits, or `&#x` and
  hex digits (either case), then `;`, standing for that code point.
- Every character, written or referenced, MUST be an XML `Char`: U+0009,
  U+000A, U+000D, U+0020–U+D7FF, U+E000–U+FFFD or U+10000–U+10FFFF.
- Nothing else is allowed: no CDATA sections, document type declarations
  or processing instructions other than the declaration. Line ends are
  not normalized.

An element's **text** is its character data and references, in order,
with references replaced by what they stand for; it is only taken of an
element whose content has no element. The body is a **listing** when:

- the element is named `ListBucketResult`;
- of its child elements, exactly one is named `IsTruncated`, whose text is
  `true` or `false`; at most one is named `NextContinuationToken`, and if
  `IsTruncated` is `true` there is one, with nonempty text, which is the
  continuation token `T`, and at least one child is named `Contents`;
  every child named `Contents` has exactly one
  child `Key` and exactly one child `Size`, neither of which has a child
  element; other children, at any depth, are not read;
- each `Contents`' `Size` text is one or more ASCII digits, of value at
  most 2^53 − 1, and its `Key` text starts with `P`.

Each `Contents` lists an object: its key is the `Key` text and its size the
`Size` value. The listing is complete after a response whose `IsTruncated`
is `false`. A key listed twice, in one response or two, rejects the input,
and so does a continuation token that an earlier response of the same
listing gave. (Each response but the last then lists at least one new key,
so a server that repeats a page, or sends empty pages, rejects the input
instead of being followed forever.)
(The order of the listed keys is not checked: the output's order comes from
[§1.4](#14-store-inputs).)

### 1.6 JSON documents

The metadata documents of a store (N5's `attributes.json`, Zarr's
`.zarray`, `.zgroup` and `.zattrs`) are read as JSON, with these rules, in
both implementations:

- The object MUST be at most 16777216 (2^24) bytes. Its bytes MUST be UTF-8
  and MUST NOT start with a byte order mark (`EF BB BF`).
- The text MUST be one JSON value as RFC 8259 defines it (whitespace is
  space, tab, LF and CR). There are no other literals: `NaN`, `Infinity`,
  comments and trailing commas reject the input. Arrays and objects MUST
  NOT nest more than 256 deep (a top-level object is depth 1).
- Every number is converted to binary64 by correct rounding ([§1.3](#13-arithmetic)), and a
  number whose value is then infinite rejects the input. A value that this
  document calls an **integer** is a number whose binary64 value is
  integral and at most 2^53 − 1 in magnitude (so `64`, `64.0` and `6.4e1`
  are the same integer); a larger magnitude is not an integer.
- When an object has a member name more than once, the last member with
  that name is used, and the others are ignored.
- A string escape for a lone surrogate (such as `"\ud800"`) is kept as
  that code unit.

A JSON value that this document copies into the output (attributes) is
copied as these rules read it, except that a number written as an integer
(no fraction or exponent) is copied exactly, every digit, even beyond
2^53 − 1; any other number is its binary64 value. [§1.1](#11-output-and-equivalence) compares them the
same way: an integer literal by its exact value, any other number by its
binary64 value.

### 1.7 Named inputs and layouts

A zarrifier MAY be told, with its input, the input's profile, and the layouts its output
holds. Neither changes what a profile accepts or what it writes for a layout.

- **A named profile.** The input is read by the named profile without the test that would
  otherwise choose one: a file's signature ([§1.2](#12-input)) or a store's root keys
  ([§1.4](#14-store-inputs)). An input that is not the profile's format is rejected by the
  profile's own rules. A file named as a TIFF variant's (`ndpi`, `qptiff`) is read by the
  TIFF profile, which finds the variant, and is rejected unless it finds that one; a file
  named as `tiff` is the variant it finds. A store named as `ome-zarr` is read as Zarr v2
  and is rejected unless that profile finds OME-NGFF 0.4 metadata at its root; a store
  named as `zarr2` is OME-Zarr's when it holds that metadata. A named store input's URL
  need not end in `/`: the zarrifier appends one when it does not.
- **Layouts.** The image layout ([§2](#2-the-zarr-layout)) is the hierarchy's root and is
  always written. A format's dataset layout ([conventions §9](conventions.md#9-dataset-layouts))
  is written unless the zarrifier is told to leave it out: the output then has no
  dataset node, and its root's property no `layouts` member
  ([conventions §2](conventions.md#2-attributes)). A zarrifier told to write a dataset
  layout for a format that has none rejects the request.

Two outputs of one input are equivalent ([§1.1](#11-output-and-equivalence)) when their
zarrifiers wrote the same layouts.

## 2. The Zarr layout

A zarrifier's output presents its input as a Zarr hierarchy whose layout
is specified by the input format's **convention**, in
[conventions.md](conventions.md): the groups and arrays and their
metadata, the OME-NGFF metadata, and the attributes under the key
`refkv_zarrified`, including the translation of the input's header. The
profiles ([Profiles](#profiles)) specify how to read and check the input, and how each
chunk of that layout becomes a reference to the input's bytes; for the
layout itself they refer to the convention, which builds on what all
conventions share ([spec/conventions.md](conventions.md): [§1](conventions.md#1-conventions) the
conventions, [§2](conventions.md#2-attributes) their attributes, [§3](conventions.md#3-arrays) arrays, [§4](conventions.md#4-images) images, [§5](conventions.md#5-units) units, [§6](conventions.md#6-source-metadata-as-json) source
metadata as JSON).

A zarrifier MUST produce exactly the layout the input format's convention
specifies for the input, at the convention's version that the profile
states, with:

- `source.url` ([conventions §2](conventions.md#2-attributes)) the input URL `U` as given ([§1.2](#12-input)): for a
  file input the URL of source 0, for a store input the store URL ([§1.4](#14-store-inputs));
- every chunk entry the convention says is present, with the reference the
  profile specifies, and no other chunk entry;
- the documents as JSON (`zarr.json`, compared by [§1.1](#11-output-and-equivalence)), and no other
  entries than those the profile names.

## Profiles

Each profile is Part 2 of its format's document in [zarrify/](zarrify/), whose Part 1
is the format's Zarr convention, and its registry entry is that document's name in
[zarrify/profiles/](zarrify/profiles/). A format's document is numbered on its own (its
Part 2 after its Part 1), and other documents cite its sections by its name
(`spec/zarrify/<name>.md §n`), so adding a profile adds its own files and renumbers nothing
else. The profiles, generated from the registry:

<!-- profiles:spec-list -->
- the CZI profile: [spec/zarrify/czi.md §7](zarrify/czi.md#7-czi-profile);
- the DICOM profile: [spec/zarrify/dicom.md §7](zarrify/dicom.md#7-dicom-profile);
- the FITS profile: [spec/zarrify/fits.md §9](zarrify/fits.md#9-fits-profile);
- the IMS profile: [spec/zarrify/ims.md §7](zarrify/ims.md#7-ims-profile);
- the MP3 profile: [spec/zarrify/mp3.md §8](zarrify/mp3.md#8-mp3-profile);
- the MP4 profile: [spec/zarrify/mp4.md §8](zarrify/mp4.md#8-mp4-profile);
- the MRC profile: [spec/zarrify/mrc.md §8](zarrify/mrc.md#8-mrc-profile);
- the N5 profile: [spec/zarrify/n5.md §8](zarrify/n5.md#8-n5-profile);
- the ND2 profile: [spec/zarrify/nd2.md §7](zarrify/nd2.md#7-nd2-profile);
- the NDPI profile: [spec/zarrify/ndpi.md §7](zarrify/ndpi.md#7-ndpi-profile);
- the NIfTI profile: [spec/zarrify/nifti.md §7](zarrify/nifti.md#7-nifti-profile);
- the OME-Zarr profile: [spec/zarrify/ome-zarr.md §11](zarrify/ome-zarr.md#11-ome-zarr-profile);
- the QPTIFF profile: [spec/zarrify/qptiff.md §7](zarrify/qptiff.md#7-qptiff-profile);
- the SAFE profile: [spec/zarrify/safe.md §8](zarrify/safe.md#8-safe-profile);
- the TIFF profile: [spec/zarrify/tiff.md §7](zarrify/tiff.md#7-tiff-profile);
- the VSI profile: [spec/zarrify/vsi.md §9](zarrify/vsi.md#9-vsi-profile);
- the Zarr v2 profile: [spec/zarrify/zarr2.md §7](zarrify/zarr2.md#7-zarr-v2-profile).
<!-- /profiles -->

A profile's Part 2 starts by naming the revision of this document it follows, if any, its
section, and the profiles it builds on. [§1](#1-introduction) and [§2](#2-the-zarr-layout)
apply to every profile, and to a store input's in particular the store input rules of
[§1.4](#14-store-inputs)–[§1.6](#16-json-documents). The output has the convention's layout
for the input ([§2](#2-the-zarr-layout)); the profile says how the input is read, which inputs
are rejected, and how each chunk references the input. In Part 2, "the convention §n" is a
section of Part 1.

## Appendix A. Conformance

This section is informative. There are two maintained implementations:
- the Python reference, `python -m refkv.zarrify <url> <out.vzip>`
  (`python/src/refkv/zarrify/`);
- the browser one, `js/src/zarrify/`, run under Node by
  `js/conformance/zarrify.ts`.

Every profile is the Rust core's (`rust/refkv-ir`: its parser or store
format, its projection and the mirror), which both implementations drive;
each host holds what it shares across profiles (`common`, and `store` for
store inputs: the listing, the object reader and the JSON reader of
[§1.4](#14-store-inputs)–[§1.6](#16-json-documents)).

**Resource limit.** The browser implementation fails (it does not reject)
a store whose listing has more than 100000 objects (counting every listed
`Contents`, ignored or not). Each chunk becomes a source and an entry, about
250 bytes of archive held in memory, and the listing takes one request per
1000 keys, so the limit bounds the archive at about 25 MB and the listing at
100 sequential requests. The same limit applies to a SAFE zip file's
entries. The Python reference has no limit; its run on a 470000-chunk
OpenOrganelle level is recorded in `spec/history/virtualize/REVISIONS.md`
(revision 12).

Implementations written from this document alone, round by round, are in
`conformance/impls/zarrify/`; `spec/history/virtualize/REVISIONS.md` records what each
round found and how this document changed, up to revision 29; since then each
profile's document records its own changes.

Both write the pins of [§1.2](#12-input) and [§1.4](#14-store-inputs), and record checksums when asked to
(`--checksums`, after the two arguments, in either command).

`conformance/zarrify/compare.py` runs implementations on a corpus and
compares their outputs by [§1.1](#11-output-and-equivalence) (`conformance/zarrify/HARNESS.md`
describes the command an implementation provides). The corpus has:
- the synthetic files and stores in `fixtures/<profile>/`, including inputs
  each profile rejects (the files of the suffixes the registry,
  [zarrify/profiles/](zarrify/profiles/), gives the profile;
  a store profile's one directory per store);
- the 205 OME-TIFFs of IDR idr0096;
- public files and stores of every profile, listed in
  `conformance/zarrify/corpus_<profile>.txt` (NDPI's in TIFF's), each
  file saying where its inputs come from.

The harness's proxy (`conformance/zarrify/proxy.py`) serves the synthetic
stores, and forwards remote ones, with the listing operation of [§1.5](#15-listing-a-store).

Pixel correctness is checked separately, against an independent reader of
the format, by `js/test/<profile>/verify.py`, whose docstring names the
reader and what it compares (the format's README in `docs/zarrify/` says
so too).

The conventions of [conventions §2](conventions.md#2-attributes) are checked by `python/tests/test_zarrify_conventions.py`:
it runs every synthetic file and store through both implementations,
validates every node that declares a convention against that convention's
JSON Schema (`spec/zarrify/<p>/schema.json`, written by
`spec/generate_schemas.py`), and checks that every node with source
metadata declares it. `just tag-conventions` creates the
`zarrify-<p>-v<N>` tags that the convention URLs name, on `main`, from
version 1 on; at version 0 the URLs name `main` itself ([conventions §1](conventions.md#1-conventions)).

Only the TIFF and ND2 profiles have been through the independent
implementation rounds (revisions 1 to 4); every profile added since
(`spec/history/virtualize/REVISIONS.md` says when) has not, and the round
implementations in `conformance/impls/zarrify/` still write JPEG headers
as literals and ranges of the file, and predate the conventions (revisions
15 and 16).
