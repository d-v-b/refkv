//! The N5 projection (spec/virtualize/n5.md §2
//! <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/n5.md#2-nodes>–§6
//! <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/n5.md#6-other-objects>) from a valid
//! store IR: the nodes the parser classified (the facts), their documents read back from the
//! held objects, the chunk objects and the other objects from the listing. It writes the
//! hierarchy outside `refkv_source` (whose group is the mirror's), and the references: each
//! chunk object at its own key, each other object at `refkv_source/objects/<k>`, whole.

use crate::ir::Ir;
use crate::n5::{compression, document_key};
use crate::out::{convention, root_property, Out, Part, CONVENTION_KEY, SOURCE_NODE};
use crate::project::store::{canonical_index, check_key, object_key, OBJECTS};
use crate::json::{self, Js, Jv};
use serde_json::{json, Value as J};
use std::collections::{BTreeMap, BTreeSet, HashMap};

const PROFILE: &str = "n5";
/// The members of a dataset's document the Zarr array metadata holds.
const LAYOUT: [&str; 6] = ["dimensions", "blockSize", "dataType", "compression", "compressionType", "n5"];

fn join(parent: &str, name: &str) -> String {
    if parent.is_empty() { name.to_string() } else { format!("{parent}/{name}") }
}

fn doc_key(path: &str) -> String {
    join(path, "zarr.json")
}

fn table(name: &str) -> &'static J {
    crate::n5::schema().table(name)
}

fn obj(m: Vec<(Js, Jv)>) -> Jv {
    Jv::Obj(m)
}

fn s(k: &str) -> Js {
    Js::new(k)
}

/// `{"attributes": A, "metadata": M}`, each member only when not empty (spec/virtualize/n5.md §5
/// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/n5.md#5-source-metadata>).
fn source_metadata(attributes: Vec<(Js, Jv)>, metadata: Vec<(Js, Jv)>) -> Vec<(Js, Jv)> {
    let mut m = Vec::new();
    if !attributes.is_empty() {
        m.push((s("attributes"), obj(attributes)));
    }
    if !metadata.is_empty() {
        m.push((s("metadata"), obj(metadata)));
    }
    m
}

/// A node's attributes (conventions §2
/// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#2-attributes>): `target`, and the
/// convention when the node is the root (`url`) or has source metadata (`own` not empty).
fn declare(target: Vec<(Js, Jv)>, url: Option<&str>, own: Vec<(Js, Jv)>) -> Jv {
    let mut value: Vec<(Js, Jv)> = match url {
        Some(u) => root_property(PROFILE, u).into_iter().map(|(k, v)| (Js::new(&k), Jv::from_j(&v))).collect(),
        None => Vec::new(),
    };
    if !own.is_empty() {
        value.push((s(PROFILE), obj(own)));
    }
    if value.is_empty() {
        return obj(target);
    }
    let mut a = target;
    a.push((s("zarr_conventions"), Jv::Arr(vec![Jv::from_j(&convention(PROFILE))])));
    a.push((s(CONVENTION_KEY), obj(value)));
    obj(a)
}

// ---- datasets (spec/virtualize/n5.md §3 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/n5.md#3-datasets>)

fn int(v: Option<&Jv>) -> i64 {
    v.and_then(|x| x.int(i64::MIN, i64::MAX)).unwrap_or(0)
}

/// The level of a gzip or zstd compression: the member `level` (gzip's −1 is 6), or the default.
fn level(t: &str, c: &Jv) -> i64 {
    let l = &table("levels")[t];
    match c.get("level") {
        None => l[2].as_i64().unwrap_or(0),
        Some(v) => match int(Some(v)) {
            -1 if t == "gzip" => 6,
            x => x,
        },
    }
}

/// The compression's codec (None for raw), and the members of the compression object it
/// does not carry.
fn compressor(c: &Jv, b: i64) -> (Option<Jv>, Vec<(Js, Jv)>) {
    let t = c.get("type").and_then(Jv::as_str).unwrap_or("");
    let (codec, carried): (Option<Jv>, &[&str]) = match t {
        "raw" => (None, &["type"]),
        "gzip" => {
            let z = matches!(c.get("useZlib"), Some(Jv::Bool(true)));
            let name = if z { "zlib" } else { "gzip" };
            (Some(Jv::obj(vec![("name", Jv::str(name)), ("configuration", Jv::obj(vec![("level", Jv::integer(level(t, c)))]))])),
             &["type", "useZlib", "level"])
        }
        "zstd" => (Some(Jv::obj(vec![("name", Jv::str("zstd")),
                                     ("configuration", Jv::obj(vec![("level", Jv::integer(level(t, c))), ("checksum", Jv::Bool(false))]))])),
                   &["type", "level"]),
        _ => {
            // blosc (the schema admits no other)
            let shuffles = table("shuffles");
            let sh = shuffles[int(c.get("shuffle")) as usize].as_str().unwrap_or("");
            let config = Jv::obj(vec![
                ("cname", c.get("cname").cloned().unwrap_or(Jv::Null)),
                ("clevel", Jv::integer(int(c.get("clevel")))),
                ("shuffle", Jv::str(sh)),
                ("typesize", Jv::integer(b)),
                ("blocksize", Jv::integer(int(c.get("blocksize")))),
            ]);
            (Some(Jv::obj(vec![("name", Jv::str("blosc")), ("configuration", config)])),
             &["type", "cname", "clevel", "shuffle", "blocksize"])
        }
    };
    (codec, c.without(carried))
}

/// A dataset's zarr.json (its attributes: its source metadata, to declare), and its number of
/// dimensions.
fn dataset(doc: &Jv) -> (Vec<(Js, Jv)>, Vec<(Js, Jv)>, usize) {
    let ints = |k: &str| -> Vec<Jv> {
        doc.get(k).and_then(Jv::items).unwrap_or(&[]).iter().map(|x| Jv::integer(int(Some(x)))).collect()
    };
    let (shape, chunks) = (ints("dimensions"), ints("blockSize"));
    let n = shape.len();
    let dtype = doc.get("dataType").and_then(Jv::as_str).unwrap_or("");
    let b = table("data_types")[dtype].as_i64().unwrap_or(1);
    let c = compression(doc).unwrap_or(Jv::Null);
    let mut inner = vec![
        Jv::obj(vec![("name", Jv::str("transpose")),
                     ("configuration", Jv::obj(vec![("order", Jv::Arr((0..n as i64).rev().map(Jv::integer).collect()))]))]),
        if b > 1 {
            Jv::obj(vec![("name", Jv::str("bytes")), ("configuration", Jv::obj(vec![("endian", Jv::str("big"))]))])
        } else {
            Jv::obj(vec![("name", Jv::str("bytes"))])
        },
    ];
    let (codec, extra) = compressor(&c, b);
    inner.extend(codec);
    let mut meta = Vec::new();
    if let Some(v) = doc.get("n5") {
        meta.push((s("n5"), v.clone()));
    }
    if !extra.is_empty() {
        meta.push((s("compression"), obj(extra)));
    }
    if doc.has("compression") && let Some(v) = doc.get("compressionType") {
        meta.push((s("compressionType"), v.clone())); // not read: `compression` is
    }
    let array = vec![
        (s("zarr_format"), Jv::integer(3)),
        (s("node_type"), Jv::str("array")),
        (s("shape"), Jv::Arr(shape)),
        (s("data_type"), Jv::str(dtype)),
        (s("chunk_grid"), Jv::obj(vec![("name", Jv::str("regular")), ("configuration", Jv::obj(vec![("chunk_shape", Jv::Arr(chunks))]))])),
        (s("chunk_key_encoding"), Jv::obj(vec![("name", Jv::str("v2")), ("configuration", Jv::obj(vec![("separator", Jv::str("/"))]))])),
        (s("fill_value"), Jv::integer(0)),
        (s("codecs"), Jv::Arr(vec![Jv::obj(vec![("name", Jv::str("n5_default")), ("configuration", Jv::obj(vec![("codecs", Jv::Arr(inner))]))])])),
    ];
    (array, source_metadata(doc.without(&LAYOUT), meta), n)
}

// ---- multiscale images (spec/virtualize/n5.md §4 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/n5.md#4-multiscale-images>)

fn axis_type(a: &Js) -> Option<&'static str> {
    a.as_str().and_then(|a| table("axis_types")[a].as_str()).map(|t| match t {
        "space" => "space",
        "time" => "time",
        _ => "channel",
    })
}

fn level_path(p: &Jv) -> Option<&str> {
    p.as_str().filter(|p| !p.is_empty() && p.split('/').all(|s| !s.is_empty() && s != "." && s != ".."))
}

fn valid_axes(names: &[Js]) -> bool {
    if names.iter().any(|a| a.as_str() == Some("")) || (1..names.len()).any(|i| names[..i].contains(&names[i])) {
        return false;
    }
    let kinds: Vec<&str> = names.iter().map(|a| axis_type(a).unwrap_or("custom")).collect();
    let mut i = 0;
    if kinds.first() == Some(&"time") {
        i += 1;
    }
    if kinds.get(i).is_some_and(|k| *k == "channel" || *k == "custom") {
        i += 1;
    }
    let rest = &kinds[i..];
    (2..=3).contains(&rest.len()) && rest.iter().all(|k| *k == "space")
}

fn unit_of(u: Option<&Js>) -> Option<String> {
    let u = u?.as_str()?;
    let units = table("units");
    if units.as_object().is_some_and(|m| m.iter().any(|(k, v)| k != "about" && v.as_str() == Some(u))) {
        return Some(u.to_string());
    }
    units.get(u).filter(|_| u != "about").and_then(|v| v.as_str()).map(str::to_string)
}

/// `n` numbers (each positive, with `positive`), as binary64 values.
fn numbers(v: Option<&Jv>, n: usize, positive: bool) -> Option<Vec<f64>> {
    let a = v?.items()?;
    if a.len() != n {
        return None;
    }
    let x: Vec<f64> = a.iter().map(Jv::num).collect::<Option<_>>()?;
    (!positive || x.iter().all(|v| *v > 0.0)).then_some(x)
}

fn strings(v: Option<&Jv>, n: usize) -> Option<Vec<Js>> {
    let a = v?.items()?;
    if a.len() != n {
        return None;
    }
    a.iter().map(|x| match x {
        Jv::Str(t) => Some(t.clone()),
        _ => None,
    }).collect()
}

fn floats(v: &[f64]) -> Jv {
    Jv::Arr(v.iter().map(|x| Jv::Float(*x)).collect())
}

struct Found {
    ome: Jv,
    levels: Vec<String>,
    axes: Vec<Js>,
}

fn ome_multiscale(name: Option<Js>, axes: &[Js], units: &[Option<String>], paths: &[String], scales: &[Vec<f64>],
                  translations: &[Vec<f64>]) -> Jv {
    let mut ms = Vec::new();
    if let Some(n) = name {
        ms.push((s("name"), Jv::Str(n)));
    }
    let ax = axes.iter().zip(units).map(|(a, u)| {
        let mut m = vec![(s("name"), Jv::Str(a.clone()))];
        if let Some(t) = axis_type(a) {
            m.push((s("type"), Jv::str(t)));
        }
        if let Some(u) = u {
            m.push((s("unit"), Jv::str(u)));
        }
        obj(m)
    }).collect();
    ms.push((s("axes"), Jv::Arr(ax)));
    let ds = paths.iter().enumerate().map(|(i, p)| {
        let t = vec![
            Jv::obj(vec![("type", Jv::str("scale")), ("scale", floats(&scales[i]))]),
            Jv::obj(vec![("type", Jv::str("translation")), ("translation", floats(&translations[i]))]),
        ];
        Jv::obj(vec![("path", Jv::str(p)), ("coordinateTransformations", Jv::Arr(t))])
    }).collect();
    ms.push((s("datasets"), Jv::Arr(ds)));
    Jv::obj(vec![("version", Jv::str("0.5")), ("multiscales", Jv::Arr(vec![obj(ms)]))])
}

fn cosem(g: &str, attrs: &Jv, ndim: &BTreeMap<String, usize>, docs: &HashMap<String, Jv>) -> Option<Found> {
    let m = attrs.get("multiscales")?.items()?.first()?;
    if !m.is_obj() {
        return None;
    }
    let ds = m.get("datasets")?.items()?;
    if ds.is_empty() || !ds.iter().all(Jv::is_obj) {
        return None;
    }
    type Level = (String, Vec<Js>, Vec<f64>, Vec<f64>, Vec<Option<Js>>);
    let mut levels: Vec<Level> = Vec::new();
    for d in ds {
        let p = level_path(d.get("path")?)?;
        let full = join(g, p);
        let n = *ndim.get(&full)?;
        let t = if d.has("transform") { d.get("transform") } else { docs[&full].get("transform") }?;
        if !t.is_obj() {
            return None;
        }
        let mut axes = strings(t.get("axes"), n)?;
        let mut scale = numbers(t.get("scale"), n, true)?;
        let mut translate = if t.has("translate") { numbers(t.get("translate"), n, false)? } else { vec![0.0; n] };
        let mut units: Vec<Option<Js>> = if t.has("units") {
            strings(t.get("units"), n)?.into_iter().map(Some).collect()
        } else {
            vec![None; n]
        };
        let order = match t.get("order") {
            None => "C",
            Some(o) => o.as_str().filter(|o| *o == "C" || *o == "F")?,
        };
        if order == "C" {
            axes.reverse();
            scale.reverse();
            translate.reverse();
            units.reverse();
        }
        levels.push((p.to_string(), axes, scale, translate, units));
    }
    if levels.iter().any(|l| l.1 != levels[0].1 || l.4 != levels[0].4) {
        return None;
    }
    let axes = levels[0].1.clone();
    if !valid_axes(&axes) {
        return None;
    }
    let name = match m.get("name") {
        Some(Jv::Str(t)) => Some(t.clone()),
        _ => None,
    };
    let units: Vec<Option<String>> = levels[0].4.iter().map(|u| unit_of(u.as_ref())).collect();
    let paths: Vec<String> = levels.iter().map(|l| l.0.clone()).collect();
    let scales: Vec<Vec<f64>> = levels.iter().map(|l| l.2.clone()).collect();
    let translations: Vec<Vec<f64>> = levels.iter().map(|l| l.3.clone()).collect();
    Some(Found {
        ome: ome_multiscale(name, &axes, &units, &paths, &scales, &translations),
        levels: paths.iter().map(|p| join(g, p)).collect(),
        axes,
    })
}

fn n5_viewer(g: &str, attrs: &Jv, ndim: &BTreeMap<String, usize>, docs: &HashMap<String, Jv>) -> Result<Option<Found>, String> {
    let s0 = join(g, "s0");
    let Some(&n) = ndim.get(&s0) else { return Ok(None) };
    let mut levels = Vec::new();
    let mut factors: Vec<Vec<f64>> = Vec::new();
    if attrs.has("scales") {
        let Some(sc) = attrs.get("scales").and_then(Jv::items).filter(|a| !a.is_empty()) else { return Ok(None) };
        for f in sc {
            let Some(f) = numbers(Some(f), n, true) else { return Ok(None) };
            factors.push(f);
        }
        levels = (0..factors.len()).map(|i| join(g, &format!("s{i}"))).collect();
        if levels.iter().any(|l| ndim.get(l) != Some(&n)) {
            return Ok(None);
        }
    } else {
        if !ndim.contains_key(&join(g, "s1")) {
            return Ok(None);
        }
        while ndim.contains_key(&join(g, &format!("s{}", levels.len()))) {
            levels.push(join(g, &format!("s{}", levels.len())));
        }
        for (i, lv) in levels.iter().enumerate() {
            if ndim[lv] != n {
                return Ok(None);
            }
            match docs[lv].get("downsamplingFactors") {
                None if i == 0 => factors.push(vec![1.0; n]),
                f => match numbers(f, n, true) {
                    Some(f) => factors.push(f),
                    None => return Ok(None),
                },
            }
        }
    }
    let res = if attrs.has("pixelResolution") {
        Some(attrs)
    } else if docs[&s0].has("pixelResolution") {
        Some(&docs[&s0])
    } else {
        None
    };
    let mut unit: Option<Js> = None;
    let r = match res.and_then(|d| d.get("pixelResolution")) {
        None => vec![1.0; n],
        Some(pr) => {
            let r = if pr.is_obj() {
                if let Some(u) = pr.get("unit") {
                    match u {
                        Jv::Str(t) => unit = Some(t.clone()),
                        _ => return Ok(None),
                    }
                }
                numbers(pr.get("dimensions"), n, true)
            } else {
                numbers(Some(pr), n, true)
            };
            match r {
                Some(r) => r,
                None => return Ok(None),
            }
        }
    };
    let axes: Vec<Js> = if attrs.has("axes") {
        match strings(attrs.get("axes"), n) {
            Some(a) => a,
            None => return Ok(None),
        }
    } else if n == 2 || n == 3 {
        ["x", "y", "z"][..n].iter().map(|a| s(a)).collect()
    } else {
        return Ok(None);
    };
    if !valid_axes(&axes) {
        return Ok(None);
    }
    let u = unit_of(unit.as_ref());
    let units: Vec<Option<String>> = axes.iter().map(|a| if axis_type(a) == Some("space") { u.clone() } else { None }).collect();
    let scales: Vec<Vec<f64>> = factors.iter().map(|f| (0..n).map(|j| r[j] * f[j]).collect()).collect();
    if scales.iter().flatten().any(|v| v.is_infinite()) {
        return Err(format!("{}: a scale is not finite", if g.is_empty() { "/" } else { g }));
    }
    // a downsampled voxel's center is (f − 1) / 2 source voxels from the origin
    let translations: Vec<Vec<f64>> = factors.iter().map(|f| (0..n).map(|j| (f[j] - 1.0) / 2.0 * r[j]).collect()).collect();
    let paths: Vec<String> = levels.iter().map(|l| if g.is_empty() { l.clone() } else { l[g.len() + 1..].to_string() }).collect();
    Ok(Some(Found { ome: ome_multiscale(None, &axes, &units, &paths, &scales, &translations), levels, axes }))
}

// ---- chunks and other objects (spec/virtualize/n5.md §3.1
// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/n5.md#31-chunks>, §6
// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/n5.md#6-other-objects>)

/// The proper ancestors of `nodes` that are not nodes.
fn implicit_groups(nodes: &BTreeSet<String>) -> BTreeSet<String> {
    let mut out = BTreeSet::new();
    for p in nodes {
        let mut q = p.as_str();
        while !q.is_empty() {
            q = q.rfind('/').map_or("", |i| &q[..i]);
            if nodes.contains(q) || !out.insert(q.to_string()) {
                break;
            }
        }
    }
    out
}

/// The N5 hierarchy of a valid store IR, its facts (`nodes`, from the parser; `ignored`, the
/// recorded ignored keys, from the host), outside `refkv_source`, and its references.
pub fn project(ir: &Ir, facts: &J, url: &str) -> Result<Out, String> {
    let src = ir.sources();
    let listing: Vec<(String, u64, u32)> = src.objects.iter().map(|&o| (ir.key(o), ir.len[o as usize], o)).collect();
    let at: HashMap<&str, u32> = listing.iter().map(|(k, _, o)| (k.as_str(), *o)).collect();
    let mut kinds: BTreeMap<String, bool> = BTreeMap::new();
    let mut docs: HashMap<String, Jv> = HashMap::new();
    for n in facts["nodes"].as_array().map(Vec::as_slice).unwrap_or(&[]) {
        let path = n[0].as_str().unwrap_or("").to_string();
        let key = document_key(&path);
        let o = *at.get(key.as_str()).ok_or(format!("internal: no object {key}"))?;
        let b = ir.value_bytes(o).ok_or(format!("internal: {key} is not held"))?;
        docs.insert(path.clone(), json::parse(b).map_err(|e| format!("internal: {key}: {e}"))?.value);
        kinds.insert(path, n[1] == "array");
    }
    let nodes: BTreeSet<String> = kinds.keys().cloned().collect();
    let implicit = implicit_groups(&nodes);

    let mut arrays: BTreeMap<String, (Vec<(Js, Jv)>, Vec<(Js, Jv)>)> = BTreeMap::new();
    let mut ndim: BTreeMap<String, usize> = BTreeMap::new();
    for (path, _) in kinds.iter().filter(|k| *k.1) {
        let (a, own, n) = dataset(&docs[path]);
        arrays.insert(path.clone(), (a, own));
        ndim.insert(path.clone(), n);
    }
    let mut groups: BTreeMap<String, Vec<(Js, Jv)>> = BTreeMap::new();
    for (path, _) in kinds.iter().filter(|k| !*k.1) {
        let d = &docs[path];
        let meta = d.get("n5").map(|v| vec![(s("n5"), v.clone())]).unwrap_or_default();
        groups.insert(path.clone(), source_metadata(d.without(&["n5"]), meta));
    }

    let mut omes: HashMap<String, Jv> = HashMap::new();
    let mut named: HashMap<String, Vec<Js>> = HashMap::new();
    let mut images = Vec::new();
    for g in groups.keys() {
        let attrs = &docs[g];
        if attrs.has("ome") {
            continue;
        }
        let (found, conv) = match cosem(g, attrs, &ndim, &docs) {
            Some(f) => (f, "cosem"),
            None => match n5_viewer(g, attrs, &ndim, &docs)? {
                Some(f) => (f, "n5-viewer"),
                None => continue,
            },
        };
        // a level of an earlier image with other axis names: not recognized after all
        if found.levels.iter().any(|l| named.get(l).is_some_and(|a| *a != found.axes)) {
            continue;
        }
        omes.insert(g.clone(), found.ome);
        images.push(json!({"path": g, "convention": conv}));
        for l in found.levels {
            named.entry(l).or_insert_with(|| found.axes.clone());
        }
    }

    let mut out = Out::new(url);
    let target = |path: &str| -> Vec<(Js, Jv)> {
        omes.get(path).map(|o| vec![(s("ome"), o.clone())]).unwrap_or_default()
    };
    let root = |path: &str| (path.is_empty()).then_some(url);
    let mut written: Vec<String> = Vec::new();
    for path in &implicit {
        let doc = Jv::obj(vec![("zarr_format", Jv::integer(3)), ("node_type", Jv::str("group")),
                               ("attributes", declare(Vec::new(), root(path), Vec::new()))]);
        written.push(doc_key(path));
        out.bytes(&doc_key(path), doc.text().into_bytes());
    }
    for (path, own) in &groups {
        let doc = Jv::obj(vec![("zarr_format", Jv::integer(3)), ("node_type", Jv::str("group")),
                               ("attributes", declare(target(path), root(path), own.clone()))]);
        written.push(doc_key(path));
        out.bytes(&doc_key(path), doc.text().into_bytes());
    }
    for (path, (a, own)) in &arrays {
        let mut m = a.clone();
        if let Some(axes) = named.get(path) {
            m.push((s("dimension_names"), Jv::Arr(axes.iter().map(|t| Jv::Str(t.clone())).collect())));
        }
        m.push((s("attributes"), declare(target(path), root(path), own.clone())));
        written.push(doc_key(path));
        out.bytes(&doc_key(path), obj(m).text().into_bytes());
    }

    // the chunk objects: under a dataset (its nearest ancestor node of a dataset), at a key of its grid
    let grids: HashMap<&str, Vec<u64>> = arrays.iter().map(|(p, (a, _))| {
        let get = |k: &str| a.iter().find(|m| m.0.as_str() == Some(k)).map(|m| &m.1);
        let dims: Vec<u64> = get("shape").and_then(Jv::items).unwrap_or(&[]).iter().map(|x| int(Some(x)) as u64).collect();
        let cfg = get("chunk_grid").and_then(|c| c.get("configuration")).and_then(|c| c.get("chunk_shape"));
        let block: Vec<u64> = cfg.and_then(Jv::items).unwrap_or(&[]).iter().map(|x| int(Some(x)) as u64).collect();
        (p.as_str(), dims.iter().zip(&block).map(|(d, b)| d.div_ceil((*b).max(1))).collect())
    }).collect();
    // Datasets do not nest (a candidate inside one is never read, so never a node: n5.rs), so a
    // key has at most one dataset among its proper ancestors, and is a chunk key when that
    // dataset's rank is the number of segments after it. So only the prefix that leaves `n`
    // segments, for each rank `n` of a dataset, can be it: at most 32 lookups of borrowed
    // slices, not one allocated prefix per segment of a deep key.
    let mut ranks: Vec<usize> = grids.values().map(Vec::len).filter(|&n| n > 0).collect();
    ranks.sort_unstable();
    ranks.dedup();
    let chunk_of = |key: &str| -> bool {
        let segments = key.bytes().filter(|&b| b == b'/').count() + 1;
        for &n in &ranks {
            let (prefix, rest) = if segments == n {
                ("", key)
            } else if segments > n {
                let (i, _) = key.rmatch_indices('/').nth(n - 1).expect("more than n segments");
                (&key[..i], &key[i + 1..])
            } else {
                continue;
            };
            if let Some(g) = grids.get(prefix).filter(|g| g.len() == n) {
                return rest.split('/').zip(g).all(|(s, n)| canonical_index(s, *n));
            }
        }
        false
    };
    let mut used: BTreeSet<&str> = BTreeSet::new();
    let documents: Vec<String> = nodes.iter().map(|p| document_key(p)).collect();
    used.extend(documents.iter().map(String::as_str));
    let (mut chunks, mut empty_chunks) = (0u64, 0u64);
    let mut refs: Vec<(String, String, u64)> = Vec::new();
    if !grids.is_empty() {
        for (k, n, _) in &listing {
            if chunk_of(k) {
                if *n > 0 {
                    chunks += 1;
                    used.insert(k);
                    refs.push((k.clone(), k.clone(), *n));
                } else {
                    empty_chunks += 1;
                }
            }
        }
    }
    let root_array = kinds.get("").copied().unwrap_or(false);
    if !root_array {
        // the group refkv_source is the mirror's: no node may be there
        if let Some(p) = nodes.iter().chain(&implicit).find(|p| *p == SOURCE_NODE || p.starts_with(&format!("{SOURCE_NODE}/"))) {
            return Err(format!("the node {p} is where the store's other objects go ({OBJECTS}...)"));
        }
    }
    let mut others = 0u64;
    for (k, n, _) in &listing {
        if *n > 0 && !used.contains(k.as_str()) {
            others += 1;
            refs.push((object_key(k), k.clone(), *n));
        }
    }
    for k in written.iter().chain(refs.iter().map(|r| &r.0)) {
        check_key(k)?;
    }
    for (entry, k, n) in refs {
        out.refs(&entry, vec![Part::Obj(k, 0, n)]);
    }
    out.summary = json!({
        "groups": groups.len() + implicit.len(), "arrays": arrays.len(), "chunks": chunks, "emptyChunks": empty_chunks,
        "objects": listing.len(), "otherObjects": others, "images": images,
    });
    Ok(out)
}
