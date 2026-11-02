# vzip: the ZIP binding of refkv

Format version: 0 (**provisional**) · Specification revision: 11.3

This document is Part 2 of the refkv specification: the **ZIP binding**, which
keeps a store in a single ZIP file, a **vzip archive**. [Part 1](../refkv.md),
in spec/refkv.md, specifies the reference value type, and its introduction
([§1](../refkv.md#1-introduction)) covers every part: the conventions, the
conformance of readers and writers, and the format version and revisions,
which this part shares with Part 1 (Part 3, the draft perfect-hash binding,
shares the revisions alone, [§1.3](../refkv.md#13-versioning)). The sections are numbered as one specification, so
this part starts at [§9](#9-what-the-zip-binding-defines).

A vzip archive is always a valid ZIP file. A ZIP reader that knows nothing
about vzip sees every key as an ordinary file. For a reference, that file
contains the encoded reference (or nothing) rather than the referenced bytes.

All multi-byte integers in ZIP structures and in the archive comment are
unsigned little-endian. `u16`, `u32`, `u64` denote unsigned integers of 16,
32, 64 bits.

"APPNOTE" means the PKWARE .ZIP File Format Specification, version 6.3.10 or
later. Where this document is silent about ZIP structures, APPNOTE applies.

# Part 2. The ZIP binding

## 9. What the ZIP binding defines

A **vzip archive** is a store kept in a single ZIP file. The ZIP binding
answers the three questions of [§3.1](../refkv.md#31-what-a-binding-defines) as
follows:

| | in a vzip archive |
|---|---|
| marking | a reference is an entry whose central directory record has one extra field block with header ID 0x7A76 (its payload is a `Range`) or 0x7A77 (a `Concat`). The block's data is the payload ([§11](#11-entries)). |
| source table | the entry `__vz__/sources`. Its auxiliary data is an optional page index, the entry `__vz__/index` ([§12](#12-page-index-optional)). The archive comment locates both, and starts with the magic `vzip/0`, which identifies the archive and its format version ([§10.4](#104-end-of-central-directory-record-and-archive-comment)). |
| names | a key is the file name of its entry, in UTF-8 ([§10.3](#103-file-names)). The binding reserves the names that begin with `__vz__/`: their entries are hidden, outside the mapping ([§11.4](#114-hidden-entries)). |

The rest of this part is what the ZIP container needs: its structure and
ZIP64 ([§10](#10-zip-container)), the rules for entries
([§11](#11-entries)), an optional page index that lets readers of large
archives skip most of the central directory
([§12](#12-page-index-optional)), and the parts of reading
([§13](#13-reading-a-vzip-archive)), writing
([§14](#14-writing-a-vzip-archive)) and security
([§15](#15-security-considerations-for-zip)) that depend on ZIP.
[§9.1](#91-related-zip-profiles-informative) relates the binding to two other
profiles of ZIP.

### 9.1 Related ZIP profiles (informative)

Two other specifications define profiles of ZIP. Neither is required here,
and nothing in this section is normative.

- **OME-Zarr in a ZIP file**
  ([RFC 9](https://github.com/ome/ngff/blob/main/rfc/9/index.md), `.ozx`)
  is Zarr in a ZIP file with further restrictions (one hierarchy, the root
  `zarr.json` at the root, and recommendations on ZIP64, compression, entry
  order and the archive comment). It adds no meaning to a ZIP file's
  entries, so there is nothing for vzip to combine with it. vzip does not
  aim to produce `.ozx` files:
  - **Reference entries.** An `.ozx` reader takes every entry's body as its
    value. For a reference entry the body is empty or the reference payload
    ([§11.3](#113-reference-entries)), so an archive with reference entries
    cannot be read as an `.ozx` file, whatever its comment says. An archive
    with none is close to zipped Zarr already, and its `bytes` entries read
    as ordinary ZIP files.
  - **Archive comment.** A ZIP file has one comment, and a vzip archive's is
    the vzip comment
    ([§10.4](#104-end-of-central-directory-record-and-archive-comment)), not
    RFC 9's JSON comment.
  - **`zarr.json` first.** An archive with a page index has its central
    directory in key order ([§12.1](#121-layout)), where `0/zarr.json` sorts
    before `zarr.json`. Pinned entries ([§12.1](#121-layout)) serve the same
    purpose.

  The relationship runs the other way: an `.ozx` file is one of the objects
  a vzip archive can refer to. RFC 9 recommends STORED entries, and the body
  of a STORED entry is a byte range of the `.ozx` file, which a reference
  ([§4.2](../refkv.md#42-range)) can name like a chunk in any other file. For file
  names, see [§14.3](#143-archive-file-names).
- **Seek-optimized ZIP** ([SOZip](https://github.com/sozip/sozip-spec))
  makes ranges of one large DEFLATE entry readable without inflating it
  from the start, using an index stored in a file that has a local header
  but no central directory record. vzip does not support SOZip: such a file
  is not allowed in a vzip archive ([§10.1](#101-structure) rule 2), a
  DEFLATE body is always inflated in full ([§13.3](#133-errors)), and an
  entry of 4 GiB or more must be STORED ([§10.1](#101-structure) rule 7).
  vzip's large values are references to other files, or STORED entries such
  as Zarr shards, which are read by range directly.

## 10. ZIP container

### 10.1 Structure

A valid archive is a single-disk ZIP archive conforming to APPNOTE that
also satisfies the following:

1. The archive begins at file offset 0. There is no prepended data, and all
   offsets in this document are absolute file offsets.
2. Every entry has exactly one local file header and one central directory
   record, and no two central directory records have the same file name.
   Every local file header belongs to an entry: a writer MUST NOT write a
   local header that no central directory record points to (as SOZip's
   index files are, [§9.1](#91-related-zip-profiles-informative)). Readers
   never look for one ([§13.4](#134-violations-readers-need-not-detect)).
   Bytes that belong to no entry MAY lie between entries, and between the
   last entry and the central directory, for example padding that makes
   bodies start at aligned offsets ([§14.2](#142-recommended-layout-informative)).
   They MUST NOT contain the local file header signature 0x04034b50.
   Readers never read them. An archive with such bytes may not be readable
   by ZIP tools that read local headers one after another instead of
   through the central directory, such as Java's `ZipInputStream`.
3. Every entry uses compression method 0 (STORED) or 8 (DEFLATE). DEFLATE
   bodies are raw DEFLATE streams (RFC 1951) with no zlib or gzip wrapper.
4. Every local file header has an extra field length of 0, except that of a
   large entry (rule 7), whose extra field is exactly one ZIP64 extended
   information extra field of 20 bytes: header ID 0x0001, data size 16, then
   the uncompressed size and the compressed size as `u64`. The body of an
   entry therefore starts at `local_header_offset + 30 + file_name_length`,
   plus 20 for a large entry, computed from the central directory record
   alone (the **body offset**).
5. No entry is encrypted (general purpose bit 0 clear), and general purpose
   bit 3 (data descriptor) is clear. General purpose bit 11 (UTF-8 file
   names) is set in both headers of every entry.
6. The CRC-32, compressed size and uncompressed size in both headers are
   correct for the entry's body.
7. An entry is **large** if its compressed or uncompressed size is
   0xFFFFFFFF or more. In both headers of a large entry, both 32-bit size
   fields are 0xFFFFFFFF and the sizes are in the ZIP64 extra field (rule 4,
   [§10.2](#102-zip64)). Sizes are less than 2^64. A large entry MUST use
   method 0, so that a range of it can be read without inflating the whole
   body; a large entry with method 8 is an entry error. Reference entries
   are never large ([§11.3](#113-reference-entries)), and nor are the format
   entries ([§11.4](#114-hidden-entries)), which use method 8: a writer whose
   source table or page index would reach 0xFFFFFFFF bytes MUST fail
   ([§14.1](#141-requirements)).
8. Central directory records have no file comment (comment length 0).
9. The archive has no central directory encryption, digital signature or
   archive extra data record.
10. The archive comment is the vzip comment
    ([§10.4](#104-end-of-central-directory-record-and-archive-comment)).

### 10.2 ZIP64

Every archive ends with the ZIP64 end records, whatever its size. Central
directory records use ZIP64 only where a value is too large for its field:

- **End records.** A writer MUST write, in this order and with nothing
  between them:
  1. the central directory;
  2. a zip64 end of central directory record (44 bytes after its size field,
     so its size field is 44; no extensible data sector; "version needed to
     extract" 45). Its two entry counts and its central directory size and
     offset hold the actual values;
  3. a zip64 end of central directory locator holding the offset of that
     record (total number of disks 1);
  4. the end of central directory record
     ([§10.4](#104-end-of-central-directory-record-and-archive-comment)). Its
     two entry counts are 0xFFFF, and its central directory size and offset
     are 0xFFFFFFFF, in every archive.

  The end of an archive therefore has the same shape at every size. A ZIP
  reader that knows nothing about vzip is sent to the zip64 record by the
  all-ones fields, so it reads the same four values a vzip reader does.
- **Central directory records.** A record's ZIP64 extended information
  extra field (`0x0001`) holds, in this order (as APPNOTE orders them), the
  uncompressed size and the compressed size if the entry is large
  ([§10.1](#101-structure) rule 7), then the local header offset if it is
  0xFFFFFFFF or more; each value it holds has its 32-bit field set to
  0xFFFFFFFF. A writer MUST NOT write the block on a record that needs
  neither. The block holds both sizes or neither, and MUST NOT hold an
  offset that fits its 32-bit field.

A reader decodes a central directory record's sizes and offset like this:

- **Sizes:** if both size fields are 0xFFFFFFFF, the entry is large and its
  sizes are the first 16 bytes of the `0x0001` block (uncompressed, then
  compressed). Otherwise the size fields are the sizes.
- **Offset:** if the offset field is 0xFFFFFFFF, the offset is the next 8
  bytes of the block, after the sizes if the entry is large. Otherwise the
  offset field is the offset.

A record has an entry error if:

- exactly one of its size fields is 0xFFFFFFFF;
- it needs values from a `0x0001` block and has none, or one shorter than
  the values it needs (16 bytes for the sizes, 8 for the offset);
- it has more than one `0x0001` block, whatever it needs.

A `0x0001` block on a record that needs no value from it is otherwise
ignored, and so are any bytes of a block after the values a record needs.

A reader takes the central directory's size and offset from the zip64 end of
central directory record, in every archive:

- a zip64 locator MUST immediately precede the end of central directory
  record: its signature is at that record's offset minus 20;
- the zip64 record the locator points to MUST lie within the file, have the
  right signature and a size field of 44.

Any failure of these rules is an archive error. In particular, a ZIP file
without the zip64 end records is not a valid archive, however small it is.

Readers MUST ignore the entry counts, central directory size and central
directory offset of the end of central directory record, whatever they hold,
and the entry counts of the zip64 record. Readers are not required to check
that the zip64 record sits between the central directory and the locator
([§13.4](#134-violations-readers-need-not-detect)).

Readers MUST ignore disk-number fields, "version made by" and "version
needed to extract" values, timestamps, and internal and external
attributes.

### 10.3 File names

The key of an entry is its file name, decoded as UTF-8. Keys follow
[§2.1](../refkv.md#21-keys): they are not empty, they are valid UTF-8, and they are
compared and sorted as byte strings. A key's UTF-8 encoding is at most 65535
bytes, the longest file name a ZIP record can hold
([§14.1](#141-requirements)).

A central directory record whose file name is empty or not valid UTF-8 is
ignored by readers: it names no key and appears in no operation
([§13.4](#134-violations-readers-need-not-detect)).

### 10.4 End of central directory record and archive comment

The ZIP archive comment of a vzip archive is one of:

| length | layout                                                           |
|--------|------------------------------------------------------------------|
| 22     | `magic`, `sources_offset: u64`, `sources_size: u64`              |
| 38     | `magic`, `sources_offset: u64`, `sources_size: u64`, `index_offset: u64`, `index_size: u64` |

- `magic` is the 6 ASCII bytes `vzip/0`: the prefix `vzip/` followed by the
  format version as one decimal digit ([§1.3](../refkv.md#13-versioning)). A future
  version 10 or above will need a different comment layout, which that
  version will define.
- `sources_offset` and `sources_size` are the body offset and the compressed
  size of the `__vz__/sources` entry.
- `index_offset` and `index_size`, if present, are the same for
  `__vz__/index`. The 38-byte form is used if and only if the archive has a
  page index ([§12](#12-page-index-optional)).

Because the comment has one of two lengths, the end of central directory
record starts either 44 or 60 bytes before the end of the file. A reader
locates it as follows:

1. If offset `file_size − 60` holds the 4-byte signature `0x06054B50` and
   that record's comment length field is 38, the end of central directory
   record is there.
2. Otherwise, if offset `file_size − 44` holds the signature and a comment
   length of 22, it is there.
3. Otherwise the file is not a vzip archive.

The reader then checks that the comment starts with `vzip/` and a version
the reader implements (`vzip/0` for this document). It does not fall back
from step 1 to step 2 when the magic doesn't match. If no record is found, or
the comment does not start with `vzip/`, the file is not a vzip archive.
If it starts with `vzip/` and a version the reader doesn't implement, the
archive is of an unsupported version. Either way, opening it is an archive
error ([§6.4](../refkv.md#64-errors)).

In a valid archive, step 1 cannot match a record that isn't the real one.
When the comment is 22 bytes long, offset `file_size − 60` is 16 bytes before
the real record, inside the zip64 locator ([§10.2](#102-zip64)): the
signature would be read from the locator's disk-number field, and the
comment length from the real record's disk-number field. Both are 0 in a
single-disk archive ([§10.1](#101-structure)). An invalid archive can set
those fields so that step 1 matches a fake record; readers then reject it
(magic mismatch, no fallback), which is the intended result.

Readers MUST read `__vz__/sources` and `__vz__/index` through the comment's
offsets and sizes. They need not consult those entries' central directory
records. Writers MUST make the two agree.

## 11. Entries

### 11.1 Classification

The kind of a present key is decided by its central directory record alone.
The record's extra field is parsed per APPNOTE as a sequence of
`(header_id: u16, data_size: u16, data[data_size])` blocks that exactly fills
the declared extra field length. Let `R` be the number of blocks whose header
ID is 0x7A76 or 0x7A77:

| condition | kind |
|---|---|
| `R = 0` | `bytes` |
| `R = 1` | `reference` |
| `R > 1`, or the extra field does not parse | entry error ([§13.3](#133-errors)) |

Other header IDs (such as `0x0001`) MAY appear and do not affect the kind.
The format entries MUST be `bytes` entries with method 8. Readers locate and
inflate them through the comment
([§10.4](#104-end-of-central-directory-record-and-archive-comment)).

### 11.2 Bytes entries

The value of a `bytes` entry is its body, inflated if the entry uses method 8.

### 11.3 Reference entries

The data of the 0x7A76 or 0x7A77 block is the reference payload
([§4.4](../refkv.md#44-reference-payloads)):

- header ID 0x7A76: an encoded `Range` ([§4.2](../refkv.md#42-range)), describing a
  value made of that one range.
- header ID 0x7A77: an encoded `Concat` ([§4.3](../refkv.md#43-concat)), describing a
  value made of zero or more ranges.

Writers MUST use 0x7A76 when a reference has exactly one range, and 0x7A77
otherwise. Readers MUST accept a 0x7A77 payload with any number of parts.

A reference entry MUST use method 0; a reference entry with method 8, or a
large one ([§10.1](#101-structure) rule 7), is an entry error. Extra field
blocks may appear in any order. Its body MUST be
either empty or byte-identical to the reference payload. Writers SHOULD make
it identical, so that ZIP tools which ignore extra fields still see the
payload. Readers MUST take the payload from the central directory, and MUST
NOT read or check the body except in the raw view
([§13.2](#132-operations)).

A reference payload MUST NOT exceed 65519 bytes; a reader treats a longer
one as malformed (a payload error, [§4.4](../refkv.md#44-reference-payloads)), even
though it may fit in the extra field. That limit leaves room in the
65535-byte extra field for the payload's own block header and a ZIP64
block, whether or not the entry ends up needing one, so a writer can check a
reference before deciding where to put it.

### 11.4 Hidden entries

Entries whose names begin with the **reserved prefix** `__vz__/` are
**hidden**. They are entries of the archive, but their keys are not part of
the store's mapping ([§3.1](../refkv.md#31-what-a-binding-defines)): classify and get
treat them as *missing*, and list leaves them out
([§13.2](#132-operations)). A ZIP tool that knows nothing about vzip still
sees them as ordinary files, and so does the raw view.

Two hidden entries hold the binding's own data. They are the **format
entries**, written by the writer itself:

- `__vz__/sources`, the source table ([§5](../refkv.md#5-source-table)), in every
  archive;
- `__vz__/index`, the page index ([§12](#12-page-index-optional)), in an
  archive that has one.

Readers find both through the archive comment
([§10.4](#104-end-of-central-directory-record-and-archive-comment)).

A writer MAY write other hidden entries, holding bytes for the archive's
own use. A `key` source ([§5](../refkv.md#5-source-table)) MAY name a hidden `bytes`
entry. A `key` source that names a format entry fails with a resolution
error, and a writer rejects it ([§14.1](#141-requirements)).

## 12. Page index (optional)

A page index lets a reader find the central directory record of a key by
reading one bounded slice of the central directory, instead of the whole
directory. An archive has a page index if and only if its comment has the
38-byte form; then it has an entry `__vz__/index` (method 8) holding a
`CdIndex`. An archive with a 22-byte comment MUST NOT have an entry named
`__vz__/index`.

```proto
message CdIndex {
  message Page {
    string first_key = 1;
    uint64 offset    = 2;
    uint64 length    = 3;
  }
  message Pinned {
    string key         = 1;
    uint64 data_offset = 2;
    uint64 size        = 3;
    uint64 csize       = 4;
    uint32 method      = 5;
  }
  repeated Page   pages  = 1;
  repeated Pinned pinned = 2;
}
```

### 12.1 Layout

In an archive with a page index:

1. The central directory consists of the **body records** (the records of
   every entry except the two format entries), sorted in UTF-8 order of
   their file names. These are followed by the two format entries' records,
   in either order.
2. The pages partition the body records:
   - page `i` covers bytes `[offset_i, offset_i + length_i)` of the central
     directory, with offsets relative to its first byte;
   - the first page starts at 0, each later page starts where the previous one
     ends, and the last page ends where the format entries' records begin;
   - pages are non-empty and hold whole records;
   - `first_key` is the key of a page's first record, so `first_key` values
     strictly increase in UTF-8 order;
   - if there are no body records, there are no pages.
3. `pinned` lists zero or more `bytes` entries, other than the format entries,
   with no key listed twice. Pinned entries may be hidden. Each one gives:
   - `data_offset`: the entry's body offset;
   - `size` and `csize`: its uncompressed and compressed sizes;
   - `method`: its method.

   These MUST match the entry's central directory record. Writers SHOULD pin
   the small entries that readers fetch when opening, such as Zarr metadata
   documents ([§14.2](#142-recommended-layout-informative)).

### 12.2 Lookup

In an archive with a page index, readers MUST determine whether a key is
present as follows:

- The format entries are known from the comment
  ([§10.4](#104-end-of-central-directory-record-and-archive-comment)).
- A pinned key is a present `bytes` entry, described by its `Pinned` values.
- To find any other key `k`, select the last page whose `first_key` is less
  than or equal to `k` in UTF-8 order. If there is no such page, `k` is
  missing. Otherwise `k` is present if and only if that page contains a record
  named `k`.

A reader that reads the whole central directory anyway gets the same result
for every valid archive. Readers are not required to check that the
central directory matches [§12.1](#121-layout)
([§13.4](#134-violations-readers-need-not-detect)).

The page index itself is checked at open ([§13.1](#131-opening)). It is
**malformed**, an archive error, if any of the following hold:

- it does not decode;
- a page has length 0, or lies outside the central directory;
- the pages are not contiguous from offset 0;
- `first_key` values do not strictly increase in UTF-8 order;
- a page's `first_key` or a pinned `key` is empty;
- a pinned key is listed twice or is a format entry;
- a pinned `method` is not 0 or 8;
- a pinned entry is large (its `size` or `csize` is 0xFFFFFFFF or more,
  [§10.1](#101-structure) rule 7) and its `method` is not 0. No central
  directory record is consulted for a pinned key, so this is where a large
  DEFLATE entry is caught;
- a pinned body (`data_offset`, `csize`) lies outside the file.

## 13. Reading a vzip archive

### 13.1 Opening

Opening a vzip archive ([§6.1](../refkv.md#61-opening)) reads and checks the following,
besides the source table. Any failure is an archive error:

- **The end records:**
  - the end of central directory record is found as in
    [§10.4](#104-end-of-central-directory-record-and-archive-comment), and
    the magic is `vzip/0`;
  - the zip64 locator immediately precedes the end of central directory
    record, and the zip64 record it points to lies within the file and has
    the right signature and size field ([§10.2](#102-zip64));
  - the central directory (offset and size) lies within the file.

  Here and throughout, "lies within the file" means the region
  `[offset, offset + size)` is inside `[0, file size]`. Readers do not check
  whether regions overlap each other or the end records.

  A resource limit ([§6.7](../refkv.md#67-reader-policy),
  [§8](../refkv.md#8-security-considerations)) reached while opening, for example while
  inflating a format entry, is an archive error ([§6.1](../refkv.md#61-opening)).

  The same corruption of a central directory record is an archive error in
  an archive without a page index (the whole directory is parsed at open),
  and an entry error, or nothing at all, in one with a page index (only the
  pages that are read are parsed). This is intentional: it is the cost of
  opening large archives without reading their whole directory.
- **The format entries:** the bodies the comment locates lie within the
  file and inflate cleanly (below). Their uncompressed sizes are not checked,
  since readers need not read their records. The source table is valid
  ([§5](../refkv.md#5-source-table)). The page index, if there is one, is not malformed
  ([§12.2](#122-lookup)).
- **The central directory, for an archive without a page index:** it parses
  as a sequence of records, each with the right signature and with its
  variable-length fields inside the directory, ending exactly at its
  declared size.
  - A problem confined to a single record's contents is not an archive
    error. Such problems include its extra field, method or flags, and a
    local header offset of 0xFFFFFFFF without a ZIP64 extra block holding
    the offset; they are entry errors ([§13.3](#133-errors)), in archives
    with or without a page index.
  - An `__vz__/index` record is an archive error here
    ([§12](#12-page-index-optional)).

A body **inflates cleanly** if it is a single complete raw DEFLATE stream
(its final block ends it) and the stream ends exactly at the end of the
body: no bytes follow it, and none are missing. For a bytes entry it must
also inflate to the record's uncompressed size. Some libraries ignore
trailing bytes by default, so implementations must check for them
explicitly.

Readers stop inflating a format entry at their limit for data read at open
([§6.7](../refkv.md#67-reader-policy) rule 6). A `bytes` entry's body is bounded
by the uncompressed size of its record (it must inflate to exactly that),
and readers SHOULD stop inflating it once its output passes that size.

A reader MAY read more, but MUST NOT report an error at open that this
section and [§6.1](../refkv.md#61-opening) do not list.

### 13.2 Operations

On a vzip archive, the operations of [§6.2](../refkv.md#62-operations) work as follows.

- **classify(key)** looks only at the key's central directory record
  ([§11.1](#111-classification)). A hidden key ([§11.4](#114-hidden-entries))
  is *missing* for classify and get.
- **list(prefix)** lists no hidden key. In an archive with a page index, it
  takes "present" as defined by [§12.2](#122-lookup):
  - pinned keys are listed;
  - a record found in a page is listed only if lookup would find it there,
    which in a valid archive is every record;
  - page `i` covers the keys from `first_key` of page `i` up to, but not
    including, `first_key` of page `i + 1`; the last page has no upper bound;
  - `list(prefix)` reads every page whose range includes at least one key
    starting with `prefix`, including pages that turn out to hold only
    hidden keys, and fails with an entry error if one of them cannot be
    parsed.

  Exactly, with byte strings compared in UTF-8 order: page `i` with range
  `[lo, hi)` is read if and only if
  - `hi` is absent or `prefix < hi`, and
  - `lo <= prefix`, or `lo` starts with `prefix`.

  (Every key starting with `prefix` is at least `prefix`, and the keys
  starting with `prefix` form one contiguous run in UTF-8 order.) Example:
  for pages with first keys `a/0`, `a/5`, `b/0` and the prefix `a/`, the
  first page is read (`a/0` starts with `a/`), and so is the second (`a/5`
  starts with `a/`). The third page is not read: `b/0` doesn't start with
  `a/`, and `a/` is less than `b/0`.

  A page **cannot be parsed** if it is not a sequence of whole central
  directory records with correct signatures that exactly fills the page.
- **raw(key)** → the inflated body of the key's entry, or *missing*. In this
  view every present entry is visible, including hidden and format entries,
  and a reference entry yields its body (empty, or its payload). This is the
  view of a ZIP tool that knows nothing about vzip. The two format entries
  are read through the comment's offsets and sizes
  ([§10.4](#104-end-of-central-directory-record-and-archive-comment)), like
  everywhere else; `raw("__vz__/index")` is *missing* in an archive without
  a page index.

### 13.3 Errors

In a vzip archive, the error classes of [§6.4](../refkv.md#64-errors) have these
causes, besides those of Part 1:

| class | causes in a vzip archive |
|---|---|
| **archive error** | everything [§13.1](#131-opening) checks |
| **entry error** | the key's record violates [§11.1](#111-classification) (unparseable extra field, several reference blocks), uses a method other than 0 and 8, has bit 0 set, is a reference entry with method 8 or a large one ([§11.3](#113-reference-entries)), is a large entry with method 8 ([§10.1](#101-structure) rule 7), or breaks the ZIP64 block rules ([§10.2](#102-zip64)); or the page that lookup ([§12.2](#122-lookup)) selects for the key cannot be parsed. In that last case the key is reported as an entry error even if it isn't in the page, because the reader cannot tell. |
| **body error** | an entry's body (`[body offset, body offset + compressed size)`, judged as a whole even for a small window) lies outside the file; a DEFLATE body does not inflate cleanly ([§13.1](#131-opening)); a STORED entry's compressed and uncompressed sizes differ. This applies to bytes entries for get and raw, and to reference entries for raw. |
| **payload error** | a reference payload longer than 65519 bytes ([§11.3](#113-reference-entries)) |

In the order of checks of [§6.4](../refkv.md#64-errors), step 3 is the lookup of the
key's central directory record, and that record's entry error. In step 4, a
DEFLATE body is always inflated and checked in full, even for a small
window, so corruption anywhere in it is reported. A STORED body is read only
within the window.

### 13.4 Violations readers need not detect

For archives that break these writer requirements, reader results are
unspecified (besides those of [§6.6](../refkv.md#66-violations-readers-need-not-detect)):

- [§10.1](#101-structure) rules 1, 2, 4, 5 (except bit 0), 6, 7 (except a
  large entry with method 8, and a large reference entry,
  [§11.3](#113-reference-entries)) and 8: readers use body offsets computed
  from the central directory, need not read local headers, and need not
  verify CRC-32 values. Duplicate names are included here. A reader that
  does verify a CRC-32 and finds a mismatch reports a body error.
- [§10.2](#102-zip64): whether a record's ZIP64 extra block was written only
  where needed; whether the end of central directory record's counts, size
  and offset are all ones (readers MUST still ignore them); and whether the
  zip64 record immediately follows the central directory and immediately
  precedes its locator.
- [§10.4](#104-end-of-central-directory-record-and-archive-comment) and
  [§11.1](#111-classification): whether the format entries' records agree
  with the comment, are `bytes` entries, and use method 8.
- [§11.3](#113-reference-entries): whether a reference entry's body is empty
  or equal to its payload. Readers MUST still ignore the body, except in the
  raw view.
- [§12.1](#121-layout): whether the central directory matches the page index
  (beyond the checks in [§12.2](#122-lookup)), and whether pinned values
  match their records.

## 14. Writing a vzip archive

### 14.1 Requirements

A writer of a vzip archive follows [§7](../refkv.md#7-writing). Its input may include
hidden keys ([§11.4](#114-hidden-entries)). It MUST also reject its input,
producing no archive, if:

- a key is a format entry's name (`__vz__/sources` or `__vz__/index`), or a
  `key` source names one;
- a reference payload exceeds 65519 bytes ([§11.3](#113-reference-entries));
- a key's UTF-8 encoding is longer than 65535 bytes;
- a bytes entry of 0xFFFFFFFF bytes or more, compressed or uncompressed,
  would use method 8, or the source table or page index would reach
  0xFFFFFFFF bytes ([§10.1](#101-structure) rule 7);
- a pinned entry is not a bytes entry, is a format entry, or is listed
  twice.

`__vz__/index` MUST follow every pinned entry: it records their body
offsets, so writing it earlier would shift them by its own size. The page
offsets it records are relative to the central directory, which always
follows every entry, so they don't constrain where the index goes.

### 14.2 Recommended layout (informative)

```
[entries ...]                     bytes and reference entries
[__vz__/sources]                  DEFLATE
[pinned entries]                  small entries readers fetch when opening (e.g. zarr.json)
[__vz__/index]                    if paged
[central directory]
[zip64 end record + locator]      always
[end of central directory + vzip comment]
```

Writers MAY pad between entries ([§10.1](#101-structure) rule 2) so that
bodies start at aligned offsets, for readers that map or read chunks
directly from storage. The cost is that ZIP tools which read local headers
in sequence, instead of through the central directory, cannot read the
archive.

With this layout a reader can open an archive with two range requests. It
reads the last 64 KiB, which holds the end records and comment, and then the
bytes from `sources_offset` to the end of the index (for a paged archive) or
to the end of the file (for an unpaged one).

For reproducible output, writers SHOULD use the DOS date 1980-01-01 00:00 and
"version made by" 20, and SHOULD use "version needed to extract" 45 for
records with a ZIP64 extra field and 20 otherwise. Readers MUST NOT depend on
any of these values.

### 14.3 Archive file names

A vzip archive SHOULD be named with the extension `.vzip`. An archive with
reference entries SHOULD NOT be named `.ozx`: tools that open `.ozx` files
read every entry's body as its value, which for a reference entry it is not
([§9.1](#91-related-zip-profiles-informative)). Readers MUST NOT depend on
the file name.

## 15. Security considerations for ZIP

- A DEFLATE body can expand greatly. The memory bound of
  [§8](../refkv.md#8-security-considerations) covers it: readers stop inflating a
  format entry at the limit of [§6.7](../refkv.md#67-reader-policy) rule 6, and a
  `bytes` entry at its record's uncompressed size ([§13.1](#131-opening)).
- To a ZIP tool, keys are file names. A tool that knows nothing about vzip
  extracts every key as a file, so a key such as `../x` meets that tool's
  own protections, or none; tools that know vzip follow
  [§8](../refkv.md#8-security-considerations). Such a tool also extracts a reference
  entry as its payload, or as an empty file, not as the referenced bytes.

## Appendix B: Constants

| name                      | value        | defined by |
|---------------------------|--------------|------------|
| reserved prefix           | `__vz__/`    | ZIP binding |
| source table entry        | `__vz__/sources` | ZIP binding |
| single-range extra ID     | `0x7A76`     | ZIP binding |
| concat extra ID           | `0x7A77`     | ZIP binding |
| page index entry          | `__vz__/index`   | ZIP binding |
| comment magic             | `vzip/0` (6 bytes) | ZIP binding |
| comment lengths           | 22 (no page index), 38 (page index) | ZIP binding |
