//! The image projections (design/ARCHITECTURE.md §3.4
//! <https://github.com/d-v-b/refkv/blob/main/design/ARCHITECTURE.md#34-projections>): a valid IR and
//! its facts into
//! today's Zarr hierarchy (spec/virtualize/tiff.md §4
//! <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/tiff.md#4-the-image>,
//! spec/virtualize/czi.md §4
//! <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/czi.md#4-output>,
//! spec/virtualize/nd2.md §4
//! <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/nd2.md#4-the-image>, and ND2's root
//! source metadata of §5.1
//! <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/nd2.md#51-decoded-chunks>). A
//! projection checks nothing (the parser checked the schema)
//! and reads nothing (the IR holds what it needs).

mod czi;
mod dicom;
mod ims;
pub mod n5;
mod nd2;
mod ndpi;
pub mod store;
mod nifti;
pub mod safe;
mod tiff;
pub mod zarr2;

use crate::ir::Ir;
use crate::out::Out;
use crate::run::Format;
use serde_json::{Map, Value as J};
use std::fmt::Write;

/// The bytes of the source's ranges a projection copies into the archive, by (offset, length).
pub type Copied = std::collections::HashMap<(u64, u64), Vec<u8>>;

/// The image projection of `format`: today's hierarchy, without `vzip_source`. `copied`
/// holds the bytes of the ranges `reads` gives.
pub fn project(format: Format, ir: &Ir, facts: &J, url: &str, copied: &Copied) -> Result<Out, String> {
    match format {
        Format::Tiff => tiff::project(ir, facts, url),
        Format::Czi => czi::project(ir, facts, url),
        Format::Nd2 => nd2::project(ir, facts, url),
        Format::Dicom => dicom::project(ir, facts, url),
        Format::Nifti => nifti::project(ir, facts, url, copied),
        Format::Ims => ims::project(ir, facts, url),
        Format::Safe => safe::project(ir, facts, url),
        Format::Ndpi => ndpi::project(ir, facts, url),
    }
}

/// The source's ranges the projection of `format` copies into the archive (NIfTI's edge
/// chunks whose padding does not fit in a reference), which the run reads after the
/// parser: a projection reads nothing itself.
pub fn reads(format: Format, ir: &Ir, facts: &J) -> Result<Vec<(u64, u64)>, String> {
    match format {
        Format::Nifti => nifti::reads(ir, facts),
        _ => Ok(Vec::new()),
    }
}

// ---- reading the facts (JSON the parsers wrote)

/// A fixed-size character field: its bytes up to the first NUL, as a text value
/// (conventions §6
/// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#6-source-metadata-as-json>):
/// UTF-8 if valid, else {"latin1": ...}.
fn json_text(b: &[u8]) -> J {
    let b = &b[..b.iter().position(|&c| c == 0).unwrap_or(b.len())];
    match std::str::from_utf8(b) {
        Ok(s) => J::String(s.to_string()),
        Err(_) => serde_json::json!({"latin1": b.iter().map(|&c| c as char).collect::<String>()}),
    }
}

/// A count or index of the facts (an integer, or a number written as a float).
fn n(v: &J) -> u64 {
    v.as_u64().or_else(|| v.as_f64().map(|f| f as u64)).unwrap_or(0)
}

/// An array of the facts (empty when absent).
fn arr(v: &J) -> &[J] {
    v.as_array().map(|a| a.as_slice()).unwrap_or(&[])
}

/// An object of the facts (empty when absent).
fn obj(v: &J) -> Map<String, J> {
    v.as_object().cloned().unwrap_or_default()
}

/// Axis names.
fn strs(v: &J) -> Vec<&str> {
    arr(v).iter().filter_map(|a| a.as_str()).collect()
}

/// A JSON value's truth as Python's.
fn truthy(v: &J) -> bool {
    match v {
        J::Null => false,
        J::Bool(b) => *b,
        J::Number(x) => x.as_f64().is_some_and(|f| f != 0.0),
        J::String(s) => !s.is_empty(),
        J::Array(a) => !a.is_empty(),
        J::Object(o) => !o.is_empty(),
    }
}

/// `translations` as `image_ome` takes them: None when absent or empty.
fn translations(v: &J) -> Option<&[J]> {
    v.as_array().filter(|a| !a.is_empty()).map(|a| a.as_slice())
}

/// A chunk key: `prefix` and the coordinates, joined by "/".
fn key(prefix: &str, coords: impl IntoIterator<Item = u64>) -> String {
    let mut k = String::from(prefix);
    for (i, c) in coords.into_iter().enumerate() {
        if i > 0 {
            k.push('/');
        }
        let _ = write!(k, "{c}");
    }
    k
}
