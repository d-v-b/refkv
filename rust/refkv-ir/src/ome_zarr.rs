//! The OME layer of the Zarr v2 store format (spec/virtualize/ome-zarr.md §3
//! <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/ome-zarr.md#3-ome-groups>–§8
//! <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/ome-zarr.md#8-source-metadata>): whether
//! a Zarr v2 store's root declares OME-NGFF 0.4 (spec/virtualize.md §1.4
//! <https://github.com/d-v-b/refkv/blob/main/spec/virtualize.md#14-store-inputs>), then, on the
//! hierarchy `zarr2.rs` read, the OME groups, the checks of §4
//! <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/ome-zarr.md#4-validation> (each a role of
//! `schema/ome-zarr.json`), and what the projection writes in place of the Zarr v2 convention's:
//! `ome` 0.5 for each OME group, the attributes the inverse of §5
//! <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/ome-zarr.md#5-group-attributes> does not
//! give back and the unversioned members, `dimension_names` for each level, and the OME-XML
//! referenced at its own key (§7
//! <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/ome-zarr.md#7-chunks-and-ome-xml>).

use crate::expr::V;
use crate::ir::Res;
use crate::json::Jv;
use crate::project::store::canonical_index;
use crate::rules::{self, Rules, Vars};
use serde_json::{json, Value as J};
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::sync::OnceLock;

pub static OME_ZARR_SCHEMA: &str = include_str!("../schema/ome-zarr.json");

pub fn schema() -> &'static Rules {
    static S: OnceLock<Rules> = OnceLock::new();
    S.get_or_init(|| Rules::new(OME_ZARR_SCHEMA).expect("the OME-Zarr schema is valid"))
}

fn check(role: &str, env: Vars<'static>) -> Res<()> {
    let mut env = env;
    schema().check(role, &mut env)
}

fn join(parent: &str, name: &str) -> String {
    if parent.is_empty() { name.to_string() } else { format!("{parent}/{name}") }
}

fn parent_of(path: &str) -> &str {
    path.rfind('/').map_or("", |i| &path[..i])
}

/// Paths in the order of their UTF-8 bytes (Python's order of `str`).
fn sorted<'a, I: IntoIterator<Item = &'a String>>(it: I) -> Vec<String> {
    let mut v: Vec<String> = it.into_iter().cloned().collect();
    v.sort_by(|a, b| a.as_bytes().cmp(b.as_bytes()));
    v
}

/// What this layer reads of a JSON value, as the frozen profile's Python reads it.
trait Read {
    /// The text of a string with no unpaired surrogate (a string with one names no key and
    /// equals no constant).
    fn s(&self) -> Option<&str>;
    fn as_arr(&self) -> Option<&Vec<Jv>>;
    fn is_str(&self) -> bool;
    fn minus(&self, drop: &[&str]) -> Jv;
    fn int_of(&self) -> Option<i64>;
    fn as_int(&self, lo: i64, hi: i64) -> Option<i64>;
    fn is_arr_of_obj(&self) -> bool;
    fn show(&self, n: usize) -> String;
}

const MAX_SAFE: i64 = 9007199254740991;

impl Read for Jv {
    fn s(&self) -> Option<&str> {
        match self {
            Jv::Str(s) if s.lone.is_empty() => Some(&s.text),
            _ => None,
        }
    }
    fn as_arr(&self) -> Option<&Vec<Jv>> {
        match self {
            Jv::Arr(a) => Some(a),
            _ => None,
        }
    }
    fn is_str(&self) -> bool {
        matches!(self, Jv::Str(_))
    }
    fn minus(&self, drop: &[&str]) -> Jv {
        match self {
            Jv::Obj(m) => Jv::Obj(m.iter().filter(|(k, _)| !(k.lone.is_empty() && drop.contains(&k.text.as_str()))).cloned().collect()),
            other => other.clone(),
        }
    }
    fn int_of(&self) -> Option<i64> {
        self.int(-MAX_SAFE, MAX_SAFE)
    }
    fn as_int(&self, lo: i64, hi: i64) -> Option<i64> {
        self.int(lo, hi)
    }
    fn is_arr_of_obj(&self) -> bool {
        self.as_arr().is_some_and(|a| a.iter().all(|x| x.is_obj()))
    }
    fn show(&self, n: usize) -> String {
        match self {
            Jv::Str(s) => s.text.chars().take(n).collect(),
            Jv::Null => "None".into(),
            Jv::Bool(true) => "True".into(),
            Jv::Bool(false) => "False".into(),
            other => other.short(n),
        }
    }
}

/// A value a check of `schema/ome-zarr.json` made sure of just before: its absence is the
/// schema's and the code's disagreeing, a rejection rather than a panic.
fn got<T>(v: Option<T>) -> Res<T> {
    v.ok_or_else(|| "internal: a value the schema checked is not there".to_string())
}

/// An array of the hierarchy, as the checks read it.
pub struct ArrayInfo {
    pub dims: usize,
    pub data_type: String,
}

const OME_KEYS: [&str; 7] = ["multiscales", "omero", "labels", "image-label", "plate", "well", "bioformats2raw.layout"];
const VERSIONED: [&str; 4] = ["omero", "image-label", "plate", "well"];

fn flag(b: bool) -> V {
    rules::flag(b)
}
fn text(s: &str) -> V {
    rules::text(s)
}
fn num(x: f64) -> V {
    rules::num(x)
}
/// A JSON value as a schema value (strings with a lone surrogate as their JSON text, so that
/// two such strings stay distinct).
fn val(v: Option<&Jv>) -> V {
    match v {
        None | Some(Jv::Null) => V::Absent,
        Some(Jv::Bool(b)) => V::Bool(*b),
        Some(x @ (Jv::Int(_) | Jv::Float(_))) => V::Num(x.num().unwrap_or(f64::NAN)),
        Some(Jv::Str(s)) if s.lone.is_empty() => V::Str(s.text.clone()),
        Some(Jv::Str(s)) => V::Str(format!("\u{0}{}", s.raw)),
        Some(Jv::Arr(a)) => V::List(a.iter().map(|x| val(Some(x))).collect()),
        Some(x @ Jv::Obj(_)) => V::Str(x.text()),
    }
}
fn shown(v: Option<&Jv>) -> V {
    V::Str(v.map_or("None".into(), |x| x.show(40)))
}
fn place(p: &str) -> String {
    if p.is_empty() { "/".into() } else { p.to_string() }
}

/// A relative path (§3 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/ome-zarr.md#3-ome-groups>): one or more segments separated by `/`, none empty, `.` or `..`.
pub fn rel_path(p: Option<&Jv>) -> bool {
    p.and_then(|v| v.s()).is_some_and(|s| !s.is_empty() && s.split('/').all(|x| !x.is_empty() && x != "." && x != ".."))
}
fn alnum(v: Option<&Jv>) -> bool {
    v.and_then(|v| v.s()).is_some_and(|s| !s.is_empty() && s.bytes().all(|c| c.is_ascii_alphanumeric()))
}
fn hex6(v: Option<&Jv>) -> bool {
    v.and_then(|v| v.s()).is_some_and(|s| s.len() == 6 && s.bytes().all(|c| c.is_ascii_hexdigit()))
}
fn is_int(v: Option<&Jv>) -> bool {
    v.is_some_and(|v| v.int_of().is_some())
}

/// `rel` resolved against the group path `base` (L6), or None above the root.
pub fn resolve(base: &str, rel: &str) -> Option<String> {
    let mut segs: Vec<&str> = if base.is_empty() { Vec::new() } else { base.split('/').collect() };
    for s in rel.split('/') {
        match s {
            "" | "." => {}
            ".." => {
                segs.pop()?;
            }
            _ => segs.push(s),
        }
    }
    Some(segs.join("/"))
}

fn multiscales_04(a: &Jv) -> bool {
    a.get("multiscales").and_then(|m| m.as_arr()).is_some_and(|ms| ms.iter().any(|m| m.get("version").and_then(|v| v.s()) == Some("0.4")))
}
fn version_04(a: &Jv, k: &str) -> bool {
    a.get(k).is_some_and(|v| v.is_obj() && v.get("version").and_then(|v| v.s()) == Some("0.4"))
}

/// Where the declaration of OME-NGFF 0.4 stands: decided, or a document to read first.
pub enum Detect {
    Yes,
    No,
    Need(String),
}

/// Does the root of a Zarr v2 store declare OME-NGFF 0.4 (spec/virtualize.md §1.4
/// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize.md#14-store-inputs>)? `doc(key)` is
/// a document's value (Err: the rejection), or None while it is not read.
pub fn declares_04(listed: &dyn Fn(&str) -> bool, doc: &mut dyn FnMut(&str) -> Option<Res<Jv>>) -> Res<Detect> {
    if listed(".zarray") {
        return Ok(Detect::No);
    }
    // a group's attributes, when it has .zgroup and .zattrs and they are an object
    let mut attrs = |path: &str| -> Res<Result<Option<Jv>, String>> {
        if !listed(&join(path, ".zgroup")) || !listed(&join(path, ".zattrs")) {
            return Ok(Ok(None));
        }
        let k = join(path, ".zattrs");
        match doc(&k) {
            None => Ok(Err(k)),
            Some(r) => Ok(Ok(Some(r?).filter(|a| a.is_obj()))),
        }
    };
    macro_rules! got {
        ($e:expr) => {
            match $e? {
                Ok(v) => v,
                Err(k) => return Ok(Detect::Need(k)),
            }
        };
    }
    let Some(a) = got!(attrs("")) else { return Ok(Detect::No) };
    if multiscales_04(&a) || version_04(&a, "plate") || version_04(&a, "well") {
        return Ok(Detect::Yes);
    }
    if !a.has("bioformats2raw.layout") {
        return Ok(Detect::No);
    }
    let mut q = None;
    if let Some(plate) = a.get("plate") {
        let w0 = plate.get("wells").and_then(|w| w.as_arr()).and_then(|w| w.first()).filter(|w| w.is_obj());
        if let Some(w0) = w0.filter(|w| rel_path(w.get("path"))) {
            let w = w0.get("path").and_then(|p| p.s()).unwrap().to_string();
            let wa = got!(attrs(&w));
            let well = wa.as_ref().and_then(|wa| wa.get("well")).filter(|x| x.is_obj());
            let i0 = well.and_then(|x| x.get("images")).and_then(|i| i.as_arr()).and_then(|i| i.first()).filter(|i| i.is_obj());
            if let Some(i0) = i0.filter(|i| rel_path(i.get("path"))) {
                q = Some(join(&w, i0.get("path").and_then(|p| p.s()).unwrap()));
            }
        }
    } else {
        q = Some("0".to_string());
        let oa = got!(attrs("OME"));
        let s0 = oa.as_ref().and_then(|o| o.get("series")).and_then(|s| s.as_arr()).and_then(|s| s.first()).cloned();
        if let Some(s0) = s0.filter(|s| rel_path(Some(s))) {
            q = Some(s0.s().unwrap().to_string());
        }
    }
    let Some(q) = q else { return Ok(Detect::No) };
    let qa = got!(attrs(&q));
    Ok(if qa.is_some_and(|qa| multiscales_04(&qa)) { Detect::Yes } else { Detect::No })
}

/// The scale of a valid list of coordinate transformations (I5).
fn transforms(where_: &str, ts: Option<&Jv>, n: usize) -> Res<Vec<f64>> {
    let list = ts.and_then(|t| t.as_arr());
    let objects = list.is_some_and(|l| l.iter().all(|t| t.is_obj()));
    let count = list.map_or(0, |l| l.len());
    let first = list.and_then(|l| l.first());
    let second = list.and_then(|l| l.get(1));
    let nums = |t: Option<&Jv>, k: &str| -> (bool, usize) {
        let v = t.and_then(|t| t.get(k)).and_then(|v| v.as_arr());
        (v.is_some_and(|v| v.iter().all(|x| x.is_number())), v.map_or(0, |v| v.len()))
    };
    let (sn, sl) = nums(first, "scale");
    let (tn, tl) = nums(second, "translation");
    let ty = |t: Option<&Jv>| t.and_then(|t| t.get("type")).and_then(|t| t.s()).map_or(V::Absent, text);
    check("transforms", rules::env([
        ("where", text(where_)), ("objects", flag(list.is_some() && objects)), ("count", num(count as f64)),
        ("first", ty(first)), ("scale_numbers", flag(sn)), ("scale_len", num(sl as f64)), ("second", ty(second)),
        ("translation_numbers", flag(tn)), ("translation_len", num(tl as f64)), ("n", num(n as f64)),
    ]))?;
    got(first.and_then(|f| f.get("scale")).and_then(|s| s.as_arr()))?.iter().map(|x| got(x.num())).collect()
}

fn version(where_: &str, obj: &Jv) -> Res<()> {
    let v = obj.get("version");
    check("version", rules::env([
        ("where", text(where_)), ("present", flag(v.is_some())), ("is_04", flag(v.and_then(|v| v.s()) == Some("0.4"))),
        ("version", shown(v)),
    ]))
}

pub fn layer(attrs: &BTreeMap<String, Jv>, arrays: &BTreeMap<String, ArrayInfo>, keys: &[(String, u64)]) -> Res<Ome> {
    for (path, a) in attrs {
        check("group", rules::env([("where", text(&place(path))), ("has_ome", flag(a.has("ome")))]))?;
    }
    let collections: Vec<String> = sorted(attrs.iter().filter(|(_, a)| a.has("bioformats2raw.layout")).map(|(p, _)| p));
    let series_groups: HashSet<String> = collections.iter().map(|c| join(c, "OME"))
        .filter(|o| attrs.get(o).is_some_and(|a| a.has("series"))).collect();
    let ome_groups: Vec<String> = sorted(attrs.iter()
        .filter(|(p, a)| series_groups.contains(*p) || OME_KEYS.iter().any(|k| a.has(k))).map(|(p, _)| p));
    let is_ome: HashSet<&str> = ome_groups.iter().map(|g| g.as_str()).collect();
    let images: BTreeSet<String> = attrs.iter().filter(|(_, a)| a.has("multiscales")).map(|(p, _)| p.clone()).collect();

    // Images (I1–I7)
    let mut levels: Vec<(String, usize, usize, String, Vec<V>)> = Vec::new();
    let mut multiscales: HashMap<String, &Vec<Jv>> = HashMap::new();
    for g in sorted(&images) {
        let ms = attrs[&g].get("multiscales").unwrap();
        let w = format!("{}: multiscales", place(&g));
        let ok = ms.as_arr().is_some_and(|m| !m.is_empty() && m.iter().all(|x| x.is_obj()));
        check("multiscales", rules::env([("where", text(&w)), ("ok", flag(ok))]))?;
        let ms = got(ms.as_arr())?;
        multiscales.insert(g.clone(), ms);
        for (i, m) in ms.iter().enumerate() {
            let w = format!("{w}[{i}]");
            version(&w, m)?;
            let axes = m.get("axes").and_then(|a| a.as_arr());
            let empty = Vec::new();
            let ax = axes.unwrap_or(&empty);
            let objects = axes.is_some() && ax.iter().all(|a| a.is_obj());
            let names: Vec<V> = ax.iter().map(|a| val(a.get("name"))).collect();
            let kinds: String = ax.iter().map(|a| match a.get("type").and_then(|t| t.s()) {
                Some("space") => 's',
                Some("time") => 't',
                _ => 'o',
            }).collect();
            check("axes", rules::env([
                ("where", text(&w)), ("objects", flag(objects)), ("count", num(ax.len() as f64)),
                ("names_text", flag(ax.iter().all(|a| a.get("name").is_some_and(|n| n.is_str())))),
                ("names", V::List(names.clone())),
                ("types_text", flag(ax.iter().all(|a| matches!(a.get("type"), None | Some(Jv::Null) | Some(Jv::Str(_)))))),
                ("bad_type", shown(ax.iter().filter_map(|a| a.get("type")).find(|t| !matches!(t, Jv::Null | Jv::Str(_))))),
                ("types", V::List(ax.iter().map(|a| a.get("type").map_or(V::Str("None".into()), |t| V::Str(t.show(40)))).collect())),
                ("kinds", text(&kinds)),
            ]))?;
            let n = ax.len();
            let datasets = m.get("datasets");
            let ok = datasets.and_then(|d| d.as_arr()).is_some_and(|d| !d.is_empty() && d.iter().all(|x| x.is_obj()));
            check("datasets", rules::env([("where", text(&w)), ("ok", flag(ok))]))?;
            let mut prev: Option<Vec<f64>> = None;
            for (j, d) in got(datasets.and_then(|d| d.as_arr()))?.iter().enumerate() {
                let wd = format!("{w}.datasets[{j}]");
                let p = d.get("path");
                let rel = rel_path(p);
                let target = if rel { join(&g, got(p.and_then(|p| p.s()))?) } else { String::new() };
                let a = arrays.get(&target);
                check("dataset", rules::env([
                    ("where", text(&wd)), ("relative", flag(rel)), ("path", shown(p)), ("target", text(&target)),
                    ("is_array", flag(a.is_some())), ("dims", num(a.map_or(0, |a| a.dims) as f64)), ("n", num(n as f64)),
                ]))?;
                let scale = transforms(&wd, d.get("coordinateTransformations"), n)?;
                if let Some(pr) = &prev {
                    let smaller = pr.iter().zip(&scale).any(|(a, b)| b < a);
                    let list = |v: &[f64]| V::List(v.iter().map(|x| num(*x)).collect());
                    check("level_order", rules::env([("where", text(&wd)), ("smaller", flag(smaller)), ("scale", list(&scale)), ("previous", list(pr))]))?;
                }
                prev = Some(scale);
                levels.push((g.clone(), i, j, target, names.clone()));
            }
            if m.has("coordinateTransformations") {
                transforms(&w, m.get("coordinateTransformations"), n)?;
            }
        }
    }

    // omero (O1)
    for g in &ome_groups {
        let Some(o) = attrs[g].get("omero") else { continue };
        let w = format!("{}: omero", place(g));
        let channels = o.get("channels").filter(|c| c.as_arr().is_some_and(|c| c.iter().all(|x| x.is_obj())));
        check("omero", rules::env([("where", text(&w)), ("channels", flag(channels.is_some()))]))?;
        for c in got(channels.and_then(|c| c.as_arr()))? {
            let win = c.get("window");
            let window = win.is_some_and(|x| x.is_obj() && ["min", "max", "start", "end"].iter().all(|k| x.get(k).is_some_and(|v| v.is_number())));
            check("channel", rules::env([("where", text(&w)), ("color", shown(c.get("color"))), ("hex", flag(hex6(c.get("color")))), ("window", flag(window))]))?;
        }
    }

    // Labels (L1–L8)
    let mut label_images: BTreeSet<String> = BTreeSet::new();
    let mut kept: HashMap<(String, usize), usize> = HashMap::new();
    for g in &ome_groups {
        let a = &attrs[g];
        if let Some(ls) = a.get("labels") {
            check("labels", rules::env([("where", text(&place(g))), ("list", flag(ls.as_arr().is_some()))]))?;
            for x in got(ls.as_arr())? {
                let image = rel_path(Some(x)) && x.s().is_some_and(|x| images.contains(&join(g, x)));
                check("label", rules::env([("where", text(&place(g))), ("label", shown(Some(x))), ("image", flag(image))]))?;
                label_images.insert(join(g, got(x.s())?));
            }
        }
        if let Some(il) = a.get("image-label") {
            let w = format!("{}: image-label", place(g));
            check("image_label", rules::env([("where", text(&w)), ("object", flag(il.is_obj())), ("image", flag(true))]))?;
            version(&w, il)?;
            check("image_label", rules::env([("where", text(&w)), ("object", flag(true)), ("image", flag(images.contains(g)))]))?;
            label_images.insert(g.clone());
            for key in ["colors", "properties"] {
                let Some(items) = il.get(key) else { continue };
                let ok = items.as_arr().is_some_and(|i| i.iter().all(|c| c.is_obj() && is_int(c.get("label-value"))));
                check("label_values", rules::env([("where", text(&w)), ("key", text(key)), ("ok", flag(ok))]))?;
            }
            if let Some(colors) = il.get("colors").and_then(|c| c.as_arr()) {
                let values = V::List(colors.iter().map(|c| got(c.get("label-value").and_then(|v| v.num())).map(num)).collect::<Res<_>>()?);
                check("label_colors", rules::env([("where", text(&w)), ("values", values)]))?;
                for c in colors {
                    if let Some(rgba) = c.get("rgba") {
                        let ok = rgba.as_arr().is_some_and(|r| r.len() == 4 && r.iter().all(|x| x.as_int(0, 255).is_some()));
                        check("rgba", rules::env([("where", text(&w)), ("ok", flag(ok)), ("rgba", shown(Some(rgba)))]))?;
                    }
                }
            }
            if let Some(src) = il.get("source") {
                let ok = src.is_obj() && src.get("image").is_none_or(|i| i.is_str());
                check("label_source", rules::env([("where", text(&w)), ("ok", flag(ok))]))?;
            }
        }
    }
    for x in &label_images {
        let il = attrs[x].get("image-label").filter(|i| i.is_obj());
        let rel = il.and_then(|i| i.get("source")).and_then(|s| s.get("image"));
        let rel_text = rel.map(|r| r.show(1 << 20));
        // a source.image with a lone surrogate names no image: no key holds one
        let source = match rel {
            Some(r) => r.s().and_then(|r| resolve(x, r)),
            None => resolve(x, "../../"),
        };
        let source_is_image = source.as_ref().is_some_and(|s| images.contains(s));
        let n = match source.as_ref().filter(|_| source_is_image) {
            Some(src) => got(multiscales[src][0].get("datasets").and_then(|d| d.as_arr()))?.len(),
            None => 0,
        };
        for (i, m) in multiscales[x].iter().enumerate() {
            let have = got(m.get("datasets").and_then(|d| d.as_arr()))?.len();
            check("label_image", rules::env([
                ("where", text(x)), ("given_source", flag(rel.is_some())), ("source_is_image", flag(source_is_image)),
                ("image", text(&rel_text.clone().unwrap_or_else(|| place(source.as_deref().unwrap_or(""))))),
                ("levels", num(have as f64)), ("image_levels", num(n as f64)),
            ]))?;
            if source_is_image && have > n {
                kept.insert((x.clone(), i), n);
            }
        }
        for (i, m) in multiscales[x].iter().enumerate() {
            let ds = got(m.get("datasets").and_then(|d| d.as_arr()))?;
            for d in &ds[..*kept.get(&(x.clone(), i)).unwrap_or(&ds.len())] {
                let level = join(x, got(d.get("path").and_then(|p| p.s()))?);
                check("label_level", rules::env([("level", text(&level)), ("data_type", text(&got(arrays.get(&level))?.data_type))]))?;
            }
        }
    }

    // Plates and wells (P1–P6)
    let wells: BTreeSet<String> = attrs.iter().filter(|(_, a)| a.has("well")).map(|(p, _)| p.clone()).collect();
    let mut well_images: HashMap<String, &Vec<Jv>> = HashMap::new();
    for g in &wells {
        let wl = attrs[g].get("well").unwrap();
        let w = format!("{}: well", place(g));
        check("well", rules::env([("where", text(&w)), ("object", flag(wl.is_obj())), ("images", flag(true))]))?;
        version(&w, wl)?;
        let ims = wl.get("images").filter(|i| i.is_arr_of_obj());
        check("well", rules::env([("where", text(&w)), ("object", flag(true)), ("images", flag(ims.is_some()))]))?;
        let ims = got(ims.and_then(|i| i.as_arr()))?;
        for i in ims {
            let p = i.get("path");
            let target = p.and_then(|p| p.s()).map(|p| join(g, p)).unwrap_or_default();
            let ok = alnum(p);
            check("field", rules::env([
                ("where", text(&w)), ("path", shown(p)), ("alnum", flag(ok)), ("target", text(&target)),
                ("image", flag(!ok || images.contains(&target))), ("acquisition", flag(i.get("acquisition").is_none_or(|a| is_int(Some(a))))),
            ]))?;
        }
        check("well_paths", rules::env([("where", text(&w)), ("paths", V::List(ims.iter().map(|i| val(i.get("path"))).collect()))]))?;
        well_images.insert(g.clone(), ims);
    }
    let plates: Vec<String> = sorted(attrs.iter().filter(|(_, a)| a.has("plate")).map(|(p, _)| p));
    for g in &plates {
        let pl = attrs[g].get("plate").unwrap();
        let w = format!("{}: plate", place(g));
        check("plate", rules::env([("where", text(&w)), ("object", flag(pl.is_obj()))]))?;
        version(&w, pl)?;
        let mut named: HashMap<&str, Vec<String>> = HashMap::new();
        for key in ["columns", "rows"] {
            let items = pl.get(key).filter(|i| i.is_arr_of_obj());
            let empty = Vec::new();
            let list = items.and_then(|i| i.as_arr()).unwrap_or(&empty);
            let ok = list.iter().all(|c| alnum(c.get("name")));
            check("plate_axis", rules::env([
                ("where", text(&w)), ("key", text(&key[..key.len() - 1])), ("objects", flag(items.is_some())), ("alnum", flag(ok)),
                ("names", V::List(list.iter().map(|c| val(c.get("name"))).collect())),
            ]))?;
            named.insert(key, list.iter().map(|c| got(c.get("name").and_then(|n| n.s())).map(String::from)).collect::<Res<_>>()?);
        }
        check("plate_members", rules::env([
            ("where", text(&w)), ("field_count", flag(pl.get("field_count").is_none_or(|f| f.as_int(1, i64::MAX).is_some()))),
                ("count", shown(pl.get("field_count"))),
            ("name", flag(pl.get("name").is_none_or(|n| n.is_str()))),
        ]))?;
        let mut ids: Option<HashSet<i64>> = None;
        if let Some(acq) = pl.get("acquisitions") {
            check("acquisitions", rules::env([("where", text(&w)), ("objects", flag(acq.is_arr_of_obj()))]))?;
            for a in got(acq.as_arr())? {
                check("acquisition", rules::env([
                    ("where", text(&w)), ("id", flag(a.get("id").is_some_and(|i| i.as_int(0, i64::MAX).is_some()))), ("value", shown(a.get("id"))),
                    ("maximumfieldcount", flag(a.get("maximumfieldcount").is_none_or(|m| m.as_int(1, i64::MAX).is_some()))),
                    ("texts", flag(["name", "description"].iter().all(|k| a.get(k).is_none_or(|v| v.is_str())))),
                    ("times", flag(["starttime", "endtime"].iter().all(|k| a.get(k).is_none_or(|v| is_int(Some(v)))))),
                ]))?;
            }
            let v: Vec<i64> = got(acq.as_arr())?.iter().map(|a| got(a.get("id").and_then(|i| i.int_of()))).collect::<Res<_>>()?;
            check("acquisition_ids", rules::env([("where", text(&w)), ("ids", V::List(v.iter().map(|x| num(*x as f64)).collect()))]))?;
            ids = Some(v.into_iter().collect());
        }
        let ws = pl.get("wells").filter(|x| x.is_arr_of_obj());
        let mut checked: HashSet<String> = HashSet::new();
        check("plate_wells", rules::env([("where", text(&w)), ("objects", flag(ws.is_some()))]))?;
        for x in got(ws.and_then(|w| w.as_arr()))? {
            let r = x.get("rowIndex").and_then(|v| v.as_int(0, i64::MAX)).map(|v| v as usize);
            let c = x.get("columnIndex").and_then(|v| v.as_int(0, i64::MAX)).map(|v| v as usize);
            let indexes = r.is_some_and(|r| r < named["rows"].len()) && c.is_some_and(|c| c < named["columns"].len());
            let (row, column) = match (r, c) {
                (Some(r), Some(c)) if indexes => (named["rows"][r].clone(), named["columns"][c].clone()),
                _ => Default::default(),
            };
            let expected = format!("{row}/{column}");
            let p = x.get("path");
            let is_named = indexes && p.and_then(|p| p.s()) == Some(expected.as_str());
            let target = if is_named { join(g, &expected) } else { String::new() };
            check("plate_well", rules::env([
                ("where", text(&w)), ("indexes", flag(indexes)), ("named", flag(is_named)), ("path", shown(p)),
                ("expected", text(&format!("row {row}/column {column}"))), ("target", text(&target)), ("well", flag(wells.contains(&target))),
            ]))?;
            // a well listed again checks the same fields against the same acquisitions
            if let Some(ids) = ids.as_ref().filter(|_| checked.insert(target.clone())) {
                for i in well_images[&target].iter() {
                    let given = i.get("acquisition");
                    check("field_acquisition", rules::env([
                        ("where", text(&target)), ("given_acquisition", flag(given.is_some())), ("acquisition", shown(given)),
                        ("known", flag(given.and_then(|a| a.int_of()).is_some_and(|a| ids.contains(&a)))), ("several", flag(ids.len() > 1)),
                    ]))?;
                }
            }
        }
    }

    // Collections (B1–B3)
    for g in &collections {
        let a = &attrs[g];
        let layout = a.get("bioformats2raw.layout").and_then(|v| v.int_of()).map_or(V::Absent, |v| num(v as f64));
        let ome = join(g, "OME");
        let mut series = true;
        let mut numbered = true;
        if series_groups.contains(&ome) {
            let s = attrs[&ome].get("series").unwrap();
            series = s.as_arr().is_some_and(|s| s.iter().all(|x| rel_path(Some(x)) && images.contains(&join(g, x.s().unwrap()))));
        } else if !a.has("plate") {
            let mut ns: Vec<u64> = images.iter().filter(|p| parent_of(p) == g && *p != g)
                .filter_map(|p| {
                    let last = p.rsplit('/').next().unwrap();
                    canonical_index(last, 1 << 53).then(|| last.parse().unwrap())
                }).collect();
            ns.sort();
            numbered = !ns.is_empty() && ns.iter().enumerate().all(|(i, &n)| i as u64 == n);
        }
        // B1 first, as the profile checks it
        check("collection", rules::env([("where", text(&place(g))), ("ome", text(&ome)), ("layout", layout.clone()), ("series", flag(true)), ("numbered", flag(true))]))?;
        check("collection", rules::env([("where", text(&place(g))), ("ome", text(&ome)), ("layout", layout), ("series", flag(series)), ("numbered", flag(numbered))]))?;
    }

    // I8, §6 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/ome-zarr.md#6-arrays>: the axis names of the levels the output keeps
    let mut names: HashMap<String, Vec<V>> = HashMap::new();
    // each level's first multiscale (image, index), whose axis names the output writes
    let mut first: HashMap<String, (String, usize)> = HashMap::new();
    for (g, i, j, target, axis_names) in &levels {
        if kept.get(&(g.clone(), *i)).is_some_and(|&k| *j >= k) {
            continue;
        }
        first.entry(target.clone()).or_insert_with(|| (g.clone(), *i));
        let prev = names.entry(target.clone()).or_insert_with(|| axis_names.clone());
        check("level_axes", rules::env([
            ("target", text(target)), ("same", flag(prev == axis_names)), ("names", V::List(prev.clone())), ("other", V::List(axis_names.clone())),
        ]))?;
    }
    let dropped: usize = kept.iter().map(|((g, i), n)| multiscales[g][*i].get("datasets").unwrap().as_arr().unwrap().len() - n).sum();

    // the output: §5 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/ome-zarr.md#5-group-attributes>, §6 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/ome-zarr.md#6-arrays>, §8 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/ome-zarr.md#8-source-metadata>
    let mut groups: BTreeMap<String, Jv> = BTreeMap::new();
    let mut omes: HashMap<String, Jv> = HashMap::new();
    let mut unversioned: HashMap<String, Vec<&str>> = HashMap::new();
    for (path, a) in attrs {
        if !is_ome.contains(path.as_str()) {
            groups.insert(path.clone(), a.clone());
            continue;
        }
        let mut keys: Vec<&str> = OME_KEYS.to_vec();
        if series_groups.contains(path) {
            keys.push("series");
        }
        let mut ome = Jv::obj(vec![]);
        ome.set("version", Jv::str("0.5"));
        let mut restored: HashSet<&str> = HashSet::new();
        let mut bare = Vec::new();
        for k in keys {
            let Some(v) = a.get(k) else { continue };
            let v = if k == "multiscales" {
                let ms = v.as_arr().unwrap();
                if !(0..ms.len()).any(|i| kept.contains_key(&(path.clone(), i))) {
                    if ms.iter().all(|m| m.get("version").and_then(|v| v.s()) == Some("0.4")) {
                        restored.insert(k);
                    } else if ms.iter().all(|m| !m.has("version")) {
                        restored.insert(k);
                        bare.push(k);
                    }
                }
                Jv::Arr(ms.iter().enumerate().map(|(i, m)| {
                    let mut out = m.minus(&["version"]);
                    if let Some(&n) = kept.get(&(path.clone(), i)) {
                        let ds = m.get("datasets").unwrap().as_arr().unwrap()[..n].to_vec();
                        out.set("datasets", Jv::Arr(ds));
                    }
                    out
                }).collect())
            } else if VERSIONED.contains(&k) {
                match v.get("version") {
                    Some(x) if x.s() == Some("0.4") => {
                        restored.insert(k);
                    }
                    None => {
                        restored.insert(k);
                        bare.push(k);
                    }
                    _ => {}
                }
                v.minus(&["version"])
            } else {
                restored.insert(k);
                v.clone()
            };
            ome.set(k, v);
        }
        let restored: Vec<&str> = restored.into_iter().collect();
        groups.insert(path.clone(), a.minus(&restored));
        omes.insert(path.clone(), ome);
        if !bare.is_empty() {
            unversioned.insert(path.clone(), bare);
        }
    }

    // the axis names as written, of each level's first multiscale (I8: all agree)
    let mut dims: HashMap<String, Jv> = HashMap::new();
    for (p, (g, i)) in &first {
        let axes = got(multiscales[g][*i].get("axes").and_then(|a| a.as_arr()))?;
        dims.insert(p.clone(), Jv::Arr(axes.iter().map(|a| got(a.get("name")).cloned()).collect::<Res<_>>()?));
    }
    let listed: HashMap<&str, u64> = keys.iter().map(|(k, n)| (k.as_str(), *n)).collect();
    let xml: Vec<(String, u64)> = collections.iter().map(|c| join(&join(c, "OME"), "METADATA.ome.xml"))
        .filter_map(|k| listed.get(k.as_str()).map(|&n| (k, n))).collect();
    let summary = json!({
        "images": images.len(), "labels": label_images.len(), "droppedLabelLevels": dropped, "plates": plates.len(),
        "wells": wells.len(), "fields": well_images.values().map(|v| v.len()).sum::<usize>(),
        "omeXml": xml.iter().filter(|x| x.1 > 0).count(),
    });
    let groups = groups.into_iter().map(|(p, a)| {
        let u = unversioned.remove(&p).unwrap_or_default().into_iter().map(String::from).collect();
        (p.clone(), OmeGroup { ome: omes.remove(&p), attributes: a, unversioned: u })
    }).collect();
    Ok(Ome { groups, dims, xml: xml.into_iter().map(|x| x.0).collect(), summary })
}

/// What the OME layer gives the projection: for each explicit group its `ome` (an OME group's)
/// and the attributes and unversioned members of its source metadata, each level's
/// `dimension_names`, the OME-XML keys, and the summary's counts.
pub struct OmeGroup {
    pub ome: Option<Jv>,
    pub attributes: Jv,
    pub unversioned: Vec<String>,
}

pub struct Ome {
    pub groups: BTreeMap<String, OmeGroup>,
    pub dims: HashMap<String, Jv>,
    pub xml: Vec<String>,
    pub summary: J,
}
