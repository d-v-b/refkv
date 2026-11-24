//! The Zarr v3 profile's class (`Zarr`), which a Zarr v2 store's run gives too (`zarr2.rs`).

use crate::formats::View;
use crate::{out_py, refused};
use pyo3::prelude::*;
use refkv_ir::ir::Ir;
use std::sync::Arc;

/// A Zarr v2 or v3 store a run parsed (`Run.view()` of a zipped Zarr v3 store, `StoreRun.view()`):
/// its IR and its description, shared with the run and never changed, so that a layout asks for
/// the Zarr convention's hierarchy from any thread.
#[pyclass(name = "Zarr", module = "refkv._core", frozen)]
pub struct PyZarr {
    pub(crate) ir: Arc<Ir>,
    pub(crate) description: Arc<serde_json::Value>,
    /// 3 for a Zarr v3 store, else 2 (Zarr v2's, and OME-Zarr's, which it reads)
    pub(crate) version: u8,
}

#[pymethods]
impl PyZarr {
    /// The Zarr convention's hierarchy but for refkv's declarations and its `refkv_source/`
    /// objects (`project::zarr2::hierarchy`, `project::zarr3::hierarchy`): its nodes as JSON, each
    /// its path, kind and source metadata `S` as JSON text, and its documents and chunks in
    /// `out_py`'s form (the url empty); or Rejected, and Failed for a failure of refkv's.
    fn hierarchy<'py>(&self, py: Python<'py>) -> PyResult<(String, Bound<'py, PyAny>)> {
        let (nodes, out) = py
            .detach(|| if self.version == 3 {
                refkv_ir::project::zarr3::hierarchy(&self.ir, &self.description)
            } else {
                refkv_ir::project::zarr2::hierarchy(&self.ir, &self.description)
            })
            .map_err(refused)?;
        Ok((serde_json::Value::Array(nodes).to_string(), out_py(py, &out)?))
    }
}

/// A zipped Zarr v3 store's, or a Zarr v3 store's.
pub fn view(py: Python<'_>, v: &View) -> PyResult<Py<PyAny>> {
    Ok(Py::new(py, PyZarr { ir: Arc::clone(&v.ir), description: Arc::clone(&v.description), version: 3 })?.into_any())
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyZarr>()
}
