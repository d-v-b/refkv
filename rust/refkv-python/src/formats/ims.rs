//! The IMS profile's class: the TIFF profile's OME image (`tiff.rs`).

use crate::formats::View;
use pyo3::prelude::*;

pub fn view(py: Python<'_>, v: &View) -> PyResult<Py<PyAny>> {
    super::tiff::ome_image(py, v, "ims")
}

pub fn register(_m: &Bound<'_, PyModule>) -> PyResult<()> {
    Ok(())
}
