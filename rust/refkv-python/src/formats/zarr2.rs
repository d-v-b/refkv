//! The Zarr v2 profile's class: the Zarr v3 profile's (`zarr3.rs`), of version 2, which reads
//! Zarr v2 stores and OME-Zarr's.

use super::zarr3::PyZarr;
use crate::formats::View;
use pyo3::prelude::*;
use std::sync::Arc;

pub fn view(py: Python<'_>, v: &View) -> PyResult<Py<PyAny>> {
    Ok(Py::new(py, PyZarr { ir: Arc::clone(&v.ir), description: Arc::clone(&v.description), version: 2 })?.into_any())
}

pub fn register(_m: &Bound<'_, PyModule>) -> PyResult<()> {
    Ok(())
}
