// `#phkvwasm` under Node (package.json "imports"): the phkv reader's wasm32 build, read from
// rust/refkv-phkv/target/web (`just js::phkv-wasm` builds it), or from the file
// $REFKV_PHKV_WASM names.

import fs from "node:fs/promises";

/** The default location of the module: rust/refkv-phkv/wasm/build.sh's output. */
export const PHKV_WASM_URL = new URL("../../rust/refkv-phkv/target/web/refkv_phkv.wasm", import.meta.url);

let url: string | URL | undefined;

/** Where to read the module from, in place of $REFKV_PHKV_WASM or `PHKV_WASM_URL` (a path or a file: URL). */
export function setPhkvWasmUrl(u: string | URL): void {
  url = u;
}

/** Compiles the module. */
export async function compilePhkv(): Promise<WebAssembly.Module> {
  const given = url ?? (process.env.REFKV_PHKV_WASM || PHKV_WASM_URL);
  const path = typeof given === "string" && /^file:/i.test(given) ? new URL(given) : given;
  let bytes: Uint8Array;
  try {
    bytes = await fs.readFile(path);
  } catch (e) {
    throw new Error(`cannot read the phkv reader's wasm module ${String(path)} (build it with \`just js::phkv-wasm\`): ${(e as Error).message}`);
  }
  return WebAssembly.compile(bytes as Uint8Array<ArrayBuffer>);
}
