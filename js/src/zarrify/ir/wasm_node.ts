// `#irwasm` under Node (package.json "imports"): the Rust core's wasm32 build,
// read from rust/refkv-ir/target/web (`just js::wasm` builds it), or from the
// file $REFKV_IR_WASM names.

import fs from "node:fs/promises";

/** The default location of the module: rust/refkv-ir/wasm/build.sh's output. */
export const WASM_URL = new URL("../../../../rust/refkv-ir/target/web/refkv_ir.wasm", import.meta.url);

let url: string | URL | undefined;

/** Where to read the module from, in place of $REFKV_IR_WASM or `WASM_URL` (a path or a file: URL). */
export function setIrWasmUrl(u: string | URL): void {
  url = u;
}

/** Compiles the module. */
export async function compileIr(): Promise<WebAssembly.Module> {
  const given = url ?? (process.env.REFKV_IR_WASM || WASM_URL);
  // a file: URL given as a string is a URL, not a path (fs.readFile reads a URL object
  // as one, on Windows too)
  const path = typeof given === "string" && /^file:/i.test(given) ? new URL(given) : given;
  let bytes: Uint8Array;
  try {
    bytes = await fs.readFile(path);
  } catch (e) {
    throw new Error(`cannot read the IR core's wasm module ${String(path)} (build it with \`just js::wasm\`): ${(e as Error).message}`);
  }
  return WebAssembly.compile(bytes as Uint8Array<ArrayBuffer>);
}
