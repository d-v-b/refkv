// HEVC decoding with libde265.wasm, libde265 (LGPL-3.0) built with emscripten
// by the Neuroglancer fork (https://github.com/d-v-b/neuroglancer, branch vzip,
// src/sliceview/hevc/), which build.mjs copies beside the page. This is the
// fork's index.ts, cut down to what contrast sampling needs: one plane of the
// pictures a `refkv.stream` chunk outputs
// ([conventions, section 10](https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#10-coded-streams)),
// in output order, without placing them by position.

interface Exports {
  memory: WebAssembly.Memory;
  _initialize(): void;
  malloc(size: number): number;
  free(ptr: number): void;
  hevc_new(): number;
  hevc_free(ctx: number): void;
  hevc_push_nal(ctx: number, data: number, length: number, pts: number): number;
  hevc_flush(ctx: number): number;
  hevc_decode(ctx: number, more: number): number;
  hevc_next_picture(ctx: number): number;
  hevc_picture_info(img: number, info: number): void;
  hevc_plane(img: number, channel: number, info: number): number;
}

const ENOSYS = 52; // WASI's "not implemented"
const env = {
  emscripten_notify_memory_growth: () => {},
  fd_write: () => ENOSYS,
  fd_close: () => ENOSYS,
  fd_seek: () => ENOSYS,
};
const IMAGE_BUFFER_FULL = 9;
const WAITING_FOR_INPUT_DATA = 13;

let module: Promise<Exports> | undefined;

function getModule(url: string | URL): Promise<Exports> {
  module ??= WebAssembly.instantiateStreaming(fetch(url), { env, wasi_snapshot_preview1: env }).then((r) => {
    const e = r.instance.exports as unknown as Exports;
    e._initialize();
    return e;
  });
  return module;
}

function varint(b: Uint8Array, at: number): [number, number] {
  let v = 0;
  for (let scale = 1; at < b.length; scale *= 128) {
    const c = b[at++];
    v += (c & 0x7f) * scale;
    if (c < 0x80) return [v, at];
  }
  throw new Error("hevc: a chunk's header ends inside a varint");
}

/** The frames of a `refkv.stream` chunk, and whether each is output (its position is not -1). */
function frames(chunk: Uint8Array): { data: Uint8Array; shown: boolean }[] {
  let [n, at] = varint(chunk, 0);
  const sizes: [number, boolean][] = [];
  let position = -1;
  for (let i = 0; i < n; ++i) {
    let size, z;
    [size, at] = varint(chunk, at);
    [z, at] = varint(chunk, at);
    position += z % 2 === 0 ? z / 2 : -(z + 1) / 2;
    sizes.push([size, position >= 0]);
  }
  return sizes.map(([size, shown]) => {
    const data = chunk.subarray(at, at + size);
    at += size;
    return { data, shown };
  });
}

/** The parameter sets of an hvcC box's payload, and its NAL unit lengths' size. */
function parameterSets(hvcc: Uint8Array): [Uint8Array[], number] {
  const nals: Uint8Array[] = [];
  let at = 23;
  for (let a = 0; a < hvcc[22]; ++a) {
    const count = (hvcc[at + 1] << 8) | hvcc[at + 2];
    at += 3;
    for (let k = 0; k < count; ++k) {
      const length = (hvcc[at] << 8) | hvcc[at + 1];
      nals.push(hvcc.subarray(at + 2, at + 2 + length));
      at += 2 + length;
    }
  }
  return [nals, (hvcc[21] & 3) + 1];
}

/**
 * The samples of plane `plane` (0 Y, 1 U, 2 V) of the pictures a `refkv.stream` HEVC chunk
 * outputs, as bytes (little-endian 16-bit samples for more than 8 bits).
 */
export async function decodeHevcPlane(
  wasmUrl: string | URL, setup: Uint8Array, chunk: Uint8Array, plane: number,
): Promise<Uint8Array> {
  const m = await getModule(wasmUrl);
  const [sets, lengthSize] = parameterSets(setup);
  const units = frames(chunk);
  const total = sets.reduce((n, s) => n + s.length, 0) + units.reduce((n, u) => n + u.data.length, 0);
  const input = m.malloc(total + 48);
  if (input === 0) throw new Error("hevc: out of memory");
  const scratch = input + total + ((4 - (total % 4)) % 4);
  const ctx = m.hevc_new();
  const out: Uint8Array[] = [];
  try {
    let at = input;
    const push = (nal: Uint8Array, pts: number) => {
      new Uint8Array(m.memory.buffer).set(nal, at);
      if (m.hevc_push_nal(ctx, at, nal.length, pts) !== 0) throw new Error("hevc: a NAL unit is refused");
      at += nal.length;
    };
    for (const s of sets) push(s, -1);
    units.forEach(({ data }, k) => {
      for (let i = 0; i < data.length; ) {
        let length = 0;
        for (let j = 0; j < lengthSize; ++j) length = length * 256 + data[i + j];
        i += lengthSize;
        if (i + length > data.length) throw new Error("hevc: a NAL unit overruns its frame");
        push(data.subarray(i, i + length), k);
        i += length;
      }
    });
    m.hevc_flush(ctx);
    for (;;) {
      const error = m.hevc_decode(ctx, scratch);
      const more = new Int32Array(m.memory.buffer, scratch, 1)[0];
      let pictures = 0;
      for (let img; (img = m.hevc_next_picture(ctx)) !== 0; ++pictures) {
        m.hevc_picture_info(img, scratch + 4);
        if (!units[new Int32Array(m.memory.buffer, scratch + 4, 1)[0]]?.shown) continue;
        const ptr = m.hevc_plane(img, plane, scratch + 24);
        if (ptr === 0) continue;
        const [w, h, stride, bits] = new Int32Array(m.memory.buffer, scratch + 24, 4);
        const row = bits > 8 ? 2 * w : w;
        const samples = new Uint8Array(row * h);
        for (let r = 0; r < h; ++r) samples.set(new Uint8Array(m.memory.buffer, ptr + r * stride, row), r * row);
        out.push(samples);
      }
      if (error === IMAGE_BUFFER_FULL && pictures > 0) continue;
      if (error === WAITING_FOR_INPUT_DATA) break;
      if (error !== 0) throw new Error(`hevc: libde265's error ${error}`);
      if (!more) break;
    }
  } finally {
    if (ctx !== 0) m.hevc_free(ctx);
    m.free(input);
  }
  const bytes = new Uint8Array(out.reduce((n, p) => n + p.length, 0));
  let at = 0;
  for (const p of out) {
    bytes.set(p, at);
    at += p.length;
  }
  return bytes;
}
