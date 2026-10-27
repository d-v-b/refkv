//! phkv stores for Python (spec/bindings/phkv.md): the reader and the standalone writer of
//! rust/refkv-phkv, as the classes `PhkvStore` and `PhkvWriter` of `refkv._core`. The Python
//! package keeps what is not the container's: the I/O (a store reads through the callbacks it is
//! given, and the Python host runs those on its own event loop), reader policy, and the
//! resolution of references, which the Python reader shares with every other reference store.

use pyo3::exceptions::{PyException, PyOSError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyList};
use refkv_phkv::{Class, Codec, EntryInfo, Error, Fetched, Kind, Limits, Options, Prop, ReadAt, ReadError, Request, Store, Writer};
use refkv_vzip::pb::{Range, Source};
use std::io;
use std::sync::Mutex;

pyo3::create_exception!(refkv._core, PhkvReadError, PyException);

fn class_name(c: Class) -> &'static str {
    match c {
        Class::Archive => "archive",
        Class::Entry => "entry",
        Class::Body => "body",
        Class::Payload => "payload",
        Class::Resolution => "resolution",
        Class::Request => "request",
        Class::Property => "property",
        Class::Io => "io",
    }
}

/// A read failure, as `PhkvReadError(class, message)`.
fn read_err(e: ReadError) -> PyErr {
    PhkvReadError::new_err((class_name(e.class), e.message))
}

fn write_err(e: Error) -> PyErr {
    match e {
        Error::Invalid(m) => PyValueError::new_err(m),
        Error::Io(e) => PyOSError::new_err(e.to_string()),
    }
}

fn io_err(e: PyErr) -> io::Error {
    io::Error::other(e.to_string())
}

/// The bytes of a store, read by Python callbacks: `size()` and `read(offset, length)`.
struct PyReadAt {
    size: Py<PyAny>,
    read: Py<PyAny>,
}

impl ReadAt for PyReadAt {
    fn size(&self) -> io::Result<u64> {
        Python::attach(|py| self.size.call0(py)?.extract::<u64>(py)).map_err(io_err)
    }

    fn read_at(&self, offset: u64, buf: &mut [u8]) -> io::Result<()> {
        Python::attach(|py| {
            let got = self.read.call1(py, (offset, buf.len()))?;
            let got = got.extract::<&[u8]>(py)?;
            if got.len() != buf.len() {
                return Err(PyValueError::new_err(format!("read {} bytes of {}", got.len(), buf.len())));
            }
            buf.copy_from_slice(got);
            Ok(())
        })
        .map_err(io_err)
    }
}

#[pyclass(name = "PhkvStore", module = "refkv._core", frozen)]
pub struct PyPhkvStore {
    store: Store<PyReadAt>,
}

#[pymethods]
impl PyPhkvStore {
    /// Opens the store `read(offset, length)` reads, of `size()` bytes (spec/bindings/phkv.md
    /// §22.1 <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#221-opening>): every
    /// failure is an archive error. `sources` and `open_bytes` are the reader policy's limits of
    /// spec/refkv.md §6.7 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>
    /// rules 4 and 6.
    #[new]
    #[pyo3(signature = (size, read, sources=None, open_bytes=None))]
    fn new(py: Python<'_>, size: Py<PyAny>, read: Py<PyAny>, sources: Option<usize>, open_bytes: Option<u64>) -> PyResult<Self> {
        let mut limits = Limits::default();
        limits.sources = sources.unwrap_or(limits.sources);
        limits.open_bytes = open_bytes.unwrap_or(limits.open_bytes);
        let at = PyReadAt { size, read };
        let store = py.detach(|| Store::open_with(at, limits)).map_err(read_err)?;
        Ok(PyPhkvStore { store })
    }

    /// The most bytes of the end of the file a reader reads first (`size()` may serve them).
    #[staticmethod]
    fn tail() -> u64 {
        refkv_phkv::DEFAULT_TAIL
    }

    #[getter]
    fn revision(&self) -> Option<u32> {
        self.store.revision()
    }

    #[getter]
    fn key_count(&self) -> u64 {
        self.store.key_count()
    }

    #[getter]
    fn hybrid(&self) -> bool {
        self.store.superblock().hybrid()
    }

    /// The source table, each source as its protobuf message.
    fn sources<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        PyList::new(py, self.store.sources().iter().map(|s| PyBytes::new(py, &s.encode())))
    }

    /// The METADATA section: (name, value) pairs.
    fn metadata<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        PyList::new(py, self.store.metadata().iter().map(|(k, v)| (k.as_str(), PyBytes::new(py, v))))
    }

    /// `"bytes"`, `"reference"`, or None if the key is missing or hidden.
    fn classify(&self, py: Python<'_>, key: &str) -> PyResult<Option<&'static str>> {
        let kind = py.detach(|| self.store.classify(key)).map_err(read_err)?;
        Ok(kind.map(|k| match k {
            Kind::Bytes => "bytes",
            Kind::Reference => "reference",
        }))
    }

    /// How a key's value is stored, hidden keys included: None if missing, else
    /// `("bytes", size, hidden)` or `("reference", payload_length, hidden)`.
    fn entry(&self, py: Python<'_>, key: &str) -> PyResult<Option<(&'static str, u64, bool)>> {
        let info = py.detach(|| self.store.entry_info(key)).map_err(read_err)?;
        Ok(info.map(|i| match i {
            EntryInfo::Bytes { size, hidden, .. } => ("bytes", size, hidden),
            EntryInfo::Reference { payload_length, hidden } => ("reference", payload_length, hidden),
        }))
    }

    /// `get`, up to resolving a reference: `("bytes", window)` or `("reference", kind, payload)`
    /// (kind 1 `Range`, 2 `Concat`), None if the key is missing or hidden. `mode` is 0 for the
    /// whole value, 1 for `[a, b)`, 2 for from `a`, 3 for the last `a` bytes.
    fn fetch<'py>(&self, py: Python<'py>, key: &str, mode: u8, a: u64, b: u64) -> PyResult<Option<Bound<'py, PyAny>>> {
        let request = match mode {
            0 => Request::Whole,
            1 => Request::Range(a, b),
            2 => Request::Offset(a),
            3 => Request::Suffix(a),
            _ => return Err(PyValueError::new_err(format!("no request mode {mode}"))),
        };
        let got = py.detach(|| self.store.fetch(key, request)).map_err(read_err)?;
        Ok(match got {
            None => None,
            Some(Fetched::Bytes(v)) => Some(("bytes", PyBytes::new(py, &v)).into_pyobject(py)?.into_any()),
            Some(Fetched::Reference { kind, payload }) => Some(("reference", kind, PyBytes::new(py, &payload)).into_pyobject(py)?.into_any()),
        })
    }

    /// Bytes `[start, end)` of the bytes value at `key`, hidden or not (a `key` source).
    fn key_bytes<'py>(&self, py: Python<'py>, key: &str, start: u64, end: u64) -> PyResult<Bound<'py, PyBytes>> {
        let b = py.detach(|| self.store.key_bytes(key, start, end)).map_err(read_err)?;
        Ok(PyBytes::new(py, &b))
    }

    fn raw<'py>(&self, py: Python<'py>, key: &str) -> PyResult<Option<Bound<'py, PyBytes>>> {
        let b = py.detach(|| self.store.raw(key)).map_err(read_err)?;
        Ok(b.map(|b| PyBytes::new(py, &b)))
    }

    fn list(&self, py: Python<'_>, prefix: &str) -> PyResult<Vec<String>> {
        py.detach(|| self.store.list(prefix)).map_err(read_err)
    }

    /// A key's property: None if the key is missing or has no value for `name`.
    fn property<'py>(&self, py: Python<'py>, key: &str, name: &str) -> PyResult<Option<Bound<'py, PyAny>>> {
        let got = py.detach(|| self.store.property(key, name)).map_err(read_err)?;
        Ok(match got.flatten() {
            None => None,
            Some(Prop::U64(n)) => Some(n.into_pyobject(py)?.into_any()),
            Some(Prop::I64(n)) => Some(n.into_pyobject(py)?.into_any()),
            Some(Prop::Str(s)) => Some(s.into_pyobject(py)?.into_any()),
            Some(Prop::Bytes(b)) => Some(PyBytes::new(py, &b).into_any()),
        })
    }
}

/// A binary file object, which the writer gives its output to.
struct PyWrite(Py<PyAny>);

impl io::Write for PyWrite {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        self.write_all(buf)?;
        Ok(buf.len())
    }

    fn write_all(&mut self, buf: &[u8]) -> io::Result<()> {
        Python::attach(|py| self.0.call_method1(py, "write", (PyBytes::new(py, buf),)).map(drop)).map_err(io_err)
    }

    fn flush(&mut self) -> io::Result<()> {
        Python::attach(|py| self.0.call_method0(py, "flush").map(drop)).map_err(io_err)
    }
}

fn codec(c: u8) -> PyResult<Codec> {
    Codec::from_u8(c).ok_or_else(|| PyValueError::new_err(format!("no codec {c}")))
}

#[pyclass(name = "PhkvWriter", module = "refkv._core")]
pub struct PyPhkvWriter {
    writer: Mutex<Option<Writer<PyWrite>>>,
}

impl PyPhkvWriter {
    fn with<T>(&self, f: impl FnOnce(&mut Writer<PyWrite>) -> Result<T, Error>) -> PyResult<T> {
        let mut w = self.writer.lock().unwrap();
        f(w.as_mut().ok_or_else(|| PyValueError::new_err("the writer is closed"))?).map_err(write_err)
    }
}

#[pymethods]
impl PyPhkvWriter {
    /// A writer to the binary file object `f`: `codec` and `section_codec` are codec numbers
    /// (0 none, 1 DEFLATE, 2 Zstandard).
    #[new]
    #[pyo3(signature = (f, codec_=0, inline_max=4096, crc32c=true, section_codec=2, page_size=4096))]
    fn new(f: Py<PyAny>, codec_: u8, inline_max: u64, crc32c: bool, section_codec: u8, page_size: u64) -> PyResult<Self> {
        let opts = Options { codec: codec(codec_)?, inline_max, crc32c, section_codec: codec(section_codec)?, page_size, distance: 0 };
        Ok(PyPhkvWriter { writer: Mutex::new(Some(Writer::new(PyWrite(f), opts))) })
    }

    /// Interns a source, given as its protobuf message, and gives its index.
    fn source(&self, message: &[u8]) -> PyResult<u32> {
        let src = Source::decode(message).map_err(|e| PyValueError::new_err(e.to_string()))?;
        self.with(|w| w.source(src))
    }

    #[pyo3(signature = (key, data, codec_=None))]
    fn add_bytes(&self, py: Python<'_>, key: &str, data: &[u8], codec_: Option<u8>) -> PyResult<()> {
        let codec_ = codec_.map(codec).transpose()?;
        py.detach(|| self.with(|w| match codec_ {
            Some(c) => w.add_bytes_with(key, data, c),
            None => w.add_bytes(key, data),
        }))
    }

    /// Adds the bytes of the file at `path`, streaming it.
    fn add_file(&self, py: Python<'_>, key: &str, path: std::path::PathBuf) -> PyResult<()> {
        py.detach(|| self.with(|w| w.add_file(key, &path)))
    }

    /// Adds a reference to the concatenation of ranges, each `(source, offset, length, crc32c)`
    /// or literal bytes.
    fn add_ranges(&self, key: &str, ranges: Vec<Bound<'_, PyAny>>) -> PyResult<()> {
        let ranges = ranges
            .into_iter()
            .map(|r| match r.extract::<Vec<u8>>() {
                Ok(data) if r.is_instance_of::<PyBytes>() => Ok(Range::literal(data)),
                _ => {
                    let (source, offset, length, crc32c): (u32, u64, u64, Option<u32>) = r.extract()?;
                    Ok(Range { source, offset, length, data: None, crc32c })
                }
            })
            .collect::<PyResult<Vec<Range>>>()?;
        self.with(|w| w.add_ranges(key, &ranges))
    }

    /// Sets a property of a key: `kind` is `"u64"`, `"i64"`, `"bytes"` or `"str"`.
    fn set_property(&self, key: &str, name: &str, kind: &str, value: &Bound<'_, PyAny>) -> PyResult<()> {
        let value = match kind {
            "u64" => Prop::U64(value.extract()?),
            "i64" => Prop::I64(value.extract()?),
            "bytes" => Prop::Bytes(value.extract()?),
            "str" => Prop::Str(value.extract()?),
            _ => return Err(PyValueError::new_err(format!("no property type {kind:?}"))),
        };
        self.with(|w| w.set_property(key, name, value))
    }

    fn set_metadata(&self, name: &str, value: &[u8]) -> PyResult<()> {
        self.with(|w| w.set_metadata(name, value))
    }

    /// Writes the records and the other sections; the writer is then closed.
    fn close(&self, py: Python<'_>) -> PyResult<()> {
        let w = self.writer.lock().unwrap().take().ok_or_else(|| PyValueError::new_err("the writer is closed"))?;
        py.detach(|| w.close()).map(drop).map_err(write_err)
    }
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyPhkvStore>()?;
    m.add_class::<PyPhkvWriter>()?;
    m.add("PhkvReadError", m.py().get_type::<PhkvReadError>())?;
    Ok(())
}
