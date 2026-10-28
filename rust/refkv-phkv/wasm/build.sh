#!/usr/bin/env bash
# Builds the phkv reader's wasm32 module for the browser code: cargo's `wasm` profile (LTO, one
# codegen unit, opt-level 2, panic=abort), then binaryen's `wasm-opt -O3` when it is installed.
# Writes rust/refkv-phkv/target/web/refkv_phkv.wasm.
#
# The module is pure Rust (DEFLATE is flate2's miniz_oxide backend, Zstandard is ruzstd), so it
# needs no C compiler for wasm32, and nothing from its host: a module with imports is an error.
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
cargo build --profile wasm --target wasm32-unknown-unknown --manifest-path "$here/Cargo.toml"
raw="$here/target/wasm32-unknown-unknown/wasm/refkv_phkv.wasm"
out="$here/target/web/refkv_phkv.wasm"
mkdir -p "$(dirname "$out")"
if command -v wasm-opt > /dev/null; then
  wasm-opt -O3 --enable-bulk-memory --enable-nontrapping-float-to-int --enable-sign-ext --enable-mutable-globals \
    "$raw" -o "$out"
else
  echo "wasm/build.sh: wasm-opt (binaryen) not found: the module is not optimized further" >&2
  cp "$raw" "$out"
fi
# a module with imports could not be instantiated by the host
if command -v node > /dev/null; then
  if ! node -e '
    const m = new WebAssembly.Module(require("fs").readFileSync(process.argv[1]));
    const imports = WebAssembly.Module.imports(m).map((i) => `${i.module}.${i.name}`);
    if (imports.length > 0) {
      console.error("wasm/build.sh: the module imports " + imports.join(", ") + ": a dependency that needs a C library was added");
      process.exit(1);
    }
  ' "$out"; then
    rm -f "$out" # not left for a test or a bundle to pick up
    exit 1
  fi
fi
