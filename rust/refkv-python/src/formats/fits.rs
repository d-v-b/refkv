//! The FITS profile's class (`Fits`).

use crate::formats::View;
use crate::piece_py;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use refkv_ir::ir::Ir;
use std::sync::Arc;

/// A FITS file a run parsed (`Run.view()`): its IR and its description, shared with the run and
/// never changed, and its heaps' elements, found once; so that a layout asks for each HDU's arrays
/// and cards without the description going through JSON for each, from any thread.
#[pyclass(name = "Fits", module = "refkv._core", frozen)]
pub struct PyFits {
    ir: Arc<Ir>,
    description: Arc<serde_json::Value>,
    heaps: std::sync::OnceLock<refkv_ir::project::fits::Heaps>,
    chunking: refkv_ir::out::Chunking,
}

#[pymethods]
impl PyFits {
    /// The arrays of HDU `index` as the FITS convention writes them, but for their declarations
    /// (`project::fits::hdu_arrays`): each `(path, document JSON, chunks)`, `path` under the HDU's
    /// node (`""`, `rows`, `heap`), each chunk `(coords, parts)` in `piece_py`'s form. ValueError
    /// for no HDU `index`.
    #[allow(clippy::type_complexity)]
    fn arrays<'py>(&self, py: Python<'py>, index: u64) -> PyResult<Vec<(String, String, Vec<(Vec<u64>, Vec<Bound<'py, PyAny>>)>)>> {
        let arrays = py.detach(|| {
            let heaps = self.heaps.get_or_init(|| refkv_ir::project::fits::Heaps::of(&self.ir));
            refkv_ir::project::fits::hdu_arrays(&self.ir, &self.description, index, heaps, &self.chunking)
        }).map_err(PyValueError::new_err)?;
        arrays.into_iter().map(|a| {
            let chunks = a.chunks.iter().map(|(coords, pieces)| {
                Ok((coords.clone(), pieces.iter().map(|p| piece_py(py, p)).collect::<PyResult<Vec<_>>>()?))
            }).collect::<PyResult<Vec<_>>>()?;
            Ok((a.path, a.doc.to_string(), chunks))
        }).collect()
    }
    /// The cards of HDU `index` as the FITS convention writes them (`project::fits::hdu_cards`):
    /// JSON, each `[keyword, value, comment]`. ValueError for no HDU `index`.
    fn cards(&self, py: Python<'_>, index: u64) -> PyResult<String> {
        let cards = py.detach(|| refkv_ir::project::fits::hdu_cards(&self.ir, &self.description, index)).map_err(PyValueError::new_err)?;
        Ok(refkv_ir::out::to_text(&cards))
    }
}

pub fn view(py: Python<'_>, v: &View) -> PyResult<Py<PyAny>> {
    let fits = PyFits { ir: Arc::clone(&v.ir), description: Arc::clone(&v.description), heaps: std::sync::OnceLock::new(),
                       chunking: v.chunking };
    Ok(Py::new(py, fits)?.into_any())
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyFits>()
}
