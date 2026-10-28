# refkv in the browser

refkv defines reference semantics for key-value storage: a value is either bytes or a reference to
byte ranges of other objects ([spec/refkv.md](../spec/refkv.md)), and vzip is
its single-file binding. This package reads and writes vzip archives
([spec/bindings/vzip.md](../spec/bindings/vzip.md)) in the browser and in Node.

A service worker that turns a remote image file or store of any format of
[docs/zarrify/README.md](../docs/zarrify/README.md#formats) (a store: a
URL ending in `/`), or a `.vzip` archive, into a plain HTTP Zarr store,
without a server and without copying pixel data:

1. A page asks for `<scope>vz/image/<id>/zarr.json`, where `<id>` is the
   base64url encoding of the file's or store's URL.
2. The service worker reads the file's structure with a few range requests
   (batched by the Rust core's read planner) and
   writes a vzip archive in memory by [spec/zarrify.md](../spec/zarrify.md): an
   OME-NGFF 0.5 dataset (or, for a store, its Zarr v3 hierarchy; for SAFE,
   a GeoZarr hierarchy) whose chunks are references to the source's tiles,
   frames, strips, subblocks or chunk objects. The
   format is detected from the file's first bytes, or from the objects at a
   store's root (`vz/tiff/<id>/` accepts TIFF only).
3. It answers every request under that URL from the archive. Metadata comes
   from the archive; a chunk request becomes one range request to the file.

Any Zarr reader on the same origin can then use the URL; it needs no refkv
support. The archive itself can be downloaded from
`<zarr url>__vz__/archive.vzip`. Existing archives are served the same way
from `<scope>vz/archive/<id>/`.

## Use

```js
import { registerVzipWorker, imageZarrUrl, archiveDownloadUrl } from "./src/client.ts";

const prefix = await registerVzipWorker("/vzip-sw.js"); // waits until the page is controlled
const zarr = imageZarrUrl(prefix, "https://example.org/slide.ome.tiff"); // or an .nd2
// e.g. in Neuroglancer: `${zarr}|zarr3:`
```

Build and run the demo, which also serves the Neuroglancer fork
(https://github.com/d-v-b/neuroglancer, branch `vzip`) at `/neuroglancer/`.
The fork is read from `$NEUROGLANCER`, by default a clone next to this
repository, built with `npm run build`. The build needs the Rust core's
wasm32 module, so a Rust toolchain with the `wasm32-unknown-unknown` target
(`just js::build` builds it first; `node build.mjs` alone stops if it is
missing):

```bash
(cd js && npm install)
just js::build
node demo/serve.mjs 8080
```

The demo opens images in Neuroglancer, and Sentinel-2 SAFE products on a map
(`map.html`: OpenLayers' GeoZarr source, with zarrita's registry given an
`imagecodecs_jpeg2k` codec that decodes with the fork's JPEG 2000 wasm
module, which the build copies from `$NEUROGLANCER`). An image whose
metadata has no usable contrast window gets one sampled from a chunk of its
coarsest level (`demo/contrast.ts`), decoded in the page, JPEG 2000 and
JPEG XR chunks with the fork's wasm modules.

The demo's Neuroglancer link for a file or store reads through the service
worker, so it opens only on the demo's origin. The link for a `.vzip` input
names the archive instead, `<archive url>|vzip:<path>|zarr3:`: host an
archive the demo downloaded (on a server that allows cross-origin reads) and
open its URL in the demo, and the link opens in any deployment of the fork,
with no service worker. The fork's `vzip:` adapter checks the archive's pins
where the response shows their headers, and skips those a cross-origin
server hides ([spec/refkv.md §6.7](../spec/refkv.md#67-reader-policy), rule 7).

Live demo: https://d-v-b.github.io/vzip-demo/image-to-zarr/ (formerly `tiff-to-zarr/`, which now redirects). `demo/pages.sh`
publishes it, with the Neuroglancer build, to its own directory of the demo
site (the `gh-pages` branch of `d-v-b/vzip-demo`, which hosts each refkv demo
in a directory and lists them at https://d-v-b.github.io/vzip-demo/).

## What is supported

The rules are [spec/zarrify.md](../spec/zarrify.md)'s profiles, one per
format (the table in [docs/zarrify/README.md](../docs/zarrify/README.md#formats)
links each). Every format is zarrified by the
Rust core (`rust/refkv-ir`) built for wasm32: `src/zarrify/ir/` performs the
requests its read planner asks for (several at once, multi-range ones under
Node), under the reader policy, and writes its output. `just js::wasm` builds
the module, which `just js::build` copies to `dist/refkv_ir.wasm` (beside the
service worker, which fetches it) and which the Node tests and CLIs read from
cargo's target directory (or `$REFKV_IR_WASM`). `src/zarrify/store.ts` holds
the store machinery (the listing, the object and JSON readers). The TypeScript
zarrifiers the core replaced are kept, frozen, in `conformance/zarrify/reference/ts/` (with CLIs
`conformance/zarrify/reference/ts/virtualize.ts` and `virtualize_file.ts`), for
comparisons. The Python implementation (`python -m refkv.zarrify`)
produces equivalent archives; `conformance/zarrify/compare.py` checks that.

**phkv stores.** The service worker also serves a standalone phkv store
([spec/bindings/phkv.md](../spec/bindings/phkv.md)) at an http(s) URL as plain HTTP, from
`<scope>vz/phkv/<id>/<key>`, reading only what a key needs: the store's tail and perfect hash at
open, then a block of slots and a record per key, and the bytes a reference names. The reader is
`rust/refkv-phkv` built for wasm32 (`just js::phkv-wasm`, which `just js::build` copies to
`dist/refkv_phkv.wasm` beside the worker; Node reads it from cargo's target directory, or
`$REFKV_PHKV_WASM`). The module does no I/O: `src/phkv.ts` calls it, and when it has read what was not
there, fetches the ranges it asks for, all at once, and calls it again. A reference's `url` sources are
read under the reader policy and their pins, as an archive's are. `Range: bytes=a-b` reads just that
window; a store gives a value's size only with the whole value, so such a response's `Content-Range` has
no total, and a suffix range or `HEAD` reads the whole value. Only standalone stores are tested here; a
hybrid store is also a valid vzip archive, which the `archive` route serves.

A file or a store at an http(s) URL is read under the reader policy (spec/refkv.md
[§6.7](../spec/refkv.md#67-reader-policy)), a store's listing, object reads and redirects included: loopback,
private, link-local and other special hosts are refused unless the caller
opts in (`allowPrivateHosts`, given to `openHttpSource`, `openHttpStore` or
`zarrifyStore`; `--allow-private-hosts` in the Node CLIs). Each source pins its size, and the input file's its ETag when every
response exposed the same strong one.

What each file format gives in the browser, and what it refuses with HTTP 422, is in the
format's own page, `docs/zarrify/<format>/README.md` ("In the browser"; the index is
[docs/zarrify/README.md](../docs/zarrify/README.md#formats)).

N5, Zarr v2 and OME-Zarr 0.4 stores (a URL ending in `/`, listed with S3 ListObjectsV2):

- Every group and array of the store under that URL, as a Zarr v3 hierarchy
  at the same paths; each chunk is the whole chunk object, one url source per
  chunk, so the archive grows with the number of chunks.
- N5: datasets through the `n5_default` codec (raw, gzip, zlib, zstd and
  blosc blocks, truncated or padded at the edges); COSEM and n5-viewer
  multiscale metadata as OME-NGFF 0.5.
- Zarr v2: C and F order, either separator, numeric and bool types in either
  byte order, zlib, gzip, zstd and blosc; attributes copied unchanged.
- OME-Zarr 0.4 (a Zarr v2 store whose root declares OME-NGFF 0.4): migrated to
  OME-Zarr 0.5 without copying data. The arrays and chunks are the Zarr v2
  profile's; images, labels, plates, wells and bioformats2raw collections are
  checked against OME-NGFF 0.4 and their metadata rewritten as 0.5 (under
  `ome`, with `dimension_names` on every level).

Not supported, and refused with HTTP 422: stores without a listing (a plain
web server), N5 lz4, xz, bzip2 and jpeg blocks, Zarr v2 filters, lz4 and other
compressors, string, object, structured, complex and date-time types, and
OME-Zarr 0.4 stores that break a rule of OME-NGFF 0.4 that 0.5 also has.
A store that lists more than 100000 objects is refused with HTTP 507 (a
limit of this implementation, [spec/zarrify.md Appendix A](../spec/zarrify.md#appendix-a-conformance)).

## Limits

- **Same origin only.** A service worker only controls pages from its own
  origin, so the viewer must be served from the same origin as the worker.
  A link to share names a hosted archive instead (see the demo, above).
- **Codecs are the viewer's job.** The chunks are the TIFF's tiles as they
  are. The Neuroglancer fork decodes `imagecodecs_jpeg2k`; other viewers
  need their own decoder for it.
- **Few ETag pins.** Every source pins its size, but an ETag only when the
  server exposes it to scripts, which cross-origin servers rarely do (nor
  `Last-Modified` or `Content-Range`). For the same reason, a 206 response
  with no visible `Content-Range` is accepted when its body has the
  requested length (see `src/http.ts`).
- **Kept in memory.** Archives are rebuilt if the browser stops the worker.
- **No page index.** The writer does not write a page index, and the reader
  holds the whole archive in memory.

## Tests

```bash
node --test js/test/                                        # unit tests
uv run python js/conformance/run_write.py /tmp/vzip-write   # the kit's write cases
just verify                                                 # every format's fixtures vs an independent reader
uv run python js/test/<format>/verify.py                    # one format's (`just verify-of <format>`)
uv run python conformance/zarrify/compare.py /tmp/vcmp    # reference vs Python vs browser zarrifier
node demo/e2e.mjs <image file url> <out dir>             # demo + Neuroglancer, and a shared archive, in Chromium
node demo/e2e_examples.mjs <out dir> [example id …]      # every demo example, with its viewer
```

- `run_write.py` checks every unpaged write case with `conformance/vzip/validate.py`
  and the reference reader, plus an unpaged archive of 70,000 entries and every
  invalid description.
- `<format>/verify.py` zarrifies each of the format's fixtures with the browser code and
  compares what zarr-python reads through the reference implementation (`python/src/refkv`)
  with an independent reader of the format; its docstring says which reader, and what else it
  checks (such as the rebuild of the file from the archive's IR mirror, byte for byte).
- `compare.py` runs three zarrifiers (`ref`, the frozen reference, which
  a format added since the core, such as MRC, does not have; `py`, the
  Python command; `web`, this code under Node) on the synthetic files, every
  OME-TIFF of IDR idr0096 and the public files of every format
  (`conformance/zarrify/corpus_*.txt`), and compares their outputs
  ([HARNESS.md](../conformance/zarrify/HARNESS.md)).
- The verify scripts and `compare.py` serve fixtures from 127.0.0.1, so they
  run the zarrifiers with private hosts allowed.
