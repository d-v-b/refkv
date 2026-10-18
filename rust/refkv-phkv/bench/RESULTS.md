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
  a record's first read stops at the end of its page, since a page holds whole records.

## Paged vzip against phkv

Later lookups are random keys on an opened store, timed over 200 of them and counted over 5,000, by which
time the caches have filled. vzip's pages and phkv's slot blocks are both 4 KiB, but a block holds 512 slots
and a page about 50 central directory records, so phkv's cache fills about 7 times sooner.

#### 100,000 keys, `refs`: over HTTP, 20 ms per request

| reader | open + first lookup | later lookup (200 lookups) | later lookup (5,000 lookups, counted) |
|---|---|---|---|
| vzip, paged | 4 req, 81.4 KiB, 103 ms | 1.96 req, 3.8 KiB, 43 ms | 1.33 req, 1.3 KiB |
| phkv, the first defaults (slots one at a time, 64 KiB first read, 512 B record read) | 3 req, 64.3 KiB, 63 ms | 2.00 req, 480 B, 42 ms | 2.00 req, 489 B |
| phkv, 4 KiB slot blocks only | 3 req, 68.2 KiB, 63 ms | 1.59 req, 2.8 KiB, 33 ms | 1.04 req, 639 B |
| phkv, defaults (4 KiB slot blocks, 512 KiB first read, 4.5 KiB record read) | 3 req, 516.2 KiB, 64 ms | 1.25 req, 3.0 KiB, 26 ms | 1.02 req, 2.1 KiB |

#### 1,000,000 keys, `refs`: over HTTP, 20 ms per request

| reader | open + first lookup | later lookup (200 lookups) | later lookup (5,000 lookups, counted) |
|---|---|---|---|
| vzip, paged | 4 req, 199.8 KiB, 202 ms | 1.99 req, 4.0 KiB, 44 ms | 1.87 req, 3.5 KiB |
| phkv, the first defaults (slots one at a time, 64 KiB first read, 512 B record read) | 5 req, 281.6 KiB, 106 ms | 2.00 req, 495 B, 42 ms | 2.00 req, 490 B |
| phkv, 4 KiB slot blocks only | 5 req, 285.6 KiB, 106 ms | 1.95 req, 4.3 KiB, 41 ms | 1.37 req, 1.9 KiB |
| phkv, defaults (4 KiB slot blocks, 512 KiB first read, 4.5 KiB record read) | 3 req, 516.8 KiB, 66 ms | 1.93 req, 5.8 KiB, 41 ms | 1.36 req, 3.4 KiB |

#### 100,000 keys, `bytes`: over HTTP, 20 ms per request

| reader | open + first lookup | later lookup (200 lookups) | later lookup (5,000 lookups, counted) |
|---|---|---|---|
| vzip, paged | 4 req, 80.4 KiB, 96 ms | 1.95 req, 4.8 KiB, 43 ms | 1.28 req, 2.1 KiB |
| phkv, the first defaults (slots one at a time, 64 KiB first read, 512 B record read) | 5 req, 161.4 KiB, 107 ms | 3.00 req, 1.0 KiB, 63 ms | 3.00 req, 1.0 KiB |
| phkv, 4 KiB slot blocks only | 5 req, 165.4 KiB, 107 ms | 2.62 req, 3.5 KiB, 54 ms | 2.04 req, 1.2 KiB |
| phkv, defaults (4 KiB slot blocks, 512 KiB first read, 4.5 KiB record read) | 3 req, 520.2 KiB, 66 ms | 1.34 req, 4.0 KiB, 28 ms | 1.02 req, 2.7 KiB |

What it shows, with the defaults:

- **Every operation takes fewer requests with phkv.** Open and first lookup: 3 against 4. Later lookups over
  200: 1.25, 1.93 and 1.34 against 1.96, 1.99 and 1.95; over 5,000: 1.02, 1.36 and 1.02 against 1.33, 1.87
  and 1.28. At 10^5 keys the first read holds most of SLOTS, so most lookups read no slot at all.
- **Bytes per lookup are close to vzip's**: 2.1 to 5.8 KiB against 1.3 to 4.8 KiB. A record read stops at the
  end of its page, and a 4 KiB slot block is read once and kept. An open reads more: 516 to 520 KiB, against
  80 to 200 KiB. At 100 MB/s that is about 4 ms; at 20 ms per request, a fifth of a round trip.
- Each default does its part: slot blocks make later lookups cheaper; the 512 KiB first read takes the
  10^6-key open from 5 requests to 3, and its share of SLOTS saves slot reads; the 4.5 KiB record read, capped
  at the page, takes a lookup of a 1 KiB value from two record reads to one.

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
| standalone | 5 req, 288.3 KiB, 107 ms | 5 req, 288.3 KiB, 107 ms | 4 req, 288.3 KiB, 86 ms | 3 req, 519.3 KiB, 65 ms | 3 req, 1.0 MiB, 67 ms |
| hybrid | 7 req, 351.4 KiB, 148 ms | 7 req, 415.3 KiB, 149 ms | 6 req, 543.3 KiB, 127 ms | 5 req, 1.0 MiB, 108 ms | 5 req, 2.0 MiB, 109 ms |

512 KiB takes the 10^6-key open from 5 requests to 3, because PTRHASH and PAGES (about 280 KiB) come with it.
For a hybrid store the first read is made twice (the end of the file, and the window before the superblock),
so its bytes double.

**Reader `Limits::record_read`**, the bytes read at a record first, 10^5 keys of 1 KiB inline values:

| record_read | 512 B | 1.0 KiB | 2.0 KiB | 4.5 KiB |
|---|---|---|---|---|
| later lookup | 2.35 req, 2.4 KiB, 42 ms | 2.35 req, 2.4 KiB, 42 ms | 1.34 req, 3.2 KiB, 21 ms | 1.34 req, 4.0 KiB, 21 ms |

A first read as long as the record (2 KiB for a 1 KiB value and its key) saves the record's second read.

**Writer `page_size` and `inline_max`**, 10^5 keys of 1 KiB values (the writer's, recorded in the file):

| page_size, inline_max | file | later lookup (record_read 512) | later lookup (record_read 4.5 KiB) | list of 100 keys |
|---|---|---|---|---|
| 4.0 KiB, 4.0 KiB | 101.2 MiB | 2.35 req, 2.4 KiB, 42 ms | 1.34 req, 4.0 KiB, 21 ms | 26 req, 106.8 KiB, 539 ms |
| 16.0 KiB, 4.0 KiB | 101.1 MiB | 2.27 req, 2.1 KiB, 42 ms | 1.27 req, 5.2 KiB, 21 ms | 7 req, 115.1 KiB, 147 ms |
| 64.0 KiB, 4.0 KiB | 101.1 MiB | 2.25 req, 2.0 KiB, 42 ms | 1.25 req, 5.4 KiB, 21 ms | 3 req, 194.1 KiB, 62 ms |
| 4.0 KiB, none | 101.5 MiB | 2.25 req, 2.5 KiB, 41 ms | 2.25 req, 4.0 KiB, 41 ms | 2 req, 8.0 KiB, 41 ms |

A larger `page_size` cuts the `list` requests (26 to 3 at 64 KiB) by reading more bytes per page; lookups do
not read pages. Values not inline (`inline_max` 0) make records small, so a `list` is 2 requests, but a lookup
reads the value too.

**Reader `Limits::slots_at_open`**, the longest SLOTS section read whole at open and kept (default 0, off), with a
bandwidth per connection. Total time for k lookups of random keys on a cold store (the first lookup, and k − 1
later ones at their mean time):

| keys, workload | bandwidth | slots_at_open | open + first lookup | later lookup | k = 1 | k = 10 | k = 100 | k = 1,000 |
|---|---|---|---|---|---|---|---|---|
| 100,000 references | none | off | 3 req, 518.4 KiB, 64 ms | 1.25 req, 26 ms | 64 ms | 299 ms | 2644 ms | 26095 ms |
| 100,000 references | none | whole | 3 req, 831.1 KiB, 65 ms | 1.00 req, 21 ms | 65 ms | 252 ms | 2116 ms | 20765 ms |
| 100,000 references | 100 MB/s | off | 3 req, 518.4 KiB, 69 ms | 1.25 req, 26 ms | 69 ms | 304 ms | 2654 ms | 26155 ms |
| 100,000 references | 100 MB/s | whole | 3 req, 831.1 KiB, 75 ms | 1.00 req, 21 ms | 75 ms | 263 ms | 2141 ms | 20926 ms |
| 100,000 references | 12.5 MB/s | off | 3 req, 518.4 KiB, 107 ms | 1.25 req, 26 ms | 107 ms | 344 ms | 2718 ms | 26459 ms |
| 100,000 references | 12.5 MB/s | whole | 3 req, 831.1 KiB, 134 ms | 1.00 req, 21 ms | 134 ms | 322 ms | 2204 ms | 21023 ms |
| 1,000,000 references | none | off | 3 req, 519.3 KiB, 65 ms | 1.93 req, 40 ms | 65 ms | 424 ms | 4016 ms | 39941 ms |
| 1,000,000 references | none | whole | 3 req, 8.1 MiB, 76 ms | 1.00 req, 21 ms | 76 ms | 263 ms | 2133 ms | 20841 ms |
| 1,000,000 references | 100 MB/s | off | 3 req, 519.3 KiB, 70 ms | 1.93 req, 40 ms | 70 ms | 432 ms | 4049 ms | 40223 ms |
| 1,000,000 references | 100 MB/s | whole | 3 req, 8.1 MiB, 160 ms | 1.00 req, 21 ms | 160 ms | 348 ms | 2227 ms | 21019 ms |
| 1,000,000 references | 12.5 MB/s | off | 3 req, 519.3 KiB, 108 ms | 1.93 req, 41 ms | 108 ms | 473 ms | 4126 ms | 40659 ms |
| 1,000,000 references | 12.5 MB/s | whole | 3 req, 8.1 MiB, 761 ms | 1.00 req, 21 ms | 761 ms | 950 ms | 2834 ms | 21675 ms |
| 100,000 keys of 1 KiB values | none | off | 3 req, 517.1 KiB, 66 ms | 1.34 req, 28 ms | 66 ms | 319 ms | 2842 ms | 28080 ms |
| 100,000 keys of 1 KiB values | none | whole | 3 req, 960.4 KiB, 67 ms | 1.00 req, 21 ms | 67 ms | 255 ms | 2132 ms | 20897 ms |
| 100,000 keys of 1 KiB values | 100 MB/s | off | 3 req, 517.1 KiB, 71 ms | 1.34 req, 28 ms | 71 ms | 323 ms | 2843 ms | 28036 ms |
| 100,000 keys of 1 KiB values | 100 MB/s | whole | 3 req, 960.4 KiB, 78 ms | 1.00 req, 22 ms | 78 ms | 272 ms | 2217 ms | 21662 ms |
| 100,000 keys of 1 KiB values | 12.5 MB/s | off | 3 req, 517.1 KiB, 109 ms | 1.34 req, 28 ms | 109 ms | 363 ms | 2905 ms | 28329 ms |
| 100,000 keys of 1 KiB values | 12.5 MB/s | whole | 3 req, 960.4 KiB, 147 ms | 1.00 req, 21 ms | 147 ms | 336 ms | 2224 ms | 21103 ms |

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
| vzip (python) | 3 req, 68.0 KiB, 4.37 ms | 3 req, 691.1 KiB, 31 ms | 3 req, 6.8 MiB, 402 ms | 3 req, 69.3 MiB, 4235 ms |
| vzip, paged (python) | 4 req, 68.3 KiB, 2.54 ms | 4 req, 69.5 KiB, 2.64 ms | 4 req, 81.4 KiB, 11 ms | 4 req, 199.8 KiB, 113 ms |
| phkv (rust) | 2 req, 36.3 KiB, 0.06 ms | 2 req, 346.5 KiB, 0.11 ms | 3 req, 518.4 KiB, 0.52 ms | 3 req, 516.8 KiB, 1.49 ms |
| hybrid, read as vzip (python) | 4 req, 68.3 KiB, 1.83 ms | 4 req, 69.5 KiB, 3.03 ms | 4 req, 81.4 KiB, 13 ms | 4 req, 199.8 KiB, 115 ms |
| hybrid, read as phkv (rust) | 2 req, 152.9 KiB, 0.08 ms | 3 req, 1.0 MiB, 0.19 ms | 5 req, 1.0 MiB, 0.58 ms | 5 req, 1.0 MiB, 1.94 ms |

**http 0 ms**

| format (reader) | 1,000 | 10,000 | 100,000 | 1,000,000 |
|---|---|---|---|---|
| vzip (python) | 3 req, 68.0 KiB, 6.23 ms | 3 req, 691.1 KiB, 36 ms | 3 req, 6.8 MiB, 401 ms | 3 req, 69.3 MiB, 4193 ms |
| vzip, paged (python) | 4 req, 68.3 KiB, 5.34 ms | 4 req, 69.5 KiB, 6.67 ms | 4 req, 81.4 KiB, 16 ms | 4 req, 199.8 KiB, 116 ms |
| phkv (rust) | 2 req, 36.3 KiB, 1.46 ms | 2 req, 346.5 KiB, 2.10 ms | 3 req, 518.4 KiB, 3.30 ms | 3 req, 516.8 KiB, 4.15 ms |
| hybrid, read as vzip (python) | 4 req, 68.3 KiB, 5.35 ms | 4 req, 69.5 KiB, 7.38 ms | 4 req, 81.4 KiB, 21 ms | 4 req, 199.8 KiB, 122 ms |
| hybrid, read as phkv (rust) | 2 req, 152.9 KiB, 1.86 ms | 3 req, 1.0 MiB, 3.51 ms | 5 req, 1.0 MiB, 3.29 ms | 5 req, 1.0 MiB, 5.99 ms |

**http 20 ms**

| format (reader) | 1,000 | 10,000 | 100,000 | 1,000,000 |
|---|---|---|---|---|
| vzip (python) | 3 req, 68.0 KiB, 68 ms | 3 req, 691.1 KiB, 103 ms | 3 req, 6.8 MiB, 431 ms | 3 req, 69.3 MiB, 4267 ms |
| vzip, paged (python) | 4 req, 68.3 KiB, 88 ms | 4 req, 69.5 KiB, 88 ms | 4 req, 81.4 KiB, 98 ms | 4 req, 199.8 KiB, 203 ms |
| phkv (rust) | 2 req, 36.3 KiB, 43 ms | 2 req, 346.5 KiB, 43 ms | 3 req, 518.4 KiB, 64 ms | 3 req, 516.8 KiB, 66 ms |
| hybrid, read as vzip (python) | 4 req, 68.3 KiB, 89 ms | 4 req, 69.5 KiB, 90 ms | 4 req, 81.4 KiB, 99 ms | 4 req, 199.8 KiB, 205 ms |
| hybrid, read as phkv (rust) | 2 req, 152.9 KiB, 43 ms | 3 req, 1.0 MiB, 65 ms | 5 req, 1.0 MiB, 106 ms | 5 req, 1.0 MiB, 109 ms |

### Later lookups of random keys in an opened store (per lookup, median)

**local**

| format (reader) | 1,000 | 10,000 | 100,000 | 1,000,000 |
|---|---|---|---|---|
| vzip (python) | 1.00 req, 8 B, 0.26 ms | 1.00 req, 8 B, 0.35 ms | 1.00 req, 8 B, 0.32 ms | 1.00 req, 9 B, 0.34 ms |
| vzip, paged (python) | 1.08 req, 334 B, 0.34 ms | 1.59 req, 2.4 KiB, 1.00 ms | 1.96 req, 3.9 KiB, 1.25 ms | 1.99 req, 4.0 KiB, 1.32 ms |
| phkv (rust) | 1.00 req, 2.0 KiB, 0.00 ms | 1.00 req, 2.0 KiB, 0.00 ms | 1.25 req, 3.0 KiB, 0.00 ms | 1.93 req, 5.8 KiB, 0.00 ms |
| hybrid, read as vzip (python) | 1.08 req, 334 B, 0.36 ms | 1.59 req, 2.4 KiB, 0.93 ms | 1.96 req, 3.9 KiB, 1.54 ms | 1.99 req, 4.0 KiB, 1.42 ms |
| hybrid, read as phkv (rust) | 1.00 req, 2.0 KiB, 0.00 ms | 1.00 req, 2.1 KiB, 0.00 ms | 1.26 req, 3.0 KiB, 0.00 ms | 1.93 req, 5.8 KiB, 0.00 ms |

**http 0 ms**

| format (reader) | 1,000 | 10,000 | 100,000 | 1,000,000 |
|---|---|---|---|---|
| vzip (python) | 1.00 req, 8 B, 1.04 ms | 1.00 req, 8 B, 0.93 ms | 1.00 req, 8 B, 0.90 ms | 1.00 req, 9 B, 1.50 ms |
| vzip, paged (python) | 1.08 req, 334 B, 1.23 ms | 1.59 req, 2.4 KiB, 2.58 ms | 1.96 req, 3.9 KiB, 2.76 ms | 1.99 req, 4.0 KiB, 2.79 ms |
| phkv (rust) | 1.00 req, 2.0 KiB, 0.15 ms | 1.00 req, 2.0 KiB, 0.14 ms | 1.25 req, 3.0 KiB, 0.17 ms | 1.93 req, 5.8 KiB, 0.30 ms |
| hybrid, read as vzip (python) | 1.08 req, 334 B, 1.10 ms | 1.59 req, 2.4 KiB, 2.37 ms | 1.96 req, 3.9 KiB, 2.87 ms | 1.99 req, 4.0 KiB, 3.09 ms |
| hybrid, read as phkv (rust) | 1.00 req, 2.0 KiB, 0.23 ms | 1.00 req, 2.1 KiB, 0.15 ms | 1.26 req, 3.0 KiB, 0.18 ms | 1.93 req, 5.8 KiB, 0.29 ms |

**http 20 ms**

| format (reader) | 1,000 | 10,000 | 100,000 | 1,000,000 |
|---|---|---|---|---|
| vzip (python) | 1.00 req, 8 B, 22 ms | 1.00 req, 8 B, 22 ms | 1.00 req, 8 B, 22 ms | 1.00 req, 9 B, 22 ms |
| vzip, paged (python) | 1.08 req, 334 B, 22 ms | 1.59 req, 2.4 KiB, 44 ms | 1.96 req, 3.9 KiB, 44 ms | 1.99 req, 4.0 KiB, 44 ms |
| phkv (rust) | 1.00 req, 2.0 KiB, 21 ms | 1.00 req, 2.0 KiB, 21 ms | 1.25 req, 3.0 KiB, 21 ms | 1.93 req, 5.8 KiB, 42 ms |
| hybrid, read as vzip (python) | 1.08 req, 334 B, 22 ms | 1.59 req, 2.4 KiB, 44 ms | 1.96 req, 3.9 KiB, 44 ms | 1.99 req, 4.0 KiB, 44 ms |
| hybrid, read as phkv (rust) | 1.00 req, 2.0 KiB, 21 ms | 1.00 req, 2.1 KiB, 21 ms | 1.26 req, 3.0 KiB, 21 ms | 1.93 req, 5.8 KiB, 42 ms |

### `list` of the 100 keys under one prefix, on a store opened cold

**local**

| format (reader) | 1,000 | 10,000 | 100,000 | 1,000,000 |
|---|---|---|---|---|
| vzip (python) | 2 req, 67.9 KiB, 3.62 ms | 2 req, 691.1 KiB, 36 ms | 2 req, 6.8 MiB, 342 ms | 2 req, 69.3 MiB, 4146 ms |
| vzip, paged (python) | 5 req, 76.3 KiB, 4.56 ms | 4 req, 73.5 KiB, 3.17 ms | 5 req, 89.5 KiB, 14 ms | 5 req, 207.9 KiB, 118 ms |
| phkv (rust) | 2 req, 8.0 KiB, 0.05 ms | 2 req, 8.0 KiB, 0.05 ms | 2 req, 8.0 KiB, 0.05 ms | 1 req, 4.0 KiB, 0.09 ms |
| hybrid, read as vzip (python) | 5 req, 76.3 KiB, 2.51 ms | 4 req, 73.5 KiB, 2.95 ms | 5 req, 89.5 KiB, 15 ms | 5 req, 207.9 KiB, 121 ms |
| hybrid, read as phkv (rust) | 2 req, 8.0 KiB, 0.07 ms | 2 req, 8.0 KiB, 0.05 ms | 2 req, 8.0 KiB, 0.05 ms | 1 req, 4.0 KiB, 0.09 ms |

**http 0 ms**

| format (reader) | 1,000 | 10,000 | 100,000 | 1,000,000 |
|---|---|---|---|---|
| vzip (python) | 2 req, 67.9 KiB, 5.24 ms | 2 req, 691.1 KiB, 33 ms | 2 req, 6.8 MiB, 374 ms | 2 req, 69.3 MiB, 4292 ms |
| vzip, paged (python) | 5 req, 76.3 KiB, 11 ms | 4 req, 73.5 KiB, 7.70 ms | 5 req, 89.5 KiB, 19 ms | 5 req, 207.9 KiB, 125 ms |
| phkv (rust) | 2 req, 8.0 KiB, 0.75 ms | 2 req, 8.0 KiB, 0.86 ms | 2 req, 8.0 KiB, 0.73 ms | 1 req, 4.0 KiB, 0.58 ms |
| hybrid, read as vzip (python) | 5 req, 76.3 KiB, 7.03 ms | 4 req, 73.5 KiB, 6.88 ms | 5 req, 89.5 KiB, 18 ms | 5 req, 207.9 KiB, 130 ms |
| hybrid, read as phkv (rust) | 2 req, 8.0 KiB, 0.88 ms | 2 req, 8.0 KiB, 0.81 ms | 2 req, 8.0 KiB, 0.71 ms | 1 req, 4.0 KiB, 0.45 ms |

**http 20 ms**

| format (reader) | 1,000 | 10,000 | 100,000 | 1,000,000 |
|---|---|---|---|---|
| vzip (python) | 2 req, 67.9 KiB, 50 ms | 2 req, 691.1 KiB, 74 ms | 2 req, 6.8 MiB, 387 ms | 2 req, 69.3 MiB, 4367 ms |
| vzip, paged (python) | 5 req, 76.3 KiB, 69 ms | 4 req, 73.5 KiB, 68 ms | 5 req, 89.5 KiB, 81 ms | 5 req, 207.9 KiB, 197 ms |
| phkv (rust) | 2 req, 8.0 KiB, 41 ms | 2 req, 8.0 KiB, 42 ms | 2 req, 8.0 KiB, 42 ms | 1 req, 4.0 KiB, 21 ms |
| hybrid, read as vzip (python) | 5 req, 76.3 KiB, 71 ms | 4 req, 73.5 KiB, 68 ms | 5 req, 89.5 KiB, 85 ms | 5 req, 207.9 KiB, 199 ms |
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
| vzip (python) | 2 req, 65.0 KiB, 3.63 ms | 3 req, 575.4 KiB, 28 ms | 3 req, 5.6 MiB, 260 ms |
| vzip, paged (python) | 3 req, 69.0 KiB, 1.75 ms | 4 req, 70.3 KiB, 5.04 ms | 4 req, 80.4 KiB, 12 ms |
| phkv (rust) | 2 req, 513.1 KiB, 0.12 ms | 2 req, 513.1 KiB, 0.30 ms | 3 req, 517.1 KiB, 1.64 ms |
| hybrid, read as vzip (python) | 4 req, 69.2 KiB, 2.14 ms | 4 req, 70.3 KiB, 2.65 ms | 4 req, 80.3 KiB, 10 ms |
| hybrid, read as phkv (rust) | 4 req, 1.0 MiB, 0.55 ms | 4 req, 1.0 MiB, 0.46 ms | 6 req, 1.0 MiB, 0.85 ms |

**http 0 ms**

| format (reader) | 1,000 | 10,000 | 100,000 |
|---|---|---|---|
| vzip (python) | 2 req, 65.0 KiB, 4.84 ms | 3 req, 575.4 KiB, 27 ms | 3 req, 5.6 MiB, 281 ms |
| vzip, paged (python) | 3 req, 69.0 KiB, 3.86 ms | 4 req, 70.3 KiB, 5.87 ms | 4 req, 80.4 KiB, 13 ms |
| phkv (rust) | 2 req, 513.1 KiB, 2.48 ms | 2 req, 513.1 KiB, 2.65 ms | 3 req, 517.1 KiB, 4.93 ms |
| hybrid, read as vzip (python) | 4 req, 69.2 KiB, 5.06 ms | 4 req, 70.3 KiB, 5.96 ms | 4 req, 80.3 KiB, 15 ms |
| hybrid, read as phkv (rust) | 4 req, 1.0 MiB, 4.25 ms | 4 req, 1.0 MiB, 3.96 ms | 6 req, 1.0 MiB, 3.47 ms |

**http 20 ms**

| format (reader) | 1,000 | 10,000 | 100,000 |
|---|---|---|---|
| vzip (python) | 2 req, 65.0 KiB, 47 ms | 3 req, 575.4 KiB, 93 ms | 3 req, 5.6 MiB, 326 ms |
| vzip, paged (python) | 3 req, 69.0 KiB, 66 ms | 4 req, 70.3 KiB, 88 ms | 4 req, 80.4 KiB, 98 ms |
| phkv (rust) | 2 req, 513.1 KiB, 43 ms | 2 req, 513.1 KiB, 44 ms | 3 req, 517.1 KiB, 66 ms |
| hybrid, read as vzip (python) | 4 req, 69.2 KiB, 87 ms | 4 req, 70.3 KiB, 88 ms | 4 req, 80.3 KiB, 98 ms |
| hybrid, read as phkv (rust) | 4 req, 1.0 MiB, 85 ms | 4 req, 1.0 MiB, 86 ms | 6 req, 1.0 MiB, 129 ms |

### Later lookups of random keys in an opened store (per lookup, median)

**local**

| format (reader) | 1,000 | 10,000 | 100,000 |
|---|---|---|---|
| vzip (python) | 0.99 req, 1014 B, 0.36 ms | 1.00 req, 1.0 KiB, 0.35 ms | 1.00 req, 1.0 KiB, 0.33 ms |
| vzip, paged (python) | 1.06 req, 1.3 KiB, 0.33 ms | 1.55 req, 3.2 KiB, 0.99 ms | 1.95 req, 4.8 KiB, 1.35 ms |
| phkv (rust) | 1.00 req, 2.6 KiB, 0.00 ms | 1.00 req, 2.6 KiB, 0.00 ms | 1.34 req, 4.0 KiB, 0.00 ms |
| hybrid, read as vzip (python) | 1.07 req, 1.3 KiB, 0.30 ms | 1.54 req, 3.2 KiB, 0.91 ms | 1.95 req, 4.8 KiB, 1.33 ms |
| hybrid, read as phkv (rust) | 2.00 req, 3.2 KiB, 0.00 ms | 2.00 req, 3.0 KiB, 0.00 ms | 2.27 req, 4.0 KiB, 0.00 ms |

**http 0 ms**

| format (reader) | 1,000 | 10,000 | 100,000 |
|---|---|---|---|
| vzip (python) | 0.99 req, 1014 B, 1.26 ms | 1.00 req, 1.0 KiB, 1.15 ms | 1.00 req, 1.0 KiB, 1.01 ms |
| vzip, paged (python) | 1.06 req, 1.3 KiB, 1.08 ms | 1.55 req, 3.2 KiB, 2.23 ms | 1.95 req, 4.8 KiB, 2.89 ms |
| phkv (rust) | 1.00 req, 2.6 KiB, 0.14 ms | 1.00 req, 2.6 KiB, 0.14 ms | 1.34 req, 4.0 KiB, 0.19 ms |
| hybrid, read as vzip (python) | 1.07 req, 1.3 KiB, 1.09 ms | 1.54 req, 3.2 KiB, 2.17 ms | 1.95 req, 4.8 KiB, 2.66 ms |
| hybrid, read as phkv (rust) | 2.00 req, 3.2 KiB, 0.30 ms | 2.00 req, 3.0 KiB, 0.33 ms | 2.27 req, 4.0 KiB, 0.32 ms |

**http 20 ms**

| format (reader) | 1,000 | 10,000 | 100,000 |
|---|---|---|---|
| vzip (python) | 0.99 req, 1014 B, 22 ms | 1.00 req, 1.0 KiB, 22 ms | 1.00 req, 1.0 KiB, 22 ms |
| vzip, paged (python) | 1.06 req, 1.3 KiB, 22 ms | 1.55 req, 3.2 KiB, 44 ms | 1.95 req, 4.8 KiB, 44 ms |
| phkv (rust) | 1.00 req, 2.6 KiB, 21 ms | 1.00 req, 2.6 KiB, 21 ms | 1.34 req, 4.0 KiB, 21 ms |
| hybrid, read as vzip (python) | 1.07 req, 1.3 KiB, 22 ms | 1.54 req, 3.2 KiB, 43 ms | 1.95 req, 4.8 KiB, 44 ms |
| hybrid, read as phkv (rust) | 2.00 req, 3.2 KiB, 42 ms | 2.00 req, 3.0 KiB, 42 ms | 2.27 req, 4.0 KiB, 42 ms |

### `list` of the 100 keys under one prefix, on a store opened cold

**local**

| format (reader) | 1,000 | 10,000 | 100,000 |
|---|---|---|---|
| vzip (python) | 1 req, 64.0 KiB, 3.15 ms | 2 req, 574.4 KiB, 22 ms | 2 req, 5.6 MiB, 244 ms |
| vzip, paged (python) | 4 req, 76.0 KiB, 2.17 ms | 4 req, 73.3 KiB, 3.09 ms | 4 req, 83.4 KiB, 10 ms |
| phkv (rust) | 26 req, 106.6 KiB, 0.04 ms | 26 req, 106.7 KiB, 0.06 ms | 26 req, 106.8 KiB, 0.20 ms |
| hybrid, read as vzip (python) | 5 req, 76.3 KiB, 2.56 ms | 4 req, 73.3 KiB, 2.87 ms | 4 req, 83.3 KiB, 14 ms |
| hybrid, read as phkv (rust) | 2 req, 8.0 KiB, 0.04 ms | 2 req, 8.0 KiB, 0.04 ms | 2 req, 8.0 KiB, 0.04 ms |

**http 0 ms**

| format (reader) | 1,000 | 10,000 | 100,000 |
|---|---|---|---|
| vzip (python) | 1 req, 64.0 KiB, 4.25 ms | 2 req, 574.4 KiB, 30 ms | 2 req, 5.6 MiB, 257 ms |
| vzip, paged (python) | 4 req, 76.0 KiB, 7.89 ms | 4 req, 73.3 KiB, 6.64 ms | 4 req, 83.4 KiB, 14 ms |
| phkv (rust) | 26 req, 106.6 KiB, 6.75 ms | 26 req, 106.7 KiB, 5.50 ms | 26 req, 106.8 KiB, 6.46 ms |
| hybrid, read as vzip (python) | 5 req, 76.3 KiB, 8.18 ms | 4 req, 73.3 KiB, 7.19 ms | 4 req, 83.3 KiB, 15 ms |
| hybrid, read as phkv (rust) | 2 req, 8.0 KiB, 0.71 ms | 2 req, 8.0 KiB, 0.70 ms | 2 req, 8.0 KiB, 0.69 ms |

**http 20 ms**

| format (reader) | 1,000 | 10,000 | 100,000 |
|---|---|---|---|
| vzip (python) | 1 req, 64.0 KiB, 25 ms | 2 req, 574.4 KiB, 78 ms | 2 req, 5.6 MiB, 324 ms |
| vzip, paged (python) | 4 req, 76.0 KiB, 49 ms | 4 req, 73.3 KiB, 68 ms | 4 req, 83.4 KiB, 77 ms |
| phkv (rust) | 26 req, 106.6 KiB, 540 ms | 26 req, 106.7 KiB, 542 ms | 26 req, 106.8 KiB, 544 ms |
| hybrid, read as vzip (python) | 5 req, 76.3 KiB, 68 ms | 4 req, 73.3 KiB, 69 ms | 4 req, 83.3 KiB, 81 ms |
| hybrid, read as phkv (rust) | 2 req, 8.0 KiB, 41 ms | 2 req, 8.0 KiB, 42 ms | 2 req, 8.0 KiB, 42 ms |

