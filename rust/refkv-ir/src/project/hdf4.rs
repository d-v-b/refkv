//! The HDF4 projection (spec/zarrify/hdf4.md §2
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf4.md#2-the-hierarchy>) from a valid
//! IR and the parser's description of the file (spec/zarrify/hdf4.md §9.6
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf4.md#96-the-description>). refkv's
//! **plan** ([`plan`]) makes the convention's decisions from the description alone: which Vgroups
//! are groups and which datasets and tables arrays, at which paths, their data types, fill values,
//! codecs and chunk stores, the attributes presented and the source metadata. The projection then
//! writes a group per presented Vgroup and an array per presented dataset or table. A dataset's
//! chunks are, by its storage (spec/zarrify/hdf4.md §3.2
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf4.md#32-chunks>): its contiguous
//! bytes cut as conventions §7
//! <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#7-source-values-as-arrays> cuts
//! contiguous values; its one compressed stream; or its chunks. Bytes in linked blocks are the
//! ranges of their blocks, and a block never written a range of the zero data source.

use crate::error::Error;

use super::{contiguous, key, Copied};
use crate::hdf4::{number_json, numeric, ATTRIBUTE, NDG, VG, VH};
use crate::hdf5::file::unhex;
use crate::hdf5::types::{float_json, int_of};
use crate::ir::Ir;
use crate::out::{declare, declared_group, grid_chunks, Chunk, Out, Part};
use crate::refs::Refs;
use crate::rules::Rules;
use crate::text::text_json;
use serde_json::{json, Map, Value as J};
use std::collections::{BTreeMap, HashMap, HashSet};

const PROFILE: &str = "hdf4";
/// The chunk limit of contiguous data (spec/zarrify/hdf4.md §3.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf4.md#32-chunks>).
const CHUNK_BYTES: u64 = 1 << 20;
/// The most bytes of the zero data source: a longer run of zeros is several ranges of it.
const ZEROS_MOST: u64 = 1 << 16;

// Compression codes (hcomp.h)
const COMP_NONE: u16 = 0;
const COMP_DEFLATE: u16 = 4;

/// The SD and GR interfaces' own Vgroup classes (hlimits.h, INTERNAL_HDF_VGS): bookkeeping, not
/// groups (spec/zarrify/hdf4.md §2.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf4.md#21-groups>).
const INTERNAL_VGS: [&str; 6] = ["Var0.0", "Dim0.0", "UDim0.0", "CDF0.0", "RIG0.0", "RI0.0"];
/// The interfaces' own Vdata classes (INTERNAL_HDF_VDS), the chunk tables' a prefix.
const INTERNAL_VDS: [&str; 7] = ["DimVal0.0", "DimVal0.1", "Attr0.0", "SDSVar", "CoordVar", "RIATTR0.0N", "RIATTR0.0C"];
const CHUNK_TABLE_CLASS: &str = "_HDF_CHK_TBL_";

/// The names a node may not take at the root, and the attribute names the hierarchy writes itself.
const RESERVED_ROOT: &[&str] = &["refkv_source"];
const RESERVED_ATTRIBUTES: &[&str] = &["zarr_conventions", "refkv_zarrified"];

fn n(v: &J) -> u64 {
    v.as_u64().unwrap_or(0)
}

fn list(v: &J) -> &[J] {
    v.as_array().map_or(&[], |a| a.as_slice())
}

fn shape_of(v: &J) -> Vec<u64> {
    list(v).iter().map(n).collect()
}

/// A storage's segments: (offset, or None for zeros, length).
fn segments(v: &J) -> Vec<(Option<u64>, u64)> {
    list(v).iter().map(|s| (s[0].as_u64(), n(&s[1]))).collect()
}

/// A description's text value as a string: its text, or the ISO 8859-1 reading of bytes that are
/// not UTF-8.
fn name_str(t: &J) -> String {
    match t {
        J::String(s) => s.clone(),
        t => t["latin1"].as_str().unwrap_or("").to_string(),
    }
}

/// The first `len` bytes of the segments `segs`.
fn prefix(segs: &[(Option<u64>, u64)], len: u64) -> Vec<(Option<u64>, u64)> {
    range(segs, 0, len)
}

/// The `len` bytes of the segments `segs` from `at`.
fn range(segs: &[(Option<u64>, u64)], mut at: u64, len: u64) -> Vec<(Option<u64>, u64)> {
    let mut out = Vec::new();
    let mut left = len;
    for &(o, m) in segs {
        if left == 0 {
            break;
        }
        if at >= m {
            at -= m;
            continue;
        }
        let k = left.min(m - at);
        out.push((o.map(|o| o + at), k));
        left -= k;
        at = 0;
    }
    out
}

fn total(segs: &[(Option<u64>, u64)]) -> u64 {
    segs.iter().map(|s| s.1).sum()
}

fn segs_json(segs: &[(Option<u64>, u64)]) -> J {
    J::Array(segs.iter().map(|&(o, k)| json!([o, k])).collect())
}

/// A Zarr node name (spec/zarrify/hdf4.md §2.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf4.md#22-paths>, HDF5's rule and no `/`):
/// valid UTF-8, not empty, not `.` or `..`, not starting with `__`.
fn node_name(t: &J) -> Option<String> {
    let s = t.as_str()?;
    (!s.is_empty() && s != "." && s != ".." && !s.starts_with("__") && !s.contains('/')).then(|| s.to_string())
}

fn internal_vg(class: &J) -> bool {
    class.as_str().is_some_and(|c| INTERNAL_VGS.contains(&c))
}

fn internal_vd(class: &J) -> bool {
    class.as_str().is_some_and(|c| INTERNAL_VDS.contains(&c) || c.starts_with(CHUNK_TABLE_CLASS))
}

fn compression_name(c: u16) -> String {
    match c {
        0 => "none".into(),
        1 => "rle".into(),
        2 => "nbit".into(),
        3 => "skipping_huffman".into(),
        4 => "deflate".into(),
        5 => "szip".into(),
        7 => "jpeg".into(),
        12 => "imcomp".into(),
        c => format!("code {c}"),
    }
}

/// A number type's name, as the source metadata gives it.
fn type_name(code: u16) -> String {
    match code & 0xff {
        3 => "uchar8".into(),
        4 => "char8".into(),
        5 => "float32".into(),
        6 => "float64".into(),
        20 => "int8".into(),
        21 => "uint8".into(),
        22 => "int16".into(),
        23 => "uint16".into(),
        24 => "int32".into(),
        25 => "uint32".into(),
        26 => "int64".into(),
        27 => "uint64".into(),
        c => format!("type {c}"),
    }
}

/// The value the SD interface reads where a dataset holds no data and has no `_FillValue`: netCDF's
/// default of the type's netCDF type, as `NC_arrayfill` writes it (spec/zarrify/hdf4.md §3
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf4.md#3-datasets>): `char` 0, `byte`
/// −127 (every byte 0x81, so 129 unsigned), `short` −32767, `long` −2147483647 (32769 and
/// 2147483649 unsigned), `float` and `double` 9.9692099683868690e36.
fn default_fill(code: u8) -> J {
    match code {
        3 => json!(0),
        20 => json!(-127),
        21 => json!(129),
        22 => json!(-32767),
        23 => json!(32769),
        24 => json!(-2147483647),
        25 => json!(2147483649u64),
        // 15 × 2^119, netCDF's FILL_FLOAT and FILL_DOUBLE, exact in both
        5 => float_json(&0x7CF0_0000u32.to_be_bytes(), true),
        _ => float_json(&0x479E_0000_0000_0000u64.to_be_bytes(), true),
    }
}

/// The leaves of a Vdata field of type `ty` and order `order` in a record's `struct`: one for text,
/// one a value for numbers.
fn leaves_of(ty: u16, order: u16) -> u64 {
    if ty & 0xFF == 4 { 1 } else { order as u64 }
}

/// A Vdata field's Zarr data type, fill value and whether it has a byte order, or why it has none:
/// `char8` of order `n` is `null_terminated_bytes` of `n`, a number of order 1 its type, of order
/// `n > 1` a `struct` of the fields `0` … `n − 1`.
fn field_type(ty: u16, isize: u16, order: u16) -> Result<(J, J, bool), &'static str> {
    if ty & !0x40FF != 0 {
        return Err("datatype");
    }
    let (order, isize) = (order as u64, isize as u64);
    if order == 0 {
        return Err("fields");
    }
    let code = (ty & 0xFF) as u8;
    if code == 4 {
        if isize != order {
            return Err("fields");
        }
        return Ok((json!({"name": "null_terminated_bytes", "configuration": {"length_bytes": order}}), json!(""), false));
    }
    let Some((one, size, _)) = numeric(code) else { return Err("datatype") };
    if isize != size * order {
        return Err("fields");
    }
    let zero = if code == 5 || code == 6 { json!(0.0) } else { json!(0) };
    if order == 1 {
        return Ok((json!(one), zero, size > 1));
    }
    let fields: Vec<J> = (0..order).map(|k| json!({"name": k.to_string(), "data_type": one})).collect();
    let fill: Map<String, J> = (0..order).map(|k| (k.to_string(), zero.clone())).collect();
    Ok((json!({"name": "struct", "configuration": {"fields": fields}}), J::Object(fill), size > 1))
}

/// The `bytes` codec of a struct or value whose elements are `ordered` (of more than one byte, a
/// struct with a text field among them, which zarr-python reads as needing an order): big- or
/// little-endian; else without a byte order.
fn bytes_codec(ordered: bool, big: bool) -> J {
    if ordered {
        json!({"name": "bytes", "configuration": {"endian": if big { "big" } else { "little" }}})
    } else {
        json!({"name": "bytes"})
    }
}

/// An attribute as the hierarchy presents it, or why not.
#[derive(Clone)]
struct Attr {
    name: J,
    /// its type and count, as the source metadata describes it
    described: J,
    value: Result<J, &'static str>,
    /// its number type code (base) and bytes, for `_FillValue`
    raw: Option<(u8, bool, Vec<u8>)>,
}

/// A dataset decided: its array as the plan gives it, or why it is not presented.
enum Decided {
    Array { array: J, own: Map<String, J>, chunks: u64 },
    Not(&'static str),
}

/// A writer's Vdata decided (spec/zarrify/hdf4.md §4
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf4.md#4-tables>): an array of its
/// records, a group of an array per field (interlace 1), or why it is not presented; with its name
/// and attributes.
struct Table {
    name: J,
    form: TableForm,
    attrs: Vec<Attr>,
}

enum TableForm {
    Records { array: J, own: Map<String, J> },
    Fields { own: Map<String, J>, arrays: Vec<(String, J)> },
    Not(&'static str),
}

/// What the plan reads of a description: its Vgroups and Vdatas by ref, each Vgroup's members
/// parsed once, and the profile's budget.
struct Doc<'a> {
    vgs: BTreeMap<u16, &'a J>,
    members: HashMap<u16, Vec<(u16, u16)>>,
    vds: BTreeMap<u16, &'a J>,
    rules: &'static Rules,
}

fn r16(v: &J) -> u16 {
    v.as_u64().unwrap_or(0) as u16
}

fn tagrefs(v: &J) -> Vec<(u16, u16)> {
    list(v).iter().map(|m| (r16(&m[0]), r16(&m[1]))).collect()
}

impl Doc<'_> {
    fn limit(&self, name: &str) -> u64 {
        self.rules.limit(name) as u64
    }

    fn is_attribute(&self, r: u16) -> bool {
        self.vds.get(&r).is_some_and(|h| h["class"] == ATTRIBUTE_CLASS)
    }

    /// The attribute Vdatas `refs` as the hierarchy presents them (spec/zarrify/hdf4.md §7
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf4.md#7-attributes>), in order.
    fn attributes(&self, refs: &[u16]) -> Vec<Attr> {
        refs.iter().filter_map(|r| self.vds.get(r)).map(|vd| self.attribute(vd)).collect()
    }

    fn attribute(&self, vd: &J) -> Attr {
        let mut a = Attr { name: vd["name"].clone(), described: J::Null, value: Err("attribute"), raw: None };
        let [f] = list(&vd["fields"]) else { return a };
        let (ty, isize, order, nvert) = (r16(&f["type"]), n(&f["size"]), n(&f["order"]), n(&vd["records"]));
        let code = (ty & 0xff) as u8;
        let mut described = json!({"type": type_name(ty), "count": order * nvert});
        if ty & 0x4000 != 0 {
            described["endian"] = json!("little");
        }
        a.described = described;
        if isize * nvert > self.limit("attribute_bytes") {
            a.value = Err("budget");
            return a;
        }
        let Some(b) = vd["bytes"].as_str().map(unhex) else { return a };
        let big = ty & 0x4000 == 0;
        a.value = match code {
            4 => {
                a.raw = Some((4, true, b.clone()));
                Ok(text_json(&b[..b.iter().position(|&c| c == 0).unwrap_or(b.len())]))
            }
            c => match numeric(c) {
                Some((_, item, _)) if isize == item * order => {
                    let vals: Vec<J> = b.chunks(item as usize).map(|v| number_json(c, v, big)).collect();
                    if vals.len() == 1 {
                        a.raw = Some((c, big, b.clone()));
                    }
                    Ok(if vals.len() == 1 { vals.into_iter().next().unwrap_or(J::Null) } else { J::Array(vals) })
                }
                _ => Err("datatype"),
            },
        };
        a
    }
}

/// The class of an attribute's Vdata.
const ATTRIBUTE_CLASS: &str = match std::str::from_utf8(ATTRIBUTE) {
    Ok(s) => s,
    Err(_) => "",
};

/// refkv's plan of the file the description `d` describes: the hierarchy's nodes, each its `path`,
/// `attributes`, `own` (its source metadata) and, of an array, `array` (its shape, data type,
/// element size `item`, fill value, codecs, dimension names and chunk `store`), and the summary
/// (spec/zarrify/hdf4.md §9.5
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf4.md#95-summary>), but for the chunks
/// written and the rounds.
pub fn plan(d: &J) -> crate::error::Res<J> {
    let doc = Doc {
        vgs: list(&d["vgroups"]).iter().map(|v| (r16(&v["ref"]), v)).collect(),
        members: list(&d["vgroups"]).iter().map(|v| (r16(&v["ref"]), tagrefs(&v["members"]))).collect(),
        vds: list(&d["vdatas"]).iter().map(|v| (r16(&v["ref"]), v)).collect(),
        rules: crate::rules::of(PROFILE),
    };
    let vars: Vec<&J> = list(&d["datasets"]).iter().collect();
    let cdf = doc.vgs.iter().find(|(_, v)| v["class"] == "CDF0.0").map(|(&r, _)| r);
    let decided: Vec<Decided> = vars.iter().map(|v| decide(&doc, v)).collect();
    let var_attrs: Vec<Vec<Attr>> = vars.iter().map(|v| doc.attributes(&list(&v["attributes"]).iter().map(r16).collect::<Vec<_>>())).collect();
    let file_attrs = match cdf {
        Some(c) => {
            let refs: Vec<u16> = doc.members[&c].iter().copied().filter(|m| m.0 == VH && doc.is_attribute(m.1)).map(|m| m.1).collect();
            doc.attributes(&refs)
        }
        None => Vec::new(),
    };
    // the writer's Vgroups, each one's attributes: its Attr0.0 members and its version 4 attributes
    let writer: Vec<u16> = doc.vgs.iter().filter(|(_, v)| !internal_vg(&v["class"])).map(|(&r, _)| r).collect();
    let mut group_attrs: HashMap<u16, Vec<Attr>> = HashMap::new();
    for &g in &writer {
        let v = doc.vgs[&g];
        let refs: Vec<u16> = doc.members[&g].iter().copied().chain(tagrefs(&v["attributes"])).filter(|m| m.0 == VH && doc.is_attribute(m.1)).map(|m| m.1).collect();
        group_attrs.insert(g, doc.attributes(&refs));
    }
    // the hierarchy: the writer's Vgroups no other writer's Vgroup holds, in ref order, then any
    // left (a loop), each depth first; then the datasets in no Vgroup at the root
    let by_ndg: HashMap<u16, usize> = vars.iter().enumerate().filter_map(|(k, v)| Some((v["ndg"].as_u64()? as u16, k))).collect();
    let by_vg: HashMap<u16, usize> = vars.iter().enumerate().map(|(k, v)| (r16(&v["vgroup"]), k)).collect();
    let writer_set: HashSet<u16> = writer.iter().copied().collect();
    let held: HashSet<u16> = writer.iter().flat_map(|g| doc.members[g].iter().filter(|m| m.0 == VG).map(|m| m.1)).filter(|r| writer_set.contains(r)).collect();
    let dataset_of = |t: u16, r: u16| -> Option<usize> {
        match t {
            NDG => by_ndg.get(&r).copied(),
            VG => by_vg.get(&r).copied(),
            _ => None,
        }
    };
    let internal_members: HashSet<u16> = doc.vgs.iter().filter(|(_, v)| internal_vg(&v["class"])).flat_map(|(r, _)| doc.members[r].iter().filter(|m| m.0 == VH).map(|m| m.1)).collect();
    // the writer's tables: the Vdatas of a class not the interfaces' own that no interface's
    // Vgroup holds
    let mut table_defs = BTreeMap::new();
    for (&r, h) in &doc.vds {
        if internal_vd(&h["class"]) || internal_members.contains(&r) {
            continue;
        }
        table_defs.insert(r, table(&doc, h, r));
    }
    let mut b = Builder { nodes: Vec::new(), paths: HashSet::new(), reached: HashSet::new(), tables: HashSet::new(), retry: HashMap::new(), paths_vg: HashMap::new(), paths_ds: HashMap::new(), paths_tables: HashMap::new(), table_defs, unpresented: BTreeMap::new(), groups: 0, arrays: 0, chunks: 0 };
    // the root
    let mut root_own = Map::new();
    if !d["version"].is_null() {
        root_own.insert("version".into(), d["version"].clone());
    }
    root_own.insert("cdf".into(), cdf.map_or(J::Null, |c| json!(c)));
    let root = b.group(String::new(), Vec::new(), file_attrs, root_own);
    let mut roots: Vec<u16> = writer.iter().copied().filter(|r| !held.contains(r)).collect();
    let mut walked: HashSet<u16> = HashSet::new();
    let mut left: Vec<u16> = writer.clone();
    let w = Walk { doc: &doc, vars: &vars, decided: &decided, var_attrs: &var_attrs, dataset_of: &dataset_of };
    loop {
        for g in std::mem::take(&mut roots) {
            if walked.contains(&g) {
                continue;
            }
            w.walk(&mut b, root, g, &mut group_attrs, &mut walked);
        }
        left.retain(|g| !walked.contains(g));
        match left.first() {
            Some(&g) => roots.push(g),
            None => break,
        }
    }
    for k in 0..vars.len() {
        // a dataset the walk did not reach: in no Vgroup, or only in Vgroups not presented
        if !b.reached.contains(&k) {
            b.dataset(root, k, vars[k], &decided[k], &var_attrs[k]);
        }
    }
    // the writer's tables the walk did not reach, and the datasets of a file without a CDF0.0
    // Vgroup, which the profile does not present
    let tables: Vec<u16> = b.table_defs.keys().copied().filter(|r| !b.tables.contains(r)).collect();
    for r in tables {
        b.table(root, r);
    }
    if cdf.is_none() {
        for r in list(&d["ndgs"]) {
            b.not(root, None, NDG, r16(r), "dfsd");
        }
    }
    let Builder { mut nodes, unpresented, groups, arrays, chunks, .. } = b;
    let mut counts: BTreeMap<String, u64> = BTreeMap::new();
    for (node, list) in unpresented {
        for u in &list {
            *counts.entry(u["reason"].as_str().unwrap_or("").to_string()).or_default() += 1;
        }
        nodes[node]["own"]["unpresented"] = J::Array(list);
    }
    let summary = json!({"groups": groups, "arrays": arrays, "unpresented": counts, "chunks": chunks, "descriptors": d["descriptors"]});
    Ok(json!({"nodes": nodes, "summary": summary}))
}

/// What the walk reads: the description, the datasets decided and their attributes, and which
/// dataset a member names.
struct Walk<'a> {
    doc: &'a Doc<'a>,
    vars: &'a [&'a J],
    decided: &'a [Decided],
    var_attrs: &'a [Vec<Attr>],
    dataset_of: &'a dyn Fn(u16, u16) -> Option<usize>,
}

impl Walk<'_> {
    /// The writer's Vgroup `g` under node `parent`, and what it holds: depth first, each group's
    /// members in their order (spec/zarrify/hdf4.md §2.2
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf4.md#22-paths>).
    fn walk(&self, b: &mut Builder, parent: usize, g: u16, group_attrs: &mut HashMap<u16, Vec<Attr>>, walked: &mut HashSet<u16>) {
        let (vgs, vds) = (&self.doc.vgs, &self.doc.vds);
        let max_depth = self.doc.limit("max_depth") as usize;
        // the group presented for Vgroup `g` under `parent` at `depth`, or None
        let mut enter = |b: &mut Builder, parent: usize, g: u16, depth: usize| -> Option<usize> {
            let v = vgs[&g];
            if let Some(path) = b.paths_vg.get(&g).cloned() {
                b.link(parent, &v["name"], VG, g, &path);
                return None;
            }
            walked.insert(g);
            if depth > max_depth {
                b.not(parent, Some(&v["name"]), VG, g, "depth");
                return None;
            }
            let name = b.name_in(parent, &v["name"], VG, g).ok()?;
            let mut own = Map::new();
            own.insert("ref".into(), json!(g));
            own.insert("class".into(), v["class"].clone());
            let node = b.group(name, vec![parent], group_attrs.remove(&g).unwrap_or_default(), own);
            b.paths_vg.insert(g, b.nodes[node]["path"].as_str().unwrap_or("").to_string());
            Some(node)
        };
        // (group node, Vgroup, the next member, depth)
        let mut stack: Vec<(usize, u16, usize, usize)> = Vec::new();
        if let Some(node) = enter(b, parent, g, 1) {
            stack.push((node, g, 0, 1));
        }
        while let Some(top) = stack.last_mut() {
            let (node, g, k, depth) = *top;
            let Some(&(t, r)) = self.doc.members[&g].get(k) else {
                stack.pop();
                continue;
            };
            top.2 += 1;
            if let Some(k) = (self.dataset_of)(t, r) {
                // presented once, at its first visit where it can be; recorded once, where it is first
                // not presented, but for a name another member of a group holds
                match b.paths_ds.get(&k).cloned() {
                    Some(path) => b.link(node, &self.vars[k]["name"], t, r, &path),
                    None if b.reached.contains(&k) && b.retry.get(&k).is_none_or(|g| g.contains(&node)) => {}
                    None => b.dataset(node, k, self.vars[k], &self.decided[k], &self.var_attrs[k]),
                }
                continue;
            }
            match t {
                VG if vgs.get(&r).is_some_and(|c| !internal_vg(&c["class"])) => {
                    if let Some(child) = enter(b, node, r, depth + 1) {
                        stack.push((child, r, 0, depth + 1));
                    }
                }
                VG => b.not(node, vgs.get(&r).map(|c| &c["name"]), t, r, "object"),
                VH => match vds.get(&r) {
                    Some(h) if h["class"] == ATTRIBUTE_CLASS => {}
                    Some(_) if b.table_defs.contains_key(&r) => b.table(node, r),
                    Some(h) => b.not(node, Some(&h["name"]), t, r, "object"),
                    None => b.not(node, None, t, r, "object"),
                },
                _ => b.not(node, None, t, r, "object"),
            }
        }
    }
}

/// Whether and how the writer's Vdata `vd`, of ref `r`, is presented (spec/zarrify/hdf4.md §4
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf4.md#4-tables>).
fn table(doc: &Doc, vd: &J, r: u16) -> Table {
    let attr_refs: Vec<u16> = list(&vd["attributes"]).iter().filter(|a| a[0].as_i64() == Some(-1) && r16(&a[1]) == VH && doc.is_attribute(r16(&a[2]))).map(|a| r16(&a[2])).collect();
    let attrs = doc.attributes(&attr_refs);
    Table { name: vd["name"].clone(), form: table_form(doc, vd, r), attrs }
}

fn table_form(doc: &Doc, vd: &J, r: u16) -> TableForm {
    let fields = list(&vd["fields"]);
    let (interlace, nvert, ivsize, version) = (n(&vd["interlace"]), n(&vd["records"]), n(&vd["record_size"]), n(&vd["version"]));
    let mut own = Map::new();
    own.insert("ref".into(), json!(r));
    own.insert("class".into(), vd["class"].clone());
    own.insert("interlace".into(), json!(interlace));
    own.insert("records".into(), json!(nvert));
    let described: Vec<J> = fields.iter().map(|f| {
        let t = r16(&f["type"]);
        let mut d = json!({"name": f["name"], "type": type_name(t), "order": f["order"], "offset": f["offset"], "size": f["size"]});
        if t & 0x4000 != 0 {
            d["endian"] = json!("little");
        }
        d
    }).collect();
    own.insert("fields".into(), J::Array(described));
    // each field's attributes, by the field's name
    let mut by_field: Map<String, J> = Map::new();
    let mut seen: HashSet<(String, String)> = HashSet::new();
    for a in list(&vd["attributes"]) {
        let Some(field) = a[0].as_u64().and_then(|k| fields.get(k as usize)) else { continue };
        let (t, at) = (r16(&a[1]), r16(&a[2]));
        if t != VH || !doc.is_attribute(at) {
            continue;
        }
        for at in doc.attributes(&[at]) {
            // the first of each name, presented or not, claims it
            if !seen.insert((name_str(&field["name"]), name_str(&at.name))) {
                continue;
            }
            if let Ok(v) = at.value {
                let e = by_field.entry(name_str(&field["name"])).or_insert_with(|| json!({}));
                e[name_str(&at.name)] = v;
            }
        }
    }
    if !by_field.is_empty() {
        own.insert("field_attributes".into(), J::Object(by_field));
    }
    if version <= 2 {
        return TableForm::Not("version");
    }
    if fields.is_empty() || interlace > 1 {
        return TableForm::Not("fields");
    }
    let mut types = Vec::with_capacity(fields.len());
    let mut big: Option<bool> = None;
    let (mut names, mut leaves) = (HashSet::new(), 0u64);
    for f in fields {
        let (ty, isize, order) = (r16(&f["type"]), r16(&f["size"]), r16(&f["order"]));
        // the leaves bounded before a field's type is built: an order is up to 65535
        leaves += leaves_of(ty, order);
        if leaves > doc.limit("max_fields") {
            return TableForm::Not("fields");
        }
        // a field of a table not interlaced is an array of its own, named by the field
        if interlace == 1 && node_name(&f["name"]).is_none() {
            return TableForm::Not("fields");
        }
        let (dt, fill, ordered) = match field_type(ty, isize, order) {
            Ok(t) => t,
            Err(why) => return TableForm::Not(why),
        };
        if ordered {
            let b = ty & 0x4000 == 0;
            // a record's fields share its one `bytes` codec; a field not interlaced has its own
            if interlace == 0 && big.is_some_and(|x| x != b) {
                return TableForm::Not("byte_order");
            }
            big = Some(b);
        }
        let name = name_str(&f["name"]);
        if !names.insert(name.clone()) {
            return TableForm::Not("fields");
        }
        types.push((name, dt, fill, ordered, isize as u64, ty));
    }
    // interlace 0: each record its fields in order, at their offsets, nothing between
    let mut at = 0u64;
    for f in fields {
        if interlace == 0 && n(&f["offset"]) != at {
            return TableForm::Not("fields");
        }
        at += n(&f["size"]);
    }
    if at != ivsize {
        return TableForm::Not("fields");
    }
    // at most 65535 bytes, the header's `uint16`: within the chunk limit
    let record = at;
    let rows = nvert;
    let size = rows * record;
    let data = &vd["data"];
    let data: Vec<(Option<u64>, u64)> = match data["kind"].as_str() {
        Some("plain") if n(&data["length"]) >= size => segments(&data["segments"]),
        None if size == 0 => Vec::new(),
        Some("plain") | None => return TableForm::Not("length"),
        Some("external") => return TableForm::Not("external"),
        _ => return TableForm::Not("special"),
    };
    let store = |s: Vec<(Option<u64>, u64)>| if total(&s) == 0 { json!({"kind": "none"}) } else { json!({"kind": "contiguous", "segments": segs_json(&s)}) };
    if interlace == 0 {
        let fields: Vec<J> = types.iter().map(|t| json!({"name": t.0, "data_type": t.1})).collect();
        let fill: Map<String, J> = types.iter().map(|t| (t.0.clone(), t.2.clone())).collect();
        let array = json!({"shape": [rows], "data_type": {"name": "struct", "configuration": {"fields": fields}}, "item": record,
                           "fill_value": fill, "codecs": [bytes_codec(types.iter().any(|t| t.4 > 1), big.unwrap_or(true))],
                           "store": store(prefix(&data, size)), "dimension_names": ["record"]});
        return TableForm::Records { array, own };
    }
    // interlace 1: each field's values together, the fields in order
    let mut arrays = Vec::with_capacity(types.len());
    let mut at = 0u64;
    for (name, dt, fill, ordered, isize, ty) in types {
        let k = rows * isize;
        let array = json!({"shape": [rows], "data_type": dt, "item": isize, "fill_value": fill,
                           "codecs": [bytes_codec(ordered || isize > 1, ty & 0x4000 == 0)], "store": store(range(&data, at, k)),
                           "dimension_names": ["record"]});
        arrays.push((name, array));
        at += k;
    }
    TableForm::Fields { own, arrays }
}

/// Whether and how the dataset `var` is an array (spec/zarrify/hdf4.md §3
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf4.md#3-datasets>).
fn decide(doc: &Doc, var: &J) -> Decided {
    let mut own = Map::new();
    own.insert("ref".into(), var["vgroup"].clone());
    if !var["ndg"].is_null() {
        own.insert("ndg".into(), var["ndg"].clone());
    }
    // the number type
    let nt = &var["number_type"];
    if nt.is_null() {
        return Decided::Not("number_type");
    }
    let (code, class) = (n(&nt["type"]) as u8, n(&nt["class"]));
    own.insert("number_type".into(), json!({"type": type_name(code as u16), "width": nt["width"], "class": class}));
    // a dataset of text: its bytes are read as `uint8` until its last axis is made its strings'
    // length (spec/zarrify/hdf4.md §3.3
    // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf4.md#33-text>)
    let text = code == 4;
    let Some((data_type, item, _)) = (if text { Some(("uint8", 1, false)) } else { numeric(code) }) else {
        return Decided::Not("number_type");
    };
    let big = match class {
        _ if item == 1 => true,
        0 | 1 => true,
        4 => false,
        _ => return Decided::Not("number_type"),
    };
    // the dimensions
    let dims = list(&var["dimensions"]);
    if var["dimension_sizes"].is_null() {
        return Decided::Not("dimensions");
    }
    let mut shape = shape_of(&var["dimension_sizes"]);
    if shape.is_empty() || shape.len() != dims.len() {
        return Decided::Not("dimensions");
    }
    let unlimited = dims[0]["unlimited"] == true;
    // the data
    let st = &var["data"];
    let (count, layout) = match st["kind"].as_str() {
        None => (0, "none"),
        Some("plain") => (n(&st["length"]), if st["linked"] == true && !list(&st["segments"]).is_empty() { "linked" } else { "contiguous" }),
        Some("compressed") => (n(&st["length"]), "compressed"),
        Some("chunked") => (n(&st["length"]), "chunked"),
        Some("external") => return Decided::Not("external"),
        _ => return Decided::Not("special"),
    };
    own.insert("layout".into(), json!(layout));
    if unlimited {
        own.insert("unlimited".into(), json!(true));
        own.insert("sdd".into(), json!(shape));
        let record = shape[1..].iter().try_fold(item, |a, &v| a.checked_mul(v)).unwrap_or(0);
        shape[0] = count.checked_div(record).unwrap_or(0);
    }
    let Some(nbytes) = shape.iter().try_fold(item, |a, &v| a.checked_mul(v)) else { return Decided::Not("size") };
    let names: Vec<J> = dims.iter().map(|d| json!(name_str(&d["name"]))).collect();
    if let Some(m) = var["coordinate"].as_bool() {
        own.insert("coordinate".into(), json!(m));
    }
    let mut codecs = vec![if item == 1 { json!({"name": "bytes"}) } else { json!({"name": "bytes", "configuration": {"endian": if big { "big" } else { "little" }}}) }];
    let zlib = json!({"name": "numcodecs.zlib", "configuration": {"level": 1}});
    let mut chunk_fill: Option<Vec<u8>> = None;
    let mut nchunks = 0u64;
    let segs = segments(&st["segments"]);
    let store = match st["kind"].as_str() {
        _ if nbytes == 0 => json!({"kind": "none"}),
        None => json!({"kind": "none"}),
        Some("plain") => {
            if count < nbytes {
                return Decided::Not("length");
            }
            json!({"kind": "contiguous", "segments": segs_json(&prefix(&segs, nbytes))})
        }
        Some("compressed") => {
            let (length, method, model) = (count, r16(&st["method"]), n(&st["model"]));
            own.insert("compression".into(), json!(compression_name(method)));
            if length == 0 {
                json!({"kind": "none"})
            } else if model != 0 {
                return Decided::Not("compression");
            } else if method == COMP_NONE {
                if total(&segs) < nbytes {
                    return Decided::Not("length");
                }
                json!({"kind": "contiguous", "segments": segs_json(&prefix(&segs, nbytes))})
            } else if method == COMP_DEFLATE {
                if length != nbytes || total(&segs) == 0 {
                    return Decided::Not("length");
                }
                codecs.push(zlib.clone());
                json!({"kind": "whole", "segments": segs_json(&segs)})
            } else {
                return Decided::Not("compression");
            }
        }
        _ => {
            let fill = unhex(st["fill"].as_str().unwrap_or(""));
            match chunked(st, &shape, item) {
                Ok((chunk, chunks, method)) => {
                    own.insert("chunks".into(), json!(chunk));
                    if let Some(m) = method {
                        own.insert("compression".into(), json!(compression_name(m)));
                        if m == COMP_DEFLATE {
                            codecs.push(zlib.clone());
                        }
                    }
                    if fill.len() as u64 == item {
                        own.insert("chunk_fill_value".into(), json!(crate::mirror::base64(&fill)));
                        chunk_fill = Some(fill);
                    }
                    nchunks = chunks.len() as u64;
                    json!({"kind": "chunked", "chunk": chunk, "chunks": chunks})
                }
                Err(why) => return Decided::Not(why),
            }
        }
    };
    // the fill value: the chunk header's, else `_FillValue`'s, else netCDF's default
    let refs: Vec<u16> = list(&var["attributes"]).iter().map(r16).collect();
    let fill_attr = doc.attributes(&refs).into_iter().find(|a| a.name == "_FillValue").and_then(|a| a.raw);
    if text {
        let byte = match (&chunk_fill, &fill_attr) {
            (Some(f), _) => f.first().copied().unwrap_or(0),
            (None, Some((4, _, raw))) => raw.first().copied().unwrap_or(0),
            _ => 0,
        };
        return as_text(shape, names, store, codecs, byte, own, nchunks);
    }
    let fill = match (&chunk_fill, &fill_attr) {
        (Some(f), _) => number_json(code, f, big),
        (None, Some((c, b, raw))) if *c == code || numeric(*c).map(|n| n.0) == Some(data_type) && numeric(*c).map(|n| n.1) == Some(item) => number_json(code, raw, *b),
        _ => default_fill(code),
    };
    Decided::Array {
        array: json!({"shape": shape, "data_type": data_type, "item": item, "fill_value": fill, "codecs": codecs, "store": store,
                      "dimension_names": names}),
        own,
        chunks: nchunks,
    }
}

/// A `char8` dataset of `shape`, read as bytes, as an array of strings
/// (spec/zarrify/hdf4.md §3.3
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf4.md#33-text>): `null_terminated_bytes`
/// of its last axis's length, its other axes the array's, its chunks the same bytes with that
/// axis dropped from their coordinates; every byte of the fill value the fill byte.
fn as_text(shape: Vec<u64>, names: Vec<J>, mut store: J, codecs: Vec<J>, fill: u8, mut own: Map<String, J>, nchunks: u64) -> Decided {
    let Some((&length, outer)) = shape.split_last() else { return Decided::Not("dimensions") };
    if length == 0 || length > 1 << 20 {
        return Decided::Not("size");
    }
    if store["kind"] == "chunked" {
        if store["chunk"].as_array().and_then(|c| c.last()).and_then(J::as_u64) != Some(length) {
            return Decided::Not("chunks");
        }
        if let Some(c) = store["chunk"].as_array_mut() {
            c.pop();
        }
        for ch in store["chunks"].as_array_mut().into_iter().flatten() {
            if let Some(at) = ch["at"].as_array_mut() {
                at.pop();
            }
        }
    }
    own.insert("text_axis".into(), names.last().cloned().unwrap_or(J::Null));
    let fill = if fill == 0 { json!("") } else { json!(crate::mirror::base64(&vec![fill; length as usize])) };
    Decided::Array {
        array: json!({"shape": outer, "data_type": {"name": "null_terminated_bytes", "configuration": {"length_bytes": length}},
                      "item": length, "fill_value": fill, "codecs": codecs, "store": store,
                      "dimension_names": names[..names.len() - 1]}),
        own,
        chunks: nchunks,
    }
}

/// A chunked dataset's chunk shape and chunks (each its grid coordinates and the segments of its
/// stored bytes) and the chunks' compression, or why it is not presented. The parser checked the
/// table and each chunk's length (spec/zarrify/hdf4.md §9.3
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf4.md#93-checks>).
#[allow(clippy::type_complexity)]
fn chunked(st: &J, shape: &[u64], item: u64) -> Result<(Vec<u64>, Vec<J>, Option<u16>), &'static str> {
    let dims = list(&st["dimensions"]);
    if dims.len() != shape.len() {
        return Err("dimensions");
    }
    let chunk: Vec<u64> = dims.iter().map(|d| n(&d["chunk"])).collect();
    let Some(chunk_bytes) = chunk.iter().try_fold(item, |a, &v| a.checked_mul(v)) else { return Err("size") };
    let mut chunks = Vec::new();
    let mut method: Option<Option<u16>> = None;
    let mut mixed = false;
    for c in list(&st["chunks"]) {
        let at = shape_of(&c["at"]);
        // a chunk of the unlimited axis past the records lies outside the array
        let outside = at.iter().zip(&chunk).zip(shape).any(|((a, c), s)| a * c >= *s);
        let data = &c["data"];
        let segs = segments(&data["segments"]);
        let (segs, m) = match data["kind"].as_str() {
            Some("plain") => (prefix(&segs, chunk_bytes), None),
            Some("compressed") => {
                let (m, model) = (r16(&data["method"]), n(&data["model"]));
                if model != 0 || !matches!(m, COMP_DEFLATE | COMP_NONE) {
                    if method.is_none() {
                        method = Some(Some(m));
                    }
                    mixed = mixed || method != Some(Some(m));
                    continue;
                }
                if m == COMP_NONE { (prefix(&segs, chunk_bytes), None) } else { (segs, Some(m)) }
            }
            _ => return Err("special"),
        };
        match method {
            None => method = Some(m),
            Some(prev) if prev != m => mixed = true,
            _ => {}
        }
        // a chunk of the unlimited axis past the records is not presented
        if outside {
            continue;
        }
        chunks.push(json!({"at": at, "segments": segs_json(&segs)}));
    }
    if mixed {
        return Err("chunks");
    }
    let method = method.flatten();
    if method.is_some_and(|m| m != COMP_DEFLATE) {
        return Err("compression");
    }
    Ok((chunk, chunks, method))
}

/// The nodes as the walk presents them, each group's unpresented members, and the counts.
struct Builder {
    nodes: Vec<J>,
    /// the nodes' paths
    paths: HashSet<String>,
    /// the datasets and tables the walk reached, presented or recorded
    reached: HashSet<usize>,
    tables: HashSet<u16>,
    /// the writer's tables, decided, and the canonical path of each presented
    table_defs: BTreeMap<u16, Table>,
    paths_tables: HashMap<u16, String>,
    /// the datasets reached and not presented for their name alone, with the groups that recorded them
    retry: HashMap<usize, HashSet<usize>>,
    /// the canonical path of each Vgroup and dataset presented
    paths_vg: HashMap<u16, String>,
    paths_ds: HashMap<usize, String>,
    unpresented: BTreeMap<usize, Vec<J>>,
    groups: u64,
    arrays: u64,
    chunks: u64,
}

impl Builder {
    fn path_of(&self, parent: &[usize], name: &str) -> String {
        match parent.first() {
            Some(&p) => {
                let base = self.nodes[p]["path"].as_str().unwrap_or("");
                if base.is_empty() { name.to_string() } else { format!("{base}/{name}") }
            }
            None => String::new(),
        }
    }

    /// A child of `parent` named `name` (a node name, the first of its name in the group), or
    /// the reason it is not presented, which is recorded.
    fn name_in(&mut self, parent: usize, name: &J, tag: u16, rf: u16) -> Result<String, &'static str> {
        let reason = match node_name(name) {
            None => "name",
            Some(n) if self.nodes[parent]["path"] == json!("") && RESERVED_ROOT.contains(&n.as_str()) => "reserved",
            Some(n) if self.paths.contains(&self.path_of(&[parent], &n)) => "duplicate",
            Some(n) => return Ok(n),
        };
        self.not(parent, Some(name), tag, rf, reason);
        Err(reason)
    }

    fn not(&mut self, parent: usize, name: Option<&J>, tag: u16, rf: u16, reason: &str) {
        let mut u = json!({"tag": tag, "ref": rf, "reason": reason});
        if let Some(n) = name {
            u["name"] = n.clone();
        }
        self.unpresented.entry(parent).or_default().push(u);
    }

    fn link(&mut self, parent: usize, name: &J, tag: u16, rf: u16, path: &str) {
        let n = &mut self.nodes[parent]["own"];
        if n.get("links").is_none() {
            n["links"] = json!([]);
        }
        if let Some(l) = n["links"].as_array_mut() {
            l.push(json!({"name": name, "tag": tag, "ref": rf, "path": path}));
        }
    }

    fn group(&mut self, name: String, parent: Vec<usize>, attrs: Vec<Attr>, mut own: Map<String, J>) -> usize {
        let path = self.path_of(&parent, &name);
        let (attributes, described, not) = presented_attributes(attrs, None);
        if !described.is_empty() {
            own.insert("attributes".into(), J::Object(described));
        }
        if !not.is_empty() {
            own.insert("unpresented_attributes".into(), J::Object(not));
        }
        self.groups += u64::from(!path.is_empty());
        self.paths.insert(path.clone());
        self.nodes.push(json!({"path": path, "attributes": attributes, "own": own}));
        self.nodes.len() - 1
    }

    /// The writer's table of ref `r` under `parent`: presented at its first visit, a link at a later
    /// one, recorded once when not presented.
    fn table(&mut self, parent: usize, r: u16) {
        if let Some(path) = self.paths_tables.get(&r).cloned() {
            let name = self.table_defs.get(&r).map(|t| t.name.clone()).unwrap_or(J::Null);
            self.link(parent, &name, VH, r, &path);
            return;
        }
        if !self.tables.insert(r) {
            return;
        }
        let Some(t) = self.table_defs.remove(&r) else { return };
        let Table { name, form, attrs } = t;
        match form {
            TableForm::Not(why) => self.not(parent, Some(&name), VH, r, why),
            TableForm::Records { array, own } => {
                if let Ok(n) = self.name_in(parent, &name, VH, r) {
                    let path = self.array_node(parent, &n, array, own, attrs);
                    self.paths_tables.insert(r, path);
                }
            }
            TableForm::Fields { own, arrays } => {
                if let Ok(n) = self.name_in(parent, &name, VH, r) {
                    let group = self.group(n, vec![parent], attrs, own);
                    for (field, array) in arrays {
                        // node names, one each (`table_form`)
                        self.array_node(group, &field, array, Map::new(), Vec::new());
                    }
                    let path = self.nodes[group]["path"].as_str().unwrap_or("").to_string();
                    self.paths_tables.insert(r, path);
                }
            }
        }
        // the table is reached: a later visit links it when it is presented, and passes it by when
        // it is not (recorded once)
        self.table_defs.insert(r, Table { name, form: TableForm::Not("table"), attrs: Vec::new() });
    }

    /// An array node `name` under `parent` of `array`, its own metadata and attributes: its path.
    fn array_node(&mut self, parent: usize, name: &str, array: J, mut own: Map<String, J>, attrs: Vec<Attr>) -> String {
        let path = self.path_of(&[parent], name);
        let (attributes, described, not) = presented_attributes(attrs, None);
        if !described.is_empty() {
            own.insert("attributes".into(), J::Object(described));
        }
        if !not.is_empty() {
            own.insert("unpresented_attributes".into(), J::Object(not));
        }
        self.arrays += 1;
        self.paths.insert(path.clone());
        self.nodes.push(json!({"path": path, "attributes": attributes, "own": own, "array": array}));
        path
    }

    fn dataset(&mut self, parent: usize, k: usize, var: &J, decided: &Decided, attrs: &[Attr]) {
        self.reached.insert(k);
        let vg = r16(&var["vgroup"]);
        let Decided::Array { array, own, chunks } = decided else {
            if let Decided::Not(why) = decided {
                self.not(parent, Some(&var["name"]), VG, vg, why);
            }
            return;
        };
        let name = match self.name_in(parent, &var["name"], VG, vg) {
            Ok(name) => name,
            // a name another member of this group holds: the dataset is tried again where the walk
            // reaches it next in another group
            Err("duplicate") => {
                self.retry.entry(k).or_default().insert(parent);
                return;
            }
            Err(_) => return,
        };
        self.retry.remove(&k);
        let path = self.path_of(&[parent], &name);
        let mut own = own.clone();
        let float = array["data_type"].as_str().is_some_and(|t| t.starts_with("float"));
        let (attributes, described, not) = presented_attributes(attrs.to_vec(), Some(float));
        if !described.is_empty() {
            own.insert("attributes".into(), J::Object(described));
        }
        if !not.is_empty() {
            own.insert("unpresented_attributes".into(), J::Object(not));
        }
        self.arrays += 1;
        self.chunks += chunks;
        self.paths_ds.insert(k, path.clone());
        self.paths.insert(path.clone());
        self.nodes.push(json!({"path": path, "attributes": attributes, "own": own, "array": array}));
    }
}

/// A node's attributes as presented, each one's described type, and the reasons for those not
/// presented (spec/zarrify/hdf4.md §7
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf4.md#7-attributes>). `float`: of a
/// dataset, whether its type is a float's, for `_FillValue`'s form.
fn presented_attributes(attrs: Vec<Attr>, float: Option<bool>) -> (Map<String, J>, Map<String, J>, Map<String, J>) {
    let (mut presented, mut described, mut not) = (Map::new(), Map::new(), Map::new());
    for a in attrs {
        let name = name_str(&a.name);
        if presented.contains_key(&name) || not.contains_key(&name) {
            continue;
        }
        if !a.described.is_null() {
            described.insert(name.clone(), a.described.clone());
        }
        if RESERVED_ATTRIBUTES.contains(&name.as_str()) {
            not.insert(name, json!("reserved"));
            continue;
        }
        match a.value {
            Ok(mut v) => {
                // `_FillValue` in the form xarray's Zarr backend reads (spec/zarrify/hdf5.md §6
                // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#6-attributes>)
                if let (true, Some(true), Some((c, big, raw))) = (name == "_FillValue", float, a.raw.as_ref().filter(|r| numeric(r.0).is_some())) {
                    let x = match c {
                        5 => raw.get(..4).map(|b| f32::from_bits(int_of(b, *big, false) as u32) as f64),
                        6 => raw.get(..8).map(|b| f64::from_bits(int_of(b, *big, false) as u64)),
                        _ => Some(int_of(raw, *big, matches!(c, 20 | 22 | 24)) as f64),
                    };
                    if let Some(x) = x {
                        v = json!(crate::mirror::base64(&x.to_le_bytes()));
                    }
                }
                presented.insert(name, v);
            }
            Err(why) => {
                not.insert(name, json!(why));
            }
        }
    }
    (presented, described, not)
}

/// The parts of the bytes `[at, at + len)` of the segments `segs`, zeros as ranges of the zero
/// data source.
fn parts_of(segs: &[(Option<u64>, u64)], mut at: u64, mut len: u64, zeros: &mut contiguous::Zeros, out: &mut Out) -> crate::error::Res<Vec<Part>> {
    let mut parts = Vec::new();
    for &(o, m) in segs {
        if len == 0 {
            break;
        }
        if at >= m {
            at -= m;
            continue;
        }
        let k = len.min(m - at);
        match o {
            Some(o) => match parts.last_mut() {
                Some(Part::Src(p, q)) if *p + *q == o + at => *q += k,
                _ => parts.push(Part::Src(o + at, k)),
            },
            None => zero_parts(&mut parts, k, zeros, out)?,
        }
        len -= k;
        at = 0;
    }
    if len > 0 {
        return Err(Error::internal("a chunk lies past its dataset's bytes"));
    }
    Ok(parts)
}

fn zero_parts(parts: &mut Vec<Part>, mut k: u64, zeros: &mut contiguous::Zeros, out: &mut Out) -> crate::error::Res<()> {
    while k > 0 {
        let m = k.min(ZEROS_MOST);
        parts.push(zeros.range(out, m)?);
        k -= m;
    }
    Ok(())
}

/// A chunk shape, and each chunk with its grid coordinates.
type Cut = (Vec<u64>, Vec<(Vec<u64>, Chunk)>);

/// The chunk shape and chunks of a contiguous array (or of one with no data, whose chunks are not
/// written).
fn cut(a: &J) -> crate::error::Res<Cut> {
    let shape = shape_of(&a["shape"]);
    grid_chunks(0, &shape, n(&a["item"]).max(1), CHUNK_BYTES, usize::MAX).ok_or_else(|| Error::internal("the data cannot be cut into chunks"))
}

/// The key of the chunk at `coords` of the array at `path`: `c` alone for an array of rank 0.
fn chunk_key(path: &str, coords: &[u64]) -> String {
    let base = if path.is_empty() { "c".to_string() } else { format!("{path}/c") };
    if coords.is_empty() { base } else { key(&format!("{base}/"), coords.iter().copied()) }
}

/// The projection of the file the description describes, through refkv's plan ([`plan`]).
pub fn project(ir: &Ir, description: &J, url: &str, _copied: &Copied) -> crate::error::Res<Out> {
    let planned = plan(description)?;
    let nodes = list(&planned["nodes"]);
    let mut out = Out::new(url);
    // each array's chunk shape, and the zero data source its padding and unwritten blocks need
    let mut cuts = Vec::with_capacity(nodes.len());
    let mut most = 0u64;
    for node in nodes {
        let a = &node["array"];
        let store = &a["store"];
        let c = match store["kind"].as_str() {
            Some("contiguous" | "none") => Some(cut(a)?),
            _ => None,
        };
        if let Some((_, chunks)) = &c {
            for (_, ch) in chunks {
                if let Chunk::Ranges(parts) = ch
                    && let Some(Part::Lit(z)) = parts.last()
                {
                    most = most.max(z.len() as u64);
                }
            }
        }
        let mut segs: Vec<&J> = vec![&store["segments"]];
        segs.extend(list(&store["chunks"]).iter().map(|c| &c["segments"]));
        for s in segs {
            for (o, m) in segments(s) {
                if o.is_none() {
                    most = most.max(m.min(ZEROS_MOST));
                }
            }
        }
        cuts.push(c);
    }
    let mut zeros = contiguous::Zeros::sized(most);
    for (node, c) in nodes.iter().zip(&cuts) {
        let path = node["path"].as_str().unwrap_or("");
        let doc_key = if path.is_empty() { "zarr.json".to_string() } else { format!("{path}/zarr.json") };
        let attributes: Map<String, J> = node["attributes"].as_object().cloned().unwrap_or_default();
        let own = node["own"].clone();
        let a = &node["array"];
        if a.is_null() {
            out.json(&doc_key, &declared_group(attributes, PROFILE, path.is_empty().then_some(url), Some(own)));
            continue;
        }
        let shape = shape_of(&a["shape"]);
        let store = &a["store"];
        let whole: Vec<u64> = shape.iter().map(|&v| v.max(1)).collect();
        let chunk_shape = match (store["kind"].as_str(), c) {
            (Some("chunked"), _) => shape_of(&store["chunk"]),
            (_, Some((cs, _))) => cs.clone(),
            _ => whole,
        };
        let doc = json!({
            "zarr_format": 3,
            "node_type": "array",
            "shape": shape,
            "data_type": a["data_type"],
            "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": chunk_shape}},
            "chunk_key_encoding": {"name": "default", "configuration": {"separator": "/"}},
            "fill_value": a["fill_value"],
            "codecs": a["codecs"],
            "attributes": declare(attributes, PROFILE, None, Some(own)),
            "dimension_names": a["dimension_names"],
        });
        out.json(&doc_key, &doc);
    }
    let mut refs = Refs::new(ir, out);
    let mut count = 0u64;
    for (node, c) in nodes.iter().zip(cuts) {
        let a = &node["array"];
        if a.is_null() {
            continue;
        }
        let path = node["path"].as_str().unwrap_or("");
        let store = &a["store"];
        match store["kind"].as_str() {
            Some("contiguous") => {
                let segs = segments(&store["segments"]);
                let Some((_, chunks)) = c else { continue };
                for (coords, chunk) in chunks {
                    let Chunk::Ranges(cp) = chunk else { return Err(Error::internal("a contiguous chunk is not a range")) };
                    let mut parts = Vec::new();
                    for p in cp {
                        match p {
                            Part::Src(o, m) => parts.extend(parts_of(&segs, o, m, &mut zeros, &mut refs.out)?),
                            Part::Lit(z) => zero_parts(&mut parts, z.len() as u64, &mut zeros, &mut refs.out)?,
                            p => parts.push(p),
                        }
                    }
                    refs.chunk(chunk_key(path, &coords), 0, 0, 0, Some(parts))?;
                    count += 1;
                }
            }
            Some("whole") => {
                let segs = segments(&store["segments"]);
                let total: u64 = segs.iter().map(|s| s.1).sum();
                let parts = parts_of(&segs, 0, total, &mut zeros, &mut refs.out)?;
                refs.chunk(chunk_key(path, &vec![0; shape_of(&a["shape"]).len()]), 0, 0, 0, Some(parts))?;
                count += 1;
            }
            Some("chunked") => {
                for ch in list(&store["chunks"]) {
                    let segs = segments(&ch["segments"]);
                    let total: u64 = segs.iter().map(|s| s.1).sum();
                    let parts = parts_of(&segs, 0, total, &mut zeros, &mut refs.out)?;
                    refs.chunk(chunk_key(path, &shape_of(&ch["at"])), 0, 0, 0, Some(parts))?;
                    count += 1;
                }
            }
            _ => {}
        }
    }
    let mut out = refs.out;
    let mut summary = planned["summary"].clone();
    summary["chunk_entries"] = json!(count);
    out.summary = summary;
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn node_names_follow_hdf5s_rule_without_slashes() {
        for (name, ok) in [(json!("a b"), true), (json!(""), false), (json!("."), false), (json!(".."), false), (json!("__x"), false), (json!("a/b"), false), (json!({"latin1": "\u{ff}"}), false)] {
            assert_eq!(node_name(&name).is_some(), ok, "{name:?}");
        }
    }

    #[test]
    fn default_fills_are_netcdfs() {
        assert_eq!(default_fill(20), json!(-127));
        assert_eq!(default_fill(21), json!(129));
        assert_eq!(default_fill(23), json!(32769));
        assert_eq!(default_fill(25), json!(2147483649u64));
        assert_eq!(default_fill(6), json!(9.969_209_968_386_869e36));
        assert_eq!(default_fill(5), json!(9.969_209_968_386_869e36));
    }

    /// A range across segments is their ranges, adjacent ones merged, and zeros the zero data
    /// source's.
    #[test]
    fn ranges_follow_the_blocks() {
        let mut out = Out::new("u");
        let mut zeros = contiguous::Zeros::sized(8);
        let segs = [(Some(100), 4), (Some(104), 4), (None, 8), (Some(10), 4)];
        let parts = parts_of(&segs, 2, 14, &mut zeros, &mut out).unwrap();
        assert_eq!(parts, vec![Part::Src(102, 6), Part::Data(1, 0, 8)]);
        let parts = parts_of(&segs, 15, 4, &mut zeros, &mut out).unwrap();
        assert_eq!(parts, vec![Part::Data(1, 0, 1), Part::Src(10, 3)]);
        assert!(parts_of(&segs, 18, 4, &mut zeros, &mut out).is_err());
    }
}
