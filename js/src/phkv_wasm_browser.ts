// `#phkvwasm` in a browser (package.json "imports"): the phkv reader's wasm32 build, which
// build.mjs copies next to the bundles (dist/refkv_phkv.wasm). It is fetched relative to the
// running script (a service worker's location, or a page's), or from the URL `setPhkvWasmUrl`
// gives, and compiled while it streams.

let url: string | URL | undefined;
let given: Promise<WebAssembly.Module> | undefined;

/** Where to fetch the module from, in place of `refkv_phkv.wasm` next to the script. */
export function setPhkvWasmUrl(u: string | URL): void {
  url = u;
}

/** The module itself, compiled by the host, in place of fetching it. */
export function setPhkvWasmModule(m: WebAssembly.Module | Promise<WebAssembly.Module>): void {
  given = Promise.resolve(m);
}

/** Compiles the module. */
export async function compilePhkv(): Promise<WebAssembly.Module> {
  if (given !== undefined) return given;
  const at = url ?? new URL("refkv_phkv.wasm", (globalThis as { location?: { href: string } }).location?.href);
  const response = await fetch(at);
  if (!response.ok) throw new Error(`cannot fetch the phkv reader's wasm module ${String(at)}: HTTP ${response.status}`);
  if ((response.headers.get("Content-Type") ?? "").split(";")[0].trim() === "application/wasm") {
    return WebAssembly.compileStreaming(response);
  }
  return WebAssembly.compile(await response.arrayBuffer());
}
