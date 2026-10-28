// Serves the values of vzip archives as plain HTTP, so that any Zarr reader
// can read them. Archives are either zarrified TIFFs or existing .vzip
// files, named by URL:
//
//   <prefix>image/<id>/<key>     the image file at decodeId(id), or the N5,
//                                Zarr v2 or OME-Zarr 0.4 store if that URL ends in "/",
//                                zarrified by spec/zarrify.md
//   <prefix>tiff/<id>/<key>      the same, for TIFF files only
//   <prefix>archive/<id>/<key>   the .vzip archive at decodeId(id)
//   <prefix>phkv/<id>/<key>      the standalone phkv store at decodeId(id), read in place by range
//                                requests (phkv.ts): only the keys asked for are read
//   .../__vz__/archive.vzip      the archive itself (keys under __vz__/ are
//                                hidden, so no key can collide with it)
//
// `id` is the base64url encoding of the URL. Used by the service worker
// (sw.ts); kept free of service worker APIs so it can run under Node.

import { Archive, type Policy, type RangeFetcher, RefkvError, urlSourceReader } from "./archive.ts";
import { HttpResolutionError, openHttpFile, readHttpRange } from "./http.ts";
import { PhkvError, type PhkvIo, PhkvStore, type PhkvSource } from "./phkv.ts";
import type { Source } from "./protobuf.ts";
import { compilePhkv } from "#phkvwasm";
import { ImageError } from "./zarrify/common.ts";
import { isStoreUrl, zarrifySource, zarrifyStore } from "./zarrify/index.ts";
import { zarrifyIr } from "./zarrify/ir/run.ts";
import { openHttpSource, type RangeSource, readerSource } from "./zarrify/ir/source.ts";
import { openHttpStore, StoreError, StoreLimitError, StoreReadError } from "./zarrify/store.ts";
import type { StoreProfile } from "./zarrify/ir/store.ts";
import { writeVzip } from "./writer.ts";

/** The titles rejections of the Rust core's store profiles carry, as the profiles' own errors did;
 * none for a SAFE directory, whose rejections read as a SAFE zip file's (an ImageError's, bare). */
const STORE_FORMATS: Record<StoreProfile, string | undefined> = {
  zarr2: "Zarr v2",
  n5: "N5",
  "ome-zarr": "OME-Zarr",
  safe: undefined,
  vsi: "VSI",
  nrrd: "NRRD",
};

export const ARCHIVE_KEY = "__vz__/archive.vzip";

/** Marks every response of the handler, so pages can tell it from the network's. */
export const WORKER_HEADER = "X-Vzip-Worker";

export function encodeId(url: string): string {
  let s = "";
  for (const b of new TextEncoder().encode(url)) s += String.fromCharCode(b);
  return btoa(s).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}

export function decodeId(id: string): string {
  if (!/^[A-Za-z0-9_-]+$/.test(id)) throw new Error(`invalid id ${id}`);
  const s = atob(id.replaceAll("-", "+").replaceAll("_", "/"));
  return new TextDecoder("utf-8", { fatal: true }).decode(
    Uint8Array.from(s, (c) => c.charCodeAt(0)),
  );
}

export interface HandlerOptions {
  /** Absolute URL that every served path starts with, ending in "/". */
  prefix: string;
  /** Opens an image file for zarrifying; defaults to `openHttpSource` under `policy`. */
  openSource?: (url: string) => Promise<RangeSource>;
  /** Opens an image file as a reader, in place of `openSource` (its requests are not planned as remote ones). */
  openFile?: typeof openHttpFile;
  /**
   * Lists and reads store inputs (spec/zarrify.md §1.5
   * <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>); defaults to
   * `storeFetch` under `policy`. A fetch given here is still checked by the policy on each URL
   * as written and on each response's final URL.
   */
  fetchStore?: (url: string, init?: RequestInit) => Promise<Response>;
  fetchRange?: RangeFetcher;
  fetchArchive?: (url: string) => Promise<Uint8Array>;
  /** Opens a phkv store's file for range reads; defaults to `openHttpFile`. */
  openPhkvFile?: typeof openHttpFile;
  /** The phkv reader's compiled wasm module; defaults to `#phkvwasm`'s. */
  phkvModule?: () => Promise<WebAssembly.Module>;
  /**
   * The reader policy (spec §6.7
   * <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>) for `.vzip`
   * archives named by URL, and for
   * the image files and stores the handler zarrifies; the default allows http(s)
   * sources on public hosts. Archives
   * the handler zarrifies itself are read with the same policy, plus
   * `unverifiablePins`: their pins were taken from the responses the handler
   * has just read, and a cross-origin server may hide the headers that would
   * check them again.
   */
  policy?: Policy;
}

interface Opened {
  archive: Archive;
  filename: string;
}

function response(status: number, body: string | Uint8Array | null, headers: Record<string, string> = {}) {
  return new Response(body as BodyInit | null, {
    status,
    headers: {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Expose-Headers": `Content-Range, Content-Length, ${WORKER_HEADER}`,
      [WORKER_HEADER]: "1",
      ...(typeof body === "string" ? { "Content-Type": "text/plain; charset=utf-8" } : {}),
      ...headers,
    },
  });
}

/** Parses a single-range `Range` header; undefined means "serve it all". */
function parseRange(header: string | null, size: number): { start: number; end: number } | "unsatisfiable" | undefined {
  const m = header?.match(/^bytes=(\d*)-(\d*)$/);
  if (!m || (m[1] === "" && m[2] === "")) return undefined;
  if (m[1] === "") {
    const n = Number(m[2]);
    if (n === 0) return "unsatisfiable";
    return { start: Math.max(0, size - n), end: size };
  }
  const start = Number(m[1]);
  const end = m[2] === "" ? size : Math.min(size, Number(m[2]) + 1);
  if (start >= size || (m[2] !== "" && Number(m[2]) < start)) return "unsatisfiable";
  return { start, end };
}

const contentType = (key: string) => (key.endsWith(".json") ? "application/json" : key.endsWith(".xml") ? "application/xml" : "application/octet-stream");

/**
 * The value of a phkv store's key as HTTP. A request for a window from a start (`bytes=a-b`,
 * `bytes=a-`) reads only that window, from the store and the sources a reference names, and its
 * `Content-Range` gives no total (`/*`), as the store gives the size only with the whole value. A
 * request for a suffix, and `HEAD`, read the whole value.
 */
async function servePhkv(store: PhkvStore, key: string, rangeHeader: string | null, head: boolean): Promise<Response> {
  if (key === "" || key.endsWith("/")) return response(404, `no key ${key}`);
  const headers = { "Content-Type": contentType(key), "Accept-Ranges": "bytes" };
  const m = rangeHeader?.match(/^bytes=(\d+)-(\d*)$/);
  if (m !== null && m !== undefined && !head) {
    const start = Number(m[1]);
    if (m[2] !== "" && Number(m[2]) < start) return response(416, "range not satisfiable");
    const body = await store.get(key, m[2] === "" ? { start } : { start, end: Number(m[2]) + 1 });
    if (body === undefined) return response(404, `no key ${key}`);
    if (body.length === 0) return response(416, "range not satisfiable");
    return response(206, body, { ...headers, "Content-Length": String(body.length), "Content-Range": `bytes ${start}-${start + body.length - 1}/*` });
  }
  const value = await store.get(key);
  if (value === undefined) return response(404, `no key ${key}`);
  const size = value.length;
  const range = parseRange(rangeHeader, size);
  if (range === "unsatisfiable") return response(416, "range not satisfiable", { "Content-Range": `bytes */${size}` });
  if (range === undefined) return response(200, head ? null : value, { ...headers, "Content-Length": String(size) });
  return response(206, head ? null : value.subarray(range.start, range.end), {
    ...headers,
    "Content-Length": String(range.end - range.start),
    "Content-Range": `bytes ${range.start}-${range.end - 1}/${size}`,
  });
}

/** A source of a phkv store as the archive reader's: its pins as the protobuf's. */
function archiveSource(s: PhkvSource): Source {
  return {
    ...(s.url !== undefined ? { url: s.url } : {}),
    ...(s.size !== undefined ? { size: BigInt(s.size) } : {}),
    ...(s.etag !== undefined ? { etag: s.etag } : {}),
    ...(s.modified_not_after !== undefined ? { modifiedNotAfter: BigInt(s.modified_not_after) } : {}),
  };
}

/** The status of a phkv failure: spec/refkv.md §6.4's classes as the archive route maps them. */
function phkvStatus(e: PhkvError): number {
  return e.errorClass === "request" ? 416 : e.errorClass === "resolution" || e.errorClass === "io" ? 502 : e.errorClass === "archive" ? 422 : 500;
}

export function makeHandler(options: HandlerOptions) {
  const {
    prefix,
    fetchStore,
    openPhkvFile = openHttpFile,
    phkvModule = compilePhkv,
    fetchRange = (url, start, end, pins, options) => readHttpRange(url, start, end, pins, undefined, options),
    policy = {},
    openSource = options.openFile
      ? async (url: string) => {
        const file = await options.openFile!(url);
        return readerSource(file.read, file.size, file.etag ? { etag: file.etag } : {});
      }
      : (url: string) => openHttpSource(url, { policy }),
    fetchArchive = async (url) => {
      const r = await fetch(url);
      if (!r.ok) throw new HttpResolutionError(`${url}: HTTP ${r.status}`);
      return new Uint8Array(await r.arrayBuffer());
    },
  } = options;
  const opened = new Map<string, Promise<Opened>>();
  const phkvStores = new Map<string, Promise<PhkvStore>>();
  let phkvWasm: Promise<WebAssembly.Module> | undefined;

  /** The phkv store at `url`, opened once: its reads are range requests, its sources are read under `policy`. */
  function openPhkv(url: string): Promise<PhkvStore> {
    let p = phkvStores.get(url);
    if (p === undefined) {
      p = (async () => {
        const file = await openPhkvFile(url);
        const readUrl = urlSourceReader(url, policy, fetchRange);
        const io: PhkvIo = {
          size: file.size,
          read: file.read,
          readSource: (source, offset, length) =>
            readUrl(archiveSource(source), BigInt(offset), BigInt(offset + length), (m) => {
              throw new PhkvError("resolution", `source ${source.index}: ${m}`);
            }),
        };
        return PhkvStore.open(await (phkvWasm ??= phkvModule()), io);
      })();
      phkvStores.set(url, p);
      p.catch(() => phkvStores.delete(url));
    }
    return p;
  }

  function open(kind: string, url: string): Promise<Opened> {
    const name = `${kind}/${url}`;
    let p = opened.get(name);
    if (p === undefined) {
      p = (async () => {
        const base = url.split(/[?#]/, 1)[0];
        const trimmed = base.endsWith("/") ? base.slice(0, -1) : base;
        const stem = decodeURIComponent(trimmed.slice(trimmed.lastIndexOf("/") + 1)) || "archive";
        if (kind === "archive") {
          return { archive: await Archive.open(await fetchArchive(url), url, fetchRange, policy), filename: stem };
        }
        let virtual;
        if (kind === "image" && isStoreUrl(url)) {
          virtual = await zarrifyStore(await openHttpStore(url, { policy, ...(fetchStore ? { fetch: fetchStore } : {}) }));
        } else {
          const source = await openSource(url);
          if (kind === "tiff") {
            virtual = await zarrifyIr("tiff", url, source);
            const etag = source.etag?.();
            // spec/zarrify.md §1.2
            // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>
            virtual.sources[0] = { ...virtual.sources[0], size: BigInt(source.size), ...(etag !== undefined ? { etag } : {}) };
          } else {
            virtual = await zarrifySource(url, source);
          }
        }
        const bytes = await writeVzip(virtual);
        return {
          archive: await Archive.open(bytes, url, fetchRange, { ...policy, unverifiablePins: true }),
          filename: `${stem.replace(/\.(ome\.tiff?|tiff?|nd2|n5|zarr)$/i, "")}.vzip`,
        };
      })();
      opened.set(name, p);
      p.catch(() => opened.delete(name));
    }
    return p;
  }

  return async function handle(request: Request): Promise<Response> {
    if (!request.url.startsWith(prefix)) return response(404, "not found");
    if (request.method !== "GET" && request.method !== "HEAD") {
      return response(405, "method not allowed", { Allow: "GET, HEAD" });
    }
    const path = new URL(request.url).pathname.slice(new URL(prefix).pathname.length);
    const m = path.match(/^(image|tiff|archive|phkv)\/([^/]+)\/(.*)$/);
    if (m === null) return response(404, "not found");
    const [, kind, id] = m;
    let key: string;
    let url: string;
    try {
      key = m[3].split("/").map(decodeURIComponent).join("/");
      url = new URL(decodeId(id)).href;
    } catch {
      return response(400, "invalid URL");
    }
    const head = request.method === "HEAD";
    try {
      if (kind === "phkv") return await servePhkv(await openPhkv(url), key, request.headers.get("Range"), head);
      const { archive, filename } = await open(kind, url);
      if (key === ARCHIVE_KEY) {
        return response(200, head ? null : archive.bytes, {
          "Content-Type": "application/zip",
          "Content-Length": String(archive.bytes.length),
          "Content-Disposition": `attachment; filename="${filename.replaceAll('"', "")}"`,
        });
      }
      const entry = key === "" || key.endsWith("/") ? undefined : archive.lookup(key);
      if (entry === undefined) return response(404, `no key ${key}`);
      const size = archive.size(entry);
      const type = key.endsWith(".json") ? "application/json" : key.endsWith(".xml") ? "application/xml" : "application/octet-stream";
      const range = parseRange(request.headers.get("Range"), size);
      if (range === "unsatisfiable") {
        return response(416, "range not satisfiable", { "Content-Range": `bytes */${size}` });
      }
      const headers = { "Content-Type": type, "Accept-Ranges": "bytes" };
      if (range === undefined) {
        const body = head ? null : await archive.read(key);
        return response(200, body!, { ...headers, "Content-Length": String(size) });
      }
      const body = head ? null : await archive.read(key, range.start, range.end);
      return response(206, body!, {
        ...headers,
        "Content-Length": String(range.end - range.start),
        "Content-Range": `bytes ${range.start}-${range.end - 1}/${size}`,
      });
    } catch (e) {
      const message = (e as Error).message;
      if (e instanceof RefkvError) {
        const status = e.errorClass === "request" ? 416 : e.errorClass === "resolution" ? 502 : 500;
        return response(status, message);
      }
      if (e instanceof PhkvError) return response(phkvStatus(e), message);
      // a store profile the core refused names itself (SAFE's does not, as its zip form does not);
      // a rejection before one was chosen is the store's
      if (e instanceof StoreError && e.format !== undefined) {
        const title = STORE_FORMATS[e.format];
        return response(422, title === undefined ? message : `${title}: ${message}`);
      }
      if (e instanceof StoreError) return response(422, `store: ${message}`);
      if (e instanceof ImageError) return response(422, message);
      if (e instanceof StoreLimitError) return response(507, message);
      if (e instanceof StoreReadError) return response(502, message);
      if (e instanceof HttpResolutionError) return response(502, message);
      return response(500, message);
    }
  };
}
