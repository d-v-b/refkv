//! The TIFF profile's class (`OmeImage`), which the QPTIFF, NDPI and IMS profiles' runs give
//! too (`qptiff.rs`, `ndpi.rs`, `ims.rs`): the OME image refkv's convention writes.

use crate::formats::View;
use crate::{out_py, refused};
use pyo3::prelude::*;
use refkv_ir::ir::Ir;
use std::sync::Arc;

/// A TIFF, QPTIFF, NDPI or IMS input a run parsed (`Run.view()`, `StoreRun.view()`): its
/// IR and its description, shared with the run and never changed, so that a
/// layout asks for the OME image refkv's convention writes from any thread, while the run is used
/// for something else or after it is gone.
#[pyclass(name = "OmeImage", module = "refkv._core", frozen)]
pub struct PyOmeImage {
    ir: Arc<Ir>,
    description: Arc<serde_json::Value>,
    profile: &'static str,
    /// the bytes a store run read for the image (`store::Read::copied`), which `image` adds to
    /// those the caller read; empty of a file run
    held: Option<Arc<refkv_ir::store::Read>>,
}

#[pymethods]
impl PyOmeImage {
    /// The input's profile: `tiff`, `qptiff`, `ndpi` or `ims`.
    #[getter]
    fn profile(&self) -> &'static str {
        self.profile
    }
    /// The ranges of the file the image samples for its `omero` windows
    /// (`project::ome_image_reads`), `(offset, length)`, which the caller reads and gives
    /// `image`; or Rejected where refkv's plan rejects the input, and Failed for a failure of
    /// refkv's.
    fn reads(&self, py: Python<'_>) -> PyResult<Vec<(u64, u64)>> {
        py.detach(|| refkv_ir::project::ome_image_reads(self.profile, &self.ir, &self.description)).map_err(refused)
    }
    /// The OME image (`project::ome_image`): its nodes as JSON, and its documents and
    /// chunks in `out_py`'s form (the url empty), its arrays sharded, ranges joined across inner
    /// chunks unless `checksums`; `copied` the bytes of the ranges `reads` gave, each
    /// `((offset, length), bytes)`. Rejected where refkv's plan rejects the input, and Failed for a
    /// failure of refkv's.
    #[pyo3(signature = (copied, checksums=false))]
    fn image<'py>(&self, py: Python<'py>, copied: Vec<((u64, u64), Vec<u8>)>, checksums: bool) -> PyResult<(String, Bound<'py, PyAny>)> {
        let mut copied: refkv_ir::project::Copied = copied.into_iter().collect();
        if let Some(read) = &self.held {
            copied.extend(read.copied.iter().map(|(k, v)| (*k, v.clone())));
        }
        let (nodes, out) = py
            .detach(|| refkv_ir::project::ome_image(self.profile, &self.ir, &self.description, &copied, checksums))
            .map_err(refused)?;
        Ok((serde_json::Value::Array(nodes).to_string(), out_py(py, &out)?))
    }
    /// The key of the file a store run was named by, whose object URL the image's root names as
    /// its source; None for a file run.
    fn named_key(&self) -> Option<String> {
        self.description["named_key"].as_str().map(str::to_string)
    }
}

/// The OME image of an input of `profile`, the TIFF family's or IMS's.
pub fn ome_image(py: Python<'_>, v: &View, profile: &'static str) -> PyResult<Py<PyAny>> {
    let image = PyOmeImage { ir: Arc::clone(&v.ir), description: Arc::clone(&v.description), profile, held: v.held.clone() };
    Ok(Py::new(py, image)?.into_any())
}

pub fn view(py: Python<'_>, v: &View) -> PyResult<Py<PyAny>> {
    ome_image(py, v, "tiff")
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyOmeImage>()
}
