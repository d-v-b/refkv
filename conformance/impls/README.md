# Independent implementations

Each directory holds a reader, writer and harness CLI that an agent built from
[the archive spec of revision 7](../../spec/history/archive/SPEC.r7.md) (format version 0; the current
spec, revision 11, is [spec/refkv.md](../../spec/refkv.md) and
[spec/bindings/vzip.md](../../spec/bindings/vzip.md)) and
[HARNESS.md](../vzip/HARNESS.md) alone. The agents had no access to the
reference implementation, the test vectors or each other's code. Each agent's
`SPEC_NOTES.md` lists the ambiguities it reported; notes from every round are
in [design/conformance/archive/](../../design/conformance/archive/).

| dir | language | dependencies | HTTP | run |
|---|---|---|---|---|
| `rust/` | Rust | flate2, crc32fast (hand-written HTTP/1.1 client, JSON parser, URI and date code) | http | `cargo build --release`, then `./vzip` |
| `typescript/` | TypeScript (Node ≥ 23, type stripping) | none | http, https | `./vzip` |
| `python/` | Python 3.12 | standard library | http, https | `./vzip` (needs `uv`) |

**Against the revision-7 suite**, all three pass every check, including the
HTTP profile:
- 5120/5120 read queries;
- 11/11 write cases;
- 41/41 rejections;
- 19892/19892 cross-reads;
- 3136/3136 HTTP checks.

There is no divergence between them.

**Against revision 8** they fail only four rules that revision 8 added
because of their round-7 notes:
- a repeated `ETag` field is an error even on an unpinned read (all three);
- the `Content-Range` unit is case-insensitive (TypeScript);
- query files with lone-surrogate keys, or with numbers of 2^53 or more, are
  invalid (TypeScript and Python).

**Against revision 11** they are unchanged, and `just conformance` reports
what revision 11 changed in the ZIP binding
([spec/bindings/vzip.md §10.1](../../spec/bindings/vzip.md#101-structure) rule 7,
[§10.2](../../spec/bindings/vzip.md#102-zip64)), besides the four rules above:
- **Writing:** none of their 11 written archives is valid, because none has
  the zip64 end records; the reference reader refuses to open them, which
  costs each 8 cross-read queries and 2 HTTP checks.
- **Reading:** each fails 14 of the 5173 read queries: it opens the
  revision-8 archives without zip64 records (unpaged and paged) and the
  archive pinning a large DEFLATE entry, all of which must fail to open; it
  takes the directory from end-record fields of zero or garbage, which
  readers must ignore (3 cases); and it gives entry errors for the entries of
  4 GiB or more in `large_entries` (6 queries).
- They read every archive the revision-11 writers write without a large
  entry: the end record's all-ones fields send them to the zip64 record.
