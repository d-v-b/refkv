//! What each profile adds to the crate, generated from the profile registry's copies in
//! profiles/ (one file per profile: spec/tools/profiles.py copies spec/virtualize/profiles/),
//! so that adding a format adds its own files and edits no shared list:
//!
//! - `modules.rs`, included by lib.rs: `pub mod <m>` for every profile but a dataset layout,
//!   from src/<m>.rs or src/<m>/mod.rs (`<m>` the profile's name, `-` as `_`);
//! - `projections.rs`, included by project/mod.rs: `pub mod <m>` for every profile with a
//!   projection module, src/project/<m>.rs;
//! - `formats.rs`, included by run.rs: `Format`, one variant per file profile (kind `file` or
//!   `both`), each with its module's `Profile` (`crate::run::FileProfile`), and the order in
//!   which a file's first bytes are tested (the registry's `sniff`, ties by name);
//! - `registry.rs`, included by out.rs: the registry's entries, by name;
//! - `schemas.rs`, included by rules.rs: the source models, schema/<name>.json, by name;
//! - `rejects_<dir>.rs`, which the integration test tests/<dir>.rs includes (`-` as `_`): a
//!   `reject!(test, "input", "message")` line per case of the repository's
//!   conformance/virtualize/rejects/<dir>.json, the inputs in fixtures/<dir> its profile
//!   rejects, so that the Rust tests and the Python ones (python/tests/ir/test_rejects.py) read
//!   one table. Outside the repository (a packaged crate) there are no tables, and none is written.

use std::fmt::Write;
use std::path::{Path, PathBuf};

struct Entry {
    name: String,
    title: String,
    kind: String,
    sniff: Option<i64>,
    path: PathBuf,
}

impl Entry {
    /// The module's name: the profile's, `-` as `_`.
    fn module(&self) -> String {
        self.name.replace('-', "_")
    }
    /// The variant's name: the profile's in upper camel case.
    fn variant(&self) -> String {
        self.name
            .split('-')
            .map(|w| {
                let mut c = w.chars();
                c.next().map(|f| f.to_ascii_uppercase().to_string() + c.as_str()).unwrap_or_default()
            })
            .collect()
    }
}

/// A path as a Rust string literal.
fn lit(p: &Path) -> String {
    format!("{:?}", p.to_str().expect("a UTF-8 path"))
}

/// The `.json` files of `dir`, by name.
fn json_files(dir: &Path) -> Vec<PathBuf> {
    let mut out: Vec<PathBuf> = std::fs::read_dir(dir)
        .unwrap_or_else(|e| panic!("{}: {e}", dir.display()))
        .map(|e| e.unwrap().path())
        .filter(|p| p.extension().is_some_and(|x| x == "json"))
        .collect();
    out.sort();
    out
}

/// The registry's entries, by name.
fn entries(dir: &Path) -> Vec<Entry> {
    let mut out: Vec<Entry> = json_files(dir)
        .into_iter()
        .map(|path| {
            let text = std::fs::read_to_string(&path).unwrap();
            let v: serde_json::Value = serde_json::from_str(&text).unwrap_or_else(|e| panic!("{}: {e}", path.display()));
            let name = v["name"].as_str().unwrap_or_else(|| panic!("{}: no name", path.display())).to_string();
            assert_eq!(path.file_stem().unwrap().to_str(), Some(name.as_str()), "{}: not named for its profile", path.display());
            let kind = v["kind"].as_str().unwrap_or_else(|| panic!("{name}: no kind")).to_string();
            let sniff = v["sniff"].as_i64();
            let title = v["title"].as_str().unwrap_or_else(|| panic!("{name}: no title")).to_string();
            Entry { name, title, kind, sniff, path }
        })
        .collect();
    out.sort_by(|a, b| a.name.cmp(&b.name));
    out
}

fn main() {
    let root = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").unwrap());
    let out = PathBuf::from(std::env::var("OUT_DIR").unwrap());
    // the registry's copies, and the modules a profile adds or removes
    for p in ["profiles", "schema", "src"] {
        println!("cargo::rerun-if-changed={p}");
    }
    let all = entries(&root.join("profiles"));
    let src = root.join("src");

    let mut modules = String::new();
    let mut projections = String::new();
    for e in all.iter().filter(|e| e.kind != "layout") {
        let m = e.module();
        let file = [src.join(format!("{m}.rs")), src.join(&m).join("mod.rs")]
            .into_iter()
            .find(|p| p.is_file())
            .unwrap_or_else(|| panic!("the profile {} has no module src/{m}.rs or src/{m}/mod.rs", e.name));
        writeln!(modules, "#[path = {}]\npub mod {m};", lit(&file)).unwrap();
        let projection = src.join("project").join(format!("{m}.rs"));
        if projection.is_file() {
            writeln!(projections, "#[path = {}]\npub mod {m};", lit(&projection)).unwrap();
        }
    }

    let files: Vec<&Entry> = all.iter().filter(|e| e.kind == "file" || e.kind == "both").collect();
    let mut sniffed: Vec<&Entry> = files.iter().copied().filter(|e| e.sniff.is_some()).collect();
    sniffed.sort_by(|a, b| (a.sniff, &a.name).cmp(&(b.sniff, &b.name)));
    let mut formats = String::from(
        "/// A file profile's format (a profile of kind `file` or `both` in the registry), which a\n\
         /// file run reads. Generated by build.rs from profiles/.\n\
         #[derive(Clone, Copy, PartialEq, Eq, Debug)]\npub enum Format {\n",
    );
    for e in &files {
        writeln!(formats, "    /// The {} profile (spec/virtualize/{}.md).\n    {},", e.title, e.name, e.variant()).unwrap();
    }
    formats.push_str("}\n\nimpl Format {\n    /// Every file profile's format, by name.\n    pub const ALL: &'static [Format] = &[");
    formats.push_str(&files.iter().map(|e| format!("Format::{}", e.variant())).collect::<Vec<_>>().join(", "));
    formats.push_str("];\n\n    /// The formats a file's first bytes are tested for, in the registry's `sniff` order.\n    const SNIFFED: &'static [Format] = &[");
    formats.push_str(&sniffed.iter().map(|e| format!("Format::{}", e.variant())).collect::<Vec<_>>().join(", "));
    formats.push_str("];\n\n    /// The format's name: its profile's in the registry, which hosts pass across every\n    /// boundary (there are no numbers).\n    pub fn name(self) -> &'static str {\n        match self {\n");
    for e in &files {
        writeln!(formats, "            Format::{} => {:?},", e.variant(), e.name).unwrap();
    }
    formats.push_str("        }\n    }\n\n    /// The profile's half of a file run: its module's `Profile`.\n    pub fn profile(self) -> &'static dyn FileProfile {\n        match self {\n");
    for e in &files {
        writeln!(formats, "            Format::{} => &crate::{}::Profile,", e.variant(), e.module()).unwrap();
    }
    formats.push_str("        }\n    }\n}\n");

    let mut registry = String::from("/// The registry's entries (profiles/<name>.json), by name.\nstatic REGISTRY_TEXT: &[&str] = &[\n");
    for e in &all {
        writeln!(registry, "    include_str!({}),", lit(&e.path)).unwrap();
    }
    registry.push_str("];\n");

    let schemas = json_files(&root.join("schema"));
    let mut schema = format!("/// The source models (schema/<name>.json), by name.\nconst SCHEMAS: [(&str, &str); {}] = [\n", schemas.len());
    for p in &schemas {
        writeln!(schema, "    ({:?}, include_str!({})),", p.file_stem().unwrap().to_str().unwrap(), lit(p)).unwrap();
    }
    schema.push_str("];\n");

    let mut generated = vec![("modules.rs".to_string(), modules), ("projections.rs".into(), projections), ("formats.rs".into(), formats),
                             ("registry.rs".into(), registry), ("schemas.rs".into(), schema)];
    generated.extend(rejects(&root.join("../../conformance/virtualize/rejects")));
    for (name, text) in generated {
        let path = out.join(name);
        if std::fs::read_to_string(&path).ok().as_deref() != Some(text.as_str()) {
            std::fs::write(&path, text).unwrap();
        }
    }
}

/// `rejects_<dir>.rs` for each table `<dir>.json` in `tables`: a `reject!` line per case, the
/// test's name, the input (relative to fixtures/<dir>) and the message, as string literals.
fn rejects(tables: &Path) -> Vec<(String, String)> {
    let Ok(files) = std::fs::read_dir(tables) else { return Vec::new() };
    println!("cargo::rerun-if-changed={}", tables.display());
    let mut out = Vec::new();
    let mut tables: Vec<PathBuf> = files.map(|d| d.unwrap().path()).filter(|p| p.extension().is_some_and(|e| e == "json")).collect();
    tables.sort();
    for table in tables {
        println!("cargo::rerun-if-changed={}", table.display());
        let dir = table.file_stem().unwrap().to_str().unwrap().replace('-', "_");
        let v: serde_json::Value = serde_json::from_str(&std::fs::read_to_string(&table).unwrap())
            .unwrap_or_else(|e| panic!("{}: {e}", table.display()));
        let mut text = format!("// Generated by build.rs from {}.
", table.display());
        for c in v["cases"].as_array().unwrap_or_else(|| panic!("{}: no cases", table.display())) {
            let field = |k: &str| c[k].as_str().unwrap_or_else(|| panic!("{}: a case has no {k}", table.display())).to_string();
            writeln!(text, "reject!({}, {:?}, {:?});", field("test"), field("input"), field("message")).unwrap();
        }
        out.push((format!("rejects_{dir}.rs"), text));
    }
    out
}
