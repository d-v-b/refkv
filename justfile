# refkv. Run `just` to list recipes; `just python::build`, `just js::test`,
# `just impls::rust::build` and so on reach the packages' own justfiles.

set shell := ["bash", "-euo", "pipefail", "-c"]

mod python
mod js
mod impls "conformance/impls"

idr_tiff := "https://ftp.ebi.ac.uk/pub/databases/IDR/idr0096-tratwal-marrowquant/20210609-ftp-ome-tiffs/4000_d11_m5_LT_2%20(20x_01).ome.tiff"

[private]
default:
    @just --list --list-submodules

# Test the Python package (python/src/refkv; it zarrifies every format of docs/zarrify/README.md with the Rust core, built first)
test *args: ir-build
    uv run pytest -q python/tests {{args}}

# Build refkv (python/: the package, with its bindings to the IR core rust/refkv-ir, the
# parsers, the read planner, the projections and the mirror, as the module refkv._core) into
# the workspace's environment: `uv sync` (and `uv run`) build it with maturin, in release, and
# again whenever the bindings, the core's Rust sources or its source models change
# (python/pyproject.toml's cache-keys)
ir-build:
    uv sync -q --locked

# Build the IR core for the browser (wasm32, without the Python bindings) and run its file
# parsers, on one fixture of each file profile of the registry (spec/tools/profiles.py sample),
# and its Zarr v2 and N5 store formats, from Node
ir-wasm:
    rust/refkv-ir/wasm/build.sh
    node rust/refkv-ir/wasm/nd2.mjs fixtures/nd2/nd2_tz_uint16.nd2
    node rust/refkv-ir/wasm/run.mjs $(uv run python spec/tools/profiles.py sample)
    node rust/refkv-ir/wasm/store.mjs fixtures/zarr2/zarr2_hierarchy > /dev/null
    node rust/refkv-ir/wasm/store.mjs --format n5 fixtures/n5/n5_cosem > /dev/null

# Test the IR core (Rust) and its Python host (after ir-build)
ir-test *args: ir-rust-test ir-build
    uv run pytest -q python/tests/ir {{args}}

# Test the IR core's Rust crate, and build its Python bindings' crate (python/Cargo.toml) alone
ir-rust-test:
    cargo test --release --manifest-path rust/refkv-ir/Cargo.toml
    just python::cargo-test

# Test the vzip archive writer (rust/refkv-vzip), the phkv store's reader and writers
# (rust/refkv-phkv), and the `vzip convert` CLI (rust/refkv-cli), and build the CLI
# (rust/refkv-cli/target/release/vzip)
cli-test:
    cargo test --release --manifest-path rust/refkv-vzip/Cargo.toml
    cargo test --release --manifest-path rust/refkv-phkv/Cargo.toml
    cargo test --release --manifest-path rust/refkv-cli/Cargo.toml
    cargo build --release --manifest-path rust/refkv-cli/Cargo.toml

# Convert the fixture corpus with `python -m refkv.zarrify` and with the Rust CLI, from local
# paths and over HTTP (a server on 127.0.0.1), with and without --checksums, and check that both
# give the same archives, byte for byte (rust/refkv-cli/tests/identical.py; e.g. `--only nd2`)
cli-identical *args: ir-build
    cargo build --release --manifest-path rust/refkv-cli/Cargo.toml
    uv run python rust/refkv-cli/tests/identical.py {{args}}

# Benchmark phkv against vzip on local files and over HTTP: requests, bytes and latency
# (rust/refkv-phkv/bench/README.md; e.g. `just phkv-bench --sizes 1000 10000 --rtt-ms 20`)
phkv-bench *args: ir-build
    cargo build --release --example bench --manifest-path rust/refkv-phkv/Cargo.toml
    uv run python rust/refkv-phkv/bench/bench.py {{args}}

# Run every offline test: the Python reference, the browser code, the independent implementations,
# and the Rust CLI against the Python one
test-all: test js::test impls::test cli-test cli-identical

# Type-check the browser code
check: js::typecheck

# Check the package without its extras, and with only the zarr extra (`just python::check`):
# pass the Python of an environment where only `pip install python/` (or
# `pip install 'python/[zarr]'`, with mode=zarr) was run (default: a fresh one of each); a
# relative path is the current directory's
base-install-check python="" mode="":
    just python::check {{quote(if python == "" { "" } else { absolute_path(python) })}} {{quote(mode)}}

# Run the example of docs/virtual-datasets.md on NOAA's data (network: 1 to 1.5 minutes,
# 50 MB), in a temporary directory
example-oisst:
    cd "$(mktemp -d)" && uv run --project "{{justfile_directory()}}" python "{{justfile_directory()}}/docs/examples/oisst.py"

# Check that every section reference ("§n") is a link to a heading that exists, and test the check;
# and that what derives from the profile registry (spec/zarrify/profiles/) is current
sections:
    uv run pytest -q spec/tools
    uv run python spec/tools/sections.py check
    uv run python spec/tools/profiles.py check

# Lint every Python file of the repository with ruff (pyproject.toml's [tool.ruff]: every rule, less
# the ones it names with their reasons)
lint:
    uv run ruff check .

# Type-check every Python file of the repository with pyrefly (pyproject.toml's [tool.pyrefly]: the strict
# preset for the package; the tests and scripts checked with what they leave unannotated as Any)
typecheck:
    uv run pyrefly check

# Regenerate what derives from the profile registry, spec/zarrify/profiles/<name>.json (the one
# place a profile's identity is written, one file per profile): the Rust core's copies, and the
# index tables of spec/zarrify.md, spec/conventions.md and docs/zarrify/README.md
profiles:
    uv run python spec/tools/profiles.py

# Run the spec/bindings/vzip.md conformance suite against the three independent implementations
conformance out="conformance/results/latest": impls::build
    uv run python conformance/vzip/run.py --out {{out}} \
        --impl rust=conformance/impls/rust/vzip --impl typescript=conformance/impls/typescript/vzip --impl python=conformance/impls/python/vzip

# Regenerate the synthetic files and stores in fixtures/<format>: every generator
# fixtures/generators/<format>/write_*.py but those with a line `# fixtures: on demand`, which need
# the network or other tools (the line says which): `just fixtures-of <format>` runs those
fixtures:
    #!/usr/bin/env bash
    set -euo pipefail
    for g in fixtures/generators/*/write_*.py; do
        if grep -q '^# fixtures: on demand' "$g"; then continue; fi
        uv run python "$g"
    done

# Regenerate one format's fixtures: fixtures/generators/<format>/write_*.py
fixtures-of format:
    for g in fixtures/generators/{{format}}/write_*.py; do uv run python "$g"; done

# Regenerate the fixtures twice and check that both runs give the same bytes, and the committed ones
fixtures-check:
    #!/usr/bin/env bash
    set -euo pipefail
    sums() { (cd fixtures && find . -path ./generators -prune -o -type f -print0 | LC_ALL=C sort -z | xargs -0 shasum -a 256); }
    just fixtures > /dev/null
    first="$(sums)"
    sleep 2  # past a one-second timestamp
    just fixtures > /dev/null
    if [ "$first" != "$(sums)" ]; then
        diff <(echo "$first") <(sums) >&2 || true
        echo "fixtures-check: two runs of the generators differ" >&2
        exit 1
    fi
    git diff --exit-code --stat -- fixtures
    if [ -n "$(git status --porcelain -- fixtures)" ]; then
        git status --short -- fixtures >&2
        echo "fixtures-check: the generators' output differs from the committed fixtures" >&2
        exit 1
    fi

# Check the browser zarrifier's pixels against an independent reader, for every format that has
# one: every js/test/<format>/verify.py (each says which reader it needs)
verify:
    #!/usr/bin/env bash
    set -euo pipefail
    for v in js/test/*/verify.py; do uv run python "$v"; done

# Check one format's pixels: js/test/<format>/verify.py
verify-of format:
    uv run python js/test/{{format}}/verify.py

# Store one virtual dataset as kerchunk JSON, kerchunk Parquet, Icechunk and vzip, and read each over HTTP (see design/comparison/README.md)
compare-formats *args:
    uv run python design/comparison/compare.py {{args}}

# spec/zarrify.md: compare implementations on the corpus (network; e.g. `just compare --quick`)
compare *args:
    uv run python conformance/zarrify/compare.py conformance/results/zarrify {{args}}

# spec/zarrify.md: compare implementations on corrupted copies of the synthetic files
compare-mutants count="10" seed="0" *args:
    rm -rf conformance/results/mutants
    python3 conformance/zarrify/mutate.py conformance/results/mutants {{count}} {{seed}}
    uv run python conformance/zarrify/compare.py conformance/results/mutants-out \
        --fixtures conformance/results/mutants {{args}}

# On main after a merge: tag each convention's version as zarrify-<profile>-v<N> at HEAD
# (spec/conventions.md §1
# <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#1-conventions>); version 0, before
# the release, has no tags
tag-conventions:
    #!/usr/bin/env bash
    set -euo pipefail
    branch="$(git rev-parse --abbrev-ref HEAD)"
    if [ "$branch" != "main" ]; then
        echo "tag-conventions: HEAD is on '$branch'; the convention tags are made on main" >&2
        exit 1
    fi
    tags="$(uv run python -c 'from refkv.zarrify.common import PROFILES
    for p, (_, v, _) in PROFILES.items(): print(f"zarrify-{p}-v{v}")')"
    for tag in $tags; do
        if [ "${tag##*-v}" = "0" ]; then
            echo "$tag: version 0 is not tagged (its URLs name main), kept untagged"
            continue
        fi
        if git rev-parse -q --verify "refs/tags/$tag" >/dev/null; then
            echo "$tag exists (at $(git rev-list -n 1 "$tag")), kept"
        else
            git tag -a "$tag" -m "spec/zarrify.md convention ${tag#zarrify-}"
            echo "$tag created at $(git rev-parse HEAD)"
        fi
    done
    echo "push them with: git push origin --tags"

# Zarrify an image file (a URL or a path) or a store (a URL ending in "/") of any format of
# docs/zarrify/README.md into a vzip archive
zarrify src out:
    uv run python -m refkv.zarrify {{quote(src)}} {{quote(out)}}

# Regenerate the (untracked) example archives in design/experiments/out (network)
archives: archives-nd2 archives-idr

# Zarrify the public ND2 files of the corpus into design/experiments/out/nd2
archives-nd2:
    grep -v '^#' conformance/zarrify/corpus_nd2.txt | grep . | while IFS='|' read -r url name _; do \
        uv run python -m refkv.zarrify "$url" "design/experiments/out/nd2/$name.vzip"; \
    done

# Zarrify the IDR idr0096 OME-TIFF (pinned, and unpinned for browsers) into design/experiments/out
archives-idr:
    mkdir -p design/experiments/out/ng_idr
    uv run python design/experiments/tiff_to_vzip.py {{quote(idr_tiff)}} design/experiments/out/idr0096_4000_d11_m5_LT_2.vzip
    uv run python design/experiments/tiff_to_vzip.py --no-pins {{quote(idr_tiff)}} \
        design/experiments/out/ng_idr/idr0096_4000_d11_m5_LT_2_unpinned.vzip

# Build and publish a demo to https://d-v-b.github.io/vzip-demo/ (see demo/pages.sh)
deploy-demo *args: js::build
    demo/pages.sh {{args}}
