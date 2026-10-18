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
  (`slot_block`; it was one 8-byte slot per lookup); the first read of the file is 512 KiB + 64 B (`tail`;
  it was 64 KiB + 64 B); the first read of a record is 4.5 KiB (`record_read`; it was 512 B), which holds a
  record whose value is inline at the writer's default `inline_max` of 4 KiB;
- the whole blocks of SLOTS that the first read already holds are kept as if a lookup had read them, and
  a record's first read stops at the end of its page, since a page holds whole records;
- a format change: a slot holds its record's length as well as its offset
  ([§18.2](../../../spec/bindings/phkv.md#182-slots-section)). The offset is from the start of RECORDS, in as
  many low bits as the RECORDS section's length needs (25 at 10^6 references), and the length takes the rest
  (39 bits there), or is 0 for a record too long for them. A record is therefore read in one read of exactly
  its length, and `record_read` applies only to a record whose slot gives no length, which takes a RECORDS
  section of about a terabyte and a record of megabytes.

## Paged vzip against phkv

Later lookups are random keys on an opened store, timed over 200 of them and counted over 5,000, by which
time the caches have filled. vzip's pages and phkv's slot blocks are both 4 KiB, but a block holds 512 slots
and a page about 50 central directory records, so phkv's cache fills about 7 times sooner.

#### 100,000 keys, `refs`: over HTTP, 20 ms per request

| reader | open + first lookup | later lookup (200 lookups) | later lookup (5,000 lookups, counted) |
|---|---|---|---|
| vzip, paged | 4 req, 81.4 KiB, 105 ms | 1.96 req, 3.8 KiB, 43 ms | 1.33 req, 1.3 KiB |
| phkv, the first defaults (slots one at a time, 64 KiB first read, 512 B record read) | 3 req, 64.1 KiB, 64 ms | 2.00 req, 36 B, 42 ms | 2.00 req, 36 B |
| phkv, 4 KiB slot blocks only | 3 req, 68.1 KiB, 63 ms | 1.59 req, 2.4 KiB, 33 ms | 1.04 req, 185 B |
| phkv, defaults (4 KiB slot blocks, 512 KiB first read, 4.5 KiB record read) | 3 req, 516.1 KiB, 65 ms | 1.25 req, 1.0 KiB, 26 ms | 1.02 req, 94 B |

#### 1,000,000 keys, `refs`: over HTTP, 20 ms per request

| reader | open + first lookup | later lookup (200 lookups) | later lookup (5,000 lookups, counted) |
|---|---|---|---|
| vzip, paged | 4 req, 199.8 KiB, 209 ms | 1.99 req, 4.0 KiB, 44 ms | 1.87 req, 3.5 KiB |
| phkv, the first defaults (slots one at a time, 64 KiB first read, 512 B record read) | 5 req, 281.1 KiB, 107 ms | 2.00 req, 38 B, 42 ms | 2.00 req, 38 B |
| phkv, 4 KiB slot blocks only | 5 req, 285.1 KiB, 107 ms | 1.95 req, 3.8 KiB, 41 ms | 1.37 req, 1.5 KiB |
| phkv, defaults (4 KiB slot blocks, 512 KiB first read, 4.5 KiB record read) | 3 req, 516.1 KiB, 66 ms | 1.93 req, 3.7 KiB, 40 ms | 1.36 req, 1.5 KiB |

#### 100,000 keys, `bytes`: over HTTP, 20 ms per request

| reader | open + first lookup | later lookup (200 lookups) | later lookup (5,000 lookups, counted) |
|---|---|---|---|
| vzip, paged | 4 req, 80.4 KiB, 97 ms | 1.95 req, 4.8 KiB, 43 ms | 1.28 req, 2.1 KiB |
| phkv, the first defaults (slots one at a time, 64 KiB first read, 512 B record read) | 4 req, 161.4 KiB, 87 ms | 2.00 req, 1.0 KiB, 42 ms | 2.00 req, 1.0 KiB |
| phkv, 4 KiB slot blocks only | 4 req, 165.4 KiB, 87 ms | 1.61 req, 3.5 KiB, 34 ms | 1.04 req, 1.2 KiB |
| phkv, defaults (4 KiB slot blocks, 512 KiB first read, 4.5 KiB record read) | 3 req, 517.1 KiB, 67 ms | 1.34 req, 2.4 KiB, 28 ms | 1.02 req, 1.1 KiB |

What it shows, with the defaults:

- **Every operation takes fewer requests with phkv.** Open and first lookup: 3 against 4. Later lookups over
  200: 1.25, 1.93 and 1.34 against 1.96, 1.99 and 1.95; over 5,000: 1.02, 1.36 and 1.02 against 1.33, 1.87
  and 1.28. At 10^5 keys the first read holds most of SLOTS, so most lookups read no slot at all.
- **Every lookup reads fewer bytes with phkv**: 94 B to 3.7 KiB against 1.3 to 4.8 KiB. A record is read in
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

Over HTTP with 20 ms per request (`sweep.py`), the other parameters at their defaults.

**Reader `Limits::tail`**, the bytes read first, at 10^6 references, open and one lookup:

| tail | 64.1 KiB | 128.0 KiB | 256.0 KiB | 512.1 KiB | 1.0 MiB |
|---|---|---|---|---|---|
| standalone | 5 req, 285.1 KiB, 106 ms | 5 req, 285.1 KiB, 107 ms | 4 req, 285.1 KiB, 86 ms | 3 req, 516.1 KiB, 65 ms | 3 req, 1.0 MiB, 66 ms |
| hybrid | 7 req, 348.2 KiB, 149 ms | 7 req, 412.1 KiB, 149 ms | 6 req, 540.1 KiB, 128 ms | 5 req, 1.0 MiB, 108 ms | 5 req, 2.0 MiB, 110 ms |

512 KiB takes the 10^6-key open from 5 requests to 3, because PTRHASH and PAGES (about 280 KiB) come with it.
For a hybrid store the first read is made twice (the end of the file, and the window before the superblock),
so its bytes double.

**Reader `Limits::record_read`**, the bytes read at a record first, 10^5 keys of 1 KiB inline values:

| record_read | 512 B | 1.0 KiB | 2.0 KiB | 4.5 KiB |
|---|---|---|---|---|
| later lookup | 1.34 req, 2.4 KiB, 21 ms | 1.34 req, 2.4 KiB, 21 ms | 1.34 req, 2.4 KiB, 21 ms | 1.34 req, 2.4 KiB, 21 ms |

Now that a slot gives its record's length, `record_read` changes nothing here: every record is read in one
read of its length. It applies only to a record whose slot gives no length.

**Writer `page_size` and `inline_max`**, 10^5 keys of 1 KiB values (the writer's, recorded in the file):

| page_size, inline_max | file | later lookup (record_read 512) | later lookup (record_read 4.5 KiB) | list of 100 keys |
|---|---|---|---|---|
| 4.0 KiB, 4.0 KiB | 101.2 MiB | 1.34 req, 2.4 KiB, 21 ms | 1.34 req, 2.4 KiB, 21 ms | 26 req, 106.8 KiB, 545 ms |
| 16.0 KiB, 4.0 KiB | 101.1 MiB | 1.27 req, 2.1 KiB, 21 ms | 1.27 req, 2.1 KiB, 21 ms | 7 req, 115.1 KiB, 146 ms |
| 64.0 KiB, 4.0 KiB | 101.1 MiB | 1.25 req, 2.0 KiB, 21 ms | 1.25 req, 2.0 KiB, 21 ms | 3 req, 194.1 KiB, 63 ms |
| 4.0 KiB, none | 101.5 MiB | 2.25 req, 2.1 KiB, 42 ms | 2.25 req, 2.1 KiB, 42 ms | 2 req, 8.0 KiB, 41 ms |

A larger `page_size` cuts the `list` requests (26 to 3 at 64 KiB) by reading more bytes per page; lookups do
not read pages. Values not inline (`inline_max` 0) make records small, so a `list` is 2 requests, but a lookup
reads the value too.

**Reader `Limits::slots_at_open`**, the longest SLOTS section read whole at open and kept (default 0, off), with a
bandwidth per connection. Total time for k lookups of random keys on a cold store (the first lookup, and k − 1
later ones at their mean time):

| keys, workload | bandwidth | slots_at_open | open + first lookup | later lookup | k = 1 | k = 10 | k = 100 | k = 1,000 |
|---|---|---|---|---|---|---|---|---|
| 100,000 references | none | off | 3 req, 516.1 KiB, 64 ms | 1.25 req, 26 ms | 64 ms | 301 ms | 2666 ms | 26316 ms |
| 100,000 references | none | whole | 3 req, 828.8 KiB, 66 ms | 1.00 req, 21 ms | 66 ms | 253 ms | 2129 ms | 20883 ms |
| 100,000 references | 100 MB/s | off | 3 req, 516.1 KiB, 70 ms | 1.25 req, 26 ms | 70 ms | 305 ms | 2662 ms | 26229 ms |
| 100,000 references | 100 MB/s | whole | 3 req, 828.8 KiB, 75 ms | 1.00 req, 21 ms | 75 ms | 262 ms | 2137 ms | 20878 ms |
| 100,000 references | 12.5 MB/s | off | 3 req, 516.1 KiB, 106 ms | 1.25 req, 26 ms | 106 ms | 342 ms | 2699 ms | 26265 ms |
| 100,000 references | 12.5 MB/s | whole | 3 req, 828.8 KiB, 135 ms | 1.00 req, 21 ms | 135 ms | 322 ms | 2196 ms | 20938 ms |
| 1,000,000 references | none | off | 3 req, 516.1 KiB, 65 ms | 1.93 req, 40 ms | 65 ms | 427 ms | 4048 ms | 40264 ms |
| 1,000,000 references | none | whole | 3 req, 8.1 MiB, 75 ms | 1.00 req, 21 ms | 75 ms | 262 ms | 2136 ms | 20868 ms |
| 1,000,000 references | 100 MB/s | off | 3 req, 516.1 KiB, 70 ms | 1.93 req, 40 ms | 70 ms | 433 ms | 4060 ms | 40327 ms |
| 1,000,000 references | 100 MB/s | whole | 3 req, 8.1 MiB, 161 ms | 1.00 req, 21 ms | 161 ms | 350 ms | 2237 ms | 21116 ms |
| 1,000,000 references | 12.5 MB/s | off | 3 req, 516.1 KiB, 109 ms | 1.93 req, 41 ms | 109 ms | 474 ms | 4121 ms | 40591 ms |
| 1,000,000 references | 12.5 MB/s | whole | 3 req, 8.1 MiB, 759 ms | 1.00 req, 21 ms | 759 ms | 947 ms | 2826 ms | 21612 ms |
| 100,000 keys of 1 KiB values | none | off | 3 req, 517.1 KiB, 67 ms | 1.34 req, 28 ms | 67 ms | 321 ms | 2860 ms | 28251 ms |
| 100,000 keys of 1 KiB values | none | whole | 3 req, 960.4 KiB, 68 ms | 1.00 req, 21 ms | 68 ms | 257 ms | 2143 ms | 21008 ms |
| 100,000 keys of 1 KiB values | 100 MB/s | off | 3 req, 517.1 KiB, 72 ms | 1.34 req, 28 ms | 72 ms | 325 ms | 2854 ms | 28141 ms |
| 100,000 keys of 1 KiB values | 100 MB/s | whole | 3 req, 960.4 KiB, 78 ms | 1.00 req, 21 ms | 78 ms | 265 ms | 2143 ms | 20918 ms |
| 100,000 keys of 1 KiB values | 12.5 MB/s | off | 3 req, 517.1 KiB, 111 ms | 1.34 req, 28 ms | 111 ms | 365 ms | 2909 ms | 28348 ms |
| 100,000 keys of 1 KiB values | 12.5 MB/s | whole | 3 req, 960.4 KiB, 146 ms | 1.00 req, 21 ms | 146 ms | 335 ms | 2222 ms | 21092 ms |

Reading SLOTS at open (8.2 bytes per key, held in memory) makes every lookup one request, the record; with slot
blocks a later lookup is 1.25 to 1.93. SLOTS is read in the same request as PAGES and PTRHASH, which lie after
it, so the open takes no more requests (3 either way). What it costs is bytes at open: 8.1 MiB at 10^6 keys,
0.3 to 0.4 MiB more at 10^5. With no bandwidth limit the first lookup costs 1 to 11 ms more. At 100 MB/s it
costs 90 ms more at 10^6 keys, which about 5 lookups pay back (each saves about 19 ms); at 12.5 MB/s (100 Mbit/s)
it costs 653 ms more, which about 33 lookups pay back. At 10^5 keys the first read already holds most of SLOTS,
so a later lookup saves only 5 to 7 ms: at 12.5 MB/s the extra 27 to 38 ms pays back after about 6 lookups. A
store opened to read a few keys is better off without it; one opened to read many, with it, which is why it
stays off by default.

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
| phkv (rust) | 33.3 KiB | 344.1 KiB | 3.5 MiB | 36.4 MiB |
| hybrid, read as vzip (python) | 150.0 KiB | 1.5 MiB | 15.2 MiB | 156.0 MiB |
| hybrid, read as phkv (rust) | 150.0 KiB | 1.5 MiB | 15.2 MiB | 156.0 MiB |

### First lookup of a store opened cold (open and one `raw`)

**local**

| format (reader) | 1,000 | 10,000 | 100,000 | 1,000,000 |
|---|---|---|---|---|
| vzip (python) | 3 req, 68.0 KiB, 3.47 ms | 3 req, 691.1 KiB, 34 ms | 3 req, 6.8 MiB, 426 ms | 3 req, 69.3 MiB, 4399 ms |
| vzip, paged (python) | 4 req, 68.3 KiB, 1.92 ms | 4 req, 69.5 KiB, 2.79 ms | 4 req, 81.4 KiB, 12 ms | 4 req, 199.8 KiB, 118 ms |
| phkv (rust) | 2 req, 33.4 KiB, 0.11 ms | 2 req, 344.2 KiB, 0.15 ms | 3 req, 516.1 KiB, 0.23 ms | 3 req, 516.1 KiB, 1.45 ms |
| hybrid, read as vzip (python) | 4 req, 68.3 KiB, 1.93 ms | 4 req, 69.5 KiB, 2.84 ms | 4 req, 81.4 KiB, 13 ms | 4 req, 199.8 KiB, 122 ms |
| hybrid, read as phkv (rust) | 2 req, 150.0 KiB, 0.07 ms | 3 req, 1.0 MiB, 0.44 ms | 5 req, 1.0 MiB, 0.65 ms | 5 req, 1.0 MiB, 2.01 ms |

**http 0 ms**

| format (reader) | 1,000 | 10,000 | 100,000 | 1,000,000 |
|---|---|---|---|---|
| vzip (python) | 3 req, 68.0 KiB, 6.59 ms | 3 req, 691.1 KiB, 42 ms | 3 req, 6.8 MiB, 377 ms | 3 req, 69.3 MiB, 4245 ms |
| vzip, paged (python) | 4 req, 68.3 KiB, 5.60 ms | 4 req, 69.5 KiB, 6.29 ms | 4 req, 81.4 KiB, 17 ms | 4 req, 199.8 KiB, 125 ms |
| phkv (rust) | 2 req, 33.4 KiB, 1.28 ms | 2 req, 344.2 KiB, 2.90 ms | 3 req, 516.1 KiB, 3.17 ms | 3 req, 516.1 KiB, 3.87 ms |
| hybrid, read as vzip (python) | 4 req, 68.3 KiB, 5.15 ms | 4 req, 69.5 KiB, 5.59 ms | 4 req, 81.4 KiB, 16 ms | 4 req, 199.8 KiB, 120 ms |
| hybrid, read as phkv (rust) | 2 req, 150.0 KiB, 1.57 ms | 3 req, 1.0 MiB, 3.70 ms | 5 req, 1.0 MiB, 3.36 ms | 5 req, 1.0 MiB, 5.92 ms |

**http 20 ms**

| format (reader) | 1,000 | 10,000 | 100,000 | 1,000,000 |
|---|---|---|---|---|
| vzip (python) | 3 req, 68.0 KiB, 68 ms | 3 req, 691.1 KiB, 100 ms | 3 req, 6.8 MiB, 458 ms | 3 req, 69.3 MiB, 4531 ms |
| vzip, paged (python) | 4 req, 68.3 KiB, 89 ms | 4 req, 69.5 KiB, 91 ms | 4 req, 81.4 KiB, 100 ms | 4 req, 199.8 KiB, 222 ms |
| phkv (rust) | 2 req, 33.4 KiB, 42 ms | 2 req, 344.2 KiB, 43 ms | 3 req, 516.1 KiB, 65 ms | 3 req, 516.1 KiB, 66 ms |
| hybrid, read as vzip (python) | 4 req, 68.3 KiB, 87 ms | 4 req, 69.5 KiB, 90 ms | 4 req, 81.4 KiB, 100 ms | 4 req, 199.8 KiB, 210 ms |
| hybrid, read as phkv (rust) | 2 req, 150.0 KiB, 43 ms | 3 req, 1.0 MiB, 65 ms | 5 req, 1.0 MiB, 107 ms | 5 req, 1.0 MiB, 109 ms |

### Later lookups of random keys in an opened store (per lookup, median)

**local**

| format (reader) | 1,000 | 10,000 | 100,000 | 1,000,000 |
|---|---|---|---|---|
| vzip (python) | 1.00 req, 8 B, 0.28 ms | 1.00 req, 8 B, 0.31 ms | 1.00 req, 8 B, 0.31 ms | 1.00 req, 9 B, 0.35 ms |
| vzip, paged (python) | 1.08 req, 334 B, 0.30 ms | 1.59 req, 2.4 KiB, 1.03 ms | 1.96 req, 3.9 KiB, 1.43 ms | 1.99 req, 4.0 KiB, 1.36 ms |
| phkv (rust) | 1.00 req, 25 B, 0.00 ms | 1.00 req, 27 B, 0.00 ms | 1.25 req, 1.0 KiB, 0.00 ms | 1.93 req, 3.7 KiB, 0.00 ms |
| hybrid, read as vzip (python) | 1.08 req, 334 B, 0.37 ms | 1.59 req, 2.4 KiB, 1.04 ms | 1.96 req, 3.9 KiB, 1.20 ms | 1.99 req, 4.0 KiB, 1.34 ms |
| hybrid, read as phkv (rust) | 1.00 req, 25 B, 0.00 ms | 1.00 req, 27 B, 0.00 ms | 1.26 req, 1.1 KiB, 0.00 ms | 1.93 req, 3.7 KiB, 0.00 ms |

**http 0 ms**

| format (reader) | 1,000 | 10,000 | 100,000 | 1,000,000 |
|---|---|---|---|---|
| vzip (python) | 1.00 req, 8 B, 1.09 ms | 1.00 req, 8 B, 1.05 ms | 1.00 req, 8 B, 0.99 ms | 1.00 req, 9 B, 1.10 ms |
| vzip, paged (python) | 1.08 req, 334 B, 1.01 ms | 1.59 req, 2.4 KiB, 2.40 ms | 1.96 req, 3.9 KiB, 2.92 ms | 1.99 req, 4.0 KiB, 2.88 ms |
| phkv (rust) | 1.00 req, 25 B, 0.16 ms | 1.00 req, 27 B, 0.18 ms | 1.25 req, 1.0 KiB, 0.16 ms | 1.93 req, 3.7 KiB, 0.29 ms |
| hybrid, read as vzip (python) | 1.08 req, 334 B, 1.22 ms | 1.59 req, 2.4 KiB, 2.48 ms | 1.96 req, 3.9 KiB, 2.65 ms | 1.99 req, 4.0 KiB, 2.67 ms |
| hybrid, read as phkv (rust) | 1.00 req, 25 B, 0.17 ms | 1.00 req, 27 B, 0.14 ms | 1.26 req, 1.1 KiB, 0.18 ms | 1.93 req, 3.7 KiB, 0.30 ms |

**http 20 ms**

| format (reader) | 1,000 | 10,000 | 100,000 | 1,000,000 |
|---|---|---|---|---|
| vzip (python) | 1.00 req, 8 B, 22 ms | 1.00 req, 8 B, 22 ms | 1.00 req, 8 B, 22 ms | 1.00 req, 9 B, 22 ms |
| vzip, paged (python) | 1.08 req, 334 B, 22 ms | 1.59 req, 2.4 KiB, 44 ms | 1.96 req, 3.9 KiB, 45 ms | 1.99 req, 4.0 KiB, 44 ms |
| phkv (rust) | 1.00 req, 25 B, 21 ms | 1.00 req, 27 B, 21 ms | 1.25 req, 1.0 KiB, 21 ms | 1.93 req, 3.7 KiB, 42 ms |
| hybrid, read as vzip (python) | 1.08 req, 334 B, 22 ms | 1.59 req, 2.4 KiB, 44 ms | 1.96 req, 3.9 KiB, 44 ms | 1.99 req, 4.0 KiB, 44 ms |
| hybrid, read as phkv (rust) | 1.00 req, 25 B, 21 ms | 1.00 req, 27 B, 21 ms | 1.26 req, 1.1 KiB, 21 ms | 1.93 req, 3.7 KiB, 42 ms |

### `list` of the 100 keys under one prefix, on a store opened cold

**local**

| format (reader) | 1,000 | 10,000 | 100,000 | 1,000,000 |
|---|---|---|---|---|
| vzip (python) | 2 req, 67.9 KiB, 3.20 ms | 2 req, 691.1 KiB, 32 ms | 2 req, 6.8 MiB, 346 ms | 2 req, 69.3 MiB, 4375 ms |
| vzip, paged (python) | 5 req, 76.3 KiB, 3.04 ms | 4 req, 73.5 KiB, 3.95 ms | 5 req, 89.5 KiB, 14 ms | 5 req, 207.9 KiB, 113 ms |
| phkv (rust) | 2 req, 8.0 KiB, 0.05 ms | 2 req, 8.0 KiB, 0.05 ms | 2 req, 8.0 KiB, 0.05 ms | 1 req, 4.0 KiB, 0.09 ms |
| hybrid, read as vzip (python) | 5 req, 76.3 KiB, 2.80 ms | 4 req, 73.5 KiB, 3.39 ms | 5 req, 89.5 KiB, 13 ms | 5 req, 207.9 KiB, 116 ms |
| hybrid, read as phkv (rust) | 2 req, 8.0 KiB, 0.05 ms | 2 req, 8.0 KiB, 0.05 ms | 2 req, 8.0 KiB, 0.05 ms | 1 req, 4.0 KiB, 0.08 ms |

**http 0 ms**

| format (reader) | 1,000 | 10,000 | 100,000 | 1,000,000 |
|---|---|---|---|---|
| vzip (python) | 2 req, 67.9 KiB, 5.68 ms | 2 req, 691.1 KiB, 34 ms | 2 req, 6.8 MiB, 398 ms | 2 req, 69.3 MiB, 4203 ms |
| vzip, paged (python) | 5 req, 76.3 KiB, 8.78 ms | 4 req, 73.5 KiB, 8.87 ms | 5 req, 89.5 KiB, 19 ms | 5 req, 207.9 KiB, 135 ms |
| phkv (rust) | 2 req, 8.0 KiB, 0.93 ms | 2 req, 8.0 KiB, 0.70 ms | 2 req, 8.0 KiB, 0.75 ms | 1 req, 4.0 KiB, 0.58 ms |
| hybrid, read as vzip (python) | 5 req, 76.3 KiB, 7.49 ms | 4 req, 73.5 KiB, 6.21 ms | 5 req, 89.5 KiB, 21 ms | 5 req, 207.9 KiB, 119 ms |
| hybrid, read as phkv (rust) | 2 req, 8.0 KiB, 0.81 ms | 2 req, 8.0 KiB, 0.81 ms | 2 req, 8.0 KiB, 0.77 ms | 1 req, 4.0 KiB, 0.53 ms |

**http 20 ms**

| format (reader) | 1,000 | 10,000 | 100,000 | 1,000,000 |
|---|---|---|---|---|
| vzip (python) | 2 req, 67.9 KiB, 47 ms | 2 req, 691.1 KiB, 77 ms | 2 req, 6.8 MiB, 411 ms | 2 req, 69.3 MiB, 4306 ms |
| vzip, paged (python) | 5 req, 76.3 KiB, 71 ms | 4 req, 73.5 KiB, 71 ms | 5 req, 89.5 KiB, 86 ms | 5 req, 207.9 KiB, 187 ms |
| phkv (rust) | 2 req, 8.0 KiB, 42 ms | 2 req, 8.0 KiB, 42 ms | 2 req, 8.0 KiB, 42 ms | 1 req, 4.0 KiB, 21 ms |
| hybrid, read as vzip (python) | 5 req, 76.3 KiB, 71 ms | 4 req, 73.5 KiB, 69 ms | 5 req, 89.5 KiB, 81 ms | 5 req, 207.9 KiB, 212 ms |
| hybrid, read as phkv (rust) | 2 req, 8.0 KiB, 42 ms | 2 req, 8.0 KiB, 42 ms | 2 req, 8.0 KiB, 42 ms | 1 req, 4.0 KiB, 21 ms |

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
| vzip (python) | 2 req, 65.0 KiB, 3.01 ms | 3 req, 575.4 KiB, 22 ms | 3 req, 5.6 MiB, 272 ms |
| vzip, paged (python) | 3 req, 69.0 KiB, 1.45 ms | 4 req, 70.3 KiB, 2.48 ms | 4 req, 80.4 KiB, 13 ms |
| phkv (rust) | 2 req, 513.1 KiB, 0.12 ms | 2 req, 513.1 KiB, 0.27 ms | 3 req, 517.1 KiB, 1.64 ms |
| hybrid, read as vzip (python) | 4 req, 69.2 KiB, 1.82 ms | 4 req, 70.3 KiB, 3.04 ms | 4 req, 80.3 KiB, 11 ms |
| hybrid, read as phkv (rust) | 4 req, 1.0 MiB, 0.55 ms | 4 req, 1.0 MiB, 0.46 ms | 6 req, 1.0 MiB, 0.65 ms |

**http 0 ms**

| format (reader) | 1,000 | 10,000 | 100,000 |
|---|---|---|---|
| vzip (python) | 2 req, 65.0 KiB, 4.58 ms | 3 req, 575.4 KiB, 33 ms | 3 req, 5.6 MiB, 263 ms |
| vzip, paged (python) | 3 req, 69.0 KiB, 3.93 ms | 4 req, 70.3 KiB, 6.91 ms | 4 req, 80.4 KiB, 15 ms |
| phkv (rust) | 2 req, 513.1 KiB, 1.80 ms | 2 req, 513.1 KiB, 2.53 ms | 3 req, 517.1 KiB, 5.80 ms |
| hybrid, read as vzip (python) | 4 req, 69.2 KiB, 5.18 ms | 4 req, 70.3 KiB, 5.85 ms | 4 req, 80.3 KiB, 15 ms |
| hybrid, read as phkv (rust) | 4 req, 1.0 MiB, 3.63 ms | 4 req, 1.0 MiB, 5.44 ms | 6 req, 1.0 MiB, 6.50 ms |

**http 20 ms**

| format (reader) | 1,000 | 10,000 | 100,000 |
|---|---|---|---|
| vzip (python) | 2 req, 65.0 KiB, 46 ms | 3 req, 575.4 KiB, 95 ms | 3 req, 5.6 MiB, 353 ms |
| vzip, paged (python) | 3 req, 69.0 KiB, 66 ms | 4 req, 70.3 KiB, 88 ms | 4 req, 80.4 KiB, 98 ms |
| phkv (rust) | 2 req, 513.1 KiB, 43 ms | 2 req, 513.1 KiB, 44 ms | 3 req, 517.1 KiB, 66 ms |
| hybrid, read as vzip (python) | 4 req, 69.2 KiB, 87 ms | 4 req, 70.3 KiB, 90 ms | 4 req, 80.3 KiB, 98 ms |
| hybrid, read as phkv (rust) | 4 req, 1.0 MiB, 86 ms | 4 req, 1.0 MiB, 86 ms | 6 req, 1.0 MiB, 127 ms |

### Later lookups of random keys in an opened store (per lookup, median)

**local**

| format (reader) | 1,000 | 10,000 | 100,000 |
|---|---|---|---|
| vzip (python) | 0.99 req, 1014 B, 0.31 ms | 1.00 req, 1.0 KiB, 0.33 ms | 1.00 req, 1.0 KiB, 0.37 ms |
| vzip, paged (python) | 1.06 req, 1.3 KiB, 0.33 ms | 1.55 req, 3.2 KiB, 0.93 ms | 1.95 req, 4.8 KiB, 1.46 ms |
| phkv (rust) | 1.00 req, 1.0 KiB, 0.00 ms | 1.00 req, 1.0 KiB, 0.00 ms | 1.34 req, 2.4 KiB, 0.00 ms |
| hybrid, read as vzip (python) | 1.07 req, 1.3 KiB, 0.33 ms | 1.54 req, 3.2 KiB, 0.85 ms | 1.95 req, 4.8 KiB, 1.42 ms |
| hybrid, read as phkv (rust) | 2.00 req, 1.0 KiB, 0.00 ms | 2.00 req, 1.0 KiB, 0.00 ms | 2.27 req, 2.1 KiB, 0.00 ms |

**http 0 ms**

| format (reader) | 1,000 | 10,000 | 100,000 |
|---|---|---|---|
| vzip (python) | 0.99 req, 1014 B, 1.32 ms | 1.00 req, 1.0 KiB, 1.04 ms | 1.00 req, 1.0 KiB, 0.88 ms |
| vzip, paged (python) | 1.06 req, 1.3 KiB, 1.14 ms | 1.55 req, 3.2 KiB, 2.28 ms | 1.95 req, 4.8 KiB, 2.99 ms |
| phkv (rust) | 1.00 req, 1.0 KiB, 0.16 ms | 1.00 req, 1.0 KiB, 0.16 ms | 1.34 req, 2.4 KiB, 0.20 ms |
| hybrid, read as vzip (python) | 1.07 req, 1.3 KiB, 0.92 ms | 1.54 req, 3.2 KiB, 2.10 ms | 1.95 req, 4.8 KiB, 3.06 ms |
| hybrid, read as phkv (rust) | 2.00 req, 1.0 KiB, 0.29 ms | 2.00 req, 1.0 KiB, 0.31 ms | 2.27 req, 2.1 KiB, 0.31 ms |

**http 20 ms**

| format (reader) | 1,000 | 10,000 | 100,000 |
|---|---|---|---|
| vzip (python) | 0.99 req, 1014 B, 22 ms | 1.00 req, 1.0 KiB, 22 ms | 1.00 req, 1.0 KiB, 22 ms |
| vzip, paged (python) | 1.06 req, 1.3 KiB, 22 ms | 1.55 req, 3.2 KiB, 44 ms | 1.95 req, 4.8 KiB, 44 ms |
| phkv (rust) | 1.00 req, 1.0 KiB, 21 ms | 1.00 req, 1.0 KiB, 21 ms | 1.34 req, 2.4 KiB, 21 ms |
| hybrid, read as vzip (python) | 1.07 req, 1.3 KiB, 22 ms | 1.54 req, 3.2 KiB, 43 ms | 1.95 req, 4.8 KiB, 44 ms |
| hybrid, read as phkv (rust) | 2.00 req, 1.0 KiB, 42 ms | 2.00 req, 1.0 KiB, 42 ms | 2.27 req, 2.1 KiB, 42 ms |

### `list` of the 100 keys under one prefix, on a store opened cold

**local**

| format (reader) | 1,000 | 10,000 | 100,000 |
|---|---|---|---|
| vzip (python) | 1 req, 64.0 KiB, 2.73 ms | 2 req, 574.4 KiB, 24 ms | 2 req, 5.6 MiB, 277 ms |
| vzip, paged (python) | 4 req, 76.0 KiB, 2.30 ms | 4 req, 73.3 KiB, 2.66 ms | 4 req, 83.4 KiB, 11 ms |
| phkv (rust) | 26 req, 106.6 KiB, 0.05 ms | 26 req, 106.7 KiB, 0.06 ms | 26 req, 106.8 KiB, 0.23 ms |
| hybrid, read as vzip (python) | 5 req, 76.3 KiB, 3.39 ms | 4 req, 73.3 KiB, 3.10 ms | 4 req, 83.3 KiB, 11 ms |
| hybrid, read as phkv (rust) | 2 req, 8.0 KiB, 0.04 ms | 2 req, 8.0 KiB, 0.04 ms | 2 req, 8.0 KiB, 0.04 ms |

**http 0 ms**

| format (reader) | 1,000 | 10,000 | 100,000 |
|---|---|---|---|
| vzip (python) | 1 req, 64.0 KiB, 4.17 ms | 2 req, 574.4 KiB, 29 ms | 2 req, 5.6 MiB, 292 ms |
| vzip, paged (python) | 4 req, 76.0 KiB, 5.71 ms | 4 req, 73.3 KiB, 6.05 ms | 4 req, 83.4 KiB, 15 ms |
| phkv (rust) | 26 req, 106.6 KiB, 8.21 ms | 26 req, 106.7 KiB, 6.88 ms | 26 req, 106.8 KiB, 5.51 ms |
| hybrid, read as vzip (python) | 5 req, 76.3 KiB, 6.30 ms | 4 req, 73.3 KiB, 6.73 ms | 4 req, 83.3 KiB, 16 ms |
| hybrid, read as phkv (rust) | 2 req, 8.0 KiB, 0.82 ms | 2 req, 8.0 KiB, 0.71 ms | 2 req, 8.0 KiB, 0.69 ms |

**http 20 ms**

| format (reader) | 1,000 | 10,000 | 100,000 |
|---|---|---|---|
| vzip (python) | 1 req, 64.0 KiB, 26 ms | 2 req, 574.4 KiB, 78 ms | 2 req, 5.6 MiB, 314 ms |
| vzip, paged (python) | 4 req, 76.0 KiB, 48 ms | 4 req, 73.3 KiB, 69 ms | 4 req, 83.4 KiB, 80 ms |
| phkv (rust) | 26 req, 106.6 KiB, 545 ms | 26 req, 106.7 KiB, 544 ms | 26 req, 106.8 KiB, 542 ms |
| hybrid, read as vzip (python) | 5 req, 76.3 KiB, 68 ms | 4 req, 73.3 KiB, 70 ms | 4 req, 83.3 KiB, 80 ms |
| hybrid, read as phkv (rust) | 2 req, 8.0 KiB, 42 ms | 2 req, 8.0 KiB, 42 ms | 2 req, 8.0 KiB, 41 ms |

