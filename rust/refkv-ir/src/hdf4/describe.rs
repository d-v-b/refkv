//! The description of an HDF4 file (spec/zarrify/hdf4.md §9.6
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf4.md#96-the-description>): what the
//! parser found of the file's known structures, in HDF4's own terms. These are its library version,
//! its Vgroups and its Vdatas (an attribute's values decoded), and the SD interface's datasets, each
//! with its number type, dimension record and how its data is stored, a chunked element's chunks
//! listed. It holds none of an output's decisions.

use super::{bad, text_json, vars_of, Acc, Fail, Pass, Rd, Special, Stored, Vg, Vh, ATTRIBUTE, NDG, NT, R, SD, SDD, VERSION, VS};
use crate::hdf5::file::hex;
use serde_json::{json, Value as J};
use std::collections::{BTreeMap, HashSet};

/// The description of the file whose Vgroups `vgs` and Vdata headers `vhs` the pass `p` read. It
/// reads what each Vdata, dataset and chunk table names, each independent thing going on past a
/// miss.
pub(super) fn description(p: &mut Pass, vgs: &BTreeMap<u16, Vg>, vhs: &BTreeMap<u16, Vh>) -> R<J> {
    let vgroups: Vec<J> = vgs.iter().map(|(&r, v)| {
        json!({"ref": r, "name": text_json(&v.name), "class": text_json(&v.class), "version": v.version,
               "members": v.members.iter().map(|m| json!([m.0, m.1])).collect::<Vec<_>>(),
               "attributes": v.attrs.iter().map(|m| json!([m.0, m.1])).collect::<Vec<_>>()})
    }).collect();
    let mut vdatas = Vec::with_capacity(vhs.len());
    let mut acc = Acc::default();
    for (&r, h) in vhs {
        if let Some(d) = acc.take(vdata(p, r, h)) {
            vdatas.push(d);
        }
        if acc.stopped() {
            break;
        }
    }
    acc.done()?;
    let (_, vars) = vars_of(vgs, vhs);
    let mut datasets = Vec::with_capacity(vars.len());
    let mut acc = Acc::default();
    for var in &vars {
        let read = (|| -> R<J> {
            let number_type = match var.nt.and_then(|r| p.by.get(&(NT, r)).copied()) {
                Some(i) => match p.header(i)?[..] {
                    [version, ty, width, class, ..] => json!({"version": version, "type": ty, "width": width, "class": class}),
                    _ => J::Null,
                },
                None => J::Null,
            };
            let sizes = match var.sdd.and_then(|r| p.by.get(&(SDD, r)).copied()) {
                Some(i) => {
                    let b = p.header(i)?;
                    let mut r = Rd::new(&b);
                    let rank = r.u16().unwrap_or(0) as usize;
                    (0..rank).map(|_| r.u32().map(|v| json!(v))).collect::<Option<Vec<J>>>().map_or(J::Null, J::Array)
                }
                None => J::Null,
            };
            let data = match var.sd {
                Some(r) => storage(p, SD, r)?,
                None => J::Null,
            };
            Ok(json!({
                "vgroup": var.vg, "name": text_json(&var.name),
                "dimensions": var.dims.iter().map(|d| json!({"name": text_json(&d.0), "unlimited": d.1})).collect::<Vec<_>>(),
                "attributes": var.attrs, "ndg": var.ndg, "sd": var.sd, "coordinate": var.marked,
                "number_type": number_type, "dimension_sizes": sizes, "data": data,
            }))
        })();
        if let Some(d) = acc.take(read) {
            datasets.push(d);
        }
        if acc.stopped() {
            break;
        }
    }
    acc.done()?;
    let version = match p.dds.iter().position(|d| d.tag == VERSION) {
        Some(i) => {
            let v = p.header(i)?;
            let mut r = Rd::new(&v);
            match (r.u32(), r.u32(), r.u32()) {
                (Some(major), Some(minor), Some(release)) => {
                    let rest = &v[12..];
                    json!({"major": major, "minor": minor, "release": release,
                           "text": text_json(&rest[..rest.iter().position(|&c| c == 0).unwrap_or(rest.len())])})
                }
                _ => J::Null,
            }
        }
        None => J::Null,
    };
    // the DFSD interface's datasets, which a file the SD interface did not write holds
    let ndgs: Vec<u16> = p.dds.iter().filter(|d| d.tag == NDG).map(|d| d.rf).collect();
    Ok(json!({"descriptors": p.dds.len(), "version": version, "vgroups": vgroups, "vdatas": vdatas, "datasets": datasets, "ndgs": ndgs}))
}

/// The Vdata of ref `r`, header `h`: its header's fields and its records' storage; of an attribute
/// (class `Attr0.0`), the bytes of its value and the value decoded, where the profile reads them.
fn vdata(p: &mut Pass, r: u16, h: &Vh) -> R<J> {
    let fields: Vec<J> = h.fields.iter().map(|(name, ty, isize, offset, order)| {
        json!({"name": text_json(name), "type": ty, "size": isize, "offset": offset, "order": order})
    }).collect();
    let mut d = json!({
        "ref": r, "name": text_json(&h.name), "class": text_json(&h.class), "version": h.version,
        "interlace": h.interlace, "records": h.nvert, "record_size": h.ivsize, "fields": fields,
        "attributes": h.attrs.iter().map(|a| json!([a.0, a.1, a.2])).collect::<Vec<_>>(),
        "data": storage(p, VS, r)?,
    });
    if h.class == ATTRIBUTE
        && let Some((bytes, value)) = attribute_value(p, r, h)?
    {
        d["bytes"] = json!(hex(&bytes));
        d["value"] = value;
    }
    Ok(d)
}

/// An attribute's bytes and its value, where the profile reads them (spec/zarrify/hdf4.md §7
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf4.md#7-attributes>): of one field,
/// not interlaced, of at most `attribute_bytes`, its records stored as they are. The value is a
/// `char8` field's text, all of it, NULs too; a number type's numbers, in a list; or null for
/// another type.
fn attribute_value(p: &mut Pass, r: u16, h: &Vh) -> R<Option<(Vec<u8>, J)>> {
    let [(_, ty, isize, _, order)] = h.fields[..] else { return Ok(None) };
    let total = isize as u64 * h.nvert as u64;
    if total > p.limit("attribute_bytes") || h.interlace != 0 || ty & !0x40ff != 0 {
        return Ok(None);
    }
    let l = match p.stored(VS, r)? {
        Stored::Plain(l) if l.len >= total => l,
        Stored::None if total == 0 => super::Logical::default(),
        _ => return Ok(None),
    };
    let b = p.read(&l.prefix(total))?;
    let (code, big) = ((ty & 0xff) as u8, ty & 0x4000 == 0);
    let value = match code {
        4 => text_json(&b),
        c => match super::numeric(c) {
            Some((_, item, _)) if isize as u64 == item * order as u64 => J::Array(b.chunks(item as usize).map(|v| super::number_json(c, v, big)).collect()),
            _ => J::Null,
        },
    };
    Ok(Some((b, value)))
}

/// How the element `tag` `rf` is stored, as the description gives it: null where no descriptor
/// describes it.
fn storage(p: &mut Pass, tag: u16, rf: u16) -> R<J> {
    let plain = p.by.contains_key(&(tag, rf));
    let st = p.stored(tag, rf)?;
    stored_json(p, st, !plain, true)
}

fn stored_json(p: &mut Pass, st: Stored, linked: bool, chunks: bool) -> R<J> {
    Ok(match st {
        Stored::None => J::Null,
        Stored::Plain(l) => json!({"kind": "plain", "length": l.len, "linked": linked, "segments": l.json()}),
        Stored::Compressed { length, method, model, data } => {
            json!({"kind": "compressed", "length": length, "method": method, "model": model, "segments": data.json()})
        }
        Stored::Chunked(i) if chunks => chunked(p, i)?,
        Stored::Chunked(_) => json!({"kind": "chunked"}),
        Stored::External => json!({"kind": "external"}),
        Stored::Other(code) => json!({"kind": "special", "code": code}),
    })
}

/// A chunked element `i`: its header (its length, chunk table, dimensions and fill value) and every
/// chunk its table places, each its coordinates in the grid and how it is stored. A table whose
/// records are not a chunk's, more than the budget, a chunk outside the grid or placed twice, or one
/// no descriptor describes, rejects the file.
fn chunked(p: &mut Pass, i: usize) -> R<J> {
    let Special::Chunked(h) = p.special(i)? else { return Err(Fail::Bad("internal: a chunked element's header is not one".into())) };
    let what = p.what(i);
    let dims: Vec<J> = h.dims.iter().map(|d| json!({"flag": d.0, "length": d.1, "chunk": d.2})).collect();
    if h.dims.iter().any(|d| d.2 == 0) {
        return bad(format!("the chunked element {what} has a chunk length of 0"));
    }
    let grid: Vec<u64> = h.dims.iter().map(|d| d.1.div_ceil(d.2)).collect();
    let Some(vh) = p_vh(p, h.table)? else {
        return bad(format!("the chunk table ref {} of {what} has no Vdata header", h.table));
    };
    let rank = h.dims.len();
    let record = 4 * rank as u64 + 4;
    if vh.ivsize as u64 != record {
        return bad(format!("the chunk table ref {} of {what} has records of {} bytes, not {record}", h.table, vh.ivsize));
    }
    let rows = vh.nvert as u64;
    if rows > p.limit("max_chunks") {
        return bad(format!("budget: the chunk table ref {} of {what} has more than {} chunks", h.table, p.limit("max_chunks")));
    }
    if rows * record > p.limit("chunk_table_bytes") {
        return bad(format!("budget: the chunk table ref {} of {what} is more than {} bytes", h.table, p.limit("chunk_table_bytes")));
    }
    let table = match p.stored(VS, h.table)? {
        Stored::Plain(l) if l.len >= rows * record => p.read(&l.prefix(rows * record))?,
        Stored::None if rows == 0 => Vec::new(),
        _ => return bad(format!("the chunk table ref {} of {what} holds fewer than its {rows} records", h.table)),
    };
    let mut seen = HashSet::new();
    let mut chunks = Vec::with_capacity(rows as usize);
    let mut acc = Acc::default();
    for k in 0..rows as usize {
        let mut r = Rd::new(&table[k * record as usize..(k + 1) * record as usize]);
        let at: Vec<u64> = (0..rank).map(|_| r.u32().unwrap_or(0) as u64).collect();
        let (ct, cr) = (r.u16().unwrap_or(0), r.u16().unwrap_or(0));
        if at.iter().zip(&grid).any(|(a, g)| a >= g) {
            return bad(format!("the chunk table of {what} places a chunk at {at:?}, outside its grid {grid:?}"));
        }
        if !seen.insert(at.clone()) {
            return bad(format!("the chunk table of {what} places two chunks at {at:?}"));
        }
        let plain = p.by.contains_key(&(ct, cr));
        let Some(st) = acc.take(p.stored(ct, cr)) else {
            if acc.stopped() {
                break;
            }
            continue;
        };
        if matches!(st, Stored::None) {
            return bad(format!("the chunk at {at:?} of {what} names tag {ct} ref {cr}, which no descriptor describes"));
        }
        chunks.push(json!({"at": at, "tag": ct, "ref": cr, "data": stored_json(p, st, !plain, false)?}));
    }
    acc.done()?;
    Ok(json!({"kind": "chunked", "length": h.length, "table": h.table, "dimensions": dims, "fill": hex(&h.fill), "chunks": chunks}))
}

/// The Vdata header of ref `r`, parsed from its element, or None where no descriptor describes it.
fn p_vh(p: &mut Pass, r: u16) -> R<Option<Vh>> {
    let Some(&i) = p.by.get(&(super::VH, r)) else { return Ok(None) };
    let b = p.header(i)?;
    Ok(super::parse_vh(&b))
}
