//! The vzip archive writer for Python (spec/bindings/vzip.md): rust/refkv-vzip's `Writer`, as the
//! class `VzipWriter` of `refkv._core`, writing to a binary file object as each entry is added.
//! `refkv.vzip.archive.VZipWriter` wraps it with the package's API: its sources as `refkv.pb`
//! messages, and the CRC-32C of each range of a url source when the archive carries checksums.

use crate::phkv::PyWrite;
use pyo3::exceptions::{PyOSError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict};
use refkv_vzip::pb::{Range, Source};
use refkv_vzip::{Error, Options, Writer};
use std::sync::Mutex;

fn write_err(e: Error) -> PyErr {
    match e {
        Error::Invalid(m) => PyValueError::new_err(m),
        Error::Io(e) => PyOSError::new_err(e.to_string()),
    }
}

#[pyclass(name = "VzipWriter", module = "refkv._core")]
pub struct PyVzipWriter {
    writer: Mutex<Option<Writer<PyWrite>>>,
    /// the format entries' sizes, once closed
    sizes: Mutex<Vec<(String, u64)>>,
}

impl PyVzipWriter {
    /// `f` on the writer, with the interpreter released: whoever holds the lock may need the
    /// interpreter to write to the file object, so nobody may wait for the lock while holding it.
    fn with<T: Send>(&self, py: Python<'_>, f: impl FnOnce(&mut Writer<PyWrite>) -> Result<T, Error> + Send) -> PyResult<T> {
        py.detach(|| {
            let mut w = self.writer.lock().unwrap();
            f(w.as_mut().ok_or_else(|| PyValueError::new_err("the writer is closed"))?).map_err(write_err)
        })
    }
}

/// A range given as `(source, offset, length, crc32c)` or literal bytes.
fn range(r: &Bound<'_, PyAny>) -> PyResult<Range> {
    if r.is_instance_of::<PyBytes>() {
        return Ok(Range::literal(r.extract()?));
    }
    let (source, offset, length, crc32c): (u32, u64, u64, Option<u32>) = r.extract()?;
    Ok(Range { source, offset, length, data: None, crc32c })
}

#[pymethods]
impl PyVzipWriter {
    /// A writer to the binary file object `f`; `mirror_refs` also writes each reference's payload
    /// as its entry's body, and `page_size` adds a page index of pages of about that many bytes.
    #[new]
    #[pyo3(signature = (f, mirror_refs=true, page_size=None))]
    fn new(f: Py<PyAny>, mirror_refs: bool, page_size: Option<u64>) -> Self {
        let w = Writer::new(PyWrite(f), Options { mirror_refs, page_size });
        PyVzipWriter { writer: Mutex::new(Some(w)), sizes: Mutex::new(Vec::new()) }
    }

    /// Interns a source, given as its protobuf message, and gives its index.
    fn source(&self, py: Python<'_>, message: &[u8]) -> PyResult<u32> {
        let src = Source::decode(message).map_err(|e| PyValueError::new_err(e.to_string()))?;
        self.with(py, |w| w.source(src))
    }

    /// How many sources the table has so far.
    fn source_count(&self) -> PyResult<usize> {
        let w = self.writer.lock().unwrap();
        Ok(w.as_ref().ok_or_else(|| PyValueError::new_err("the writer is closed"))?.source_count())
    }

    fn add_bytes(&self, py: Python<'_>, key: &str, data: &[u8], compress: bool, late: bool) -> PyResult<()> {
        self.with(py, |w| w.add_bytes(key, data, compress, late))
    }

    /// Adds `__vz__/<name>`, and gives its key.
    fn add_hidden(&self, py: Python<'_>, name: &str, data: &[u8], compress: bool, late: bool) -> PyResult<String> {
        self.with(py, |w| w.add_hidden(name, data, compress, late))
    }

    /// Adds the bytes of the file at `path` as a STORED entry, streaming it.
    fn add_file(&self, py: Python<'_>, key: &str, path: std::path::PathBuf) -> PyResult<()> {
        self.with(py, |w| w.add_file(key, &path))
    }

    /// Adds a reference to the concatenation of `ranges`, each `(source, offset, length,
    /// crc32c)` or literal bytes.
    fn add_ranges(&self, py: Python<'_>, key: &str, ranges: Vec<Bound<'_, PyAny>>) -> PyResult<()> {
        let ranges = ranges.iter().map(range).collect::<PyResult<Vec<Range>>>()?;
        self.with(py, |w| w.add_ranges(key, ranges))
    }

    /// The bulk path: for each `(key, url, size, crc32c)`, a url source of its own (pinning `size`
    /// with `pin_size`) and a reference to all of it.
    fn add_url_refs(&self, py: Python<'_>, items: Vec<(String, String, u64, Option<u32>)>, pin_size: bool) -> PyResult<()> {
        self.with(py, |w| w.add_url_refs(items, pin_size))
    }

    /// Writes the source table, the late entries, the page index and the central directory.
    fn close(&self, py: Python<'_>) -> PyResult<()> {
        let w = self.writer.lock().unwrap().take().ok_or_else(|| PyValueError::new_err("the writer is closed"))?;
        let (_, sizes) = py.detach(|| w.close_with_sizes()).map_err(write_err)?;
        *self.sizes.lock().unwrap() = sizes;
        Ok(())
    }

    /// The uncompressed sizes of the format entries written (after `close`).
    fn format_sizes<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let d = PyDict::new(py);
        for (k, n) in self.sizes.lock().unwrap().iter() {
            d.set_item(k, n)?;
        }
        Ok(d)
    }
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyVzipWriter>()
}
