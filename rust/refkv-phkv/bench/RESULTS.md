# phkv benchmark results

Method, files and caveats: [README.md](README.md). A request is one read of the archive (a `Range` request
over HTTP, a `pread` on a file). "http 20 ms" is loopback with 20 ms added to each request. The tables are
generated: the full matrix by `report.py`, the parameters by `sweep.py`, paged vzip against phkv by `versus.py`.

The reader measured here, against the first run:

- a hybrid store's superblock is found from the ZIP end of central directory record instead of by probing,
  and the window before it is read in one request
  ([§17.2](../../../spec/bindings/phkv.md#172-finding-the-superblock)); the hybrid file has no padding;
- a record's continuation read, and a section that overlaps a read already made, read only the bytes still
  missing;
- new defaults (`Limits`): SLOTS is read in 4 KiB blocks, the size of a vzip page, kept for later lookups
  (`slot_block`; it was one slot per lookup), rounded down to whole 12-byte slots, so 4,092 bytes; the first read of the file is 512 KiB + 64 B (`tail`;
  it was 64 KiB + 64 B); the first read of a record is 4.5 KiB (`record_read`; it was 512 B), which holds a
  record whose value is inline at the writer's default `inline_max` of 4 KiB;
- the whole blocks of SLOTS that the first read already holds are kept as if a lookup had read them, and
  a record's first read stops at the end of its page, since a page holds whole records;
- a format change: a slot holds its record's length as well as its offset
  ([§18.2](../../../spec/bindings/phkv.md#182-slots-section)). A slot is 12 bytes: a `u64` offset from the start
  of RECORDS and a `u32` length, so a slot is 4 bytes larger than the 8 it was when the two shared a `u64` (the
  numbers here are from the 12-byte layout; the 8-byte one split the `u64` by the bit length of RECORDS). A
  length of 0 is an empty slot, `0xFFFFFFFF` says that the record is 2^32 − 1 bytes or longer, and any other length
  is exact. A record is therefore read in one read of exactly its length, and `record_read` applies only to a
  record whose slot gives no length, which takes a record of 4 GiB.

## Paged vzip against phkv

Later lookups are random keys on an opened store, timed over 200 of them and counted over 5,000, by which
time the caches have filled. vzip's pages and phkv's slot blocks are both 4 KiB, but a block holds 341 slots
and a page about 50 central directory records, so phkv's cache fills about 7 times sooner.


#### 100,000 keys, `refs`: over HTTP, 20 ms per request

| reader | open + first lookup | later lookup (200 lookups) | later lookup (5,000 lookups, counted) |
|---|---|---|---|
| vzip, paged | 4 req, 81.4 KiB, 98 ms | 1.96 req, 3.8 KiB, 44 ms | 1.33 req, 1.3 KiB |
| phkv, the first defaults (slots one at a time, 64 KiB first read, 512 B record read) | 3 req, 64.1 KiB, 67 ms | 2.00 req, 40 B, 43 ms | 2.00 req, 40 B |
| phkv, 4 KiB slot blocks only | 3 req, 68.1 KiB, 66 ms | 1.71 req, 2.9 KiB, 37 ms | 1.06 req, 267 B |
| phkv, defaults (4 KiB slot blocks, 512 KiB first read, 4.5 KiB record read) | 3 req, 516.1 KiB, 67 ms | 1.46 req, 1.9 KiB, 31 ms | 1.04 req, 175 B |

#### 1,000,000 keys, `refs`: over HTTP, 20 ms per request

| reader | open + first lookup | later lookup (200 lookups) | later lookup (5,000 lookups, counted) |
|---|---|---|---|
| vzip, paged | 4 req, 199.8 KiB, 185 ms | 1.99 req, 4.0 KiB, 45 ms | 1.87 req, 3.5 KiB |
| phkv, the first defaults (slots one at a time, 64 KiB first read, 512 B record read) | 5 req, 281.1 KiB, 112 ms | 2.00 req, 42 B, 43 ms | 2.00 req, 42 B |
| phkv, 4 KiB slot blocks only | 5 req, 285.1 KiB, 112 ms | 1.96 req, 3.8 KiB, 42 ms | 1.49 req, 2.0 KiB |
| phkv, defaults (4 KiB slot blocks, 512 KiB first read, 4.5 KiB record read) | 3 req, 516.1 KiB, 68 ms | 1.94 req, 3.8 KiB, 41 ms | 1.48 req, 1.9 KiB |

#### 100,000 keys, `bytes`: over HTTP, 20 ms per request

| reader | open + first lookup | later lookup (200 lookups) | later lookup (5,000 lookups, counted) |
|---|---|---|---|
| vzip, paged | 4 req, 80.4 KiB, 97 ms | 1.95 req, 4.8 KiB, 44 ms | 1.28 req, 2.1 KiB |
| phkv, the first defaults (slots one at a time, 64 KiB first read, 512 B record read) | 4 req, 161.4 KiB, 90 ms | 2.00 req, 1.0 KiB, 43 ms | 2.00 req, 1.0 KiB |
| phkv, 4 KiB slot blocks only | 4 req, 165.4 KiB, 93 ms | 1.74 req, 4.0 KiB, 37 ms | 1.06 req, 1.3 KiB |
| phkv, defaults (4 KiB slot blocks, 512 KiB first read, 4.5 KiB record read) | 3 req, 517.1 KiB, 68 ms | 1.54 req, 3.2 KiB, 33 ms | 1.04 req, 1.2 KiB |

What it shows, with the defaults:

- **Every operation takes fewer requests with phkv.** Open and first lookup: 3 against 4. Later lookups over
  200: 1.46, 1.94 and 1.54 against 1.96, 1.99 and 1.95; over 5,000: 1.04, 1.48 and 1.04 against 1.33, 1.87
  and 1.28. At 10^5 keys the first read holds part of SLOTS, so some lookups read no slot (1.46 requests over 200 lookups, against 1.25 with 8-byte slots).
- **Every lookup reads fewer bytes with phkv**: 175 B to 3.8 KiB against 1.3 to 4.8 KiB. A record is read in
  one read of exactly its length (28 B for a reference, 1.0 KiB for a 1 KiB value), since its slot gives it; what
  is left is the 4 KiB slot block, read once and kept. An open reads more: 516 to 517 KiB, against 80 to 200
  KiB. At 100 MB/s that is about 4 ms; at 20 ms per request, a fifth of a round trip.
- Each part does its share: slot blocks make later lookups cheaper; the 512 KiB first read takes the 10^6-key
  open from 5 requests to 3, and its share of SLOTS saves slot reads; the record's length in its slot makes
  every record one read of its own bytes.

## What else the full matrix shows

1. **The unpaged vzip archive's open cost grows with the key count**: it reads the whole central directory
   (69 MiB, 3.9 s at 10^6 keys, in Python). Paged vzip and phkv do not.
2. **A hybrid archive read as phkv opens in 4 to 7 requests** (8 to 18 when the superblock was found by
   probing). It is also smaller than before: 158 to 168 B per key, against 172 to 225 B.
3. **`list` with phkv reads one page per request.** Pages are 4 KiB. For references, 100 keys fit in 1 to 2
   pages. With 1 KiB values inline, the same 100 keys span 26 pages: 26 requests and about 540 ms at 20 ms,
   against 4 requests for paged vzip. A writer's larger `page_size` cuts that (below). A hybrid store's
   records are small, since its values are ZIP entries: 2 requests.
4. **Size.** For references, per key: standalone phkv 38 B at 10^3 and 42 B at 10^6; vzip 119 to 125 B;
   hybrid 158 to 168 B. With 1 KiB values the values dominate: phkv is 5% smaller than vzip.
5. **Languages.** vzip rows are the Python reader and phkv rows the Rust one, so compare requests and bytes;
   at 10^6 keys the Python reader's own work is about 90 ms even on a local file.

## Tuning

Over HTTP with 20 ms per request (`sweep.py`), the other parameters at their defaults.

**Reader `Limits::tail`**, the bytes read first, at 10^6 references, open and one lookup:

| tail | 64.1 KiB | 128.0 KiB | 256.0 KiB | 512.1 KiB | 1.0 MiB |
|---|---|---|---|---|---|
| standalone | 5 req, 285.1 KiB, 114 ms | 5 req, 285.1 KiB, 108 ms | 4 req, 285.1 KiB, 87 ms | 3 req, 516.1 KiB, 67 ms | 3 req, 1.0 MiB, 71 ms |
| hybrid | 7 req, 348.2 KiB, 151 ms | 7 req, 412.1 KiB, 154 ms | 6 req, 540.1 KiB, 133 ms | 5 req, 1.0 MiB, 114 ms | 5 req, 2.0 MiB, 114 ms |

512 KiB takes the 10^6-key open from 5 requests to 3, because PTRHASH and PAGES (about 280 KiB) come with it.
For a hybrid store the first read is made twice (the end of the file, and the window before the superblock),
so its bytes double.

**Reader `Limits::record_read`**, the bytes read at a record first, 10^5 keys of 1 KiB inline values:

| record_read | 512 B | 1.0 KiB | 2.0 KiB | 4.5 KiB |
|---|---|---|---|---|
| later lookup | 1.54 req, 3.2 KiB, 42 ms | 1.54 req, 3.2 KiB, 42 ms | 1.54 req, 3.2 KiB, 42 ms | 1.54 req, 3.2 KiB, 42 ms |

Now that a slot gives its record's length, `record_read` changes nothing here: every record is read in one
read of its length. It applies only to a record whose slot gives no length.

**Writer `page_size` and `inline_max`**, 10^5 keys of 1 KiB values (the writer's, recorded in the file):

| page_size, inline_max | file | later lookup (record_read 512) | later lookup (record_read 4.5 KiB) | list of 100 keys |
|---|---|---|---|---|
| 4.0 KiB, 4.0 KiB | 101.6 MiB | 1.54 req, 3.2 KiB, 42 ms | 1.54 req, 3.2 KiB, 42 ms | 26 req, 106.8 KiB, 553 ms |
| 16.0 KiB, 4.0 KiB | 101.5 MiB | 1.48 req, 2.9 KiB, 22 ms | 1.48 req, 2.9 KiB, 22 ms | 7 req, 115.1 KiB, 149 ms |
| 64.0 KiB, 4.0 KiB | 101.5 MiB | 1.46 req, 2.9 KiB, 22 ms | 1.46 req, 2.9 KiB, 22 ms | 3 req, 194.1 KiB, 65 ms |
| 4.0 KiB, none | 101.9 MiB | 2.46 req, 2.8 KiB, 43 ms | 2.46 req, 2.8 KiB, 43 ms | 2 req, 8.0 KiB, 42 ms |

A larger `page_size` cuts the `list` requests (26 to 3 at 64 KiB) by reading more bytes per page; lookups do
not read pages. Values not inline (`inline_max` 0) make records small, so a `list` is 2 requests, but a lookup
reads the value too.

**Reader `Limits::slots_at_open`**, the longest SLOTS section read whole at open and kept (default 0, off), with a
bandwidth per connection. Total time for k lookups of random keys on a cold store (the first lookup, and k − 1
later ones at their mean time):

| keys, workload | bandwidth | slots_at_open | open + first lookup | later lookup | k = 1 | k = 10 | k = 100 | k = 1,000 |
|---|---|---|---|---|---|---|---|---|
| 100,000 references | none | off | 3 req, 516.1 KiB, 68 ms | 1.46 req, 31 ms | 68 ms | 345 ms | 3120 ms | 30864 ms |
| 100,000 references | none | whole | 3 req, 1.2 MiB, 69 ms | 1.00 req, 21 ms | 69 ms | 262 ms | 2193 ms | 21505 ms |
| 100,000 references | 100 MB/s | off | 3 req, 516.1 KiB, 72 ms | 1.46 req, 31 ms | 72 ms | 349 ms | 3121 ms | 30835 ms |
| 100,000 references | 100 MB/s | whole | 3 req, 1.2 MiB, 83 ms | 1.00 req, 21 ms | 83 ms | 274 ms | 2181 ms | 21248 ms |
| 100,000 references | 12.5 MB/s | off | 3 req, 516.1 KiB, 108 ms | 1.46 req, 31 ms | 108 ms | 388 ms | 3181 ms | 31117 ms |
| 100,000 references | 12.5 MB/s | whole | 3 req, 1.2 MiB, 171 ms | 1.00 req, 21 ms | 171 ms | 363 ms | 2285 ms | 21502 ms |
| 1,000,000 references | none | off | 3 req, 516.1 KiB, 68 ms | 1.94 req, 41 ms | 68 ms | 439 ms | 4149 ms | 41255 ms |
| 1,000,000 references | none | whole | 3 req, 12.1 MiB, 117 ms | 1.00 req, 21 ms | 117 ms | 308 ms | 2219 ms | 21328 ms |
| 1,000,000 references | 100 MB/s | off | 3 req, 516.1 KiB, 75 ms | 1.94 req, 41 ms | 75 ms | 445 ms | 4150 ms | 41194 ms |
| 1,000,000 references | 100 MB/s | whole | 3 req, 12.1 MiB, 239 ms | 1.00 req, 21 ms | 239 ms | 430 ms | 2345 ms | 21488 ms |
| 1,000,000 references | 12.5 MB/s | off | 3 req, 516.1 KiB, 112 ms | 1.94 req, 41 ms | 112 ms | 483 ms | 4194 ms | 41300 ms |
| 1,000,000 references | 12.5 MB/s | whole | 3 req, 12.1 MiB, 1118 ms | 1.00 req, 21 ms | 1118 ms | 1309 ms | 3212 ms | 22245 ms |
| 100,000 keys of 1 KiB values | none | off | 3 req, 517.1 KiB, 71 ms | 1.54 req, 33 ms | 71 ms | 365 ms | 3305 ms | 32701 ms |
| 100,000 keys of 1 KiB values | none | whole | 3 req, 1.3 MiB, 76 ms | 1.00 req, 21 ms | 76 ms | 267 ms | 2171 ms | 21210 ms |
| 100,000 keys of 1 KiB values | 100 MB/s | off | 3 req, 517.1 KiB, 75 ms | 1.54 req, 33 ms | 75 ms | 368 ms | 3308 ms | 32703 ms |
| 100,000 keys of 1 KiB values | 100 MB/s | whole | 3 req, 1.3 MiB, 92 ms | 1.00 req, 21 ms | 92 ms | 283 ms | 2195 ms | 21310 ms |
| 100,000 keys of 1 KiB values | 12.5 MB/s | off | 3 req, 517.1 KiB, 112 ms | 1.54 req, 33 ms | 112 ms | 411 ms | 3401 ms | 33293 ms |
| 100,000 keys of 1 KiB values | 12.5 MB/s | whole | 3 req, 1.3 MiB, 186 ms | 1.00 req, 21 ms | 186 ms | 377 ms | 2287 ms | 21394 ms |

Reading SLOTS at open (12.3 bytes per key, held in memory) makes every lookup one request, the record; with slot
blocks a later lookup is 1.46 to 1.94. SLOTS is read in the same request as PAGES and PTRHASH, which lie after
it, so the open takes no more requests (3 either way). What it costs is bytes at open: 12.1 MiB at 10^6 keys,
0.7 to 0.8 MiB more at 10^5. With no bandwidth limit the first lookup costs 1 to 49 ms more. At 100 MB/s it
costs 164 ms more at 10^6 keys, which about 8 lookups pay back (each saves about 20 ms); at 12.5 MB/s (100 Mbit/s)
it costs 1,006 ms more, which about 50 lookups pay back. At 10^5 keys the first read holds part of SLOTS, so a
later lookup saves 10 to 12 ms: at 12.5 MB/s the extra 63 to 74 ms pays back after about 6 lookups. A
store opened to read a few keys is better off without it; one opened to read many, with it, which is why it
stays off by default.

**Reading SLOTS in blocks** (`slots_model.py`, a calculation, not a measurement). A lookup wants one 12-byte slot, and the
hash scatters keys, so which slot is uniform over the section, with no locality. Blocks of B slots read when needed
and kept cost, for k lookups, M(1 − (1 − 1/M)^k) requests (M = N/B), each R + 12B/W. Over 3,465 combinations of
section size (10^4 to 10^8 slots), round trip (1 to 500 ms), bandwidth (0.1 MB/s to unlimited) and k (1 to 10^8), a block between one
slot and the whole section never beat both by 5%: the best size is one slot (12 bytes) or the whole section. (That sweep was run with 8-byte slots. A slot's size enters the cost only as bytes over bandwidth, so it was not repeated.) The whole section
wins once k > 1 + 12N/(W R): at 10^6 keys and 20 ms, k ≥ 8 at 100 MB/s, k ≥ 51 at 12.5 MB/s, any k > 1 with
no bandwidth limit. With k unknown, one fixed block size is at most 1.94 times (100 MB/s, 1.8 MiB) to 2.04 times
(12.5 MB/s, 228 KiB) the best, and a reader that reads slots one at a time until k reaches that threshold and then
reads the whole section is at most 2 times (the ski-rental rule). For a lookup of many keys at once, sort their slots and
merge reads whose gap is below R × W bytes (2 MB at 100 MB/s and 20 ms; 250 KB at 12.5 MB/s). The measured block of 4 KiB (4,092 bytes, whole slots) is not that optimum for a given k, but it costs a lookup the same
one request as a single slot, so it is never worse than one slot at a time, and it is what makes later lookups
cheaper than paged vzip's. The default block of 4 KiB is not that optimum for a given k, but it costs a lookup the same
one request as a single slot, so it is never worse than one slot at a time, and it is what makes later lookups
cheaper than paged vzip's.

What the parameters do not fix: `list` still takes one request per page, only fewer pages; and a longer
`tail` or `slots_at_open` is bytes read at every open.

## Tables

## Workload `refs`

### File size

| format (reader) | 1,000 | 10,000 | 100,000 | 1,000,000 |
|---|---|---|---|---|
| vzip (python) | 116.2 KiB | 1.2 MiB | 11.7 MiB | 119.5 MiB |
| vzip, paged (python) | 116.5 KiB | 1.2 MiB | 11.7 MiB | 119.6 MiB |
| phkv (rust) | 37.3 KiB | 384.0 KiB | 3.9 MiB | 40.3 MiB |
| hybrid, read as vzip (python) | 154.0 KiB | 1.5 MiB | 15.5 MiB | 159.9 MiB |
| hybrid, read as phkv (rust) | 154.0 KiB | 1.5 MiB | 15.5 MiB | 159.9 MiB |

### First lookup of a store opened cold (open and one `raw`)

**local**

| format (reader) | 1,000 | 10,000 | 100,000 | 1,000,000 |
|---|---|---|---|---|
| vzip (python) | 3 req, 68.0 KiB, 3.79 ms | 3 req, 691.1 KiB, 24 ms | 3 req, 6.8 MiB, 261 ms | 3 req, 69.3 MiB, 2836 ms |
| vzip, paged (python) | 4 req, 68.3 KiB, 2.36 ms | 4 req, 69.5 KiB, 3.01 ms | 4 req, 81.4 KiB, 11 ms | 4 req, 199.8 KiB, 95 ms |
| phkv (rust) | 2 req, 37.3 KiB, 0.16 ms | 2 req, 384.0 KiB, 0.31 ms | 3 req, 516.1 KiB, 0.57 ms | 3 req, 516.1 KiB, 1.58 ms |
| hybrid, read as vzip (python) | 4 req, 68.3 KiB, 2.41 ms | 4 req, 69.5 KiB, 3.53 ms | 4 req, 81.4 KiB, 11 ms | 4 req, 199.8 KiB, 99 ms |
| hybrid, read as phkv (rust) | 2 req, 154.0 KiB, 0.19 ms | 3 req, 1.0 MiB, 0.52 ms | 5 req, 1.0 MiB, 0.80 ms | 5 req, 1.0 MiB, 1.92 ms |

**http 0 ms**

| format (reader) | 1,000 | 10,000 | 100,000 | 1,000,000 |
|---|---|---|---|---|
| vzip (python) | 3 req, 68.0 KiB, 5.67 ms | 3 req, 691.1 KiB, 26 ms | 3 req, 6.8 MiB, 261 ms | 3 req, 69.3 MiB, 2997 ms |
| vzip, paged (python) | 4 req, 68.3 KiB, 4.22 ms | 4 req, 69.5 KiB, 5.30 ms | 4 req, 81.4 KiB, 14 ms | 4 req, 199.8 KiB, 97 ms |
| phkv (rust) | 2 req, 37.3 KiB, 2.63 ms | 2 req, 384.0 KiB, 2.52 ms | 3 req, 516.1 KiB, 3.82 ms | 3 req, 516.1 KiB, 4.58 ms |
| hybrid, read as vzip (python) | 4 req, 68.3 KiB, 4.30 ms | 4 req, 69.5 KiB, 6.26 ms | 4 req, 81.4 KiB, 14 ms | 4 req, 199.8 KiB, 98 ms |
| hybrid, read as phkv (rust) | 2 req, 154.0 KiB, 1.97 ms | 3 req, 1.0 MiB, 3.95 ms | 5 req, 1.0 MiB, 5.64 ms | 5 req, 1.0 MiB, 6.62 ms |

**http 20 ms**

| format (reader) | 1,000 | 10,000 | 100,000 | 1,000,000 |
|---|---|---|---|---|
| vzip (python) | 3 req, 68.0 KiB, 69 ms | 3 req, 691.1 KiB, 89 ms | 3 req, 6.8 MiB, 319 ms | 3 req, 69.3 MiB, 3023 ms |
| vzip, paged (python) | 4 req, 68.3 KiB, 88 ms | 4 req, 69.5 KiB, 89 ms | 4 req, 81.4 KiB, 97 ms | 4 req, 199.8 KiB, 181 ms |
| phkv (rust) | 2 req, 37.3 KiB, 44 ms | 2 req, 384.0 KiB, 46 ms | 3 req, 516.1 KiB, 69 ms | 3 req, 516.1 KiB, 70 ms |
| hybrid, read as vzip (python) | 4 req, 68.3 KiB, 88 ms | 4 req, 69.5 KiB, 89 ms | 4 req, 81.4 KiB, 99 ms | 4 req, 199.8 KiB, 181 ms |
| hybrid, read as phkv (rust) | 2 req, 154.0 KiB, 45 ms | 3 req, 1.0 MiB, 69 ms | 5 req, 1.0 MiB, 110 ms | 5 req, 1.0 MiB, 114 ms |

### Later lookups of random keys in an opened store (per lookup, median)

**local**

| format (reader) | 1,000 | 10,000 | 100,000 | 1,000,000 |
|---|---|---|---|---|
| vzip (python) | 1.00 req, 8 B, 0.56 ms | 1.00 req, 8 B, 0.56 ms | 1.00 req, 8 B, 0.48 ms | 1.00 req, 9 B, 0.54 ms |
| vzip, paged (python) | 1.08 req, 334 B, 0.54 ms | 1.59 req, 2.4 KiB, 1.16 ms | 1.96 req, 3.9 KiB, 1.51 ms | 1.99 req, 4.0 KiB, 1.39 ms |
| phkv (rust) | 1.00 req, 25 B, 0.00 ms | 1.00 req, 27 B, 0.00 ms | 1.46 req, 1.8 KiB, 0.01 ms | 1.94 req, 3.8 KiB, 0.01 ms |
| hybrid, read as vzip (python) | 1.08 req, 334 B, 0.63 ms | 1.59 req, 2.4 KiB, 1.52 ms | 1.96 req, 3.9 KiB, 1.57 ms | 1.99 req, 4.0 KiB, 1.52 ms |
| hybrid, read as phkv (rust) | 1.00 req, 25 B, 0.00 ms | 1.00 req, 27 B, 0.00 ms | 1.41 req, 1.6 KiB, 0.00 ms | 1.94 req, 3.8 KiB, 0.01 ms |

**http 0 ms**

| format (reader) | 1,000 | 10,000 | 100,000 | 1,000,000 |
|---|---|---|---|---|
| vzip (python) | 1.00 req, 8 B, 1.13 ms | 1.00 req, 8 B, 1.04 ms | 1.00 req, 8 B, 1.18 ms | 1.00 req, 9 B, 1.06 ms |
| vzip, paged (python) | 1.08 req, 334 B, 1.09 ms | 1.59 req, 2.4 KiB, 2.24 ms | 1.96 req, 3.9 KiB, 2.38 ms | 1.99 req, 4.0 KiB, 2.44 ms |
| phkv (rust) | 1.00 req, 25 B, 0.42 ms | 1.00 req, 27 B, 0.47 ms | 1.46 req, 1.8 KiB, 0.76 ms | 1.94 req, 3.8 KiB, 0.83 ms |
| hybrid, read as vzip (python) | 1.08 req, 334 B, 1.02 ms | 1.59 req, 2.4 KiB, 2.25 ms | 1.96 req, 3.9 KiB, 2.46 ms | 1.99 req, 4.0 KiB, 2.75 ms |
| hybrid, read as phkv (rust) | 1.00 req, 25 B, 0.51 ms | 1.00 req, 27 B, 0.55 ms | 1.41 req, 1.6 KiB, 1.09 ms | 1.94 req, 3.8 KiB, 1.16 ms |

**http 20 ms**

| format (reader) | 1,000 | 10,000 | 100,000 | 1,000,000 |
|---|---|---|---|---|
| vzip (python) | 1.00 req, 8 B, 22 ms | 1.00 req, 8 B, 22 ms | 1.00 req, 8 B, 22 ms | 1.00 req, 9 B, 22 ms |
| vzip, paged (python) | 1.08 req, 334 B, 22 ms | 1.59 req, 2.4 KiB, 44 ms | 1.96 req, 3.9 KiB, 45 ms | 1.99 req, 4.0 KiB, 44 ms |
| phkv (rust) | 1.00 req, 25 B, 21 ms | 1.00 req, 27 B, 21 ms | 1.46 req, 1.8 KiB, 22 ms | 1.94 req, 3.8 KiB, 43 ms |
| hybrid, read as vzip (python) | 1.08 req, 334 B, 22 ms | 1.59 req, 2.4 KiB, 44 ms | 1.96 req, 3.9 KiB, 45 ms | 1.99 req, 4.0 KiB, 45 ms |
| hybrid, read as phkv (rust) | 1.00 req, 25 B, 21 ms | 1.00 req, 27 B, 21 ms | 1.41 req, 1.6 KiB, 22 ms | 1.94 req, 3.8 KiB, 43 ms |

### `list` of the 100 keys under one prefix, on a store opened cold

**local**

| format (reader) | 1,000 | 10,000 | 100,000 | 1,000,000 |
|---|---|---|---|---|
| vzip (python) | 2 req, 67.9 KiB, 3.14 ms | 2 req, 691.1 KiB, 22 ms | 2 req, 6.8 MiB, 247 ms | 2 req, 69.3 MiB, 2829 ms |
| vzip, paged (python) | 5 req, 76.3 KiB, 2.32 ms | 4 req, 73.5 KiB, 2.80 ms | 5 req, 89.5 KiB, 11 ms | 5 req, 207.9 KiB, 99 ms |
| phkv (rust) | 2 req, 8.0 KiB, 0.08 ms | 2 req, 8.0 KiB, 0.11 ms | 2 req, 8.0 KiB, 0.07 ms | 1 req, 4.0 KiB, 0.13 ms |
| hybrid, read as vzip (python) | 5 req, 76.3 KiB, 2.33 ms | 4 req, 73.5 KiB, 3.50 ms | 5 req, 89.5 KiB, 12 ms | 5 req, 207.9 KiB, 98 ms |
| hybrid, read as phkv (rust) | 2 req, 8.0 KiB, 0.09 ms | 2 req, 8.0 KiB, 0.14 ms | 2 req, 8.0 KiB, 0.06 ms | 1 req, 4.0 KiB, 0.12 ms |

**http 0 ms**

| format (reader) | 1,000 | 10,000 | 100,000 | 1,000,000 |
|---|---|---|---|---|
| vzip (python) | 2 req, 67.9 KiB, 4.60 ms | 2 req, 691.1 KiB, 26 ms | 2 req, 6.8 MiB, 259 ms | 2 req, 69.3 MiB, 2952 ms |
| vzip, paged (python) | 5 req, 76.3 KiB, 5.53 ms | 4 req, 73.5 KiB, 5.36 ms | 5 req, 89.5 KiB, 15 ms | 5 req, 207.9 KiB, 99 ms |
| phkv (rust) | 2 req, 8.0 KiB, 1.07 ms | 2 req, 8.0 KiB, 1.17 ms | 2 req, 8.0 KiB, 1.16 ms | 1 req, 4.0 KiB, 0.67 ms |
| hybrid, read as vzip (python) | 5 req, 76.3 KiB, 5.61 ms | 4 req, 73.5 KiB, 5.32 ms | 5 req, 89.5 KiB, 15 ms | 5 req, 207.9 KiB, 100 ms |
| hybrid, read as phkv (rust) | 2 req, 8.0 KiB, 1.18 ms | 2 req, 8.0 KiB, 1.22 ms | 2 req, 8.0 KiB, 1.39 ms | 1 req, 4.0 KiB, 0.68 ms |

**http 20 ms**

| format (reader) | 1,000 | 10,000 | 100,000 | 1,000,000 |
|---|---|---|---|---|
| vzip (python) | 2 req, 67.9 KiB, 48 ms | 2 req, 691.1 KiB, 67 ms | 2 req, 6.8 MiB, 298 ms | 2 req, 69.3 MiB, 3019 ms |
| vzip, paged (python) | 5 req, 76.3 KiB, 68 ms | 4 req, 73.5 KiB, 69 ms | 5 req, 89.5 KiB, 79 ms | 5 req, 207.9 KiB, 165 ms |
| phkv (rust) | 2 req, 8.0 KiB, 43 ms | 2 req, 8.0 KiB, 43 ms | 2 req, 8.0 KiB, 43 ms | 1 req, 4.0 KiB, 21 ms |
| hybrid, read as vzip (python) | 5 req, 76.3 KiB, 68 ms | 4 req, 73.5 KiB, 69 ms | 5 req, 89.5 KiB, 80 ms | 5 req, 207.9 KiB, 169 ms |
| hybrid, read as phkv (rust) | 2 req, 8.0 KiB, 43 ms | 2 req, 8.0 KiB, 43 ms | 2 req, 8.0 KiB, 42 ms | 1 req, 4.0 KiB, 21 ms |

## Workload `bytes`

### File size

| format (reader) | 1,000 | 10,000 | 100,000 |
|---|---|---|---|
| vzip (python) | 1.1 MiB | 10.7 MiB | 107.3 MiB |
| vzip, paged (python) | 1.1 MiB | 10.7 MiB | 107.4 MiB |
| phkv (rust) | 1.0 MiB | 10.2 MiB | 101.6 MiB |
| hybrid, read as vzip (python) | 1.1 MiB | 11.1 MiB | 111.6 MiB |
| hybrid, read as phkv (rust) | 1.1 MiB | 11.1 MiB | 111.6 MiB |

### First lookup of a store opened cold (open and one `get`)

**local**

| format (reader) | 1,000 | 10,000 | 100,000 |
|---|---|---|---|
| vzip (python) | 2 req, 65.0 KiB, 2.83 ms | 3 req, 575.4 KiB, 20 ms | 3 req, 5.6 MiB, 192 ms |
| vzip, paged (python) | 3 req, 69.0 KiB, 1.87 ms | 4 req, 70.3 KiB, 2.74 ms | 4 req, 80.4 KiB, 9.17 ms |
| phkv (rust) | 2 req, 513.1 KiB, 0.39 ms | 2 req, 513.1 KiB, 0.65 ms | 3 req, 517.1 KiB, 2.58 ms |
| hybrid, read as vzip (python) | 4 req, 69.2 KiB, 2.73 ms | 4 req, 70.3 KiB, 2.55 ms | 4 req, 80.3 KiB, 9.71 ms |
| hybrid, read as phkv (rust) | 4 req, 1.0 MiB, 0.52 ms | 4 req, 1.0 MiB, 0.64 ms | 6 req, 1.0 MiB, 0.89 ms |

**http 0 ms**

| format (reader) | 1,000 | 10,000 | 100,000 |
|---|---|---|---|
| vzip (python) | 2 req, 65.0 KiB, 3.86 ms | 3 req, 575.4 KiB, 23 ms | 3 req, 5.6 MiB, 195 ms |
| vzip, paged (python) | 3 req, 69.0 KiB, 3.31 ms | 4 req, 70.3 KiB, 5.16 ms | 4 req, 80.4 KiB, 12 ms |
| phkv (rust) | 2 req, 513.1 KiB, 2.78 ms | 2 req, 513.1 KiB, 2.98 ms | 3 req, 517.1 KiB, 6.07 ms |
| hybrid, read as vzip (python) | 4 req, 69.2 KiB, 5.23 ms | 4 req, 70.3 KiB, 5.47 ms | 4 req, 80.3 KiB, 11 ms |
| hybrid, read as phkv (rust) | 4 req, 1.0 MiB, 4.79 ms | 4 req, 1.0 MiB, 4.62 ms | 6 req, 1.0 MiB, 5.41 ms |

**http 20 ms**

| format (reader) | 1,000 | 10,000 | 100,000 |
|---|---|---|---|
| vzip (python) | 2 req, 65.0 KiB, 46 ms | 3 req, 575.4 KiB, 86 ms | 3 req, 5.6 MiB, 263 ms |
| vzip, paged (python) | 3 req, 69.0 KiB, 66 ms | 4 req, 70.3 KiB, 89 ms | 4 req, 80.4 KiB, 95 ms |
| phkv (rust) | 2 req, 513.1 KiB, 46 ms | 2 req, 513.1 KiB, 46 ms | 3 req, 517.1 KiB, 71 ms |
| hybrid, read as vzip (python) | 4 req, 69.2 KiB, 88 ms | 4 req, 70.3 KiB, 88 ms | 4 req, 80.3 KiB, 98 ms |
| hybrid, read as phkv (rust) | 4 req, 1.0 MiB, 90 ms | 4 req, 1.0 MiB, 90 ms | 6 req, 1.0 MiB, 134 ms |

### Later lookups of random keys in an opened store (per lookup, median)

**local**

| format (reader) | 1,000 | 10,000 | 100,000 |
|---|---|---|---|
| vzip (python) | 0.99 req, 1014 B, 0.49 ms | 1.00 req, 1.0 KiB, 0.51 ms | 1.00 req, 1.0 KiB, 0.55 ms |
| vzip, paged (python) | 1.06 req, 1.3 KiB, 0.46 ms | 1.55 req, 3.2 KiB, 1.08 ms | 1.95 req, 4.8 KiB, 1.66 ms |
| phkv (rust) | 1.00 req, 1.0 KiB, 0.00 ms | 1.00 req, 1.0 KiB, 0.00 ms | 1.54 req, 3.2 KiB, 0.01 ms |
| hybrid, read as vzip (python) | 1.07 req, 1.3 KiB, 0.51 ms | 1.54 req, 3.2 KiB, 1.21 ms | 1.95 req, 4.8 KiB, 1.50 ms |
| hybrid, read as phkv (rust) | 2.00 req, 1.0 KiB, 0.00 ms | 2.00 req, 1.0 KiB, 0.01 ms | 2.40 req, 2.6 KiB, 0.01 ms |

**http 0 ms**

| format (reader) | 1,000 | 10,000 | 100,000 |
|---|---|---|---|
| vzip (python) | 0.99 req, 1014 B, 0.94 ms | 1.00 req, 1.0 KiB, 0.96 ms | 1.00 req, 1.0 KiB, 0.97 ms |
| vzip, paged (python) | 1.06 req, 1.3 KiB, 0.95 ms | 1.55 req, 3.2 KiB, 2.21 ms | 1.95 req, 4.8 KiB, 2.46 ms |
| phkv (rust) | 1.00 req, 1.0 KiB, 0.46 ms | 1.00 req, 1.0 KiB, 0.55 ms | 1.54 req, 3.2 KiB, 0.87 ms |
| hybrid, read as vzip (python) | 1.07 req, 1.3 KiB, 1.02 ms | 1.54 req, 3.2 KiB, 2.20 ms | 1.95 req, 4.8 KiB, 2.66 ms |
| hybrid, read as phkv (rust) | 2.00 req, 1.0 KiB, 1.27 ms | 2.00 req, 1.0 KiB, 1.18 ms | 2.40 req, 2.6 KiB, 1.29 ms |

**http 20 ms**

| format (reader) | 1,000 | 10,000 | 100,000 |
|---|---|---|---|
| vzip (python) | 0.99 req, 1014 B, 22 ms | 1.00 req, 1.0 KiB, 22 ms | 1.00 req, 1.0 KiB, 22 ms |
| vzip, paged (python) | 1.06 req, 1.3 KiB, 22 ms | 1.55 req, 3.2 KiB, 44 ms | 1.95 req, 4.8 KiB, 45 ms |
| phkv (rust) | 1.00 req, 1.0 KiB, 21 ms | 1.00 req, 1.0 KiB, 21 ms | 1.54 req, 3.2 KiB, 42 ms |
| hybrid, read as vzip (python) | 1.07 req, 1.3 KiB, 22 ms | 1.54 req, 3.2 KiB, 44 ms | 1.95 req, 4.8 KiB, 45 ms |
| hybrid, read as phkv (rust) | 2.00 req, 1.0 KiB, 43 ms | 2.00 req, 1.0 KiB, 43 ms | 2.40 req, 2.6 KiB, 43 ms |

### `list` of the 100 keys under one prefix, on a store opened cold

**local**

| format (reader) | 1,000 | 10,000 | 100,000 |
|---|---|---|---|
| vzip (python) | 1 req, 64.0 KiB, 2.30 ms | 2 req, 574.4 KiB, 19 ms | 2 req, 5.6 MiB, 190 ms |
| vzip, paged (python) | 4 req, 76.0 KiB, 1.82 ms | 4 req, 73.3 KiB, 2.48 ms | 4 req, 83.4 KiB, 9.56 ms |
| phkv (rust) | 26 req, 106.6 KiB, 0.08 ms | 26 req, 106.7 KiB, 0.10 ms | 26 req, 106.8 KiB, 0.26 ms |
| hybrid, read as vzip (python) | 5 req, 76.3 KiB, 2.86 ms | 4 req, 73.3 KiB, 3.00 ms | 4 req, 83.3 KiB, 9.39 ms |
| hybrid, read as phkv (rust) | 2 req, 8.0 KiB, 0.10 ms | 2 req, 8.0 KiB, 0.11 ms | 2 req, 8.0 KiB, 0.06 ms |

**http 0 ms**

| format (reader) | 1,000 | 10,000 | 100,000 |
|---|---|---|---|
| vzip (python) | 1 req, 64.0 KiB, 3.11 ms | 2 req, 574.4 KiB, 21 ms | 2 req, 5.6 MiB, 198 ms |
| vzip, paged (python) | 4 req, 76.0 KiB, 4.18 ms | 4 req, 73.3 KiB, 4.90 ms | 4 req, 83.4 KiB, 12 ms |
| phkv (rust) | 26 req, 106.6 KiB, 14 ms | 26 req, 106.7 KiB, 14 ms | 26 req, 106.8 KiB, 13 ms |
| hybrid, read as vzip (python) | 5 req, 76.3 KiB, 5.35 ms | 4 req, 73.3 KiB, 4.95 ms | 4 req, 83.3 KiB, 12 ms |
| hybrid, read as phkv (rust) | 2 req, 8.0 KiB, 1.28 ms | 2 req, 8.0 KiB, 1.22 ms | 2 req, 8.0 KiB, 1.09 ms |

**http 20 ms**

| format (reader) | 1,000 | 10,000 | 100,000 |
|---|---|---|---|
| vzip (python) | 1 req, 64.0 KiB, 25 ms | 2 req, 574.4 KiB, 63 ms | 2 req, 5.6 MiB, 241 ms |
| vzip, paged (python) | 4 req, 76.0 KiB, 50 ms | 4 req, 73.3 KiB, 69 ms | 4 req, 83.4 KiB, 78 ms |
| phkv (rust) | 26 req, 106.6 KiB, 552 ms | 26 req, 106.7 KiB, 553 ms | 26 req, 106.8 KiB, 555 ms |
| hybrid, read as vzip (python) | 5 req, 76.3 KiB, 68 ms | 4 req, 73.3 KiB, 68 ms | 4 req, 83.3 KiB, 76 ms |
| hybrid, read as phkv (rust) | 2 req, 8.0 KiB, 43 ms | 2 req, 8.0 KiB, 43 ms | 2 req, 8.0 KiB, 43 ms |

