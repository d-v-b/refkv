// refkv's virtualizer for a host that reads the input itself, such as a viewer's worker (the
// Neuroglancer fork's `refkv:` kvstore), and the decoder of the `refkv.fits_tiles` codec, both the
// Rust core's: the input file, read through the host's own reads, or
// the store at an http(s) URL, virtualized by the Rust core (spec/virtualize.md
// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize.md>) and written as a vzip archive
// (spec/refkv.md <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md>), which the host reads
// as any other. build.mjs bundles this module, with what it imports, as refkv_embed.js, beside
// refkv_ir.wasm; the host says where that is with `setIrWasmUrl`, or
// gives the module itself with `setIrWasmModule`.

import type { Policy } from "./archive.ts";
import { virtualizeSource, virtualizeStore } from "./virtualize/index.ts";
import { readerSource, type RangeSource } from "./virtualize/ir/source.ts";
import { ir, out, put } from "./virtualize/ir/wasm.ts";
import { type ArchiveDesc, writeVzip } from "./writer.ts";

export { setIrWasmModule, setIrWasmUrl } from "./virtualize/ir/wasm_browser.ts";

/** A file the host reads: its URL (recorded as the archive's source), its size, and its bytes. */
export interface FileInput {
  url: string;
  size: number;
  /** Exactly `length` bytes at `offset`. */
  read(offset: number, length: number): Promise<Uint8Array>;
  /** For a VSI input: the file `key` beside it (relative to its directory), or undefined when
   * there is none. */
  open?(key: string): Promise<Omit<FileInput, "url" | "open"> | undefined>;
}

export interface EmbedOptions {
  /** Record each source's size (spec/refkv.md §5
   * <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#5-source-table>), default true. A
   * reader that cannot check a pin of a source refuses it, so a host whose reader checks none
   * for its source's scheme leaves them out. */
  pins?: boolean;
  /** The reader policy a store URL is listed and read under (spec/refkv.md §6.7
   * <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>; default: private
   * and special hosts refused). */
  policy?: Policy;
}

export interface Virtualized {
  /** The vzip archive. */
  archive: Uint8Array;
  /** The profile that virtualized the input (`summary.format`, for the Rust core's own choice). */
  format: string;
  summary: object;
}

const READS_AT_ONCE = 8;

function unpinned(desc: ArchiveDesc): void {
  desc.sources = desc.sources.map(({ size: _size, etag: _etag, modifiedNotAfter: _m, ...s }) => s);
}

/** Virtualizes the file `input`, or the store at `input` (an http(s) URL whose path ends in
 * `/`, listed and read by refkv under `options.policy`), and writes its vzip archive. */
export async function virtualizeToArchive(input: FileInput | string, options: EmbedOptions = {}): Promise<Virtualized> {
  let desc: ArchiveDesc & { format: string; summary: object };
  if (typeof input === "string") {
    desc = await virtualizeStore(input, { policy: options.policy ?? {} });
  } else {
    const source: RangeSource = readerSource((o, n) => input.read(o, n), input.size, { concurrency: READS_AT_ONCE });
    const open = input.open;
    desc = await virtualizeSource(input.url, source, open ? {
      open: async (key) => {
        const file = await open(key);
        return file && readerSource((o, n) => file.read(o, n), file.size, { concurrency: READS_AT_ONCE });
      },
    } : {});
  }
  if (options.pins === false) unpinned(desc);
  const { format, summary } = desc;
  return { archive: await writeVzip(desc), format: (summary as { format?: string }).format ?? format, summary };
}

/** Decodes a chunk of the `refkv.fits_tiles` codec (spec/virtualize/fits.md §3.3
 * <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/fits.md#33-tile-compressed-images>)
 * with the codec's `configuration` into the pixels of the chunk shape `shape`, little-endian
 * values of the array's data type, as the Rust core decodes it (refkv.zarr's codec too). */
export async function decodeFitsTiles(chunk: Uint8Array, configuration: unknown, shape: number[]): Promise<Uint8Array> {
  const w = await ir();
  const config = new TextEncoder().encode(JSON.stringify(configuration));
  const dims = new Uint8Array(shape.length * 8);
  shape.forEach((n, i) => new DataView(dims.buffer).setBigUint64(i * 8, BigInt(n), true));
  const pc = put(w, chunk);
  const pg = put(w, config);
  const ps = put(w, dims);
  try {
    const n = Number(w.vz_fits_tiles_decode(pc, chunk.length, pg, config.length, ps, shape.length));
    if (n < 0) throw new Error(`refkv.fits_tiles: ${new TextDecoder().decode(out(w, -n))}`);
    return out(w, n);
  } finally {
    w.vz_free(pc, chunk.length);
    w.vz_free(pg, config.length);
    w.vz_free(ps, dims.length);
  }
}
