//! The netCDF3 profile's class (`Netcdf3`).

use crate::formats::View;
use crate::{out_py, refused};
use pyo3::prelude::*;
use refkv_ir::ir::Ir;
use std::sync::Arc;

/// A netCDF3 file a run parsed (`Run.view()`): its IR and its description, shared with the run
/// and never changed, so that a layout asks for the netCDF3 convention's hierarchy from any thread,
/// while the run is used for something else or after it is gone.
#[pyclass(name = "Netcdf3", module = "refkv._core", frozen)]
pub struct PyNetcdf3 {
    ir: Arc<Ir>,
    description: Arc<serde_json::Value>,
}

#[pymethods]
impl PyNetcdf3 {
    /// The netCDF3 convention's hierarchy (`project::netcdf3::hierarchy`): its nodes as JSON, its
    /// documents and chunks in `out_py`'s form (the url empty) but for the edge chunks it copies,
    /// and those, each `(key, offset, length, padding)` of the file; or Rejected.
    #[allow(clippy::type_complexity)]
    fn hierarchy<'py>(&self, py: Python<'py>) -> PyResult<(String, Bound<'py, PyAny>, Vec<(String, u64, u64, u64)>)> {
        let (nodes, out, copies) = py
            .detach(|| refkv_ir::project::netcdf3::hierarchy(&self.ir, &self.description))
            .map_err(refused)?;
        Ok((serde_json::Value::Array(nodes).to_string(), out_py(py, &out)?, copies))
    }
}

pub fn view(py: Python<'_>, v: &View) -> PyResult<Py<PyAny>> {
    Ok(Py::new(py, PyNetcdf3 { ir: Arc::clone(&v.ir), description: Arc::clone(&v.description) })?.into_any())
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyNetcdf3>()
}
