//! Each format's own class of an input a run parsed, one module per profile of the registry that
//! has one (`src/formats/<profile>.rs`, `-` as `_`), which `build.rs` finds and registers: a
//! module gives `view`, the class of a run's input, and `register`, which adds the classes it
//! defines. A profile without a module gives none (`Run.view()` is None).

use pyo3::prelude::*;
use refkv_ir::ir::Ir;
use std::sync::Arc;

/// What a format's class is made of: the run's IR and description, shared with the run, whether
/// the run is a store run, what it read for its projection (a store run's), shared, and how its
/// projection cuts the source, which the class's routines cut with too.
pub struct View {
    pub ir: Arc<Ir>,
    pub description: Arc<serde_json::Value>,
    pub store: bool,
    pub held: Option<Arc<refkv_ir::store::Read>>,
    pub chunking: refkv_ir::out::Chunking,
}

// `pub mod <m>;` for each profile with a module, `view` and `register` (build.rs)
include!(concat!(env!("OUT_DIR"), "/formats.rs"));
