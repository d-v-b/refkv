# refkv: reference-aware key-value storage

> [!CAUTION]
> This project is almost entirely vibe-coded. It is intended for personal use,
> and as a proof of concept. If you need virtual Zarr datasets, use
> [Icechunk](https://icechunk.io/) or [kerchunk](https://github.com/fsspec/kerchunk).

This repository is an experiment. It defines two things at different levels of abstraction:

**refkv** is a __reference value type__ for key-value storage:
a value is either bytes or a reference to byte ranges of other objects, such
as the chunks inside HDF5, netCDF or TIFF files. The model is specified
without a backend ([spec/refkv.md](spec/refkv.md)), so any key-value store
that can tell bytes from references can hold such a store.

**vzip** is an implementation of `refkv` that stores bytes and references in a plain zip archive. 
A `vzip` archive contains regular files or `refkv` references, which can point to byte ranges in external objects.
A `vzip` reader resolves those references before returning bytes to the consumer.

The code in this repository exercises the model by writing vzip archives. A
Rust core **zarrifies** image files and chunked array stores in
[a dozen formats](docs/zarrify/README.md#formats): it presents each
as a Zarr hierarchy whose chunks are references to the original bytes,
without copying them. It runs natively from Python and as WebAssembly in the
browser.

```python
from refkv.zarrify import zarrify

store = zarrify("https://example.org/slide.ndpi")  # a refkv.ReferenceStore
store.save("slide.vzip")                               # one vzip archive
```

For netCDF, HDF5 and the other files
[VirtualiZarr](https://virtualizarr.readthedocs.io/) reads,
`refkv.ReferenceStore.from_virtualizarr(vds)` turns a virtual dataset into a
store that xarray opens, through `refkv.zarr` with the `zarr` extra
([docs/virtual-datasets.md](docs/virtual-datasets.md) has a runnable example).
[docs/](docs/README.md) is for users: installing, the live demos, and a page
per format.

Status: experimental, proof of concept, anything can change. There are no
production users, no release history and no support, and the specification
can change without regard for existing archives.

## Layout

Specifications, documentation and design notes are organized by component;
code is organized by language.

| directory | holds |
|---|---|
| [spec/](spec/) | the specifications: the reference value type ([refkv.md](spec/refkv.md)), its ZIP binding ([bindings/vzip.md](spec/bindings/vzip.md)), and the draft perfect-hash binding ([bindings/phkv.md](spec/bindings/phkv.md)), zarrification ([zarrify.md](spec/zarrify.md)), the conventions every zarrified hierarchy shares ([conventions.md](spec/conventions.md)), one document per format with its Zarr convention and its profile ([zarrify/](spec/zarrify/), each `<format>.md` beside `<format>/schema.json`), the wire schema ([proto/](spec/proto/)), and earlier revisions ([history/](spec/history/)) |
| [docs/](docs/README.md) | user documentation: [virtual datasets with xarray](docs/virtual-datasets.md), and a page per format in [docs/zarrify/](docs/zarrify/README.md) |
| [design/](design/) | design notes and studies: [ARCHITECTURE.md](design/ARCHITECTURE.md), [FINDINGS.md](design/FINDINGS.md), [PITCH.md](design/PITCH.md), [COMPARISON.md](design/COMPARISON.md), [THIRD_PARTY_READERS.md](design/THIRD_PARTY_READERS.md) (third-party readers as format parsers), the IR notes ([IR_NOTES.md](design/IR_NOTES.md)), the conformance rounds' notes and proposals, and the experiment scripts the notes cite |
| [python/](python/) | the Python distribution `refkv` ([pyproject.toml](python/pyproject.toml)): the package (`python/src/refkv`, with the ZIP binding in `refkv.vzip`), its tests (`python/tests`) and its [release process](python/RELEASING.md); the root `pyproject.toml` is a uv workspace over it, with the repository's development tools |
| [js/](js/README.md) | the TypeScript library (`js/src`) and its Node tests |
| [demo/](demo/) | the browser demo, which `just js::build` bundles with the library and `demo/pages.sh` publishes to https://d-v-b.github.io/refkv-demo/ |
| [rust/refkv-ir](rust/refkv-ir/) | the Rust core: the format parsers, the IR, the projections and the read planner, built for Python (through the bindings in [rust/refkv-python](rust/refkv-python/)) and for the browser (wasm32) |
| [rust/refkv-python](rust/refkv-python/) | the core's Python bindings, which the `refkv` wheel builds in as the module `refkv._core` (`manifest-path` in [python/pyproject.toml](python/pyproject.toml)) |
| [fixtures/](fixtures/) | the synthetic test inputs every implementation shares, one directory per format, and their generators ([fixtures/generators/](fixtures/generators/)) |
| [conformance/](conformance/) | the conformance harnesses: [vzip/](conformance/vzip/HARNESS.md) for the vzip binding, [zarrify/](conformance/zarrify/HARNESS.md) for zarrification (with the frozen reference implementations, Python and TypeScript), and the agent-written independent implementations ([impls/](conformance/impls/README.md)) |

`just` lists the recipes: `just test` (Python), `just python::build` (a wheel), `just js::test` (Node),
`just ir-test` (Rust), `just compare` and `just compare-mutants` (the
zarrifiers against the frozen reference), `just conformance` (the vzip
binding's independent implementations).

## License

Licensed under either of

- Apache License, Version 2.0 ([LICENSE-APACHE](LICENSE-APACHE))
- MIT License ([LICENSE-MIT](LICENSE-MIT))

at your option. This covers the specification, the schema, and all code in
this repository.

Unless you explicitly state otherwise, any contribution you intentionally
submit for inclusion in this repository, as defined in the Apache-2.0
license, is dual licensed as above, without any additional terms or
conditions.

### Third-party data

Some files in `design/experiments/out/` derive from IDR study
[idr0096](https://doi.org/10.17867/10000170), "Quantification of Bone Marrow
Compartments in Histological Sections" (Tratwal et al.,
[doi:10.3389/fendo.2020.00480](https://doi.org/10.3389/fendo.2020.00480)),
licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/):

- `idr0096_4000_d11_m5_LT_2.vzip` and `ng_idr/idr0096_4000_d11_m5_LT_2_unpinned.vzip`
  (generated by `just archives-idr`, not version-controlled) embed the image's
  OME-XML metadata;
- the screenshots in `ng_idr/` and `web_demo/` show the image.

`just archives-nd2` writes zarrifications of public Nikon ND2 files from the
BioImage Archive and Zenodo, some under CC BY 4.0, to `design/experiments/out/nd2/`;
its [README](design/experiments/out/nd2/README.md) credits each source.

These files are not covered by the licenses above.
