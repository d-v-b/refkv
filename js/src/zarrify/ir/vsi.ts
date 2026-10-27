// An input read with the files beside it: a VSI input (spec/zarrify/vsi.md §9.2
// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#92-the-tile-files>), the `.vsi`
// file named by the input URL and the tile files beside it, and a NRRD input
// (spec/zarrify/nrrd.md §8.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nrrd.md#83-the-data-file>),
// the header file named by the input URL and a detached header's data file. The Rust core's
// `vsi` and `nrrd` store formats ask for those files by key (the probes of ir/store.ts) rather
// than from a listing, since a plain web server cannot list a directory. The files are the
// objects of a store whose URL is the named file's directory, each read through a range source
// of its own (ir/source.ts); a probe opens the file, and an object the server answers 404 for is
// absent. The twin of python/src/refkv/zarrify/store/beside.py.

import type { ArchiveDesc } from "../../writer.ts";
import { HttpResolutionError } from "../../http.ts";
import { ImageError } from "../common.ts";
import { objectUrl, type Store, StoreError } from "../store.ts";
import { HttpSource, type HttpSourceOptions, type RangeSource } from "./source.ts";
import { zarrifyStoreIr } from "./store.ts";

/** The formats whose input is read with the files beside it: each one's title, as a rejection
 * names it. */
export const BESIDE = { vsi: "VSI", nrrd: "NRRD" } as const;
export type BesideFormat = keyof typeof BESIDE;

/** [the directory's URL (ending in `/`), the named file's key (its last segment,
 * percent-decoded)] of an input of the format `title` names (VSI, NRRD). */
export function splitVsiUrl(url: string, title = "VSI"): [string, string] {
  const base = url.split(/[?#]/, 1)[0];
  if (base !== url) throw new ImageError(`${title} input ${JSON.stringify(url)} has a query or a fragment`);
  const k = base.lastIndexOf("/");
  const name = base.slice(k + 1);
  let key: string;
  try {
    key = decodeURIComponent(name);
  } catch {
    throw new ImageError(`${title} input ${JSON.stringify(url)}: its file name is not percent-encoded UTF-8`);
  }
  if (k < 0 || !key || key.includes("/")) throw new ImageError(`${title} input ${JSON.stringify(url)} names no file`);
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
  /** Opens the file `key` beside the named file (`key` relative to its directory), or
   * undefined when it does not exist; by default the directory's URL is read over http(s)
   * with `http`'s options, or, when the named file's source is an `HttpSource`, with the
   * options it was opened with (the same reader policy and headers). */
  open?: (key: string) => Promise<RangeSource | undefined>;
  http?: HttpSourceOptions;
  mirror?: boolean;
}

/** The files of a VSI or NRRD input as a store the run probes: `objects` holds the named file
 * (read through `source`), then every file found beside it. */
export function vsiFiles(url: string, source: RangeSource, options: VsiOptions = {}, format: BesideFormat = "vsi"): Store & { dir: string; key: string } {
  const title = BESIDE[format];
  const [dir, key] = splitVsiUrl(url, title);
  const sources = new Map<string, RangeSource>([[key, source]]);
  const open = options.open ?? ((k: string) => {
    if (!/^https?:\/\//.test(dir)) {
      return Promise.reject(new ImageError(`${title} input ${JSON.stringify(url)}: the files beside it can only be opened at an http(s) URL`));
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
      if (s === undefined) throw new StoreError(`${k}: not a file of the ${title} input`, format);
      return s.read(offset, length);
    },
  };
  return store;
}

/** Zarrifies the VSI or NRRD input at `url` (its named file opened as `source`): a store run of
 * the core's `format` over the files, probed as the run asks for them. The description's
 * sources are the files some reference uses, their sizes pinned. */
export async function zarrifyVsi(url: string, source: RangeSource, options: VsiOptions = {}, format: BesideFormat = "vsi"): Promise<ArchiveDesc & { summary: Record<string, unknown>; store: Store }> {
  const store = vsiFiles(url, source, options, format);
  const desc = await zarrifyStoreIr(format, store, { mirror: options.mirror ?? true, concurrency: source.concurrency });
  return { ...desc, store };
}
