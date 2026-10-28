#!/usr/bin/env bash
# Builds the phkv reader's wasm32 module for the browser code: cargo's `wasm` profile (LTO, one
# codegen unit, opt-level 2, panic=abort), then binaryen's `wasm-opt -O3` when it is installed.
# Writes rust/refkv-phkv/target/web/refkv_phkv.wasm.
#
# The module holds the C zlib and zstd (the crates' build scripts compile them with clang for
# wasm32), so it needs a clang that targets wasm32 and an archiver that can archive wasm objects.
# macOS's own `ar` cannot: it makes empty libraries without a word, and the linker then turns
# every C function it misses into an import, so the build succeeds and the module cannot be
# instantiated. A module with imports is therefore an error here, and the archiver is llvm-ar
# when the Rust toolchain has one (rustup's `llvm-tools` component) or $PATH does.
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
if [ -z "${AR_wasm32_unknown_unknown:-}" ]; then
  host="$(rustc -vV | sed -n 's/^host: //p')"
  toolchain_ar="$(rustc --print sysroot)/lib/rustlib/$host/bin/llvm-ar"
  if [ -x "$toolchain_ar" ]; then
    export AR_wasm32_unknown_unknown="$toolchain_ar"
  elif command -v llvm-ar > /dev/null; then
    export AR_wasm32_unknown_unknown="llvm-ar"
  fi
fi
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
# a module with imports could not be instantiated by the host (see above)
if command -v node > /dev/null; then
  if ! node -e '
    const m = new WebAssembly.Module(require("fs").readFileSync(process.argv[1]));
    const imports = WebAssembly.Module.imports(m).map((i) => `${i.module}.${i.name}`);
    if (imports.length > 0) {
      console.error("wasm/build.sh: the module imports " + imports.join(", ") + ": a C library was not linked in (an archiver that cannot archive wasm objects?); set AR_wasm32_unknown_unknown to llvm-ar");
      process.exit(1);
    }
  ' "$out"; then
    rm -f "$out" # not left for a test or a bundle to pick up
    exit 1
  fi
fi
