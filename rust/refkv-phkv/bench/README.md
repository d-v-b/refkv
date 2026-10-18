# phkv benchmarks

Request counts, bytes and latency of phkv and vzip, on a local file system and over HTTP.
[RESULTS.md](RESULTS.md) has the numbers and what they show.

```text
cargo build --release --example bench --manifest-path rust/refkv-phkv/Cargo.toml
uv run python rust/refkv-phkv/bench/bench.py            # writes the files and results.json under bench/data
uv run python rust/refkv-phkv/bench/report.py rust/refkv-phkv/bench/data > tables.md
```

`just phkv-bench` does the first two. `sweep.py --data DIR` then sweeps the reader's `Limits::tail` and
`record_read` and the writer's `page_size` and `inline_max` over HTTP, on those files (about 5 minutes). `bench.py --sizes 1000 10000 --bytes-sizes 10000 --rtt-ms 20 --reps 3`
runs a smaller matrix; the default one (up to 10^6 keys) takes about 15 minutes.

## What is measured

- **Files.** The same keys written five ways by `examples/bench.rs build`: vzip without and with a
  page index (4 KiB pages), standalone phkv, and a hybrid archive. A hybrid archive is read both as vzip
  (by the Python reader) and as phkv (by the Rust reader), so those two rows are the same bytes.
  Keys are `arr/c/a/b/c`, 100 under each `arr/c/a/b/`.
- **Workloads.** `refs`: each key is a reference, one 4 KiB range of one URL; the lookup is `raw`, so
  no source is read. `bytes`: each key holds 1 KiB of incompressible bytes in the store; the lookup
  is `get`.
- **Operations.** (1) *First lookup*: a fresh store, then one lookup of a random key (open plus
  lookup; median of `reps` runs). (2) *Later lookups*: 200 random keys on one opened store, per
  lookup. (3) *List*: a fresh store, then `list` of the 100 keys under one prefix.
- **Counts.** A request is one read of the archive: a `Range` request over HTTP, a `pread` on a file.
  The Python reader's counts are its `Stats.archive_requests` and `archive_bytes`; the Rust
  harness counts in its `ReadAt` implementations. The harness's HTTP server counts requests too
  (`server_requests` in `results.json`).
- **HTTP.** A server on 127.0.0.1 (`bench.py`) answers each `Range` request after a fixed delay, 0 or
  20 ms, which stands for the round trip to a remote object store. It keeps connections alive and
  sends headers and body in one write (two writes cost 40 ms each, from Nagle's algorithm and delayed ACKs).

## Caveats

- **Two readers, two languages.** vzip has no Rust reader, so vzip rows are the Python reader and
  phkv rows the Rust one. Request and byte counts do not depend on that. Milliseconds do:
  on a local file the Rust reader's sub-millisecond times are mostly the language, and the
  comparison that means something is over HTTP with a delay, where requests dominate.
- **The Rust HTTP `ReadAt` is the harness's.** The crate gives only the trait. This one asks for the
  last 64 KiB + 64 B with one suffix request (its `Content-Range` gives the size), and serves later
  reads inside those bytes from them, so a store that fits in them (every phkv file of 1,000 to
  ~10,000 references) is read by that one request, and later lookups cost nothing. A `ReadAt` that
  asked for the size with `HEAD` would add a request to every open.
- **Caches.** Each store keeps what it has read. In *later lookups*, vzip with a page index fetches a page once
  and phkv has no page to fetch, so those rows compare a warming cache with none. *First lookup* is the
  cold cost of one lookup.
- **One machine, loopback, one run.** The delay is added, not measured on a network. Times under
  10 ms vary by a fraction of a millisecond between runs; request counts do not vary.
