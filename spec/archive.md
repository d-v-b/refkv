# vzip: ZIP plus a reference value type

Format version: 0 (**provisional**) · Specification revision: 11

## 1. Introduction

vzip is ZIP plus a reference value type: a key-value store in which every
value is either bytes or a **reference**, a description of how to assemble
the value from byte ranges of other objects. Because the reference type does
not depend on ZIP, any storage backend that can tell a bytes value from a
reference value can hold the same virtual store.

This document has two parts:

- [Part 1](#part-1-reference-values) specifies the reference value type and
  everything that follows from it without a backend: the data model, the
  encoded references, the table of the objects they read, how readers
  resolve them, the errors, the reader policy and the writer's checks.
- [Part 2](#part-2-the-zip-binding) is the **ZIP binding**: how a store is
  kept in a single ZIP file, a **vzip archive**.

A binding defines only three things ([§3](#3-bindings)): how a value is
marked as a reference, where the store's source table is kept, and how keys
map to the backend's names. ZIP is the one binding this document makes
normative.

A reference reads its bytes from external objects named by URL, from other
values of the same store, or from literal bytes it carries. The motivating
use is "virtual" Zarr datasets, whose chunks live inside other files (HDF5,
netCDF, TIFF, ...). Nothing in this specification depends on Zarr; keys are
opaque strings.

A vzip archive is always a valid ZIP file. A ZIP reader that knows nothing
about vzip sees every key as an ordinary file. For a reference, that file
contains the encoded reference (or nothing) rather than the referenced bytes.

### 1.1 Conventions

The key words MUST, MUST NOT, REQUIRED, SHOULD, SHOULD NOT, MAY, and OPTIONAL
are to be interpreted as described in RFC 2119.

A **store** is the key-value mapping of [§2](#2-data-model), as a backend
holds it. A **binding** says how a backend holds a store
([§3](#3-bindings)). A store held by the ZIP binding is a **vzip archive**,
or an **archive** for short. Part 1 says "store" wherever any binding is
meant.

All multi-byte integers in ZIP structures and in the archive comment are
unsigned little-endian. `u16`, `u32`, `u64` denote unsigned integers of 16,
32, 64 bits.

"APPNOTE" means the PKWARE .ZIP File Format Specification, version 6.3.10 or
later. Where this document is silent about ZIP structures, APPNOTE applies.

Byte ranges are written `[a, b)`: start inclusive, end exclusive. Strings are
compared by their UTF-8 bytes, lexicographically ("UTF-8 order").

### 1.2 Conformance

This document places requirements on **writers** (programs that create
stores) and **readers** (programs that read them). A **valid store**
satisfies every requirement placed on writers, by Part 1 and by its binding.
A **valid archive** is a valid store held by the ZIP binding.

Reader behavior is fully specified for valid stores, and for the invalid
stores that [§6](#6-reading) and the binding ([§13](#13-reading-a-vzip-archive)
for ZIP) classify. [§6.6](#66-violations-readers-need-not-detect) and
[§13.4](#134-violations-readers-need-not-detect) list the writer violations
that readers are not required to detect; for those, the results are
unspecified.

A reader or a writer follows Part 1 and a binding. This document defines one
binding, ZIP, and its conformance suite tests vzip archives.

### 1.3 Versioning

vzip has an integer **format version**, recorded where the binding says
([§3.1](#31-what-a-binding-defines)): a vzip archive writes it in its comment
as the magic `vzip/<version>`
([§10.4](#104-end-of-central-directory-record-and-archive-comment)). This
document specifies format version **0**. A format version covers Part 1 and
the ZIP binding together: a change to either is a change to the format.

- **What a version is:** a format version fixes the meaning of every valid
  store. Readers implement a set of versions and MUST reject a store whose
  version they don't implement, as an archive error. Readers MUST NOT guess
  at an unknown version, even a higher one.
- **Revisions of this document:** the document itself has revisions, such as
  "version 0, revision 5". A revision may:
  - clarify text;
  - resolve an ambiguity by choosing one of the readings implementations
    already used;
  - fix a contradiction;
  - add conformance tests.

  A revision does not change the result of any operation on a valid store
  whose result was already fully determined by the previous revision.
- **What needs a new version:** anything else, such as new message fields,
  new pins, new source kinds, or changed layout rules. This is necessary
  because readers skip unknown protobuf fields ([§4.1](#41-encoding)): a
  field added within a version would be silently ignored by older readers.
  For a field like a pin, that would turn "fail closed" into "fail open".
- **Writers** SHOULD write the lowest format version that can express the
  store, so that the most readers can read it.
- **Conformance:** a conformance suite states the format version and the
  spec revision it tests.
- **The recorded revision:** every store records, in its source table
  ([§5](#5-source-table)), the revision of this document its writer followed
  (the whole number: 9 for revision 9, 9.1 or 9.2). Readers MUST NOT reject a
  store because of its recorded revision, or because it has none; they MAY
  report it. It tells apart stores written under different revisions of a
  format version, which matters while that version is provisional and
  revisions may still change results.

**Releases.** Until vzip's first release, every store is format version 0
and records its revision as above. The release rule is:

1. At the first release, the text of this document becomes format version
   1, and the commit is tagged `vzip-format-v1` in the specification's
   repository.
2. After that, every **breaking change** (any change that
   [§1.3](#13-versioning) says needs a new version, and any change to the
   result of an operation on a valid store) increments the format version
   recorded by the binding (the magic of a vzip archive,
   [§10.4](#104-end-of-central-directory-record-and-archive-comment)), and
   the commit that makes it is tagged `vzip-format-v<N>`. Changes that are
   not breaking are revisions of the current version, and need no tag.
3. Readers MUST reject a store whose format version they don't implement
   ([§10.4](#104-end-of-central-directory-record-and-archive-comment)),
   which is what keeps every breaking change from being read with the wrong
   meaning.

**Status.** A format version is in one of three states:

| status | meaning |
|---|---|
| draft | Anything may change. |
| **provisional** | The format is believed complete, and implementers may rely on it. Revisions may still change it in response to feedback from outside the project. Every such change is listed, with its reason, in the [changelog](history/archive/REVISIONS.md). An incompatible change (one that changes the result of an operation on a store that was valid before) must also be announced there as incompatible, with the conformance suite updated to match. Incompatible changes are avoided wherever a compatible one would do. |
| final | No incompatible change is ever made. Changes of that kind become the next format version. |

**Format version 0 is provisional** as of revision 8. Its draft history,
revisions 1–7, was driven by seven rounds of independent implementations
(see the changelog). It becomes final once it has gone through a period of
outside review with no incompatible change needed.

Revision 11 made an **incompatible** change to the ZIP binding in response
to outside review: the ZIP64 end records are present in every archive
([§10.2](#102-zip64)), so an archive written to an earlier revision without
them is no longer valid. It also allowed entries of 4 GiB or more
([§10.1](#101-structure) rule 7), which changes nothing for an archive that
was valid before.

**Feedback** — ambiguities, implementation reports, test cases, objections —
is welcome as issues or pull requests on the specification's repository. A
report is most useful when it names the section, the input, and the
behavior each reading would produce.

### 1.4 Prior work (informative)

vzip is a binary encoding for a model of references that other projects
established. This section credits them; nothing in it is normative.

- **kerchunk.** Virtual Zarr originates with
  [kerchunk](https://github.com/fsspec/kerchunk) (Martin Durant and
  contributors, fsspec). kerchunk records where the chunks of HDF5, netCDF,
  GRIB, TIFF and FITS files live, and fsspec's `ReferenceFileSystem` serves
  them so that Zarr can read the files in place. vzip's data model
  ([§2](#2-data-model)) is the one defined by kerchunk's
  [reference specification](https://fsspec.github.io/kerchunk/spec.html):

  | kerchunk reference specification | vzip |
  |---|---|
  | a string value: inline data | a `bytes` value ([§2](#2-data-model)) |
  | `[url, offset, length]` | a reference with one source range ([§4.2](#42-range)) |
  | a `base64:` string: inline binary data | a literal range ([§4.2](#42-range)), or a `data` source ([§5](#5-source-table)) |
  | `templates` shared by many URLs | the source table ([§5](#5-source-table)) |

  kerchunk's version 1 specification anticipated "future possible binary
  storage" of references. This document specifies one. kerchunk's Parquet
  layout showed how compactly references can be stored.
- **VirtualiZarr.**
  [VirtualiZarr](https://github.com/zarr-developers/VirtualiZarr) (started
  by Tom Nicholas, a zarr-developers project, grown out of kerchunk) recast
  virtual Zarr as chunk manifests: a path, offset and length for every chunk
  of a Zarr array. Those manifests are what a store's references record,
  and the reference implementation's converters take VirtualiZarr datasets
  as input. The need for a Zarr-native on-disk manifest format, raised in
  [zarr-specs#287](https://github.com/zarr-developers/zarr-specs/issues/287),
  motivated this work.
- **Icechunk.** Source pins ([§5.1](#51-pins)) adapt
  [Icechunk](https://icechunk.io/)'s checks of virtual chunks against the
  ETag or modification time of the object they reference.

What vzip contributes is a reference value type with a binary encoding that
does not depend on the backend; its binding to a single ZIP file; and a
specification with a conformance suite that is independent of any one
language or library.

# Part 1. Reference values

This part does not depend on any backend, and uses no ZIP terms. Where a
rule needs the backend, it says what the binding defines
([§3.1](#31-what-a-binding-defines)).

## 2. Data model

A store defines a partial function from **keys** to **values**:

- A **key** is a non-empty Unicode string ([§2.1](#21-keys)).
- A **value** is a finite byte sequence (possibly empty).

Each key present in the store has a **kind**:

| kind        | value                                                         |
|-------------|---------------------------------------------------------------|
| `bytes`     | the bytes the store holds under the key                       |
| `reference` | the bytes described by the key's reference payload ([§4.4](#44-reference-payloads)) |

A key that is not present has kind `missing` and no value. Asking for a
missing key is not an error.

Both kinds share one key space. The binding says how a reader tells them
apart ([§3.1](#31-what-a-binding-defines)).

A store also has exactly one **source table** ([§5](#5-source-table)), the
list of objects that references read. It is not a value of the mapping.

### 2.1 Keys

A key is stored as its UTF-8 encoding, which the binding maps to the
backend's names ([§3.1](#31-what-a-binding-defines)). A key MUST NOT be
empty and MUST be valid UTF-8. Keys are otherwise unrestricted; for example,
`a/../b` and `x/` are ordinary distinct keys
([§8](#8-security-considerations)).

Keys are compared and sorted as byte strings (UTF-8 order), never as
decoded text. In particular:

- A leading U+FEFF (byte order mark) is part of the key. Decoders that strip
  it must be told not to.
- UTF-8 order is code point order, which differs from UTF-16 code unit order
  for characters above U+FFFF. For example, `"\u{1F600}"` (😀) sorts after
  `"\u{FF5E}"` (～) in UTF-8 order but before it in UTF-16 order. Several
  languages' default string sort uses UTF-16 order.

## 3. Bindings

A **binding** says how a backend holds a store.

### 3.1 What a binding defines

A binding defines exactly three things:

1. **Marking:** how a value is marked as a reference rather than bytes,
   where its reference payload is kept, and whether that payload is a
   `Range` or a `Concat` ([§4.4](#44-reference-payloads)).
2. **The source table:** where and how the store's source table
   ([§5](#5-source-table)) is stored, and how a reader finds it, together
   with any auxiliary data of the binding's own, such as ZIP's page index.
   This is also where the binding identifies the store as vzip and records
   its format version ([§1.3](#13-versioning)).
3. **Names:** how keys map to the backend's names, and back, including any
   names the binding reserves for itself (for its source table or its
   auxiliary data) and how it keeps them out of the mapping.

Everything else comes from Part 1: the messages, the contents of the source
table and its pins, how references resolve, the operations, the error
classes and their order, the reader policy, and the writer's checks.

A binding may add causes to the error classes of [§6.4](#64-errors) for what
its backend can get wrong, and limits that follow from its encoding, such as
a maximum payload size. It MUST NOT add an error class, change what an
operation returns for a valid store, or relax a requirement of Part 1.

ZIP is the one normative binding. [§9](#9-what-the-zip-binding-defines)
gives its three answers, and the rest of Part 2 what the ZIP container needs.

### 3.2 Other backends (informative)

Nothing in Part 1 needs ZIP. This document specifies no other binding, but
other backends could hold a store:

- **Object stores** (S3 and similar): each key is an object. A reference is
  an object whose `Content-Type` is a vzip media type, or whose user metadata
  holds a marker, with the payload in its body or in that metadata.
- **File systems:** each key is a file. A reference is a file with an
  extended attribute (xattr) that holds the payload, or marks the file's
  contents as one.
- **Databases:** a table of `(key, kind, value)` rows, where the `kind`
  column says whether `value` holds bytes or a reference payload.
- **HTTP:** a server returns each value with a response header that says
  whether its body is bytes or a reference payload.

In each case the backend needs only a way to tell a bytes value from a
reference value. A key-value backend could keep the source table under the
ZIP binding's name for it, `__vz__/sources` ([§11.4](#114-hidden-entries)),
reserved in the same way; that is a suggestion, not a rule. A binding for
one of them would also have to settle what ZIP settles by being a single
file that is written once:

- **Atomicity of the source table.** A vzip archive's source table always
  matches its references. In a backend whose values change one at a time,
  a reference and the source table it numbers into can change separately. A
  binding must say how a reader sees a consistent pair, for example by
  never renumbering or removing sources, by writing new sources before the
  references that use them, or by versioning the table.
- **Listing.** `list` returns keys in UTF-8 order
  ([§6.2](#62-operations)). A binding for a backend that lists in another
  order, or not at all, must say how a reader produces that result, and
  what it costs.
- **The base URI.** Relative `url` sources resolve against the store's base
  URI ([§5](#5-source-table)). A store that is not one file must say what
  that URI is, for example the prefix of its objects.
- **Names.** Which keys the backend can name (length limits, reserved names,
  case folding), and how the names the binding reserves for itself stay out
  of the mapping.
- **Recognition.** Where a reader finds the format version before it reads
  anything else, and how it tells a store from unrelated data.
- **Payload size.** How large a payload the marking can hold (user metadata
  and extended attributes are small), which is the binding's payload limit
  ([§4.4](#44-reference-payloads)).
- **Errors and the raw view.** Which of the backend's failures are archive,
  entry and body errors ([§6.4](#64-errors)), and what `raw` returns for a
  reference ([§6.2](#62-operations)).

## 4. Messages

### 4.1 Encoding

Messages use the Protocol Buffers binary wire format with proto3 semantics.
Appendix A gives the schema, which is normative together with these rules.

Decoding:

- A decoder MUST skip fields whose field number is not in the schema, for
  wire types VARINT (0), I64 (1), LEN (2) and I32 (5).
- The following make the message malformed:
  - wire types 3, 4, 6 and 7;
  - field number 0, or above 2^29 − 1;
  - truncated input, or a LEN field extending past the end of its enclosing
    message;
  - a varint longer than 10 bytes, or whose value exceeds 2^64 − 1;
  - a known field whose wire type differs from the one its type uses (VARINT
    for integers, LEN for strings, bytes and messages);
  - a `uint32` field whose value exceeds 2^32 − 1;
  - a string that is not valid UTF-8.
- Fields may appear in any order. If a non-repeated field appears more than
  once, the last occurrence wins, but every occurrence must be valid: an
  invalid earlier occurrence (for example a `uint32` overflow or a string
  that is not UTF-8) makes the message malformed even though it is
  overridden. "Valid UTF-8" is as defined in RFC 3629: overlong forms and
  encoded surrogates are invalid. If more than one member of a `oneof`
  appears, the last one on the wire wins. Non-minimal varints are accepted.
  (The schema has no singular message fields, so protobuf's rule of merging
  repeated occurrences of an embedded message never applies.) Field 2 of
  `Range` is reserved: it is skipped like any unknown field.

Implementations built on a protobuf library get most of these rules for
free, but typically must add checks for three of them: groups (wire types 3
and 4) on unknown fields, `uint32` overflow (libraries truncate), and UTF-8
validity of strings (some runtimes don't check).

Encoding:

- Encoders MUST emit fields in field-number order, use minimal varints, and
  not emit a scalar field equal to its default value (0 or empty), with two
  exceptions: a set `oneof` member and a set `optional` field are always
  emitted, even when empty. With these rules each message has exactly one
  encoding, so implementations can compare encodings byte for byte.
- Every element of a repeated message field is emitted, including one whose
  own encoding is empty. For example, a `Concat` part that is a `Range` with
  all fields 0 is written `0a 00`.
- An `int64` is encoded as the 64-bit two's complement of its value, as a
  varint, so a negative value always takes 10 bytes. For example, −1 is
  `ff ff ff ff ff ff ff ff ff 01`.

### 4.2 Range

```proto
message Range {
  uint32 source = 1;
  reserved 2;
  uint64 offset = 3;
  uint64 length = 4;
  optional bytes data = 5;
  optional uint32 crc32c = 6;
}
```

A Range describes a byte sequence of a known **size**:

- **Literal range**: field 5 (`data`) is present, even with zero length. The
  bytes are `data`, and the size is `len(data)`. In a literal range,
  `source`, `offset` and `length` MUST all have the value 0; otherwise the
  payload is malformed. Only the values are checked: a field explicitly
  encoded as 0 is accepted, like any non-canonical encoding
  ([§4.1](#41-encoding)).
- **Source range**: field 5 is absent. The bytes are
  `[offset, offset + length)` of the **source value** of source number
  `source` of the source table ([§5](#5-source-table)), and the size is `length`.
  `source` MUST be less than the number of sources, and `offset + length`
  MUST NOT exceed 2^64 − 1; otherwise the payload is malformed.

The size is known without I/O.

**Checksum.** A source range MAY carry field 6, `crc32c`: the CRC-32C of its
bytes, that is of bytes `[offset, offset + length)` of the source value. A
literal range with field 6 present is malformed. CRC-32C is the CRC of
RFC 3720 §B.4 (iSCSI): the Castagnoli polynomial 0x1EDC6F41, reflected
input and output, initial value and final XOR 0xFFFFFFFF. Its check value,
for the nine ASCII bytes `123456789`, is 0xE3069283; for the empty sequence
it is 0. Readers check it after they read the range
([§6.3](#63-resolving-a-reference)), so it detects a changed source wherever
the range can be read, including where pins cannot be checked because the
reader cannot see the response's headers (a browser reading a cross-origin
object, [§5.2](#52-reading-over-http)). It covers only the bytes of the
range: a change elsewhere in the object goes unnoticed.

### 4.3 Concat

```proto
message Concat {
  repeated Range parts = 1;
}
```

A Concat describes the concatenation of its parts, in order. Its size is the
sum of the parts' sizes, which MUST NOT exceed 2^64 − 1. A Concat with no
parts describes the empty value.

### 4.4 Reference payloads

The **reference payload** of a reference is one encoded message:

- a `Range` ([§4.2](#42-range)), describing a value made of that one range;
  or
- a `Concat` ([§4.3](#43-concat)), describing a value made of zero or more
  ranges.

The binding records which of the two a payload is
([§3.1](#31-what-a-binding-defines)). Writers MUST use a `Range` when a
reference has exactly one range, and a `Concat` otherwise. Readers MUST
accept a `Concat` with any number of parts.

A payload is **malformed** if it breaks a rule of [§4.1](#41-encoding),
[§4.2](#42-range) or [§4.3](#43-concat). A binding may limit the size of a
payload ([§11.3](#113-reference-entries) for ZIP); a payload over that limit
is malformed too.

### 4.5 Where bytes that no source holds go (informative)

Most of a value usually comes from source ranges, but some values also need
bytes that exist in no source: a header rewritten for one tile, so that the
tile decodes on its own; a shard index that a format never wrote; a decoding
header that a file stores once and every tile needs. A reference can carry
such bytes three ways, and each fits a different case:

| bytes that are | put them in | because |
|---|---|---|
| used by one value only, such as one tile's rewritten header or one shard's index | a literal range ([§4.2](#42-range)), in that value's reference | they are read with the reference that needs them, and only when it is read |
| shared by many values, such as the JPEG tables every tile of a slide needs | a `data` source ([§5](#5-source-table)), named by a source range of each reference | they are stored once, however many references use them |
| a value of the store in their own right | a `bytes` value, named by a `key` source ([§5](#5-source-table)) | a reader can get them by their key, and references reuse them without a copy |

The first two trade storage against what a reader reads at open. The source
table is read in full when a store is opened ([§6.1](#61-opening)), while a
reference is read only when its key is. A literal range used by one value
costs nothing until that value is read; the same bytes as a `data` source
would be read by every reader at open, so a store with one such header per
chunk would load all of them before reading any. A `data` source shared by
many values is read once instead of once per reference. Literal ranges also
represent the inline values of other reference formats, such as kerchunk's
`base64:` strings ([§1.4](#14-prior-work-informative)).

## 5. Source table

A store has exactly one source table. The `source` field of every source
range ([§4.2](#42-range)) is an index into it. The binding says where the
table is stored ([§3.1](#31-what-a-binding-defines)); a vzip archive keeps it
in the entry `__vz__/sources` ([§11.4](#114-hidden-entries)). The table is
a `SourceTable`:

```proto
message Source {
  oneof kind {
    string url  = 1;
    string key  = 2;
    bytes  data = 3;
  }
  // Pins (§5.1); allowed only on `url` sources.
  optional uint64 size               = 4;
  optional string etag               = 5;
  optional int64  modified_not_after = 6;
}

message SourceTable {
  repeated Source sources = 1;
  optional uint32 revision = 2;
}
```

`revision` is the revision of this document the writer followed
([§1.3](#13-versioning)). Writers MUST write it; readers only report it.

Sources are numbered from 0 in wire order. When the table is read
([§6.1](#61-opening)), any of the following is an archive error:

- the table is malformed;
- a Source has no `kind` member;
- a `url` or a `key` is empty;
- a pin appears on a `key` or `data` source;
- an `etag` pin is not a strong entity tag ([§5.1](#51-pins)).

The **source value** of each kind:

- **`url`**: the bytes of an external object.
  - `url` MUST match the `URI-reference` rule of RFC 3986 §4.1 exactly. It
    is therefore ASCII only, with every other byte percent-encoded (non-ASCII
    characters as their UTF-8 bytes). For example, a file named `a b.bin` is
    referenced as `a%20b.bin`, and `é.bin` as `%C3%A9.bin`.
  - It is resolved against the store's **base URI** using the strict
    algorithm of RFC 3986 §5.2.2: a reference with a scheme is used as it
    is, even if the scheme matches the base's.
  - When a store is opened from a local path, its base URI is built in
    two steps:
    - Make the path absolute and normalize it. A relative path is joined to
      the process's current directory as the operating system reports it
      (`getcwd`, not `$PWD`). Empty segments (`//`) and `.` segments are
      removed, `..` segments are removed lexically together with the segment
      before them, and symbolic links are not resolved.
    - Form `file://` followed by the path's bytes: its UTF-8 bytes, or the
      raw bytes on systems where paths are not text. Each byte that is not an
      unreserved character, a sub-delim, `:`, `@` or `/` is written as `%`
      and two uppercase hex digits. For example, `/data/my file.vzip` becomes
      `file:///data/my%20file.vzip`. A `..` segment at the root is removed,
      so `/../a` becomes `/a`.
  - When opened from a URL, the base URI is that URL.
  - A `file:` URI maps to a local path as follows:
    - the scheme and the host `localhost` are case-insensitive;
    - its authority MUST be absent (`file:/x`), empty (`file:///x`) or
      exactly `localhost` (in any case): no userinfo, no port, and no
      percent-encoding;
    - its path MUST be absolute (start with `/`);
    - it MUST have no query component, not even an empty one (`?`);
    - any fragment is ignored;
    - its path is percent-decoded to bytes, which MUST NOT contain `/` from
      a decoded `%2F` or a NUL byte. The bytes are used as the local path
      as they are; they need not be UTF-8;
    - after decoding, no path segment may be `.` or `..`. Resolution has
      already removed literal dot segments, so this only rejects encoded ones
      such as `%2E%2E`, which would otherwise escape the directory after
      decoding.

    This mapping is for POSIX paths; other systems follow RFC 8089.

  Readers MUST support `file:`, SHOULD support `http:` and `https:` (byte
  ranges via HTTP Range requests), and MAY support other schemes. It is a
  resolution error if the reference is not a valid URI reference, uses an
  unsupported scheme, or violates the `file:` rules, if the reader's policy
  refuses the URL ([§6.7](#67-reader-policy)), or if the object cannot be
  read ([§5.2](#52-reading-over-http) for HTTP). Readers check URL syntax
  only when they resolve a range of the source, not at open; writers check
  it when writing ([§7](#7-writing)). A reference with only a fragment (such
  as `#x`) resolves to the store's own location (for a vzip archive, the
  archive file); that is valid but rarely useful.
- **`key`**: the value of the `bytes` key of this store with that name. A
  binding may also let it name bytes that the binding keeps out of the
  mapping ([§11.4](#114-hidden-entries) for ZIP). It is a resolution error
  if the key is missing, is a reference, or has an entry error or a body
  error ([§6.4](#64-errors)). References therefore never chain.
- **`data`**: the bytes of `data`. Writers SHOULD use this for small byte
  strings shared by many ranges, such as a decoding header. Bytes used by a
  single range belong in a literal range instead.

Readers MUST NOT resolve sources except as
[§6.3](#63-resolving-a-reference) requires. A store whose source table names
an unreachable URL is fully readable, except for the ranges that use that
URL.

### 5.1 Pins

A reference names bytes the store does not control. If the object behind a
`url` is replaced, for example a forecast file re-published under the same
name, the reference silently returns bytes from the new object. HDF5 and
netCDF files regenerated with the same layout decode cleanly to the wrong
values. **Pins** let a writer record what it saw, so that a reader detects
the change.

A `url` source may carry any combination of three pins. Each one asserts
something about the object at resolution time:

| pin | assertion |
|---|---|
| `size` | the object's total size in bytes equals `size` |
| `etag` | the object's current entity tag equals `etag`, by strong comparison (RFC 9110 §8.8.3.2) |
| `modified_not_after` | the object's last modification time, in whole seconds since the Unix epoch (UTC, rounded down, i.e. towards negative infinity), is less than or equal to `modified_not_after` |

A pin is present if its field is present on the wire, even with the value 0
(the fields are `optional`). Implementations built on protobuf libraries
must use the library's presence tracking for them, not compare with 0.

`etag` holds a strong entity tag exactly as it appears in an HTTP `ETag`
header, including its double quotes, for example `"abc123"`. It MUST match
`DQUOTE *etagc DQUOTE`, where `etagc` is any ASCII character from `!` to `~`
except `"` (RFC 9110 §8.8.3, without `obs-text`). Anything else, including a
weak tag (`W/"..."`), is an archive error ([§5](#5-source-table)).

Before returning any byte read from a pinned source, a reader MUST check
every pin on that source. A pin that fails, or that the reader cannot check
for that scheme, is a resolution error. A reader MUST NOT skip a pin it
does not understand: pins fail closed. The one exception is a pin that the
response's headers would check but that the reader cannot see, which an
application may allow the reader to skip ([§6.7](#67-reader-policy)).
Checks, by scheme:

| pin | `http:`/`https:` | `file:` |
|---|---|---|
| `size` | the object's size, from the response ([§5.2](#52-reading-over-http)) | the file's size |
| `etag` | send `If-Match: <etag>` with the range request; a 412 response fails the pin | cannot be checked: resolution error |
| `modified_not_after` | send `If-Unmodified-Since: <HTTP-date of the pin>` with the range request; a 412 response fails the pin | the file's modification time, rounded down to whole seconds (as above) |

Over HTTP the checks ride on the range requests the reader makes anyway, so
pins cost no extra requests. A reader that checks a pin once per source
SHOULD do so per opening of the store, not cache the result across opens.

Writers SHOULD:

- pin `size` on every `url` source whose size they know. It costs nothing
  to check over HTTP (the size is in every range response) and catches
  appends and most rewrites. Modification times have one-second
  resolution, so a rewrite within the same second as `modified_not_after` is
  not detected, while `size` may catch it;
- pin `etag` when they saw a strong entity tag for the object, from a
  response that also gave them the bytes they describe;
- take `modified_not_after` from the object itself (its `Last-Modified`
  value, or its file modification time), not from the writer's clock. A
  value from the writer's clock fails whenever the server's clock or the
  object's timestamp is ahead of it;
- add a `crc32c` ([§4.2](#42-range)) to ranges whose bytes they read anyway,
  or when asked to: it is the only check that does not depend on headers;
- pin only `size` in stores meant to be copied together with their data
  ([§5](#5-source-table), relative URLs). Copying an object changes its
  modification time and may change its entity tag.

### 5.2 Reading over HTTP

This section applies to readers that support `http:` and `https:`.

- **Requests:** a read of bytes `[a, b)` of an HTTP object is a GET with
  `Range: bytes=a-(b−1)` and `Accept-Encoding: identity`. A reader SHOULD
  combine reads of nearby ranges of the same object into one request
  ([§6.3](#63-resolving-a-reference)). It MUST NOT make any other request
  to resolve a range or check its pins: no HEAD, and no request for the
  whole object.
- **Pins** add `If-Match` and `If-Unmodified-Since` headers to those same
  requests ([§5.1](#51-pins)). Servers and proxies may ignore those headers,
  so a reader also checks every successful (200 or 206) response itself:
  - an `etag` pin requires the response's `ETag` header to equal the pin
    (strong comparison);
  - a `modified_not_after` pin requires its `Last-Modified` header to be an
    IMF-fixdate (RFC 9110 §5.6.7, e.g. `Sun, 06 Nov 1994 08:49:37 GMT`) no
    later than the pin.

    The two obsolete HTTP-date formats are not accepted. RFC 850 dates
    resolve two-digit years against the reader's clock, so accepting them
    would make results depend on when the reader runs. The day name must be
    the correct one for the date. Day and month names are case-sensitive,
    as written in RFC 9110. A second of `60` (a leap second) is accepted and
    counts as the following second.

  A missing or unparseable header means the pin cannot be checked, which is
  a resolution error.
- **Responses:**

  | response | meaning |
  |---|---|
  | 206 with one `Content-Range: bytes a-z/total` | the requested bytes. `total` is the object's size. It is a resolution error if the returned range is not the one requested, if `z` is not less than `total`, or if the body's length is not `z − a + 1`. |
  | 206 with `Content-Range: bytes a-z/*` | the requested bytes, with the same range and body-length checks; the size is unknown, so a `size` pin cannot be checked (resolution error) |
  | 200 | the server ignored `Range` and sent the whole object. The reader takes the requested bytes from the body; the size is the body's length. Readers MUST accept this. If the body is shorter than the end of the requested range, it is a resolution error. |
  | 412 | a pin failed: resolution error |
  | 416 | the object is shorter than the requested range: resolution error |
  | 206 without exactly one valid `Content-Range`, including `multipart/byteranges` | resolution error |
  | any other status | resolution error |

  Header rules for the final 200 or 206 response:
  - **`Content-Range`** follows RFC 9110 §14.4: the unit `bytes` is
    case-insensitive and is followed by exactly one space.
  - **`Content-Encoding`:** if any value, trimmed and compared
    case-insensitively, is anything but `identity`, it is a resolution error.
    Repeated `Content-Encoding` fields combine into one list (RFC 9110
    §5.3), so two `identity` fields are the list `identity, identity`,
    which is also an error. Redirects, 412 and 416 responses are not
    checked for `Content-Encoding`.
  - **Repeated fields:** more than one `Content-Range`, `ETag` or
    `Last-Modified` field is a resolution error. So is more than one
    `Location` field on a redirect.
  - **Chunked transfer coding** is part of HTTP/1.1 framing, not of the
    representation, and readers MUST accept it.

- **Redirects:** readers MUST follow redirects:
  - statuses 301, 302, 303, 307 and 308. A reader follows at most 5
    redirects, so it makes at most 6 requests for one range; a redirect
    received in reply to the 6th request is a resolution error;
  - the `Location` header is resolved against the URL that was requested
    (RFC 3986 §5.2.2); a redirect without a `Location` header, or with one
    that is not a valid URI reference, is a resolution error;
  - only to `http:` or `https:`; a redirect to any other scheme is a
    resolution error;
  - only to a URL the reader's policy allows ([§6.7](#67-reader-policy)); a
    redirect to any other URL is a resolution error, and the reader SHOULD
    detect it before it sends the request. A reader that cannot see a
    redirect before it is followed (a browser's `fetch`) checks the final
    URL instead;
  - each hop re-sends `Range`, `Accept-Encoding` and the pin headers. Pins
    and the size apply to the final response.
- **URLs:** an `http:` or `https:` URL is a resolution error if it has
  userinfo (`user@`), an empty or absent host (`http:/x` and `http:///x`
  included), or a port above 65535. An empty port (`host:`) means the
  default port. These rules apply to every URL requested, including
  redirect targets.
- **Memory:** a 200 response delivers the whole object. A reader MAY refuse
  to read a body larger than a documented resource limit
  ([§8](#8-security-considerations)); that is a request error.
- **Timestamps:** `If-Unmodified-Since` carries `modified_not_after` as an
  IMF-fixdate (RFC 9110 §5.6.7). A value outside the years 1–9999 cannot be
  sent, so the pin cannot be checked: resolution error.

## 6. Reading

### 6.1 Opening

Opening a store reads and checks the following. Any failure is an
**archive error**, and opening fails:

- what the binding checks at open: that the store is one, of a format
  version the reader implements, and that its source table and auxiliary
  data can be found and read ([§13.1](#131-opening) for ZIP);
- the source table, which must be valid ([§5](#5-source-table));
- the resource limits that apply at open ([§6.7](#67-reader-policy),
  [§8](#8-security-considerations)). A resource limit reached while
  opening, for example while reading the source table, or a source table
  with more sources than the reader allows, is an archive error.

A reader MAY read more, but MUST NOT report an error at open that this
section and its binding do not list.

### 6.2 Operations

A conforming reader provides the following operations on an opened store:

- **classify(key)** → `bytes` | `reference` | `missing`. A name the binding
  keeps out of the mapping ([§3.1](#31-what-a-binding-defines)) classifies
  as `missing`. It looks only at how the key is marked, so a reference with
  a malformed payload still classifies as `reference`.
- **get(key, request)** → a byte sequence, *missing*, or an error. A name
  the binding keeps out of the mapping is *missing*. `request` is one of:
  - *whole*: the whole value;
  - *range(start, end)* with `start <= end`: bytes `[min(start, n), min(end, n))`
    of the value, where `n` is the value's size;
  - *offset(start)*: bytes `[min(start, n), n)`;
  - *suffix(count)*: bytes `[max(n − count, 0), n)`.

  `start > end` is a request error, whether or not the key is present.
- **list(prefix)** → every present key of the mapping that starts with
  `prefix` (compared on UTF-8 bytes), sorted in UTF-8 order. The empty
  prefix lists all keys. Keys with entry errors are listed. The binding says
  how a reader finds the keys, and which of its failures make `list` fail
  ([§13.2](#132-operations) for ZIP).
- **raw(key)** → what the backend holds under the key's name, without
  interpreting references, or *missing*. This view also shows the names the
  binding reserves. The binding says what it yields for those and for a
  reference. It is the view of a client of the backend that knows nothing
  about vzip ([§13.2](#132-operations) for ZIP).

### 6.3 Resolving a reference

`get` on a reference proceeds as follows:

1. Decode the payload. If it is malformed
   ([§4.4](#44-reference-payloads)), the `get` fails, whatever the request.
2. Compute the requested window `[a, b)` of the value
   ([§6.2](#62-operations)).
3. For each range whose bytes overlap `[a, b)` in a non-empty interval, take
   those bytes from the range:
   - A literal range needs no I/O.
   - A source range reads bytes `[offset + i, offset + j)` of its source value,
     where `[i, j)` is the overlap relative to the range's start. The read is
     a resolution error if the source cannot be resolved
     ([§5](#5-source-table)), if the reader's policy refuses it
     ([§6.7](#67-reader-policy)), if a pin on the source fails or cannot be
     checked ([§5.1](#51-pins)), or if the source value is shorter than
     `offset + j`.
   - A source range with a `crc32c` ([§4.2](#42-range)) is read whole,
     `[offset, offset + length)`, whatever part of it the window needs. If
     the CRC-32C of the bytes read differs from `crc32c`, it is a
     resolution error. The reader returns the window's part only after the
     check.
4. Ranges that do not overlap the window are not resolved and cause no
   error, even if their source is missing or too short. This includes every
   zero-length range.

**Coalescing.** A reader SHOULD combine reads of step 3 that fall in the
same source value and lie close together into one read that also covers the
bytes between them, and take each range's bytes from that read. Otherwise a
reference made of many short ranges of one object, such as one range per row
of an image whose rows are padded, costs one request per range. The bytes
between the ranges are read but never returned. How close counts as "close"
is up to the reader; the implementations in the specification's repository
combine reads at most 64 KiB apart.

Coalescing never changes the result of a `get`: a combined read ends at the
end of one of the ranges it covers, so it fails only where that range's own
read would. Apart from such gaps, readers SHOULD fetch only the bytes in
step 3. A reader MUST NOT return bytes for a request it could not resolve
completely.

### 6.4 Errors

There are six classes of error. Each says which operations it makes fail.

| class | causes | effect |
|---|---|---|
| **archive error** | everything [§6.1](#61-opening) checks | opening fails |
| **entry error** | the reader cannot tell the key's kind, or the key's own record in the backend breaks a rule of the binding ([§13.3](#133-errors) for ZIP) | classify, get and raw fail for that key; other keys are unaffected |
| **body error** | the bytes the store holds under the key cannot be read as the binding requires ([§13.3](#133-errors) for ZIP). This applies to `bytes` keys for get and raw, and to references for raw. | get and raw of that key fail; a range of a `key` source naming it fails with a resolution error |
| **payload error** | the reference payload is malformed ([§4.4](#44-reference-payloads)) | get fails for that key |
| **resolution error** | [§5](#5-source-table), [§5.1](#51-pins), [§6.3](#63-resolving-a-reference) step 3, a URL the reader's policy refuses ([§6.7](#67-reader-policy)) | get fails for the requests that need the range |
| **request error** | `start > end`; a request exceeding a resource limit the reader documents ([§6.7](#67-reader-policy), [§8](#8-security-considerations)), such as the requests one get may make | that operation fails |

The names are the same in every binding, and are the ones readers report
([§6.5](#65-error-reporting)). They come from the ZIP binding, in which the
store is an archive and a key's record is its entry. A binding adds causes
to the archive, entry and body errors, and may add causes to the payload
error ([§3.1](#31-what-a-binding-defines)).

Resource limits are implementation-defined. Conformance suites do not test
them, and the classes of errors they cause may differ between readers.

Each operation checks in this order, and reports the first failure. A
resource limit is the one exception: it may be reported at whatever step the
reader reaches it.

1. the request (request error);
2. whether the binding keeps the key's name out of the mapping
   ([§3.1](#31-what-a-binding-defines)): such a key is *missing* for
   classify and get;
3. the lookup of the key's record in the backend, and that record's entry
   error;
4. for get of a `bytes` key, or raw: the bytes the store holds (body error).
   The binding says how much of them is read and checked
   ([§13.3](#133-errors) for ZIP);
5. for get of a reference: the payload (payload error), then the ranges, in
   order (resolution errors). Payload checks cover every range, including
   zero-length ranges and ranges outside the window: a `source` index out of
   bounds is a payload error even when that range would not be resolved.

### 6.5 Error reporting

A failed operation returns no bytes. A missing key is not an error. Readers
SHOULD report the class of an error along with its message.

### 6.6 Violations readers need not detect

For stores that break these writer requirements, reader results are
unspecified:

- [§5](#5-source-table): whether the source table records the revision
  ([§1.3](#13-versioning)).

A binding lists its own ([§13.4](#134-violations-readers-need-not-detect)
for ZIP).

### 6.7 Reader policy

A store names the objects its references read, and a reader resolves
them with whatever access it has: local files, private networks, ambient
credentials. A store from an untrusted place could therefore make a
reader read, and return, data its user never meant to expose. Readers MUST
apply a **policy** to every `url` source they resolve, and to every URL they
request on its behalf (redirect targets included). The application chooses
the policy; this section says what it contains and what readers do with
it. A URL the policy refuses is a resolution error for the ranges that need
it; the other limits are resource limits ([§6.4](#64-errors)).

1. **Schemes.** A store is **local** if its base URI
   ([§5](#5-source-table)) has the scheme `file`, that is, if it was opened
   from a local path, and **remote** otherwise. Schemes are compared without
   regard to case. By default a reader allows:
   - `http` and `https` URLs, for every store;
   - `file` URLs, for a local store only.

   Every other scheme, such as `s3`, `gs` or `data`, is refused unless the
   application allows that scheme. An application MAY also allow `file`
   URLs for a remote store, but a reader MUST NOT let the setting that
   allows other schemes do it: it is a separate setting, whose name says
   that it is unsafe. So by default a remote store cannot name
   `file:///etc/passwd`, and no store can name an `s3:` bucket the reader
   has credentials for.
2. **Prefixes.** An application MAY give a list of URL **prefixes**. When it
   does, the list replaces rule 1: a URL is allowed if and only if it
   matches one of the prefixes. A URL `u` matches a prefix `p` if `u` starts
   with `p` (compared as strings, exactly) and either `p` ends with `/`, or
   `u` equals `p`, or the character of `u` after `p` is `/`, `?` or `#`. So
   `https://data.example/a` matches `https://data.example/a/x.bin` but not
   `https://data.example/ab`, and `https://data.example` does not match
   `https://data.example.evil.test/`. A URL whose path holds `%2E`, `%2F`
   or `%5C` (in either case) matches no prefix, since a server may decode
   them into a dot segment or a separator that the comparison did not see.
   Prefixes should be written as resolution produces URLs
   ([§5](#5-source-table)), with a lowercase scheme and host; a URL that is
   spelled differently does not match, which fails closed. A prefix list
   does not lift rule 3.
3. **Hosts.** An IP address belongs to the first of these classes that
   contains it:

   | class | addresses |
   |---|---|
   | loopback | `127.0.0.0/8`, `::1/128` |
   | private | `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `fc00::/7` |
   | link-local | `169.254.0.0/16`, `fe80::/10` (this includes cloud metadata services) |
   | special | `0.0.0.0/8`, `100.64.0.0/10`, `192.0.0.0/24`, `192.0.2.0/24`, `198.18.0.0/15`, `198.51.100.0/24`, `203.0.113.0/24`, `224.0.0.0/3`; `::/8`, `64:ff9b:1::/48`, `100::/63`, `2001::/23`, `2001:db8::/32`, `3fff::/20`, `5f00::/16`, `fec0::/10`, `ff00::/8` |
   | public | every other address |

   An IPv6 address that embeds an IPv4 address belongs to that address's
   class: an IPv4-mapped address (`::ffff:0:0/96`) and a NAT64 address
   (`64:ff9b::/96`) embed their last 32 bits, and a 6to4 address
   (`2002::/16`) the 32 bits after its first 16. So `::ffff:127.0.0.1` is
   loopback. The special class holds the unspecified, shared, reserved,
   documentation, benchmarking, multicast and broadcast ranges. With the
   embedding rule, the non-public classes include every range that the
   IANA special-purpose address registries mark as not globally
   reachable.

   A reader MUST NOT send a request, redirects included, to an address of
   a non-public class, whatever the store's own location (a local store,
   or one on such a host, is no exception), unless the application allows
   **private hosts**. That permission is a setting whose name says that it
   is unsafe, and it allows every class. A refused address is a resolution
   error. The rule applies to the address the request is actually sent
   to, not to the host name in the URL:
   - a reader that resolves names itself MUST resolve the name, check each
     address, and connect only to an address it has checked, keeping the
     URL's host for the `Host` header and the TLS server name (or check
     the connected socket's peer address before it sends the request). A
     connection kept alive from an earlier request is checked again before
     it is reused. A host that is an IP literal is checked as the address it
     denotes, however it is written: a URL parser that accepts decimal,
     octal or hexadecimal IPv4 forms such as `2130706433` or `0x7f.1`
     yields the address `127.0.0.1`;
   - **proxies:** a request sent through a proxy cannot be checked, since
     the proxy connects. While this rule applies, a reader MUST refuse to
     send a request through a proxy (resolution error) unless the
     application allows **unchecked proxies**, a second setting whose name
     says that it is unsafe; the error names that setting. With it, the
     reader checks the addresses the name resolves to where the reader
     runs, which catches literal addresses but not DNS rebinding. A request
     that the reader's proxy settings exempt (for example through
     `no_proxy`) is sent directly, and checked. When the application allows
     private hosts, the rule does not apply, and proxies are used as
     configured;
   - a reader that cannot see addresses at all (a browser's `fetch`)
     checks the host as written in the URL, before the request and on the
     final URL after redirects ([§5.2](#52-reading-over-http)): an IP
     literal by its class, and `localhost` and names under `.localhost` as
     loopback. For names that resolve to other classes, and for redirect
     hops it cannot see, it relies on the platform: browsers that implement
     Private Network Access refuse such requests from public pages unless
     the private server consents. It cannot see the browser's proxy either.

   The store's own location is not checked: the application chose it.
4. **Sources.** A reader MUST have a maximum number of sources. Opening a
   store whose source table has more is an archive error.
5. **Requests per value.** A reader MUST have a maximum number of reads of
   `url` sources that one `get` may make, counted after coalescing
   ([§6.3](#63-resolving-a-reference)), with each read counting once however
   many redirects it follows. A `get` that would need more is a request
   error, reported before any of its reads is made.
6. **Data read at open.** A reader MUST have a maximum size for the source
   table and for each piece of the binding's auxiliary data, and MUST stop
   reading or decompressing either as soon as its output passes that size,
   so that a small stored form cannot make it allocate more. Passing it is
   an archive error. The binding says how values are bounded
   ([§13.1](#131-opening) for ZIP).
7. **Unverifiable pins.** An application MAY allow the reader to skip a pin
   that the response's headers would check ([§5.2](#52-reading-over-http))
   when the reader cannot see those headers: a browser reading a
   cross-origin object whose server does not expose `Content-Range`, `ETag`
   or `Last-Modified`. A pin that the reader can check, and that fails,
   still fails, and every `crc32c` is still checked. Without that
   permission, such a pin fails ([§5.1](#51-pins)).

The limits of rules 4–6 are implementation-defined, and readers MUST
document them. The readers in the specification's repository use these
defaults:

| | default |
|---|---|
| schemes (rule 1) | `http` and `https` for every store, `file` for a local one; no other scheme |
| `file` for a remote store | refused |
| prefixes | none (rule 1 applies) |
| hosts (rule 3) | public addresses only, for every store |
| proxies while rule 3 applies | refused |
| sources per store | 4,194,304 (2^22) |
| reads per `get` | 1024 |
| the source table, or auxiliary data (in a vzip archive, a format entry, inflated) | 256 MiB |
| unverifiable pins | fail |

They name the unsafe settings `allow_files_from_remote_archives`,
`allow_private_hosts` and `allow_unchecked_proxy` (Python), and
`allowFilesFromRemoteArchives`, `allowPrivateHosts` and
`allowUncheckedProxy` (TypeScript). The reference command-line tool takes
`--allow-private-hosts`, which the conformance harness passes, since its
archives' sources are served over HTTP on the loopback interface.

## 7. Writing

A writer produces a store from a list of keys, each with either bytes or a
list of ranges, and a source table. It MUST produce a valid store, and it
MUST reject its input, producing no store, if:

- a key is empty, not valid UTF-8, or duplicated;
- a range's `source` is not less than the number of sources (this includes
  every source range when there are no sources);
- a `url` source is empty or does not match RFC 3986 `URI-reference`;
- a `key` source is empty, or names a key that is absent or is a reference;
- a source range of a `key` or `data` source extends past the end of that
  source's value, that is, `offset + length` is greater than the value's
  size. This includes a zero-length range whose offset is past the end. The
  writer knows these values; `url` sources are not checked;
- a pin is on a `key` or `data` source, or an `etag` pin is not a strong
  entity tag in double quotes ([§5.1](#51-pins));
- a literal range has a `crc32c` ([§4.2](#42-range));
- a reference payload exceeds the binding's limit
  ([§4.4](#44-reference-payloads));
- a source range's `offset + length`, or a reference's total size, exceeds
  2^64 − 1 ([§4.2](#42-range), [§4.3](#43-concat)).

A binding adds the reasons that come from its backend
([§14.1](#141-requirements) for ZIP).

## 8. Security considerations

- A reader resolves the URLs a store names with the reader's own access.
  The policy of [§6.7](#67-reader-policy) limits what that access reaches by
  default:
  - a remote store cannot read local files, and no store can use other
    schemes, such as an `s3:` bucket the reader holds credentials for
    (rule 1);
  - no store, local or remote, can reach loopback, private, link-local
    (which includes cloud metadata services) or other special addresses
    (rule 3). A reader that resolves names checks the address it connects
    to, so DNS rebinding does not get past the check, and it does not send
    requests through a proxy, whose connections it cannot check.

  It does not cover:
  - public hosts: any store can make the reader send requests to any
    public host. The requests carry no credentials of the reader's, but
    they reveal that the store was read;
  - readers that cannot see addresses: a browser checks hosts written as
    IP literals or `localhost`, and relies on Private Network Access for
    names that resolve to private addresses and for redirect hops;
  - applications that set an unsafe setting. With unchecked proxies, a
    name that resolves to a public address where the reader runs, and to
    a private one where the proxy runs, is not detected. With private
    hosts, a store from an untrusted place, local files included, can
    send requests to services on the reader's machine and network; an
    application that needs private hosts for some stores SHOULD give
    those a prefix list (rule 2) as well.

  Applications that read stores from untrusted places SHOULD give a
  prefix list ([§6.7](#67-reader-policy) rule 2).
- One reference can name thousands of ranges, and a source table millions
  of sources; the limits of [§6.7](#67-reader-policy) bound the requests one
  `get` makes and the memory a store takes to open.
- Keys are not file names. Tools that extract a store to disk MUST NOT
  trust keys such as `../x` or `/etc/passwd`.
- A reference can describe a value of up to 2^64 − 1 bytes. Readers SHOULD
  bound the memory a single request can use, and document the bound; a
  request beyond it is a request error.
- Pins ([§5.1](#51-pins)) and range checksums ([§4.2](#42-range)) detect
  accidental replacement of a referenced object, not tampering: the store
  itself is not signed, a `modified_not_after` pin cannot see a rewrite
  within the same second, and CRC-32C is not a cryptographic hash.

# Part 2. The ZIP binding

## 9. What the ZIP binding defines

A **vzip archive** is a store kept in a single ZIP file. The ZIP binding
answers the three questions of [§3.1](#31-what-a-binding-defines) as
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
  ([§4.2](#42-range)) can name like a chunk in any other file. For file
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
[§2.1](#21-keys): they are not empty, they are valid UTF-8, and they are
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
  format version as one decimal digit ([§1.3](#13-versioning)). A future
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
error ([§6.4](#64-errors)).

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
([§4.4](#44-reference-payloads)):

- header ID 0x7A76: an encoded `Range` ([§4.2](#42-range)), describing a
  value made of that one range.
- header ID 0x7A77: an encoded `Concat` ([§4.3](#43-concat)), describing a
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
one as malformed (a payload error, [§4.4](#44-reference-payloads)), even
though it may fit in the extra field. That limit leaves room in the
65535-byte extra field for the payload's own block header and a ZIP64
block, whether or not the entry ends up needing one, so a writer can check a
reference before deciding where to put it.

### 11.4 Hidden entries

Entries whose names begin with the **reserved prefix** `__vz__/` are
**hidden**. They are entries of the archive, but their keys are not part of
the store's mapping ([§3.1](#31-what-a-binding-defines)): classify and get
treat them as *missing*, and list leaves them out
([§13.2](#132-operations)). A ZIP tool that knows nothing about vzip still
sees them as ordinary files, and so does the raw view.

Two hidden entries hold the binding's own data. They are the **format
entries**, written by the writer itself:

- `__vz__/sources`, the source table ([§5](#5-source-table)), in every
  archive;
- `__vz__/index`, the page index ([§12](#12-page-index-optional)), in an
  archive that has one.

Readers find both through the archive comment
([§10.4](#104-end-of-central-directory-record-and-archive-comment)).

A writer MAY write other hidden entries, holding bytes for the archive's
own use. A `key` source ([§5](#5-source-table)) MAY name a hidden `bytes`
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

Opening a vzip archive ([§6.1](#61-opening)) reads and checks the following,
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

  A resource limit ([§6.7](#67-reader-policy),
  [§8](#8-security-considerations)) reached while opening, for example while
  inflating a format entry, is an archive error ([§6.1](#61-opening)).

  The same corruption of a central directory record is an archive error in
  an archive without a page index (the whole directory is parsed at open),
  and an entry error, or nothing at all, in one with a page index (only the
  pages that are read are parsed). This is intentional: it is the cost of
  opening large archives without reading their whole directory.
- **The format entries:** the bodies the comment locates lie within the
  file and inflate cleanly (below). Their uncompressed sizes are not checked,
  since readers need not read their records. The source table is valid
  ([§5](#5-source-table)). The page index, if there is one, is not malformed
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
([§6.7](#67-reader-policy) rule 6). A `bytes` entry's body is bounded
by the uncompressed size of its record (it must inflate to exactly that),
and readers SHOULD stop inflating it once its output passes that size.

A reader MAY read more, but MUST NOT report an error at open that this
section and [§6.1](#61-opening) do not list.

### 13.2 Operations

On a vzip archive, the operations of [§6.2](#62-operations) work as follows.

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

In a vzip archive, the error classes of [§6.4](#64-errors) have these
causes, besides those of Part 1:

| class | causes in a vzip archive |
|---|---|
| **archive error** | everything [§13.1](#131-opening) checks |
| **entry error** | the key's record violates [§11.1](#111-classification) (unparseable extra field, several reference blocks), uses a method other than 0 and 8, has bit 0 set, is a reference entry with method 8 or a large one ([§11.3](#113-reference-entries)), is a large entry with method 8 ([§10.1](#101-structure) rule 7), or breaks the ZIP64 block rules ([§10.2](#102-zip64)); or the page that lookup ([§12.2](#122-lookup)) selects for the key cannot be parsed. In that last case the key is reported as an entry error even if it isn't in the page, because the reader cannot tell. |
| **body error** | an entry's body (`[body offset, body offset + compressed size)`, judged as a whole even for a small window) lies outside the file; a DEFLATE body does not inflate cleanly ([§13.1](#131-opening)); a STORED entry's compressed and uncompressed sizes differ. This applies to bytes entries for get and raw, and to reference entries for raw. |
| **payload error** | a reference payload longer than 65519 bytes ([§11.3](#113-reference-entries)) |

In the order of checks of [§6.4](#64-errors), step 3 is the lookup of the
key's central directory record, and that record's entry error. In step 4, a
DEFLATE body is always inflated and checked in full, even for a small
window, so corruption anywhere in it is reported. A STORED body is read only
within the window.

### 13.4 Violations readers need not detect

For archives that break these writer requirements, reader results are
unspecified (besides those of [§6.6](#66-violations-readers-need-not-detect)):

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

A writer of a vzip archive follows [§7](#7-writing). Its input may include
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
  [§8](#8-security-considerations) covers it: readers stop inflating a
  format entry at the limit of [§6.7](#67-reader-policy) rule 6, and a
  `bytes` entry at its record's uncompressed size ([§13.1](#131-opening)).
- To a ZIP tool, keys are file names. A tool that knows nothing about vzip
  extracts every key as a file, so a key such as `../x` meets that tool's
  own protections, or none; tools that know vzip follow
  [§8](#8-security-considerations). Such a tool also extracts a reference
  entry as its payload, or as an empty file, not as the referenced bytes.

## Appendix A: Complete schema

`CdIndex` is used only by the ZIP binding ([§12](#12-page-index-optional)).

```proto
syntax = "proto3";
package vzip.v0;

message Range {
  uint32 source = 1;
  reserved 2;
  uint64 offset = 3;
  uint64 length = 4;
  optional bytes data = 5;
  optional uint32 crc32c = 6;
}

message Concat {
  repeated Range parts = 1;
}

message Source {
  oneof kind {
    string url  = 1;
    string key  = 2;
    bytes  data = 3;
  }
  optional uint64 size               = 4;
  optional string etag               = 5;
  optional int64  modified_not_after = 6;
}

message SourceTable {
  repeated Source sources = 1;
  optional uint32 revision = 2;
}

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
