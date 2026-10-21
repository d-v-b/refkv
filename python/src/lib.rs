//! refkv's Python bindings to the Rust core (rust/refkv-ir), compiled into the package
//! as the module `refkv._core`: the parsers as step/feed/finish objects, and the IR's
//! columns, tables and per-element reads.

use refkv_ir::check;
use refkv_ir::czi::Czi;
use refkv_ir::ir::*;
use refkv_ir::nd2::{Nd2, Step};
use refkv_ir::tiff::Tiff;
use refkv_ir::types::{self, Val};
use pyo3::exceptions::{PyException, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList};
use std::collections::HashMap;
use std::sync::Mutex;

pyo3::create_exception!(refkv._core, Rejected, PyException);
pyo3::create_exception!(refkv._core, Violation, PyException);

fn bytes_of<T: Copy, const N: usize>(v: &[T], f: impl Fn(T) -> [u8; N]) -> Vec<u8> {
    let mut out = Vec::with_capacity(v.len() * N);
    for &x in v {
        out.extend_from_slice(&f(x));
    }
    out
}

fn to_py<'py>(py: Python<'py>, v: &Val) -> PyResult<Bound<'py, PyAny>> {
    Ok(match v {
        Val::Int(i) => {
            if let Ok(x) = i64::try_from(*i) {
                x.into_pyobject(py)?.into_any()
            } else {
                (*i as u64).into_pyobject(py)?.into_any()
            }
        }
        Val::Float(f) => f.into_pyobject(py)?.into_any(),
        Val::Str(s) => s.into_pyobject(py)?.into_any(),
        Val::Bytes(b) => PyBytes::new(py, b).into_any(),
        Val::List(items) => {
            let out: Vec<Bound<'py, PyAny>> = items
                .iter()
                .map(|x| to_py(py, x))
                .collect::<PyResult<_>>()?;
            PyList::new(py, out)?.into_any()
        }
        Val::Rec(fields) => {
            let d = PyDict::new(py);
            for (k, x) in fields {
                d.set_item(k, to_py(py, x)?)?;
            }
            d.into_any()
        }
    })
}

#[pyclass(name = "Ir", module = "refkv._core")]
pub struct PyIr {
    pub ir: Ir,
    types: Mutex<HashMap<u32, Option<types::Ty>>>,
}

impl PyIr {
    pub fn new(ir: Ir) -> Self {
        PyIr {
            ir,
            types: Mutex::new(HashMap::new()),
        }
    }
    fn idx(&self, i: i64) -> PyResult<u32> {
        if i < 0 || i as usize >= self.ir.len() {
            return Err(PyValueError::new_err(format!("no element {i}")));
        }
        Ok(i as u32)
    }
}

#[pymethods]
impl PyIr {
    fn __len__(&self) -> usize {
        self.ir.len()
    }

    #[getter]
    fn size(&self) -> u64 {
        self.ir.size
    }

    /// The dense columns as little-endian bytes: kind u1, parent u4, name u4,
    /// nidx u8, type u4, space u4, start u8, len u8.
    fn columns<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let d = PyDict::new(py);
        let ir = &self.ir;
        d.set_item("kind", PyBytes::new(py, &ir.kind))?;
        d.set_item(
            "parent",
            PyBytes::new(py, &bytes_of(&ir.parent, u32::to_le_bytes)),
        )?;
        d.set_item(
            "name",
            PyBytes::new(py, &bytes_of(&ir.name, u32::to_le_bytes)),
        )?;
        d.set_item(
            "nidx",
            PyBytes::new(py, &bytes_of(&ir.nidx, u64::to_le_bytes)),
        )?;
        d.set_item(
            "type",
            PyBytes::new(py, &bytes_of(&ir.ty, u32::to_le_bytes)),
        )?;
        d.set_item(
            "space",
            PyBytes::new(py, &bytes_of(&ir.space, u32::to_le_bytes)),
        )?;
        d.set_item(
            "start",
            PyBytes::new(py, &bytes_of(&ir.start, u64::to_le_bytes)),
        )?;
        d.set_item(
            "len",
            PyBytes::new(py, &bytes_of(&ir.len, u64::to_le_bytes)),
        )?;
        Ok(d)
    }

    fn names(&self) -> Vec<String> {
        self.ir.names.list.clone()
    }
    fn types(&self) -> Vec<String> {
        self.ir.types.list.clone()
    }
    fn forms(&self) -> Vec<String> {
        self.ir.forms.list.clone()
    }
    /// (run root, count, stride)
    fn runs(&self) -> Vec<(u32, u64, u64)> {
        self.ir.runs.clone()
    }
    /// (element, form id)
    fn form_ids(&self) -> Vec<(u32, u32)> {
        self.ir.form_of.clone()
    }
    /// (element, target)
    fn targets(&self) -> Vec<(u32, u32)> {
        self.ir.targets.clone()
    }
    /// (element, kind it had): aliases finish() made
    fn was(&self) -> Vec<(u32, u8)> {
        self.ir.was.clone()
    }
    /// (element, offset, length) into `value_bytes()`
    fn value_index(&self) -> Vec<(u32, u64, u32)> {
        self.ir.values.clone()
    }
    fn value_bytes<'py>(&self, py: Python<'py>) -> Bound<'py, PyBytes> {
        PyBytes::new(py, &self.ir.vbytes)
    }

    fn kind(&self, i: i64) -> PyResult<u8> {
        Ok(self.ir.kind[self.idx(i)? as usize])
    }
    fn parent(&self, i: i64) -> PyResult<i64> {
        let p = self.ir.parent[self.idx(i)? as usize];
        Ok(if p == NONE { -1 } else { p as i64 })
    }
    fn name(&self, i: i64) -> PyResult<String> {
        Ok(self.ir.name_of(self.idx(i)?))
    }
    fn nidx(&self, i: i64) -> PyResult<Option<u64>> {
        let k = self.ir.nidx[self.idx(i)? as usize];
        Ok(if k == NO_INDEX { None } else { Some(k) })
    }
    fn path(&self, i: i64) -> PyResult<String> {
        Ok(self.ir.path(self.idx(i)?))
    }
    #[pyo3(name = "type")]
    fn type_(&self, i: i64) -> PyResult<String> {
        Ok(self
            .ir
            .types
            .get(self.ir.ty[self.idx(i)? as usize])
            .to_string())
    }
    fn space(&self, i: i64) -> PyResult<u32> {
        Ok(self.ir.space[self.idx(i)? as usize])
    }
    fn extent(&self, i: i64) -> PyResult<(u64, u64)> {
        let i = self.idx(i)? as usize;
        Ok((self.ir.start[i], self.ir.len[i]))
    }
    fn run(&self, i: i64) -> PyResult<Option<(u64, u64)>> {
        Ok(self.ir.run(self.idx(i)?))
    }
    fn form(&self, i: i64) -> PyResult<Option<String>> {
        Ok(self.ir.form(self.idx(i)?).map(|s| s.to_string()))
    }
    fn target(&self, i: i64) -> PyResult<i64> {
        let t = self.ir.target(self.idx(i)?);
        Ok(if t == NONE { -1 } else { t as i64 })
    }
    fn was_kind(&self, i: i64) -> PyResult<Option<u8>> {
        Ok(self.ir.was_kind(self.idx(i)?))
    }
    fn raw<'py>(&self, py: Python<'py>, i: i64) -> PyResult<Option<Bound<'py, PyBytes>>> {
        Ok(self
            .ir
            .value_bytes(self.idx(i)?)
            .map(|b| PyBytes::new(py, b)))
    }
    /// The decoded value: decode(type, its bytes), or None when the parser kept no bytes.
    fn value<'py>(&self, py: Python<'py>, i: i64) -> PyResult<Option<Bound<'py, PyAny>>> {
        let i = self.idx(i)?;
        let Some(raw) = self.ir.value_bytes(i) else {
            return Ok(None);
        };
        let tid = self.ir.ty[i as usize];
        let mut cache = self.types.lock().unwrap();
        let ty = cache
            .entry(tid)
            .or_insert_with(|| types::parse(self.ir.types.get(tid)).ok());
        let Some(ty) = ty else { return Ok(None) };
        let v = types::decode(ty, raw).map_err(PyValueError::new_err)?;
        Ok(Some(to_py(py, &v)?))
    }
    fn children(&self, i: i64) -> PyResult<Vec<u32>> {
        Ok(self.ir.children(self.idx(i)?).to_vec())
    }
    fn child(&self, i: i64, name: &str) -> PyResult<Option<u32>> {
        let i = self.idx(i)?;
        Ok(self
            .ir
            .children(i)
            .iter()
            .copied()
            .find(|&c| self.ir.name_of(c) == name))
    }
    /// Coverage, injectivity, the root and the names, and the other invariants
    /// (conventions §8.1
    /// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#81-elements>); raises
    /// Violation.
    fn check(&self) -> PyResult<()> {
        check::check(&self.ir).map_err(Violation::new_err)
    }
    /// The leaves in source order, as ranges (adjacent ones merged).
    fn leaves(&self) -> PyResult<Vec<(u64, u64)>> {
        check::leaves(&self.ir).map_err(Violation::new_err)
    }
    fn memory(&self) -> usize {
        self.ir.memory()
    }
    /// The data members of element `i`, runs expanded: four lists (name index,
    /// start, length, form id).
    fn members(&self, i: i64) -> PyResult<(Vec<u64>, Vec<u64>, Vec<u64>, Vec<u32>)> {
        let m = self.ir.members(self.idx(i)?);
        Ok((
            m.iter().map(|x| x.0).collect(),
            m.iter().map(|x| x.1).collect(),
            m.iter().map(|x| x.2).collect(),
            m.iter().map(|x| x.3).collect(),
        ))
    }
    /// The shared data sources recipes name.
    fn shared<'py>(&self, py: Python<'py>) -> Vec<Bound<'py, PyBytes>> {
        self.ir.shared.iter().map(|b| PyBytes::new(py, b)).collect()
    }
    /// A store IR's objects in id order: (key, virtual offset of its byte 0, size) (conventions
    /// §8.9 <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#89-stores>), for reading
    /// an object described in place at virtual offsets; empty for a file IR.
    fn object_bases(&self) -> Vec<(String, u64, u64)> {
        let src = self.ir.sources();
        src.objects.iter().map(|&o| (self.ir.key(o), src.base[o as usize], self.ir.len[o as usize])).collect()
    }
    /// A digest of every row (for comparing builds).
    fn digest(&self) -> u64 {
        self.ir.digest()
    }
    fn runs_count(&self) -> usize {
        self.ir.runs.len()
    }
    fn budget<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let d = PyDict::new(py);
        if let Some(b) = &self.ir.budget {
            d.set_item("records", (b.spent_records, b.records))?;
            d.set_item("elements", (b.spent_elements, b.elements))?;
        }
        Ok(d)
    }

    /// The IR a mirror's table describes (`refkv_source/ir/...`, its entries given by
    /// `get(key) -> bytes | None`), its column runs expanded; `read(offset, length)`
    /// gives bytes of the source, for the columns read from arrays in it. Raises
    /// Violation for a bad table.
    #[staticmethod]
    fn from_archive(get: Bound<'_, PyAny>, read: Bound<'_, PyAny>) -> PyResult<PyIr> {
        let g = |k: &str| -> Option<Vec<u8>> { get.call1((k,)).ok()?.extract::<Option<Vec<u8>>>().ok()? };
        let t = refkv_ir::mirror::table_from(&g).map_err(Violation::new_err)?;
        let mut rd = |o: u64, n: u64| -> Result<Vec<u8>, String> {
            read.call1((o, n)).map_err(|e| e.to_string())?.extract::<Vec<u8>>().map_err(|e| e.to_string())
        };
        let ir = refkv_ir::mirror::load(&t, &mut rd).map_err(Violation::new_err)?;
        Ok(PyIr::new(ir))
    }

    /// An IR from a stored table (the mirror's), for checking and rebuilding: each
    /// element's name (default `""`) and name index (default none) are `name` and
    /// `nidx` when given.
    #[staticmethod]
    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (size, kind, parent, start, len, space, runs, targets, name=None, nidx=None))]
    fn from_table(
        size: u64,
        kind: Vec<u8>,
        parent: Vec<i64>,
        start: Vec<u64>,
        len: Vec<u64>,
        space: Vec<u32>,
        runs: Vec<(u32, u64, u64)>,
        targets: Vec<(u32, u32)>,
        name: Option<Vec<String>>,
        nidx: Option<Vec<Option<u64>>>,
    ) -> PyResult<PyIr> {
        let n = kind.len();
        let mut ir = Ir {
            size,
            budget: None,
            ..Default::default()
        };
        ir.types.id("");
        ir.names.id("");
        ir.kind = kind;
        ir.parent = parent
            .iter()
            .map(|&p| if p < 0 { NONE } else { p as u32 })
            .collect();
        ir.name = match name {
            Some(v) => v.iter().map(|s| ir.names.id(s)).collect(),
            None => vec![0; n],
        };
        ir.nidx = match nidx {
            Some(v) => v.into_iter().map(|x| x.unwrap_or(NO_INDEX)).collect(),
            None => vec![NO_INDEX; n],
        };
        ir.ty = vec![0; n];
        ir.space = space;
        ir.start = start;
        ir.len = len;
        ir.runs = runs;
        ir.targets = targets;
        ir.finished = true;
        if [
            ir.parent.len(),
            ir.start.len(),
            ir.len.len(),
            ir.space.len(),
            ir.name.len(),
            ir.nidx.len(),
        ]
        .iter()
        .any(|&m| m != n)
        {
            return Err(PyValueError::new_err("columns of different lengths"));
        }
        ir.build_children();
        Ok(PyIr::new(ir))
    }
}

fn step_py(r: Result<Step, String>) -> PyResult<Option<Vec<(u64, u64)>>> {
    match r {
        Ok(Step::Read(v)) => Ok(Some(v)),
        Ok(Step::Done) => Ok(None),
        Err(e) => Err(Rejected::new_err(e)),
    }
}

/// The ND2 parser: `step()` gives the next batch of ranges (None when done),
/// `feed(offset, bytes)` gives one range's bytes, `finish()` the IR and the facts (JSON).
#[pyclass(name = "Nd2Parser", module = "refkv._core")]
pub struct PyNd2 {
    p: Option<Nd2>,
}

#[pymethods]
impl PyNd2 {
    #[new]
    fn new(size: u64) -> Self {
        PyNd2 {
            p: Some(Nd2::new(size)),
        }
    }
    fn step(&mut self, py: Python<'_>) -> PyResult<Option<Vec<(u64, u64)>>> {
        let p = self
            .p
            .as_mut()
            .ok_or_else(|| PyValueError::new_err("finished"))?;
        step_py(py.detach(|| p.step()))
    }
    fn feed(&mut self, offset: u64, data: &[u8]) -> PyResult<()> {
        self.p
            .as_mut()
            .ok_or_else(|| PyValueError::new_err("finished"))?
            .feed(offset, data.to_vec());
        Ok(())
    }
    fn finish(&mut self, py: Python<'_>) -> PyResult<(PyIr, String)> {
        let p = self
            .p
            .take()
            .ok_or_else(|| PyValueError::new_err("finished"))?;
        let (ir, facts) = py.detach(|| p.finish()).map_err(Rejected::new_err)?;
        Ok((PyIr::new(ir), facts.to_string()))
    }
    fn memory(&self) -> usize {
        self.p.as_ref().map(|p| p.ir.memory()).unwrap_or(0)
    }
}

/// The TIFF parser: as `Nd2Parser` (`finish()` gives the IR and the facts as JSON).
#[pyclass(name = "TiffParser", module = "refkv._core")]
pub struct PyTiff {
    p: Option<Tiff>,
}

#[pymethods]
impl PyTiff {
    #[new]
    fn new(size: u64) -> Self {
        PyTiff { p: Some(Tiff::new(size)) }
    }
    fn step(&mut self, py: Python<'_>) -> PyResult<Option<Vec<(u64, u64)>>> {
        let p = self.p.as_mut().ok_or_else(|| PyValueError::new_err("finished"))?;
        step_py(py.detach(|| p.step()))
    }
    fn feed(&mut self, offset: u64, data: &[u8]) -> PyResult<()> {
        self.p.as_mut().ok_or_else(|| PyValueError::new_err("finished"))?.feed(offset, data.to_vec());
        Ok(())
    }
    fn finish(&mut self, py: Python<'_>) -> PyResult<(PyIr, String)> {
        let p = self.p.take().ok_or_else(|| PyValueError::new_err("finished"))?;
        let (ir, facts) = py.detach(|| p.finish()).map_err(Rejected::new_err)?;
        Ok((PyIr::new(ir), facts.to_string()))
    }
}

#[pyclass(name = "CziParser", module = "refkv._core")]
pub struct PyCzi {
    p: Option<Czi>,
}

#[pymethods]
impl PyCzi {
    #[new]
    fn new(size: u64) -> Self {
        PyCzi {
            p: Some(Czi::new(size)),
        }
    }
    fn step(&mut self, py: Python<'_>) -> PyResult<Option<Vec<(u64, u64)>>> {
        let p = self
            .p
            .as_mut()
            .ok_or_else(|| PyValueError::new_err("finished"))?;
        step_py(py.detach(|| p.step()))
    }
    fn feed(&mut self, offset: u64, data: &[u8]) -> PyResult<()> {
        self.p
            .as_mut()
            .ok_or_else(|| PyValueError::new_err("finished"))?
            .feed(offset, data.to_vec());
        Ok(())
    }
    fn finish(&mut self, py: Python<'_>) -> PyResult<(PyIr, String)> {
        let p = self.p.take().ok_or_else(|| PyValueError::new_err("finished"))?;
        let (ir, facts) = py.detach(|| p.finish()).map_err(Rejected::new_err)?;
        Ok((PyIr::new(ir), facts.to_string()))
    }
}

/// A synthetic stream of `n` segments (a struct and a 32-byte header value each),
/// emitted into a compact IR, folded into runs or not: the per-element cost.
#[pyfunction]
fn synthetic(n: u64, fold: bool, distinct: bool) -> PyResult<(usize, usize)> {
    let size = 32 * n + 1;
    let mut ir = Ir::new(size);
    let mut prev: Option<u32> = None;
    for k in 0..n {
        let o = 32 * k;
        let mut h = [0u8; 32];
        h[..7].copy_from_slice(b"DELETED");
        if distinct {
            h[24..32].copy_from_slice(&k.to_le_bytes());
        }
        let s = ir
            .struct_(0, "segments/", k, 0, Some((o, 32)))
            .map_err(Rejected::new_err)?;
        ir.value(
            s,
            "header",
            NO_INDEX,
            "{id:cstr[16],allocated_size:<i8,used_size:<i8}",
            0,
            (o, 32),
            Some(&h),
        )
        .map_err(Rejected::new_err)?;
        if fold {
            match prev {
                Some(p) if ir.fold(p, s) => {}
                _ => prev = Some(s),
            }
        }
    }
    check::finish(&mut ir).map_err(Rejected::new_err)?;
    Ok((ir.len(), ir.memory()))
}

#[pyfunction]
fn schema_text() -> &'static str {
    refkv_ir::schema::ND2_SCHEMA
}

/// The values the CZI layout reads from a metadata XML (JSON), as the parser streams them.
#[pyfunction]
fn czi_xml_values(data: &[u8]) -> String {
    refkv_ir::cxml::values(data, 1 << 26).to_json().to_string()
}

/// The y chunk lengths (JSON) and bands (rows a band holds, bands a tile row) of a
/// CZI level of r tile rows of height h (the last h2), w pixels of q bytes wide.
#[pyfunction]
fn czi_bands(h: u64, h2: u64, r: u64, w: u64, q: u64, uncompressed: bool) -> (String, u64, u64) {
    let (y, rows, per) = refkv_ir::czi::bands(h, h2, r, w, q, uncompressed, 1 << 24, 1 << 20);
    (y.to_string(), rows, per)
}

/// A run: a format's parser and the read planner (sans-IO). The host loops
/// `poll()` (the requests to make now, each a list of (start, end) spans: one
/// span a plain range request, several a multi-range one) and `complete(k,
/// parts, seconds)` (each span's bytes, or None for a multi-range request the
/// server did not answer with its ranges), until `poll()` gives None; then
/// `finish()` gives the IR, the facts (JSON) and the planner's counts (JSON).
#[pyclass(name = "Run", module = "refkv._core")]
pub struct PyRun {
    r: Option<refkv_ir::run::Run>,
}

impl PyRun {
    fn get(&mut self) -> PyResult<&mut refkv_ir::run::Run> {
        self.r.as_mut().ok_or_else(|| PyValueError::new_err("finished"))
    }
}

#[pymethods]
impl PyRun {
    #[new]
    #[pyo3(signature = (format, size, remote, concurrency, amplification=None, byte_cost=None, whole_below=None))]
    fn new(format: &str, size: u64, remote: bool, concurrency: usize, amplification: Option<f64>, byte_cost: Option<f64>,
           whole_below: Option<u64>) -> PyResult<Self> {
        let f = refkv_ir::run::Format::from_name(format).ok_or_else(|| PyValueError::new_err(format!("no format {format:?}")))?;
        let mut r = refkv_ir::run::Run::new(f, size, remote, concurrency);
        r.planner.cap_override = amplification;
        if let Some(c) = byte_cost {
            r.planner.byte_cost = c;
        }
        if let Some(w) = whole_below {
            r.planner.whole_below = w;
        }
        Ok(PyRun { r: Some(r) })
    }
    fn poll(&mut self, py: Python<'_>) -> PyResult<Option<Vec<Vec<(u64, u64)>>>> {
        let r = self.get()?;
        let got = py.detach(|| r.poll()).map_err(Rejected::new_err)?;
        Ok(got.map(|v| v.into_iter().map(|q| q.spans).collect()))
    }
    #[pyo3(signature = (k, parts, seconds))]
    fn complete(&mut self, k: usize, parts: Option<Vec<Vec<u8>>>, seconds: f64) -> PyResult<()> {
        self.get()?.complete(k, parts, seconds).map_err(Rejected::new_err)
    }
    /// A request the host made itself (n bytes in `seconds`), for the cost model.
    fn observe(&mut self, n: u64, seconds: f64) -> PyResult<()> {
        self.get()?.planner.observe(n, seconds);
        Ok(())
    }
    /// Bytes the host holds already (from offset): ranges in them need no request.
    fn seed(&mut self, offset: u64, data: Vec<u8>) -> PyResult<()> {
        self.get()?.planner.seed(offset, data);
        Ok(())
    }
    #[getter]
    fn multirange(&mut self) -> PyResult<Option<bool>> {
        Ok(self.get()?.planner.multirange)
    }
    #[setter]
    fn set_multirange(&mut self, v: Option<bool>) -> PyResult<()> {
        self.get()?.planner.multirange = v;
        Ok(())
    }
    /// The run's format: "tiff" becomes "ndpi" once its parser finds the file is NDPI.
    #[getter]
    fn format(&mut self) -> PyResult<&'static str> {
        Ok(self.get()?.format.name())
    }
    /// The planner's counts and model (JSON).
    fn stats(&mut self) -> PyResult<String> {
        Ok(self.get()?.planner.stats_json().to_string())
    }
    /// Finishes, projects and (with `mirror`) mirrors: (the output in `out_py`'s form,
    /// the IR).
    #[pyo3(signature = (url, mirror=true))]
    fn output<'py>(&mut self, py: Python<'py>, url: &str, mirror: bool) -> PyResult<(Bound<'py, PyAny>, PyIr)> {
        let r = self.r.take().ok_or_else(|| PyValueError::new_err("finished"))?;
        let format = r.format;
        let (o, ir) = py
            .detach(|| -> Result<_, String> {
                let (ir, facts, stats, copied) = r.finish_copied()?;
                Ok((refkv_ir::run::output(format, &ir, &facts, stats, url, mirror, &copied)?, ir))
            })
            .map_err(Rejected::new_err)?;
        Ok((out_py(py, &o)?, PyIr::new(ir)))
    }
    fn finish(&mut self, py: Python<'_>) -> PyResult<(PyIr, String, String)> {
        let r = self.r.take().ok_or_else(|| PyValueError::new_err("finished"))?;
        let (ir, facts, stats) = py.detach(|| r.finish()).map_err(Rejected::new_err)?;
        Ok((PyIr::new(ir), facts.to_string(), stats.to_string()))
    }
}

/// A store run (src/store.rs): `StoreRun(format, listing, concurrency)` over the listing
/// [(key, size)]; the host loops `poll()` (the requests to make now, concurrently: (object
/// index, key, spans [(start, end)])) and `complete(k, parts)` (each span's bytes) until
/// `poll()` gives None; then `output(url, mirror)` or `finish()`. `ignore(keys, folders)`,
/// before the first poll, gives the keys the listing ignores.
#[pyclass(name = "StoreRun", module = "refkv._core")]
pub struct PyStoreRun {
    r: Option<refkv_ir::store::StoreRun>,
}

impl PyStoreRun {
    fn get(&mut self) -> PyResult<&mut refkv_ir::store::StoreRun> {
        self.r.as_mut().ok_or_else(|| PyValueError::new_err("finished"))
    }
}

#[pymethods]
impl PyStoreRun {
    #[new]
    fn new(format: &str, listing: Vec<(String, u64)>, concurrency: usize) -> PyResult<Self> {
        let f = refkv_ir::store::StoreFormat::from_name(format).ok_or_else(|| PyValueError::new_err(format!("no store format {format:?}")))?;
        let r = refkv_ir::store::StoreRun::new(f, listing, concurrency).map_err(Rejected::new_err)?;
        Ok(PyStoreRun { r: Some(r) })
    }
    #[allow(clippy::type_complexity)]
    fn poll(&mut self, py: Python<'_>) -> PyResult<Option<Vec<(usize, String, Vec<(u64, u64)>)>>> {
        let r = self.get()?;
        let got = py.detach(|| r.poll()).map_err(Rejected::new_err)?;
        Ok(got.map(|v| v.into_iter().map(|q| (q.object, q.key, q.spans)).collect()))
    }
    fn complete(&mut self, k: usize, parts: Vec<Vec<u8>>) -> PyResult<()> {
        self.get()?.complete(k, parts).map_err(Rejected::new_err)
    }
    /// The run's counts (JSON).
    fn stats(&mut self) -> PyResult<String> {
        Ok(self.get()?.stats_json().to_string())
    }
    /// The keys the listing ignores, before the first poll (`StoreRun::ignore`): `keys`, the
    /// relative keys the store reader ignored and recorded (`Store.ignored`), which the
    /// projection records as its convention says, and `folders`, its empty `d/` keys
    /// (`Store.folders`), which a SAFE directory's parser reads as directories.
    #[pyo3(signature = (keys, folders=Vec::new()))]
    fn ignore(&mut self, keys: Vec<String>, folders: Vec<String>) -> PyResult<()> {
        self.get()?.ignore(keys, folders).map_err(Rejected::new_err)
    }
    /// The profile the run's store is decided to be so far (`StoreRun::profile`).
    fn profile(&mut self) -> PyResult<String> {
        Ok(self.get()?.profile().to_string())
    }
    #[pyo3(signature = (url, mirror=true))]
    fn output<'py>(&mut self, py: Python<'py>, url: &str, mirror: bool) -> PyResult<(Bound<'py, PyAny>, PyIr)> {
        let r = self.r.take().ok_or_else(|| PyValueError::new_err("finished"))?;
        let format = r.format;
        let (o, ir) = py
            .detach(|| -> Result<_, String> {
                let (ir, facts, stats) = r.finish()?;
                Ok((refkv_ir::store::store_output(format, &ir, &facts, stats, url, mirror)?, ir))
            })
            .map_err(Rejected::new_err)?;
        Ok((out_py(py, &o)?, PyIr::new(ir)))
    }
    fn finish(&mut self, py: Python<'_>) -> PyResult<(PyIr, String, String)> {
        let r = self.r.take().ok_or_else(|| PyValueError::new_err("finished"))?;
        let (ir, facts, stats) = py.detach(|| r.finish()).map_err(Rejected::new_err)?;
        Ok((PyIr::new(ir), facts.to_string(), stats.to_string()))
    }
}

/// The rebuild of a store's mirror (conventions §8.9
/// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#89-stores>) from an archive's
/// entries (`get(key)`: bytes or None): every object as (key, size, bytes), the bytes of a held
/// object rebuilt from its leaves, None for a referenced or empty one; or Rejected.
#[pyfunction]
#[allow(clippy::type_complexity)]
fn rebuild_store<'py>(py: Python<'py>, get: Bound<'py, PyAny>) -> PyResult<Vec<(String, u64, Option<Bound<'py, PyBytes>>)>> {
    let g = |k: &str| -> Option<Vec<u8>> { get.call1((k,)).ok()?.extract::<Option<Vec<u8>>>().ok()? };
    let v = refkv_ir::mirror::rebuild_store(&g).map_err(Rejected::new_err)?;
    Ok(v.into_iter().map(|(k, n, b)| (k, n, b.map(|b| PyBytes::new(py, &b)))).collect())
}

/// An `Out` as Python objects: (url, entries [(key, bytes)], refs [(key, parts)],
/// data sources [bytes], lazy prefixes [str], summary JSON); a part is (offset,
/// length) of source 0, (source, offset, length), bytes, or ("object", key, offset, length) of a
/// store's object.
fn out_py<'py>(py: Python<'py>, o: &refkv_ir::out::Out) -> PyResult<Bound<'py, PyAny>> {
    use refkv_ir::out::Part;
    let entries = PyList::empty(py);
    for (k, v) in &o.entries {
        entries.append((k.as_str(), PyBytes::new(py, v)))?;
    }
    let refs = PyList::empty(py);
    for (k, parts) in &o.refs {
        let ps = PyList::empty(py);
        for p in parts {
            match p {
                Part::Src(a, n) => ps.append((*a, *n))?,
                Part::Data(s, a, n) => ps.append((*s, *a, *n))?,
                Part::Lit(b) => ps.append(PyBytes::new(py, b))?,
                Part::Obj(k, a, n) => ps.append(("object", k.as_str(), *a, *n))?,
            }
        }
        refs.append((k.as_str(), ps))?;
    }
    let data: Vec<Bound<'py, PyBytes>> = o.data.iter().map(|d| PyBytes::new(py, d)).collect();
    let t = (o.url.as_str(), entries, refs, data, o.lazy.clone(), refkv_ir::out::to_text(&o.summary));
    Ok(t.into_pyobject(py)?.into_any())
}

/// The image projection of an IR (format "nd2", "tiff", "czi", "dicom", "nifti", "ims", "ndpi" or "mrc"; facts as JSON) as
/// Python objects (`out_py`), or Rejected.
#[pyfunction]
fn project<'py>(py: Python<'py>, format: &str, ir: &PyIr, facts: &str, url: &str) -> PyResult<Bound<'py, PyAny>> {
    let f = refkv_ir::run::Format::from_name(format).ok_or_else(|| PyValueError::new_err(format!("no format {format:?}")))?;
    let facts: serde_json::Value = serde_json::from_str(facts).map_err(|e| PyValueError::new_err(e.to_string()))?;
    let o = py.detach(|| refkv_ir::project::project(f, &ir.ir, &facts, url, &Default::default())).map_err(Rejected::new_err)?;
    out_py(py, &o)
}

/// The mirror of an IR (for the profile "nd2", "tiff", "czi", "dicom", "nifti", "ims", "ndpi" or "mrc"): its entries and
/// references under `refkv_source` (`out_py`'s form, the url empty), and what the
/// table folded (JSON).
#[pyfunction]
fn mirror<'py>(py: Python<'py>, ir: &PyIr, profile: &str) -> PyResult<(Bound<'py, PyAny>, String)> {
    let (o, folded) = py.detach(|| {
        let mut o = refkv_ir::out::Out::new("");
        refkv_ir::mirror::mirror(&ir.ir, &mut o, profile).map(|f| (o, f))
    }).map_err(Rejected::new_err)?;
    Ok((out_py(py, &o)?, folded.json().to_string()))
}

/// What makes a stored mirror non-canonical (conventions §8.8
/// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#88-canonical-form>), or None:
/// `get(key)` gives the archive's entries (bytes or None), `read(offset, length)` the source's
/// bytes.
#[pyfunction]
fn canonical_problem(get: Bound<'_, PyAny>, read: Bound<'_, PyAny>) -> Option<String> {
    let g = |k: &str| -> Option<Vec<u8>> { get.call1((k,)).ok()?.extract::<Option<Vec<u8>>>().ok()? };
    let mut rd = |o: u64, n: u64| -> Result<Vec<u8>, String> {
        read.call1((o, n)).map_err(|e| e.to_string())?.extract::<Vec<u8>>().map_err(|e| e.to_string())
    };
    refkv_ir::mirror::canonical_problem(&g, &mut rd)
}

/// Where a stored mirror's view differs from the one its table and the source give
/// (conventions §8.7 <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#87-the-view>, the
/// validator's check), or None: `keys` are the archive's entry
/// names, `get(key)` its entries, `read(offset, length)` the source's bytes.
#[pyfunction]
fn view_problem(keys: Vec<String>, get: Bound<'_, PyAny>, read: Bound<'_, PyAny>) -> Option<String> {
    let g = |k: &str| -> Option<Vec<u8>> { get.call1((k,)).ok()?.extract::<Option<Vec<u8>>>().ok()? };
    let mut rd = |o: u64, n: u64| -> Result<Vec<u8>, String> {
        read.call1((o, n)).map_err(|e| e.to_string())?.extract::<Vec<u8>>().map_err(|e| e.to_string())
    };
    refkv_ir::mirror::view_problem(&keys, &g, &mut rd)
}

/// The planner's merge of ranges (offset, length) into requests (start, end), for a
/// gap threshold, an amplification cap and a concurrency (the policy's core, for tests).
#[pyfunction]
fn plan_spans(ranges: Vec<(u64, u64)>, threshold: u64, cap: f64, concurrency: usize) -> Vec<(u64, u64)> {
    refkv_ir::plan::plan_spans(&ranges, threshold, cap, concurrency)
}

/// For tests: the reference payload past which a NIfTI or MRC edge chunk is copied (None:
/// the payload limit), so that small files reach the copy (spec/virtualize/nifti.md §7.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/nifti.md#72-chunks>,
/// spec/virtualize/mrc.md §14.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mrc.md#142-chunks>).
#[pyfunction]
#[pyo3(signature = (n=None))]
fn _set_copy_threshold(n: Option<usize>) {
    refkv_ir::out::set_copy_threshold(n);
}

#[pymodule]
#[pyo3(name = "_core")]
fn _core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyIr>()?;
    m.add_class::<PyNd2>()?;
    m.add_class::<PyCzi>()?;
    m.add_class::<PyTiff>()?;
    m.add_class::<PyRun>()?;
    m.add_class::<PyStoreRun>()?;
    m.add_function(wrap_pyfunction!(rebuild_store, m)?)?;
    m.add_function(wrap_pyfunction!(plan_spans, m)?)?;
    m.add_function(wrap_pyfunction!(_set_copy_threshold, m)?)?;
    m.add_function(wrap_pyfunction!(project, m)?)?;
    m.add_function(wrap_pyfunction!(mirror, m)?)?;
    m.add_function(wrap_pyfunction!(canonical_problem, m)?)?;
    m.add_function(wrap_pyfunction!(view_problem, m)?)?;
    m.add("REVISION", refkv_ir::out::REVISION)?;
    m.add_function(wrap_pyfunction!(synthetic, m)?)?;
    m.add_function(wrap_pyfunction!(schema_text, m)?)?;
    m.add_function(wrap_pyfunction!(czi_xml_values, m)?)?;
    m.add_function(wrap_pyfunction!(czi_bands, m)?)?;
    m.add("Rejected", m.py().get_type::<Rejected>())?;
    m.add("Violation", m.py().get_type::<Violation>())?;
    Ok(())
}
