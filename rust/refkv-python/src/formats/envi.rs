//! The ENVI profile's class (`Envi`).

use crate::formats::View;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use std::sync::Arc;

/// An ENVI raster a store run parsed (`StoreRun.view()`): its description, shared with the run
/// and never changed, so that a layout asks for the ENVI convention's image from any thread, while
/// the run is used for something else or after it is gone.
#[pyclass(name = "Envi", module = "refkv._core", frozen)]
pub struct PyEnvi {
    description: Arc<serde_json::Value>,
}

#[pymethods]
impl PyEnvi {
    /// The ENVI convention's image (`project::envi::image`): the root's source metadata (JSON),
    /// the array's document (JSON, with no attributes), its chunks, each `(coords, offset, length)`
    /// of the data file, and the header and data files' keys.
    #[allow(clippy::type_complexity)]
    fn image(&self) -> PyResult<(String, String, Vec<(Vec<u64>, u64, u64)>, String, String)> {
        let img = refkv_ir::project::envi::image(&self.description).map_err(PyValueError::new_err)?;
        let chunks = img.chunks.iter().map(|(coords, (offset, length))| (coords.to_vec(), *offset, *length)).collect();
        Ok((refkv_ir::out::to_text(&img.own), refkv_ir::out::to_text(&img.array), chunks, img.header_key, img.data_key))
    }
}

pub fn view(py: Python<'_>, v: &View) -> PyResult<Py<PyAny>> {
    Ok(Py::new(py, PyEnvi { description: Arc::clone(&v.description) })?.into_any())
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyEnvi>()
}
