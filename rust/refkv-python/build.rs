//! On macOS, leave the Python symbols for the interpreter to resolve at import (`-undefined
//! dynamic_lookup`), so that a plain `cargo build` or `cargo test` of the module links, as a
//! maturin build does.
fn main() {
    pyo3_build_config::add_extension_module_link_args();
}
