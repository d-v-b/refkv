//! The HDF5 profile's class (`Hdf5`).

use crate::formats::View;
use crate::{out_py, Rejected};
use pyo3::prelude::*;
use refkv_ir::ir::Ir;
use std::sync::Arc;

/// An HDF5 input a run parsed (`Run.view()`, `StoreRun.view()`): its IR and its description (a
/// store run's, of each file it read), shared with the run and never changed, so that a layout
/// asks for the HDF5 convention's hierarchy from any thread, while the run is used for something
/// else or after it is gone.
#[pyclass(name = "Hdf5", module = "refkv._core", frozen)]
pub struct PyHdf5 {
    ir: Arc<Ir>,
    description: Arc<serde_json::Value>,
    store: bool,
    chunking: refkv_ir::out::Chunking,
}

#[pymethods]
impl PyHdf5 {
    /// The HDF5 convention's hierarchy (`project::hdf5::hierarchy`): its nodes as JSON, its
    /// documents and chunks in `out_py`'s form (the url empty) but for the edge chunks it copies,
    /// and those, each `(key, offset, length, padding)` of the file; or Rejected.
    #[allow(clippy::type_complexity)]
    fn hierarchy<'py>(&self, py: Python<'py>) -> PyResult<(String, Bound<'py, PyAny>, Vec<(String, u64, u64, u64)>)> {
        let (nodes, out, copies) = py
            .detach(|| refkv_ir::project::hdf5::hierarchy(&self.ir, &self.description, self.store, &self.chunking))
            .map_err(Rejected::new_err)?;
        Ok((serde_json::Value::Array(nodes).to_string(), out_py(py, &out)?, copies))
    }
    /// The key of the file a store run was named by, whose object URL the hierarchy's root names
    /// as its source; None for a file run.
    fn named_key(&self) -> Option<String> {
        self.description["named_key"].as_str().filter(|_| self.store).map(str::to_string)
    }
}

pub fn view(py: Python<'_>, v: &View) -> PyResult<Py<PyAny>> {
    Ok(Py::new(py, PyHdf5 { ir: Arc::clone(&v.ir), description: Arc::clone(&v.description), store: v.store, chunking: v.chunking })?.into_any())
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyHdf5>()
}
