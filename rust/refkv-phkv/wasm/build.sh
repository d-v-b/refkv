#!/usr/bin/env bash
# Builds the phkv reader's wasm32 module for the browser code: cargo's `wasm` profile (LTO, one
# codegen unit, opt-level 2, panic=abort), then binaryen's `wasm-opt -O3` when it is installed.
# Writes rust/refkv-phkv/target/web/refkv_phkv.wasm.
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
