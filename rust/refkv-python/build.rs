//! On macOS, leave the Python symbols for the interpreter to resolve at import (`-undefined
//! dynamic_lookup`), so that a plain `cargo build` or `cargo test` of the module links, as a
//! maturin build does. And `formats.rs`, which src/formats/mod.rs includes: for each profile of the
//! registry (`../refkv-ir/profiles/<name>.json`) whose class has a module, `src/formats/<m>.rs`
//! (`<m>` the name with `-` as `_`), the module, `view`, which gives the class of a run of the
//! profile, and `register`, which adds every module's classes; so that adding a format's class
//! edits no shared list.
use std::fmt::Write;
use std::path::Path;

fn main() {
    pyo3_build_config::add_extension_module_link_args();
    let root = Path::new(env!("CARGO_MANIFEST_DIR"));
    let profiles = root.join("../refkv-ir/profiles");
    let formats = root.join("src/formats");
    println!("cargo:rerun-if-changed={}", profiles.display());
    println!("cargo:rerun-if-changed={}", formats.display());
    let mut names: Vec<String> = std::fs::read_dir(&profiles)
        .expect("the profile registry")
        .filter_map(|e| {
            let p = e.ok()?.path();
            (p.extension()? == "json").then(|| p.file_stem()?.to_str().map(str::to_string)).flatten()
        })
        .collect();
    names.sort();
    let with: Vec<(String, String)> = names
        .into_iter()
        .map(|n| {
            let m = n.replace('-', "_");
            (n, m)
        })
        .filter(|(_, m)| formats.join(format!("{m}.rs")).is_file())
        .collect();
    let mut text = String::new();
    for (_, m) in &with {
        let path = formats.join(format!("{m}.rs"));
        writeln!(text, "#[path = {:?}]\npub mod {m};", path.display().to_string()).unwrap();
    }
    text.push_str("\n/// The class of a run of `profile` (`View`), or None for a profile without one.\npub fn view(py: Python<'_>, profile: &str, v: &View) -> PyResult<Option<Py<PyAny>>> {\n    match profile {\n");
    for (n, m) in &with {
        writeln!(text, "        {n:?} => {m}::view(py, v).map(Some),").unwrap();
    }
    text.push_str("        _ => Ok(None),\n    }\n}\n\n/// Adds every format's classes to the module.\npub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {\n");
    for (_, m) in &with {
        writeln!(text, "    {m}::register(m)?;").unwrap();
    }
    text.push_str("    Ok(())\n}\n");
    std::fs::write(Path::new(&std::env::var("OUT_DIR").unwrap()).join("formats.rs"), text).unwrap();
}
