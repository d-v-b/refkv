# vzip conformance harness interface

For format version 0 (provisional), specification revision 11. Revision 11
changed the ZIP binding: every archive has the ZIP64 end records
([spec §10.2](../../spec/archive.md#102-zip64)), and entries of 4 GiB or more
are allowed ([spec §10.1](../../spec/archive.md#101-structure) rule 7). The
suite has cases for both. The checks that revisions 9 and 10 added (range
checksums, the reader policy) have no cases yet, and the policy is set as
below.

**Large entries.** The crafted archive `large_entries` holds entries of
0xFFFFFFFF and 2^32 + 3 bytes, and entries after them past 8 GiB. The runner
writes it as a sparse file, which takes little disk on file systems that
support them. Its queries read short ranges, so a reader that reads only the
bytes a window needs from a STORED body
([spec §13.3](../../spec/archive.md#133-errors)) never holds a large body in
memory.

**Reader policy ([spec §6.7](../../spec/archive.md#67-reader-policy), revision 10).** The runner serves the `http`
sources of its archives on the loopback interface (`127.0.0.1`), which rule
3 refuses by default. The `read` command must therefore allow private
hosts, and that permission is unsafe, so it is asked for by name: the
runner is given each implementation's command line with whatever option
does it, such as the reference's `uv run python -m vzip.cli
--allow-private-hosts`. The archives are local files, so they may read
`file` sources by default. The other limits are the implementation's
defaults.

Every implementation provides one executable (the "CLI") with two commands.
The conformance runner calls it as a subprocess. All JSON is UTF-8. Byte
strings in JSON are lowercase hex.

## `read`

```
<cli> read <archive-path> <queries-path>
```

Open the archive at `<archive-path>`, which is a local file path, so its base
URI is the `file:` URI of its absolute path. Then run every query in the JSON
array at `<queries-path>`, in order, and print one JSON object to stdout:

```json
{
  "open": {"ok": true},
  "results": [ ... one result per query ... ]
}
```

If opening the archive fails with an archive error ([spec §6.4](../../spec/archive.md#64-errors)), print
`{"open": {"ok": false, "class": "archive", "error": "<message>"}, "results": []}`.
A file that cannot be opened or read at all is also an archive error. Report
only archive errors this way; every other error belongs to the query that hit
it.

Exit with status 0 whenever the JSON was printed, including when the open
failed or individual queries failed. A non-zero exit status means the CLI
itself crashed, or the queries file was unreadable or invalid.

### Queries and results

| query | meaning | success result |
|---|---|---|
| `{"op": "classify", "key": K}` | [spec §6.2](../../spec/archive.md#62-operations) classify | `{"ok": true, "kind": "bytes" \| "reference" \| "missing"}` |
| `{"op": "get", "key": K}` | [spec §6.2](../../spec/archive.md#62-operations) get, whole value | `{"ok": true, "value": "<hex>"}`, or `{"ok": true, "value": null}` if missing |
| `{"op": "get", "key": K, "range": {"start": S, "end": E}}` | get, range(S, E) | same |
| `{"op": "get", "key": K, "range": {"offset": S}}` | get, offset(S) | same |
| `{"op": "get", "key": K, "range": {"suffix": N}}` | get, suffix(N) | same |
| `{"op": "get_raw", "key": K}` | [spec §6.2](../../spec/archive.md#62-operations) raw (no `range`) | same |
| `{"op": "list", "prefix": P}` | [spec §6.2](../../spec/archive.md#62-operations) list | `{"ok": true, "keys": [ ... ]}` |

A `range` object has exactly one of these forms. All numbers in queries and
descriptions are integers whose magnitude is below 2^53. Only
`modified_not_after` may be negative.

A malformed query object is an invalid queries file, so exit non-zero. A
query is malformed if it has:
- an unknown `op`;
- a missing `key`, or a missing `prefix` on `list`;
- a `range` on anything other than `get`;
- a `range` that is `null`, has several forms or a partial form (`start`
  without `end`), or has a negative or non-integer number.

A query is also malformed if:
- its `key` or `prefix` is not a string, or contains a lone surrogate (not
  valid Unicode);
- a `range` member is `null`;
- a number is 2^53 or more in magnitude.

Unknown members of a query or of a `range` object are ignored. A JSON object
with duplicate member names, in a queries file or a description, is invalid.

If something goes wrong inside the implementation while running one query
(a bug, not an error the spec defines), report
`{"ok": false, "class": "internal", "error": "..."}` for that query and keep
going. The runner always counts `internal` as a failure.

A query that fails with an error ([spec §6.4](../../spec/archive.md#64-errors)) produces
`{"ok": false, "class": "<class>", "error": "<message>"}`, where `<class>` is
one of `entry`, `body`, `payload`, `resolution` or `request` ([spec §6.4](../../spec/archive.md#64-errors)). The
runner checks the class; the message is free-form. One failed query must not
prevent the others from running.

## `write`

```
<cli> write <description-path> <out-path>
```

Write a vzip archive to `<out-path>` from the JSON description below. If the
description is invalid ([spec §7](../../spec/archive.md#7-writing) and
[§14.1](../../spec/archive.md#141-requirements) list what a writer must reject, and the
rules below add some), exit with a non-zero status, print a message to
stderr, and do not create a file at `<out-path>`. The runner never passes an
`<out-path>` that already exists.

```json
{
  "page_size": null,
  "mirror": true,
  "sources": [
    {"url": "data/a.bin"},
    {"key": "__vz__/hdr"},
    {"data": "48445221"}
  ],
  "entries": [
    {"key": "x/zarr.json", "bytes": "7b7d", "compress": false, "pinned": false},
    {"key": "x/c/0", "ranges": [{"source": 0, "offset": 10, "length": 4}]},
    {"key": "x/c/1", "ranges": [{"source": 2, "offset": 0, "length": 3}, {"data": "00ff"}]}
  ]
}
```

- `sources`: the source table, in order. Index `i` in a range refers to
  `sources[i]`. Each element has exactly one of `url`, `key`, `data`. A
  `url` element may also have pins ([spec §5.1](../../spec/archive.md#51-pins)): `"size"` (integer), `"etag"`
  (string, including its double quotes) and `"modified_not_after"` (integer
  seconds since the Unix epoch).
- `entries`: each element is either a bytes entry (`bytes`) or a reference
  entry (`ranges`).
  - `compress: true` means the entry uses DEFLATE. It is only allowed on bytes
    entries; on a reference entry the description is invalid.
  - `pinned: true` means the entry is listed in the page index's `pinned`
    list. That requires `page_size` to be non-null and the entry to be a bytes
    entry (hidden entries may be pinned); otherwise the description is
    invalid.
  - A range element is either a source range `{"source", "offset", "length"}`
    or a literal range `{"data"}`, never a mix of the two. A missing `source`,
    `offset` or `length` means 0, so `{}` is a range of source 0, which is
    invalid when there are no sources.
  - An entry has exactly one of `bytes` and `ranges`.
  - Keys may start with `__vz__/` (hidden entries), except `__vz__/sources`
    and `__vz__/index`, which the writer creates itself.
- `page_size`: `null` means no page index. An integer of at least 1 means
  write a page index ([spec §12](../../spec/archive.md#12-page-index-optional)); anything else is invalid. How records are
  grouped into pages is up to the writer, but a page SHOULD hold about
  `page_size` bytes of central directory records.
- `mirror`: `true` means each reference entry's body is its payload; `false`
  means it is empty ([spec §11.3](../../spec/archive.md#113-reference-entries)).
- Absent members take these defaults: `page_size` null, `mirror` true,
  `sources` [], `entries` [], `compress` false, `pinned` false. Unknown
  members are ignored.
- Types are strict: flags are JSON booleans, numbers are JSON integers (`1.0`
  is invalid), and hex strings are lowercase with even length. A description
  that breaks these rules is invalid.
- `null` is allowed only for `page_size`. Any other known member that is
  `null` makes the description invalid; unknown members are ignored whatever
  their value.
- `compress: false` is allowed on reference entries.

The written archive must satisfy the spec. The runner validates its
structure, reads it back with the reference implementation and with your own
`read`, and compares the results to the description.
