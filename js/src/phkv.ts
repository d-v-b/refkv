// A phkv store (spec/bindings/phkv.md) read from a browser or a service worker: the Rust reader of
// rust/refkv-phkv built for wasm32 (`just js::phkv-wasm`), driven here.
//
// The module does no I/O of its own. A call that reads what the host has not fed answers "need",
// with the ranges it read; this driver reads them (all at once, in parallel), feeds them, and
// calls again, until the call is done. The host gives the reads: `PhkvIo`.

/** Where a store's bytes come from. */
export interface PhkvIo {
  /** The store file's size in bytes. */
  size: number;
  /** Bytes `[offset, offset + length)` of the store file. */
  read(offset: number, length: number): Promise<Uint8Array>;
  /** Bytes `[offset, offset + length)` of a `url` source, which the store's references name. The
   * host applies the pins of `source` (spec/refkv.md §5.1) and its own policy. */
  readSource(source: PhkvSource, offset: number, length: number): Promise<Uint8Array>;
}

/** An entry of the store's source table, a `url` source or another: a `key` source is read from the
 * store itself, and a `data` source holds its bytes, so only a url source is read through `PhkvIo`. */
export interface PhkvSource {
  index: number;
  kind: "url" | "key" | "data";
  url?: string;
  key?: string;
  size?: number;
  etag?: string;
  modified_not_after?: number;
}

/** A window of a value: the whole value, `[start, end)`, from `start` on, or the last `n` bytes. */
export type PhkvRange = { start: number; end?: number } | { suffix: number };

/** A read failure, of one of the classes of spec/refkv.md §6.4. */
export class PhkvError extends Error {
  readonly errorClass: string;
  constructor(errorClass: string, message: string) {
    super(`${errorClass} error: ${message}`);
    this.name = "PhkvError";
    this.errorClass = errorClass;
  }
}

const DONE = 0;
const NEED = 1;
const ABSENT = 2;
const CLASSES = ["archive", "entry", "body", "payload", "resolution", "request", "property", "io"];
/** More rounds than any store needs (open takes a few, a get at most about four). */
const MAX_ROUNDS = 64;

interface Exports {
  memory: WebAssembly.Memory;
  phkv_alloc(n: number): number;
  phkv_free(p: number, n: number): void;
  phkv_out(): number;
  phkv_out_len(): number;
  phkv_new(size: bigint): number;
  phkv_close(h: number): void;
  phkv_feed(h: number, offset: bigint, p: number, n: number): number;
  phkv_feed_source(h: number, index: number, offset: bigint, p: number, n: number): number;
  phkv_open(h: number): number;
  phkv_key_count(h: number): bigint;
  phkv_source_count(h: number): number;
  phkv_source(h: number, index: number): number;
  phkv_get(h: number, key: number, keyLen: number, mode: number, a: bigint, b: bigint): number;
  phkv_list(h: number, prefix: number, len: number): number;
}

/** An open phkv store. */
export class PhkvStore {
  readonly #x: Exports;
  readonly #io: PhkvIo;
  readonly #h: number;
  #sources: Map<number, Promise<PhkvSource>> = new Map();
  #closed = false;

  private constructor(x: Exports, io: PhkvIo, h: number) {
    this.#x = x;
    this.#io = io;
    this.#h = h;
  }

  /** Opens the store: its tail, then the sections before it, are read through `io`. */
  static async open(module: WebAssembly.Module, io: PhkvIo): Promise<PhkvStore> {
    const x = new WebAssembly.Instance(module, {}).exports as unknown as Exports;
    const store = new PhkvStore(x, io, x.phkv_new(BigInt(io.size)));
    try {
      await store.#run(() => x.phkv_open(store.#h));
    } catch (e) {
      store.close();
      throw e;
    }
    return store;
  }

  /** The number of keys. */
  get keyCount(): number {
    return Number(this.#x.phkv_key_count(this.#h));
  }

  /** Releases the session (the instance is garbage collected with the store). */
  close(): void {
    if (!this.#closed) this.#x.phkv_close(this.#h);
    this.#closed = true;
  }

  /** The value of `key` (or a window of it), or `undefined` if the store has no such key. */
  async get(key: string, range?: PhkvRange): Promise<Uint8Array | undefined> {
    const [mode, a, b] = range === undefined ? [0, 0n, 0n] : "suffix" in range ? [3, BigInt(range.suffix), 0n] : range.end === undefined ? [2, BigInt(range.start), 0n] : [1, BigInt(range.start), BigInt(range.end)];
    return this.#run(() => this.#withBytes(new TextEncoder().encode(key), (p, n) => this.#x.phkv_get(this.#h, p, n, mode as number, a as bigint, b as bigint)));
  }

  /** The keys that start with `prefix`, in UTF-8 order. */
  async list(prefix = ""): Promise<string[]> {
    const b = (await this.#run(() => this.#withBytes(new TextEncoder().encode(prefix), (p, n) => this.#x.phkv_list(this.#h, p, n)))) as Uint8Array;
    const v = new DataView(b.buffer, b.byteOffset, b.byteLength);
    const out: string[] = [];
    const text = new TextDecoder("utf-8", { fatal: true });
    for (let i = 0; i < b.length; ) {
      const n = v.getUint32(i, true);
      out.push(text.decode(b.subarray(i + 4, i + 4 + n)));
      i += 4 + n;
    }
    return out;
  }

  /** Source `index` of the source table. */
  source(index: number): Promise<PhkvSource> {
    let s = this.#sources.get(index);
    if (s === undefined) {
      if (this.#x.phkv_source(this.#h, index) !== DONE) this.#fail();
      s = Promise.resolve({ index, ...JSON.parse(new TextDecoder().decode(this.#out())) } as PhkvSource);
      this.#sources.set(index, s);
    }
    return s;
  }

  /** The bytes the last call left in the module's output buffer, copied out. */
  #out(): Uint8Array {
    return new Uint8Array(this.#x.memory.buffer, this.#x.phkv_out(), this.#x.phkv_out_len()).slice();
  }

  #fail(): never {
    const b = this.#out();
    throw new PhkvError(CLASSES[b[0]] ?? "unknown", new TextDecoder().decode(b.subarray(1)));
  }

  /** Writes `bytes` into the module's memory for the duration of `call`, which gets their place. */
  #withBytes<T>(bytes: Uint8Array, call: (p: number, n: number) => T): T {
    const n = bytes.length;
    const p = this.#x.phkv_alloc(n);
    try {
      // the memory may have grown in the allocation: take its buffer after it
      new Uint8Array(this.#x.memory.buffer, p, n).set(bytes);
      return call(p, n);
    } finally {
      this.#x.phkv_free(p, n);
    }
  }

  /** Runs an operation by rounds: the bytes it left once it is done, or `undefined` for a key the
   * store does not have. The output buffer is the module's one, so it is copied out in the same
   * step as the call that filled it: a call of another operation may run at the next `await`. */
  async #run(call: () => number): Promise<Uint8Array | undefined> {
    for (let round = 0; round < MAX_ROUNDS; round++) {
      const status = call();
      if (status === DONE) return this.#out();
      if (status === ABSENT) return undefined;
      if (status !== NEED) this.#fail();
      const wants = this.#out();
      const v = new DataView(wants.buffer, wants.byteOffset, wants.byteLength);
      await Promise.all(
        Array.from({ length: wants.length / 24 }, (_, i) => {
          const [source, offset, length] = [v.getBigUint64(24 * i, true), Number(v.getBigUint64(24 * i + 8, true)), Number(v.getBigUint64(24 * i + 16, true))];
          return this.#fetch(Number(source), offset, length);
        }),
      );
    }
    throw new PhkvError("io", `not done in ${MAX_ROUNDS} rounds`);
  }

  /** Reads one range a call wanted, and feeds it: `source` is 0 for the store's file, else a source's index + 1. */
  async #fetch(source: number, offset: number, length: number): Promise<void> {
    const bytes = source === 0 ? await this.#io.read(offset, length) : await this.#io.readSource(await this.source(source - 1), offset, length);
    if (bytes.length !== length) throw new PhkvError(source === 0 ? "io" : "resolution", `a read of ${length} bytes at ${offset} gave ${bytes.length}`);
    this.#withBytes(bytes, (p, n) => (source === 0 ? this.#x.phkv_feed(this.#h, BigInt(offset), p, n) : this.#x.phkv_feed_source(this.#h, source - 1, BigInt(offset), p, n)));
  }
}
