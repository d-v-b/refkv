// A VSI input (spec/virtualize/vsi.md §15.2
// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/vsi.md#152-the-tile-files>): the `.vsi`
// file named by the input URL, and the tile files beside it, which the Rust core's `vsi` store
// format asks for by key (the probes of ir/store.ts) rather than from a listing, since a plain
// web server cannot list a directory. The files are the objects of a store whose URL is the
// `.vsi` file's directory, each read through a range source of its own (ir/source.ts); a probe
// opens the file, and an object the server answers 404 for is absent. The twin of
// python/src/refkv/virtualize/vsi.py.

import type { ArchiveDesc } from "../../writer.ts";
import { HttpResolutionError } from "../../http.ts";
import { ImageError } from "../common.ts";
import { objectUrl, type Store, StoreError } from "../store.ts";
import { HttpSource, type HttpSourceOptions, type RangeSource } from "./source.ts";
import { virtualizeStoreIr } from "./store.ts";

/** [the directory's URL (ending in `/`), the `.vsi` file's key (its last segment, percent-decoded)]. */
export function splitVsiUrl(url: string): [string, string] {
  const base = url.split(/[?#]/, 1)[0];
  if (base !== url) throw new ImageError(`VSI input ${JSON.stringify(url)} has a query or a fragment`);
  const k = base.lastIndexOf("/");
  const name = base.slice(k + 1);
  let key: string;
  try {
    key = decodeURIComponent(name);
  } catch {
    throw new ImageError(`VSI input ${JSON.stringify(url)}: its file name is not percent-encoded UTF-8`);
  }
  if (k < 0 || !key || key.includes("/")) throw new ImageError(`VSI input ${JSON.stringify(url)} names no file`);
  return [base.slice(0, k + 1), key];
}

/** Opens the http(s) object at `url` as a range source, or undefined when the server answers
 * 404 for it. */
async function probeHttp(url: string, options: HttpSourceOptions): Promise<RangeSource | undefined> {
  try {
    return await new HttpSource(url, options).open();
  } catch (e) {
    if (e instanceof HttpResolutionError && / HTTP 404 /.test(e.message)) return undefined;
    throw e;
  }
}

export interface VsiOptions {
  /** Opens the file `key` beside the `.vsi` file (`key` relative to its directory), or
   * undefined when it does not exist; by default the directory's URL is read over http(s)
   * with `http`'s options, or, when the `.vsi` file's source is an `HttpSource`, with the
   * options it was opened with (the same reader policy and headers). */
  open?: (key: string) => Promise<RangeSource | undefined>;
  http?: HttpSourceOptions;
  mirror?: boolean;
}

/** The files of a VSI input as a store the run probes: `objects` holds the `.vsi` file (read
 * through `source`), then every tile file found. */
export function vsiFiles(url: string, source: RangeSource, options: VsiOptions = {}): Store & { dir: string; key: string } {
  const [dir, key] = splitVsiUrl(url);
  const sources = new Map<string, RangeSource>([[key, source]]);
  const open = options.open ?? ((k: string) => {
    if (!/^https?:\/\//.test(dir)) {
      return Promise.reject(new ImageError(`VSI input ${JSON.stringify(url)}: its tile files can only be opened at an http(s) URL`));
    }
    return probeHttp(objectUrl(dir, k), options.http ?? (source instanceof HttpSource ? source.options : {}));
  });
  const store = {
    dir,
    key,
    url: dir,
    objects: new Map<string, number>([[key, source.size]]),
    listed: 1,
    requests: 0,
    concurrency: source.concurrency,
    async probe(k: string): Promise<number | undefined> {
      const have = store.objects.get(k);
      if (have !== undefined) return have;
      const s = await open(k);
      if (s === undefined) return undefined;
      sources.set(k, s);
      store.requests++;
      return s.size;
    },
    async read(k: string): Promise<Uint8Array> {
      return store.readRange(k, 0, store.objects.get(k) ?? 0);
    },
    async readRange(k: string, offset: number, length: number): Promise<Uint8Array> {
      const s = sources.get(k);
      if (s === undefined) throw new StoreError(`${k}: not a file of the VSI input`, "vsi");
      return s.read(offset, length);
    },
  };
  return store;
}

/** Virtualizes the VSI input at `url` (its `.vsi` file opened as `source`): a store run of the
 * core's `vsi` format over the files, probed as the run asks for them. The description's
 * sources are the files some reference uses, their sizes pinned. */
export async function virtualizeVsi(url: string, source: RangeSource, options: VsiOptions = {}): Promise<ArchiveDesc & { summary: Record<string, unknown>; store: Store }> {
  const store = vsiFiles(url, source, options);
  const desc = await virtualizeStoreIr("vsi", store, { mirror: options.mirror ?? true, concurrency: source.concurrency });
  return { ...desc, store };
}
