#!/usr/bin/env bash
# Builds the core's wasm32 module for the browser code: the workspace's `wasm` profile (LTO,
# one codegen unit, opt-level 2, panic=abort), then binaryen's `wasm-opt -O3` when it
# is installed (without it the module is about 19% larger, and up to 8% slower). Writes
# rust/target/web/refkv_ir.wasm. `build.sh test` builds the test module instead:
# the same, with the test-only hooks (feature `test-hooks`), in its own target
# directory, into rust/target/web-test/refkv_ir.wasm; the shipped module never
# has them. `-p refkv-ir` builds the core alone, so that no other member's features
# (refkv-vzip's zlib) are unified into it.
set -euo pipefail
rust="$(cd "$(dirname "$0")/../.." && pwd)"
target="$rust/target"
if [ "${1:-}" = test ]; then
  cargo build --profile wasm --target wasm32-unknown-unknown --manifest-path "$rust/Cargo.toml" -p refkv-ir \
    --features test-hooks --target-dir "$target/test-hooks"
  raw="$target/test-hooks/wasm32-unknown-unknown/wasm/refkv_ir.wasm"
  out="$target/web-test/refkv_ir.wasm"
else
  cargo build --profile wasm --target wasm32-unknown-unknown --manifest-path "$rust/Cargo.toml" -p refkv-ir
  raw="$target/wasm32-unknown-unknown/wasm/refkv_ir.wasm"
  out="$target/web/refkv_ir.wasm"
fi
mkdir -p "$(dirname "$out")"
if command -v wasm-opt > /dev/null; then
  wasm-opt -O3 --enable-bulk-memory --enable-nontrapping-float-to-int --enable-sign-ext --enable-mutable-globals \
    "$raw" -o "$out"
else
  echo "wasm/build.sh: wasm-opt (binaryen) not found: the module is not optimized further" >&2
  cp "$raw" "$out"
fi
