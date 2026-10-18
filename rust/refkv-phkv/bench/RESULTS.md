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
- SLOTS is read in 4 KiB blocks, the size of a vzip page, kept for later lookups (`Limits::slot_block`,
  the default); it used to be one 8-byte slot per lookup.

## Paged vzip against phkv

Later lookups are random keys on an opened store, timed over 200 of them and counted over 5,000, by which
time the caches have filled. vzip's pages and phkv's slot blocks are both 4 KiB, but a block holds 512 slots
and a page about 50 central directory records, so phkv's cache fills about 7 times sooner.

#### 100,000 keys, `refs`: over HTTP, 20 ms per request

| reader | open + first lookup | later lookup (200 lookups) | later lookup (5,000 lookups, counted) |
|---|---|---|---|
| vzip, paged | 4 req, 81.4 KiB, 98 ms | 1.96 req, 3.8 KiB, 43 ms | 1.33 req, 1.3 KiB |
| phkv, slots one at a time | 3 req, 64.6 KiB, 63 ms | 1.96 req, 520 B, 41 ms | 1.96 req, 520 B |
| phkv, defaults (4 KiB slot blocks) | 3 req, 68.6 KiB, 63 ms | 1.59 req, 2.9 KiB, 33 ms | 1.04 req, 669 B |
| phkv, + record_read 2 KiB, tail 512 KiB | 3 req, 518.0 KiB, 64 ms | 1.25 req, 3.0 KiB, 26 ms | 1.02 req, 2.1 KiB |

#### 1,000,000 keys, `refs`: over HTTP, 20 ms per request

| reader | open + first lookup | later lookup (200 lookups) | later lookup (5,000 lookups, counted) |
|---|---|---|---|
| vzip, paged | 4 req, 199.8 KiB, 196 ms | 1.99 req, 4.0 KiB, 44 ms | 1.87 req, 3.5 KiB |
| phkv, slots one at a time | 5 req, 281.6 KiB, 106 ms | 2.00 req, 520 B, 41 ms | 2.00 req, 520 B |
| phkv, defaults (4 KiB slot blocks) | 5 req, 285.6 KiB, 107 ms | 1.95 req, 4.3 KiB, 42 ms | 1.37 req, 2.0 KiB |
| phkv, + record_read 2 KiB, tail 512 KiB | 3 req, 518.0 KiB, 65 ms | 1.93 req, 5.7 KiB, 40 ms | 1.36 req, 3.4 KiB |

#### 100,000 keys, `bytes`: over HTTP, 20 ms per request

| reader | open + first lookup | later lookup (200 lookups) | later lookup (5,000 lookups, counted) |
|---|---|---|---|
| vzip, paged | 4 req, 80.4 KiB, 97 ms | 1.95 req, 4.8 KiB, 43 ms | 1.28 req, 2.1 KiB |
| phkv, slots one at a time | 5 req, 161.4 KiB, 107 ms | 3.00 req, 1.0 KiB, 62 ms | 3.00 req, 1.0 KiB |
| phkv, defaults (4 KiB slot blocks) | 5 req, 165.4 KiB, 107 ms | 2.62 req, 3.5 KiB, 54 ms | 2.04 req, 1.2 KiB |
| phkv, + record_read 2 KiB, tail 512 KiB | 3 req, 518.0 KiB, 66 ms | 1.34 req, 3.4 KiB, 28 ms | 1.02 req, 2.1 KiB |

In the last row of each table, part of the gain in later lookups comes from the harness: its HTTP `ReadAt`
keeps the 512 KiB it read first and serves reads inside it, and at 10^5 keys that holds about half of SLOTS.
The store itself does not keep it.

What it shows:

- **Later lookups: phkv takes fewer requests than paged vzip with its defaults** at 10^5 and 10^6
  references: 1.59 and 1.95 per lookup over 200 lookups against 1.96 and 1.99, and 1.04 and 1.37 over 5,000
  against 1.33 and 1.87. With 1 KiB values inline it takes more (2.62 and 2.04 against 1.95 and 1.28),
  because the first read of a record, 512 B, does not hold the record. With `record_read` 2 KiB and a
  512 KiB first read it takes fewer: 1.34 and 1.02 (in part the harness's doing, above).
- **Open and first lookup**: phkv takes 3 requests at 10^5 references, against vzip's 4, but 5 at 10^6 keys
  and with 1 KiB values, where PTRHASH and PAGES do not fit in the 64 KiB first read. With a first read
  of 512 KiB, 3 in every case.
- **Bytes**: a lookup reads 0.7 to 5.7 KiB with phkv and 1.3 to 4.8 KiB with vzip. The longer first read
  is 518 KiB at every open.

## What else the full matrix shows

1. **The unpaged vzip archive's open cost grows with the key count**: it reads the whole central directory
   (69 MiB, 3.9 s at 10^6 keys, in Python). Paged vzip and phkv do not.
2. **A hybrid archive read as phkv opens in 4 to 7 requests** (8 to 18 when the superblock was found by
   probing). It is also smaller than before: 153 to 164 B per key, against 172 to 225 B.
3. **`list` with phkv reads one page per request.** Pages are 4 KiB. For references, 100 keys fit in 1 to 2
   pages. With 1 KiB values inline, the same 100 keys span 26 pages: 26 requests and about 540 ms at 20 ms,
   against 4 requests for paged vzip. A writer's larger `page_size` cuts that (below). A hybrid store's
   records are small, since its values are ZIP entries: 2 requests.
4. **Size.** For references, per key: standalone phkv 34 B at 10^3 and 38 B at 10^6; vzip 119 to 125 B;
   hybrid 153 to 164 B. With 1 KiB values the values dominate: phkv is 6% smaller than vzip.
5. **Languages.** vzip rows are the Python reader and phkv rows the Rust one, so compare requests and bytes;
   at 10^6 keys the Python reader's own work is about 90 ms even on a local file.

## Tuning

Over HTTP with 20 ms per request (`sweep.py`), with the default `slot_block`.

**Reader `Limits::tail`**, the bytes read first (default 64 KiB + 64 B), at 10^6 references, open and one lookup:

| tail | 64.1 KiB | 128.0 KiB | 256.0 KiB | 512.0 KiB | 1.0 MiB |
|---|---|---|---|---|---|
| standalone | 5 req, 285.6 KiB, 106 ms | 5 req, 285.6 KiB, 106 ms | 4 req, 285.6 KiB, 85 ms | 3 req, 516.5 KiB, 65 ms | 3 req, 1.0 MiB, 70 ms |
| hybrid | 7 req, 348.6 KiB, 151 ms | 7 req, 412.6 KiB, 148 ms | 6 req, 540.6 KiB, 127 ms | 5 req, 1.0 MiB, 107 ms | 5 req, 2.0 MiB, 108 ms |

A first read of 512 KiB takes the 10^6-key open from 5 requests to 3, because PTRHASH and PAGES (about 280 KiB)
come with it. For a hybrid store the first read is made twice (the end of the file, and the window before the
superblock), so its bytes double.

**Reader `Limits::record_read`**, the bytes read at a record first (default 512 B), 10^5 keys of 1 KiB inline values:

| record_read | 512 B | 1.0 KiB | 2.0 KiB | 4.0 KiB |
|---|---|---|---|---|
| later lookup | 2.61 req, 3.5 KiB, 62 ms | 2.61 req, 3.5 KiB, 62 ms | 1.61 req, 4.4 KiB, 41 ms | 1.61 req, 6.4 KiB, 41 ms |

A first read as long as the record (2 KiB for a 1 KiB value and its key) saves the record's second read.
A store of references has records of tens of bytes, which 512 B holds.

**Writer `page_size` and `inline_max`**, 10^5 keys of 1 KiB values (the writer's, recorded in the file):

| page_size, inline_max | file | later lookup (record_read 512) | later lookup (record_read 4096) | list of 100 keys |
|---|---|---|---|---|
| 4.0 KiB, 4.0 KiB | 101.2 MiB | 2.61 req, 3.5 KiB, 62 ms | 1.61 req, 6.4 KiB, 41 ms | 26 req, 106.8 KiB, 537 ms |
| 16.0 KiB, 4.0 KiB | 101.1 MiB | 2.61 req, 3.5 KiB, 62 ms | 1.61 req, 6.4 KiB, 41 ms | 7 req, 115.1 KiB, 146 ms |
| 64.0 KiB, 4.0 KiB | 101.1 MiB | 2.59 req, 3.4 KiB, 62 ms | 1.59 req, 6.4 KiB, 41 ms | 3 req, 194.1 KiB, 63 ms |
| 4.0 KiB, none | 101.5 MiB | 2.59 req, 3.9 KiB, 62 ms | 2.59 req, 7.4 KiB, 62 ms | 2 req, 8.0 KiB, 41 ms |

A larger `page_size` cuts the `list` requests (26 to 3 at 64 KiB) by reading more bytes per page; lookups do
not read pages. Values not inline (`inline_max` 0) make records small, so a `list` is 2 requests, but a lookup
reads the value too.

**Reader `Limits::slots_at_open`**, the longest SLOTS section read whole at open and kept (default 0, off), with a
bandwidth per connection. Total time for k lookups of random keys on a cold store (the first lookup, and k − 1 later
ones at their mean time):

| keys, workload | bandwidth | slots_at_open | open + first lookup | later lookup | k = 1 | k = 10 | k = 100 | k = 1,000 |
|---|---|---|---|---|---|---|---|---|
| 100,000 references | none | off | 3 req, 68.6 KiB, 63 ms | 1.59 req, 33 ms | 63 ms | 360 ms | 3326 ms | 32987 ms |
| 100,000 references | none | whole | 3 req, 829.2 KiB, 65 ms | 1.00 req, 21 ms | 65 ms | 252 ms | 2125 ms | 20860 ms |
| 100,000 references | 100 MB/s | off | 3 req, 68.6 KiB, 64 ms | 1.59 req, 33 ms | 64 ms | 361 ms | 3328 ms | 33001 ms |
| 100,000 references | 100 MB/s | whole | 3 req, 829.2 KiB, 73 ms | 1.00 req, 21 ms | 73 ms | 261 ms | 2145 ms | 20988 ms |
| 100,000 references | 12.5 MB/s | off | 3 req, 68.6 KiB, 69 ms | 1.59 req, 33 ms | 69 ms | 368 ms | 3356 ms | 33238 ms |
| 100,000 references | 12.5 MB/s | whole | 3 req, 829.2 KiB, 133 ms | 1.00 req, 21 ms | 133 ms | 320 ms | 2191 ms | 20904 ms |
| 1,000,000 references | none | off | 5 req, 285.6 KiB, 106 ms | 1.95 req, 41 ms | 106 ms | 474 ms | 4156 ms | 40973 ms |
| 1,000,000 references | none | whole | 3 req, 8.1 MiB, 78 ms | 1.00 req, 21 ms | 78 ms | 264 ms | 2124 ms | 20719 ms |
| 1,000,000 references | 100 MB/s | off | 5 req, 285.6 KiB, 109 ms | 1.95 req, 40 ms | 109 ms | 473 ms | 4116 ms | 40546 ms |
| 1,000,000 references | 100 MB/s | whole | 3 req, 8.1 MiB, 161 ms | 1.00 req, 21 ms | 161 ms | 347 ms | 2209 ms | 20835 ms |
| 1,000,000 references | 12.5 MB/s | off | 5 req, 285.6 KiB, 130 ms | 1.95 req, 41 ms | 130 ms | 501 ms | 4210 ms | 41299 ms |
| 1,000,000 references | 12.5 MB/s | whole | 3 req, 8.1 MiB, 761 ms | 1.00 req, 21 ms | 761 ms | 948 ms | 2824 ms | 21586 ms |
| 100,000 keys of 1 KiB values (record_read 2 KiB) | none | off | 4 req, 166.3 KiB, 86 ms | 1.61 req, 33 ms | 86 ms | 387 ms | 3391 ms | 33432 ms |
| 100,000 keys of 1 KiB values (record_read 2 KiB) | none | whole | 3 req, 961.4 KiB, 66 ms | 1.00 req, 21 ms | 66 ms | 253 ms | 2115 ms | 20741 ms |
| 100,000 keys of 1 KiB values (record_read 2 KiB) | 100 MB/s | off | 4 req, 166.3 KiB, 89 ms | 1.61 req, 34 ms | 89 ms | 391 ms | 3406 ms | 33565 ms |
| 100,000 keys of 1 KiB values (record_read 2 KiB) | 100 MB/s | whole | 3 req, 961.4 KiB, 77 ms | 1.00 req, 21 ms | 77 ms | 266 ms | 2154 ms | 21034 ms |
| 100,000 keys of 1 KiB values (record_read 2 KiB) | 12.5 MB/s | off | 4 req, 166.3 KiB, 101 ms | 1.61 req, 34 ms | 101 ms | 404 ms | 3435 ms | 33747 ms |
| 100,000 keys of 1 KiB values (record_read 2 KiB) | 12.5 MB/s | whole | 3 req, 961.4 KiB, 146 ms | 1.00 req, 21 ms | 146 ms | 334 ms | 2209 ms | 20966 ms |

Reading SLOTS at open (8.2 bytes per key, held in memory) makes a lookup one request, the record, instead of
up to two. It is read in the same request as PAGES and PTRHASH, which lie after it, so the open does not take more
requests: at 10^6 keys it takes fewer (3 against 5), since those sections no longer need reads of their own.
What it costs is bytes at open, 8.1 MiB at 10^6 keys. With no bandwidth limit the open costs the same or
less. At 100 MB/s the first lookup costs 52 ms more at 10^6 keys, and reading SLOTS pays for itself after about 3
lookups; at 12.5 MB/s (100 Mbit/s) the first lookup costs 634 ms more, and it pays after about 32. At
10^5 keys (0.8 MiB) it pays after about 5 lookups even at 12.5 MB/s, since slot blocks already make a later
lookup 1.59 requests rather than 2. A store opened to read one key is better off
without it; one opened to read many is better off with it.

**Reading SLOTS in blocks** (`slots_model.py`, a calculation, not a measurement). A lookup wants one 8-byte slot, and the
hash scatters keys, so which slot is uniform over the section, with no locality. Blocks of B slots read when needed
and kept cost, for k lookups, M(1 − (1 − 1/M)^k) requests (M = N/B), each R + 8B/W. Over 3,465 combinations of
section size (10^4 to 10^8 slots), round trip (1 to 500 ms), bandwidth (0.1 MB/s to unlimited) and k (1 to 10^8), a block between one
slot and the whole section never beat both by 5%: the best size is 8 bytes or the whole section. The whole section
wins once k > 1 + 8N/(W R): at 10^6 keys and 20 ms, k ≥ 5 at 100 MB/s, k ≥ 34 at 12.5 MB/s, any k > 1 with
no bandwidth limit. With k unknown, one fixed block size is at most 1.9 times (100 MB/s, 1.7 MiB) to 2.05 times
(12.5 MB/s, 256 KiB) the best, and a reader that reads slots one at a time until k reaches that threshold and then
reads the whole section is at most 2 times (the ski-rental rule). For a lookup of many keys at once, sort their slots and
merge reads whose gap is below R × W bytes (2 MB at 100 MB/s and 20 ms; 250 KB at 12.5 MB/s). The measured block of 4 KiB is not that optimum for a given k, but it costs a lookup the same
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
| phkv (rust) | 33.3 KiB | 344.1 KiB | 3.5 MiB | 36.4 MiB |
| hybrid, read as vzip (python) | 150.0 KiB | 1.5 MiB | 15.2 MiB | 156.0 MiB |
| hybrid, read as phkv (rust) | 150.0 KiB | 1.5 MiB | 15.2 MiB | 156.0 MiB |

### First lookup of a store opened cold (open and one `raw`)

**local**

| format (reader) | 1,000 | 10,000 | 100,000 | 1,000,000 |
|---|---|---|---|---|
| vzip (python) | 3 req, 68.0 KiB, 3.36 ms | 3 req, 691.1 KiB, 31 ms | 3 req, 6.8 MiB, 360 ms | 3 req, 69.3 MiB, 4223 ms |
| vzip, paged (python) | 4 req, 68.3 KiB, 1.43 ms | 4 req, 69.5 KiB, 2.73 ms | 4 req, 81.4 KiB, 12 ms | 4 req, 199.8 KiB, 112 ms |
| phkv (rust) | 3 req, 37.8 KiB, 0.06 ms | 3 req, 68.6 KiB, 0.08 ms | 3 req, 68.6 KiB, 0.16 ms | 5 req, 285.6 KiB, 1.24 ms |
| hybrid, read as vzip (python) | 4 req, 68.3 KiB, 2.11 ms | 4 req, 69.5 KiB, 2.77 ms | 4 req, 81.4 KiB, 13 ms | 4 req, 199.8 KiB, 125 ms |
| hybrid, read as phkv (rust) | 4 req, 132.6 KiB, 0.07 ms | 5 req, 132.6 KiB, 0.13 ms | 5 req, 132.6 KiB, 0.17 ms | 7 req, 348.6 KiB, 1.17 ms |

**http 0 ms**

| format (reader) | 1,000 | 10,000 | 100,000 | 1,000,000 |
|---|---|---|---|---|
| vzip (python) | 3 req, 68.0 KiB, 5.60 ms | 3 req, 691.1 KiB, 29 ms | 3 req, 6.8 MiB, 381 ms | 3 req, 69.3 MiB, 4180 ms |
| vzip, paged (python) | 4 req, 68.3 KiB, 4.51 ms | 4 req, 69.5 KiB, 5.42 ms | 4 req, 81.4 KiB, 14 ms | 4 req, 199.8 KiB, 122 ms |
| phkv (rust) | 1 req, 33.3 KiB, 1.04 ms | 2 req, 64.6 KiB, 1.55 ms | 3 req, 68.6 KiB, 1.84 ms | 5 req, 285.6 KiB, 4.55 ms |
| hybrid, read as vzip (python) | 4 req, 68.3 KiB, 4.83 ms | 4 req, 69.5 KiB, 5.64 ms | 4 req, 81.4 KiB, 19 ms | 4 req, 199.8 KiB, 128 ms |
| hybrid, read as phkv (rust) | 4 req, 132.6 KiB, 2.15 ms | 5 req, 132.6 KiB, 2.65 ms | 5 req, 132.6 KiB, 2.46 ms | 7 req, 348.6 KiB, 5.06 ms |

**http 20 ms**

| format (reader) | 1,000 | 10,000 | 100,000 | 1,000,000 |
|---|---|---|---|---|
| vzip (python) | 3 req, 68.0 KiB, 68 ms | 3 req, 691.1 KiB, 99 ms | 3 req, 6.8 MiB, 434 ms | 3 req, 69.3 MiB, 4466 ms |
| vzip, paged (python) | 4 req, 68.3 KiB, 87 ms | 4 req, 69.5 KiB, 88 ms | 4 req, 81.4 KiB, 100 ms | 4 req, 199.8 KiB, 207 ms |
| phkv (rust) | 1 req, 33.3 KiB, 21 ms | 2 req, 64.6 KiB, 42 ms | 3 req, 68.6 KiB, 63 ms | 5 req, 285.6 KiB, 106 ms |
| hybrid, read as vzip (python) | 4 req, 68.3 KiB, 86 ms | 4 req, 69.5 KiB, 88 ms | 4 req, 81.4 KiB, 101 ms | 4 req, 199.8 KiB, 209 ms |
| hybrid, read as phkv (rust) | 4 req, 132.6 KiB, 83 ms | 5 req, 132.6 KiB, 105 ms | 5 req, 132.6 KiB, 105 ms | 7 req, 348.6 KiB, 149 ms |

### Later lookups of random keys in an opened store (per lookup, median)

**local**

| format (reader) | 1,000 | 10,000 | 100,000 | 1,000,000 |
|---|---|---|---|---|
| vzip (python) | 1.00 req, 8 B, 0.23 ms | 1.00 req, 8 B, 0.30 ms | 1.00 req, 8 B, 0.34 ms | 1.00 req, 9 B, 0.32 ms |
| vzip, paged (python) | 1.08 req, 334 B, 0.23 ms | 1.59 req, 2.4 KiB, 0.82 ms | 1.96 req, 3.9 KiB, 1.25 ms | 1.99 req, 4.0 KiB, 1.27 ms |
| phkv (rust) | 1.01 req, 544 B, 0.00 ms | 1.10 req, 918 B, 0.00 ms | 1.61 req, 2.9 KiB, 0.00 ms | 1.95 req, 4.3 KiB, 0.00 ms |
| hybrid, read as vzip (python) | 1.08 req, 334 B, 0.33 ms | 1.59 req, 2.4 KiB, 0.95 ms | 1.96 req, 3.9 KiB, 1.37 ms | 1.99 req, 4.0 KiB, 1.29 ms |
| hybrid, read as phkv (rust) | 1.01 req, 544 B, 0.00 ms | 1.10 req, 918 B, 0.00 ms | 1.63 req, 3.0 KiB, 0.00 ms | 1.95 req, 4.3 KiB, 0.00 ms |

**http 0 ms**

| format (reader) | 1,000 | 10,000 | 100,000 | 1,000,000 |
|---|---|---|---|---|
| vzip (python) | 1.00 req, 8 B, 0.92 ms | 1.00 req, 8 B, 0.96 ms | 1.00 req, 8 B, 1.09 ms | 1.00 req, 9 B, 1.02 ms |
| vzip, paged (python) | 1.08 req, 334 B, 0.70 ms | 1.59 req, 2.4 KiB, 2.09 ms | 1.96 req, 3.9 KiB, 2.57 ms | 1.99 req, 4.0 KiB, 2.66 ms |
| phkv (rust) | 0.00 req, 0 B, 0.00 ms | 1.02 req, 613 B, 0.14 ms | 1.59 req, 2.9 KiB, 0.28 ms | 1.95 req, 4.3 KiB, 0.29 ms |
| hybrid, read as vzip (python) | 1.08 req, 334 B, 1.10 ms | 1.59 req, 2.4 KiB, 2.32 ms | 1.96 req, 3.9 KiB, 2.74 ms | 1.99 req, 4.0 KiB, 2.78 ms |
| hybrid, read as phkv (rust) | 1.01 req, 544 B, 0.14 ms | 1.10 req, 918 B, 0.15 ms | 1.63 req, 3.0 KiB, 0.29 ms | 1.95 req, 4.3 KiB, 0.29 ms |

**http 20 ms**

| format (reader) | 1,000 | 10,000 | 100,000 | 1,000,000 |
|---|---|---|---|---|
| vzip (python) | 1.00 req, 8 B, 22 ms | 1.00 req, 8 B, 22 ms | 1.00 req, 8 B, 22 ms | 1.00 req, 9 B, 22 ms |
| vzip, paged (python) | 1.08 req, 334 B, 22 ms | 1.59 req, 2.4 KiB, 43 ms | 1.96 req, 3.9 KiB, 44 ms | 1.99 req, 4.0 KiB, 44 ms |
| phkv (rust) | 0.00 req, 0 B, 0.00 ms | 1.02 req, 613 B, 21 ms | 1.59 req, 2.9 KiB, 42 ms | 1.95 req, 4.3 KiB, 42 ms |
| hybrid, read as vzip (python) | 1.08 req, 334 B, 22 ms | 1.59 req, 2.4 KiB, 43 ms | 1.96 req, 3.9 KiB, 44 ms | 1.99 req, 4.0 KiB, 44 ms |
| hybrid, read as phkv (rust) | 1.01 req, 544 B, 21 ms | 1.10 req, 918 B, 21 ms | 1.63 req, 3.0 KiB, 41 ms | 1.95 req, 4.3 KiB, 42 ms |

### `list` of the 100 keys under one prefix, on a store opened cold

**local**

| format (reader) | 1,000 | 10,000 | 100,000 | 1,000,000 |
|---|---|---|---|---|
| vzip (python) | 2 req, 67.9 KiB, 3.05 ms | 2 req, 691.1 KiB, 28 ms | 2 req, 6.8 MiB, 355 ms | 2 req, 69.3 MiB, 4400 ms |
| vzip, paged (python) | 5 req, 76.3 KiB, 2.43 ms | 4 req, 73.5 KiB, 3.28 ms | 5 req, 89.5 KiB, 15 ms | 5 req, 207.9 KiB, 127 ms |
| phkv (rust) | 2 req, 8.0 KiB, 0.05 ms | 2 req, 8.0 KiB, 0.05 ms | 2 req, 8.0 KiB, 0.06 ms | 1 req, 4.0 KiB, 0.08 ms |
| hybrid, read as vzip (python) | 5 req, 76.3 KiB, 2.60 ms | 4 req, 73.5 KiB, 2.66 ms | 5 req, 89.5 KiB, 15 ms | 5 req, 207.9 KiB, 126 ms |
| hybrid, read as phkv (rust) | 2 req, 8.0 KiB, 0.05 ms | 2 req, 8.0 KiB, 0.05 ms | 2 req, 8.0 KiB, 0.05 ms | 1 req, 4.0 KiB, 0.08 ms |

**http 0 ms**

| format (reader) | 1,000 | 10,000 | 100,000 | 1,000,000 |
|---|---|---|---|---|
| vzip (python) | 2 req, 67.9 KiB, 5.19 ms | 2 req, 691.1 KiB, 34 ms | 2 req, 6.8 MiB, 372 ms | 2 req, 69.3 MiB, 4146 ms |
| vzip, paged (python) | 5 req, 76.3 KiB, 6.48 ms | 4 req, 73.5 KiB, 6.34 ms | 5 req, 89.5 KiB, 22 ms | 5 req, 207.9 KiB, 129 ms |
| phkv (rust) | 0 req, 0 B, 0.05 ms | 2 req, 8.0 KiB, 0.54 ms | 2 req, 8.0 KiB, 0.67 ms | 1 req, 4.0 KiB, 0.51 ms |
| hybrid, read as vzip (python) | 5 req, 76.3 KiB, 6.74 ms | 4 req, 73.5 KiB, 6.59 ms | 5 req, 89.5 KiB, 22 ms | 5 req, 207.9 KiB, 131 ms |
| hybrid, read as phkv (rust) | 2 req, 8.0 KiB, 0.57 ms | 2 req, 8.0 KiB, 0.49 ms | 2 req, 8.0 KiB, 0.77 ms | 1 req, 4.0 KiB, 0.52 ms |

**http 20 ms**

| format (reader) | 1,000 | 10,000 | 100,000 | 1,000,000 |
|---|---|---|---|---|
| vzip (python) | 2 req, 67.9 KiB, 46 ms | 2 req, 691.1 KiB, 73 ms | 2 req, 6.8 MiB, 406 ms | 2 req, 69.3 MiB, 4358 ms |
| vzip, paged (python) | 5 req, 76.3 KiB, 68 ms | 4 req, 73.5 KiB, 69 ms | 5 req, 89.5 KiB, 81 ms | 5 req, 207.9 KiB, 196 ms |
| phkv (rust) | 0 req, 0 B, 0.05 ms | 2 req, 8.0 KiB, 42 ms | 2 req, 8.0 KiB, 42 ms | 1 req, 4.0 KiB, 21 ms |
| hybrid, read as vzip (python) | 5 req, 76.3 KiB, 69 ms | 4 req, 73.5 KiB, 70 ms | 5 req, 89.5 KiB, 81 ms | 5 req, 207.9 KiB, 187 ms |
| hybrid, read as phkv (rust) | 2 req, 8.0 KiB, 41 ms | 2 req, 8.0 KiB, 42 ms | 2 req, 8.0 KiB, 42 ms | 1 req, 4.0 KiB, 21 ms |

## Workload `bytes`

### File size

| format (reader) | 1,000 | 10,000 | 100,000 |
|---|---|---|---|
| vzip (python) | 1.1 MiB | 10.7 MiB | 107.3 MiB |
| vzip, paged (python) | 1.1 MiB | 10.7 MiB | 107.4 MiB |
| phkv (rust) | 1.0 MiB | 10.1 MiB | 101.2 MiB |
| hybrid, read as vzip (python) | 1.1 MiB | 11.1 MiB | 111.2 MiB |
| hybrid, read as phkv (rust) | 1.1 MiB | 11.1 MiB | 111.2 MiB |

### First lookup of a store opened cold (open and one `get`)

**local**

| format (reader) | 1,000 | 10,000 | 100,000 |
|---|---|---|---|
| vzip (python) | 2 req, 65.0 KiB, 3.95 ms | 3 req, 575.4 KiB, 22 ms | 3 req, 5.6 MiB, 278 ms |
| vzip, paged (python) | 3 req, 69.0 KiB, 1.68 ms | 4 req, 70.3 KiB, 2.49 ms | 4 req, 80.4 KiB, 12 ms |
| phkv (rust) | 4 req, 69.1 KiB, 0.08 ms | 4 req, 69.1 KiB, 0.34 ms | 5 req, 165.4 KiB, 2.29 ms |
| hybrid, read as vzip (python) | 4 req, 69.2 KiB, 1.46 ms | 4 req, 70.3 KiB, 2.31 ms | 4 req, 80.3 KiB, 11 ms |
| hybrid, read as phkv (rust) | 5 req, 133.6 KiB, 0.08 ms | 6 req, 133.6 KiB, 0.10 ms | 6 req, 133.6 KiB, 0.16 ms |

**http 0 ms**

| format (reader) | 1,000 | 10,000 | 100,000 |
|---|---|---|---|
| vzip (python) | 2 req, 65.0 KiB, 4.78 ms | 3 req, 575.4 KiB, 28 ms | 3 req, 5.6 MiB, 291 ms |
| vzip, paged (python) | 3 req, 69.0 KiB, 4.55 ms | 4 req, 70.3 KiB, 7.31 ms | 4 req, 80.4 KiB, 13 ms |
| phkv (rust) | 3 req, 65.1 KiB, 1.91 ms | 3 req, 65.1 KiB, 2.15 ms | 5 req, 165.4 KiB, 5.40 ms |
| hybrid, read as vzip (python) | 4 req, 69.2 KiB, 5.72 ms | 4 req, 70.3 KiB, 5.60 ms | 4 req, 80.3 KiB, 16 ms |
| hybrid, read as phkv (rust) | 4 req, 129.6 KiB, 2.66 ms | 6 req, 133.6 KiB, 3.39 ms | 6 req, 133.6 KiB, 2.80 ms |

**http 20 ms**

| format (reader) | 1,000 | 10,000 | 100,000 |
|---|---|---|---|
| vzip (python) | 2 req, 65.0 KiB, 46 ms | 3 req, 575.4 KiB, 94 ms | 3 req, 5.6 MiB, 339 ms |
| vzip, paged (python) | 3 req, 69.0 KiB, 66 ms | 4 req, 70.3 KiB, 89 ms | 4 req, 80.4 KiB, 98 ms |
| phkv (rust) | 3 req, 65.1 KiB, 63 ms | 3 req, 65.1 KiB, 84 ms | 5 req, 165.4 KiB, 107 ms |
| hybrid, read as vzip (python) | 4 req, 69.2 KiB, 87 ms | 4 req, 70.3 KiB, 89 ms | 4 req, 80.3 KiB, 98 ms |
| hybrid, read as phkv (rust) | 4 req, 129.6 KiB, 84 ms | 6 req, 133.6 KiB, 126 ms | 6 req, 133.6 KiB, 126 ms |

### Later lookups of random keys in an opened store (per lookup, median)

**local**

| format (reader) | 1,000 | 10,000 | 100,000 |
|---|---|---|---|
| vzip (python) | 0.99 req, 1014 B, 0.33 ms | 1.00 req, 1.0 KiB, 0.28 ms | 1.00 req, 1.0 KiB, 0.29 ms |
| vzip, paged (python) | 1.06 req, 1.3 KiB, 0.32 ms | 1.55 req, 3.2 KiB, 0.89 ms | 1.95 req, 4.8 KiB, 1.34 ms |
| phkv (rust) | 2.01 req, 1.1 KiB, 0.00 ms | 2.10 req, 1.4 KiB, 0.00 ms | 2.61 req, 3.5 KiB, 0.00 ms |
| hybrid, read as vzip (python) | 1.07 req, 1.3 KiB, 0.33 ms | 1.54 req, 3.2 KiB, 1.02 ms | 1.95 req, 4.8 KiB, 1.30 ms |
| hybrid, read as phkv (rust) | 2.01 req, 1.5 KiB, 0.00 ms | 2.10 req, 1.9 KiB, 0.00 ms | 2.63 req, 4.0 KiB, 0.01 ms |

**http 0 ms**

| format (reader) | 1,000 | 10,000 | 100,000 |
|---|---|---|---|
| vzip (python) | 0.99 req, 1014 B, 1.19 ms | 1.00 req, 1.0 KiB, 1.12 ms | 1.00 req, 1.0 KiB, 1.29 ms |
| vzip, paged (python) | 1.06 req, 1.3 KiB, 1.24 ms | 1.55 req, 3.2 KiB, 2.02 ms | 1.95 req, 4.8 KiB, 2.70 ms |
| phkv (rust) | 1.89 req, 992 B, 0.29 ms | 2.04 req, 1.2 KiB, 0.28 ms | 2.61 req, 3.5 KiB, 0.43 ms |
| hybrid, read as vzip (python) | 1.07 req, 1.3 KiB, 1.04 ms | 1.54 req, 3.2 KiB, 2.49 ms | 1.95 req, 4.8 KiB, 2.91 ms |
| hybrid, read as phkv (rust) | 2.00 req, 1.5 KiB, 0.28 ms | 2.10 req, 1.9 KiB, 0.32 ms | 2.63 req, 4.0 KiB, 0.42 ms |

**http 20 ms**

| format (reader) | 1,000 | 10,000 | 100,000 |
|---|---|---|---|
| vzip (python) | 0.99 req, 1014 B, 22 ms | 1.00 req, 1.0 KiB, 22 ms | 1.00 req, 1.0 KiB, 22 ms |
| vzip, paged (python) | 1.06 req, 1.3 KiB, 22 ms | 1.55 req, 3.2 KiB, 43 ms | 1.95 req, 4.8 KiB, 44 ms |
| phkv (rust) | 1.89 req, 992 B, 42 ms | 2.04 req, 1.2 KiB, 42 ms | 2.61 req, 3.5 KiB, 62 ms |
| hybrid, read as vzip (python) | 1.07 req, 1.3 KiB, 22 ms | 1.54 req, 3.2 KiB, 44 ms | 1.95 req, 4.8 KiB, 44 ms |
| hybrid, read as phkv (rust) | 2.00 req, 1.5 KiB, 42 ms | 2.10 req, 1.9 KiB, 42 ms | 2.63 req, 4.0 KiB, 62 ms |

### `list` of the 100 keys under one prefix, on a store opened cold

**local**

| format (reader) | 1,000 | 10,000 | 100,000 |
|---|---|---|---|
| vzip (python) | 1 req, 64.0 KiB, 2.60 ms | 2 req, 574.4 KiB, 24 ms | 2 req, 5.6 MiB, 273 ms |
| vzip, paged (python) | 4 req, 76.0 KiB, 2.51 ms | 4 req, 73.3 KiB, 2.63 ms | 4 req, 83.4 KiB, 12 ms |
| phkv (rust) | 26 req, 106.6 KiB, 0.08 ms | 26 req, 106.7 KiB, 0.07 ms | 26 req, 106.8 KiB, 0.22 ms |
| hybrid, read as vzip (python) | 5 req, 76.3 KiB, 3.07 ms | 4 req, 73.3 KiB, 2.85 ms | 4 req, 83.3 KiB, 11 ms |
| hybrid, read as phkv (rust) | 2 req, 8.0 KiB, 0.04 ms | 2 req, 8.0 KiB, 0.04 ms | 2 req, 8.0 KiB, 0.04 ms |

**http 0 ms**

| format (reader) | 1,000 | 10,000 | 100,000 |
|---|---|---|---|
| vzip (python) | 1 req, 64.0 KiB, 3.85 ms | 2 req, 574.4 KiB, 26 ms | 2 req, 5.6 MiB, 273 ms |
| vzip, paged (python) | 4 req, 76.0 KiB, 6.26 ms | 4 req, 73.3 KiB, 7.88 ms | 4 req, 83.4 KiB, 15 ms |
| phkv (rust) | 26 req, 106.6 KiB, 4.72 ms | 26 req, 106.7 KiB, 4.73 ms | 26 req, 106.8 KiB, 6.30 ms |
| hybrid, read as vzip (python) | 5 req, 76.3 KiB, 6.45 ms | 4 req, 73.3 KiB, 5.84 ms | 4 req, 83.3 KiB, 15 ms |
| hybrid, read as phkv (rust) | 2 req, 8.0 KiB, 0.56 ms | 2 req, 8.0 KiB, 0.65 ms | 2 req, 8.0 KiB, 0.46 ms |

**http 20 ms**

| format (reader) | 1,000 | 10,000 | 100,000 |
|---|---|---|---|
| vzip (python) | 1 req, 64.0 KiB, 25 ms | 2 req, 574.4 KiB, 68 ms | 2 req, 5.6 MiB, 309 ms |
| vzip, paged (python) | 4 req, 76.0 KiB, 48 ms | 4 req, 73.3 KiB, 68 ms | 4 req, 83.4 KiB, 76 ms |
| phkv (rust) | 26 req, 106.6 KiB, 540 ms | 26 req, 106.7 KiB, 543 ms | 26 req, 106.8 KiB, 541 ms |
| hybrid, read as vzip (python) | 5 req, 76.3 KiB, 68 ms | 4 req, 73.3 KiB, 70 ms | 4 req, 83.3 KiB, 77 ms |
| hybrid, read as phkv (rust) | 2 req, 8.0 KiB, 42 ms | 2 req, 8.0 KiB, 42 ms | 2 req, 8.0 KiB, 42 ms |

