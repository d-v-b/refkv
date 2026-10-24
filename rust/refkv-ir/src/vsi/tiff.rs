//! The TIFF side of a `.vsi` file (spec/virtualize/vsi.md §2.3
//! <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/vsi.md#23-the-tiff-images>): a
//! little-endian classic TIFF whose IFDs (the macro image, each stack's coarse thumbnail, a
//! focus map) lie inside the metadata tree's TIFF regions. The chain is read from the whole
//! file; each IFD's elements (its entries, out-of-line values, strips and EXIF IFD) are
//! emitted under the field whose region holds it, and what the region has left is gaps.

use crate::fed::Word;
use crate::ir::{GAP, Ir, NO_INDEX, Res};
use crate::rules::{self, Rules, num};
use serde_json::json;
use std::collections::HashMap;

/// The Adobe APP14 marker that tells a decoder a 3-component stream is RGB (transform 0),
/// as the TIFF profile writes it before a JPEG tile's tables.
pub const ADOBE: [u8; 15] = [0xFF, 0xEE, 0x00, 0x0E, b'A', b'd', b'o', b'b', b'e', 0x00, 0x64, 0x00, 0x00, 0x00, 0x00];

#[derive(Clone, Debug)]
pub struct Entry {
    pub tag: u16,
    pub typ: u16,
    pub count: u32,
    /// the 4-byte value field
    pub field: [u8; 4],
    /// the value's bytes: None for an unknown type
    pub nbytes: Option<u64>,
    pub inline: bool,
    /// the value's offset (0 when inline)
    pub off: u64,
}

#[derive(Clone, Debug)]
pub struct Ifd {
    pub offset: u64,
    /// the end of the entries and the next-IFD word
    pub end: u64,
    pub entries: Vec<Entry>,
    pub next: u32,
    /// the main chain's index, or the index of the IFD whose EXIF IFD this is
    pub index: usize,
    pub exif: bool,
}

pub fn type_size(typ: u16) -> Option<u64> {
    Some(match typ {
        1 | 2 | 6 | 7 => 1,
        3 | 8 => 2,
        4 | 9 | 11 | 13 => 4,
        5 | 10 | 12 | 16 | 17 | 18 => 8,
        _ => return None,
    })
}

fn value_type(typ: u16, count: u32) -> String {
    match typ {
        1 => format!("u1[{count}]"),
        6 => format!("i1[{count}]"),
        2 => format!("ascii[{count}]"),
        7 => format!("bytes[{count}]"),
        5 => format!("<u4[{count},2]"),
        10 => format!("<i4[{count},2]"),
        3 => format!("<u2[{count}]"),
        8 => format!("<i2[{count}]"),
        4 | 13 => format!("<u4[{count}]"),
        9 => format!("<i4[{count}]"),
        11 => format!("<f4[{count}]"),
        12 => format!("<f8[{count}]"),
        16 => format!("<u8[{count}]"),
        17 => format!("<i8[{count}]"),
        _ => format!("bytes[{}]", count as u64 * type_size(typ).unwrap_or(1)),
    }
}

impl Ifd {
    pub fn entry(&self, tag: u16) -> Option<&Entry> {
        self.entries.iter().find(|e| e.tag == tag)
    }

    /// The unsigned integer values of `tag` (types BYTE, SHORT, LONG), from the file.
    pub fn ints(&self, b: &[u8], tag: u16) -> Option<Vec<u64>> {
        let e = self.entry(tag)?;
        let w = match e.typ {
            1 => 1,
            3 => 2,
            4 | 13 => 4,
            _ => return None,
        };
        let n = e.nbytes? as usize;
        let d: &[u8] = if e.inline { &e.field[..n] } else { b.get(e.off as usize..e.off as usize + n)? };
        Some(d.chunks_exact(w).map(|c| match w {
            1 => c[0] as u64,
            2 => u16::le_at(c, 0) as u64,
            _ => u32::le_at(c, 0) as u64,
        }).collect())
    }

    pub fn int(&self, b: &[u8], tag: u16) -> Option<u64> {
        self.ints(b, tag)?.first().copied()
    }

    /// The bytes of `tag`'s value (any type), from the file.
    pub fn bytes<'a>(&self, b: &'a [u8], tag: u16) -> Option<&'a [u8]> {
        let e = self.entry(tag)?;
        let n = e.nbytes? as usize;
        if e.inline {
            // the inline bytes live in the entry itself
            let at = self.offset as usize + 2 + self.entries.iter().position(|x| std::ptr::eq(x, e))? * 12 + 8;
            b.get(at..at + n)
        } else {
            b.get(e.off as usize..e.off as usize + n)
        }
    }
}

/// Reads the IFD at `o` (the file's bytes `b`), checking it.
fn read_ifd(rules: &Rules, b: &[u8], o: u64, index: usize, exif: bool, seen: usize) -> Res<Ifd> {
    let size = b.len() as u64;
    let count = if o + 2 <= size { u16::le_at(b, o as usize) as u64 } else { 0 };
    rules.check("ifd", &mut rules::env([("offset", num(o as f64)), ("count", num(count as f64)), ("size", num(size as f64)), ("ifds", num(seen as f64))]))?;
    let mut entries = Vec::with_capacity(count as usize);
    for i in 0..count as usize {
        let at = o as usize + 2 + 12 * i;
        let tag = u16::le_at(b, at);
        let typ = u16::le_at(b, at + 2);
        let n = u32::le_at(b, at + 4);
        let field: [u8; 4] = b[at + 8..at + 12].try_into().unwrap();
        let nbytes = type_size(typ).map(|z| z * n as u64);
        let inline = nbytes.is_some_and(|x| x <= 4);
        let off = if inline { 0 } else { u32::le_at(&field, 0) as u64 };
        entries.push(Entry { tag, typ, count: n, field, nbytes, inline, off });
    }
    let end = o + 2 + 12 * count + 4;
    let next = u32::le_at(b, (end - 4) as usize);
    Ok(Ifd { offset: o, end, entries, next, index, exif })
}

/// The main chain of IFDs, each followed by its EXIF IFD when it has one (tag 34665).
pub fn chain(rules: &Rules, b: &[u8], first: u64) -> Res<Vec<Ifd>> {
    let mut out: Vec<Ifd> = Vec::new();
    let mut at: HashMap<u64, ()> = HashMap::new();
    let mut o = first;
    let mut index = 0;
    while o != 0 {
        if at.insert(o, ()).is_some() {
            return Err(format!("the IFD chain of the .vsi file returns to the IFD at {o}"));
        }
        let ifd = read_ifd(rules, b, o, index, false, out.len())?;
        o = ifd.next as u64;
        let exif = ifd.entry(34665).filter(|e| e.typ == 4 || e.typ == 13 || e.typ == 3).map(|e| u32::le_at(&e.field, 0) as u64);
        out.push(ifd);
        if let Some(x) = exif.filter(|&x| x != 0) {
            if at.insert(x, ()).is_some() {
                return Err(format!("the EXIF IFD of IFD {index} is an IFD read already (at {x})"));
            }
            out.push(read_ifd(rules, b, x, index, true, out.len())?);
        }
        index += 1;
    }
    Ok(out)
}

/// What the projection reads of an IFD image (§4.3
/// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/vsi.md#43-the-macro-image>): its
/// size, samples, bits, compression, and its strips (offset, length) with the JPEG prefix.
#[derive(Clone, Debug)]
pub struct Image {
    pub index: usize,
    pub width: u64,
    pub height: u64,
    pub samples: u64,
    pub bits: u64,
    pub compression: u64,
    pub photometric: u64,
    pub strips: Vec<(u64, u64)>,
    /// the shared JPEG prefix (the Adobe marker for 3 samples, then the tables' body), when the
    /// IFD has JPEGTables
    pub prefix: Vec<u8>,
}

/// The segments (offset, length) of `offsets`/`counts` tags, in order.
fn segments(ifd: &Ifd, b: &[u8], ot: u16, ct: u16) -> Option<Vec<(u64, u64)>> {
    let o = ifd.ints(b, ot)?;
    let c = ifd.ints(b, ct)?;
    if o.len() != c.len() {
        return None;
    }
    Some(o.into_iter().zip(c).collect())
}

/// The JPEG prefix of an IFD (its JPEGTables between SOI and EOI, after the Adobe marker for 3
/// samples), or empty.
fn jpeg_prefix(ifd: &Ifd, b: &[u8], samples: u64, photometric: u64) -> Vec<u8> {
    let mut p = Vec::new();
    if ifd.int(b, 259) != Some(7) {
        return p;
    }
    if samples == 3 {
        p.extend_from_slice(&ADOBE);
        p.push(if photometric == 2 { 0 } else { 1 });
    }
    if let Some(t) = ifd.bytes(b, 347)
        && t.len() >= 4 && t[..2] == [0xFF, 0xD8] && t[t.len() - 2..] == [0xFF, 0xD9]
    {
        p.extend_from_slice(&t[2..t.len() - 2]);
    }
    p
}

/// IFD `index` of the chain as an image the layout projects, checked (`ifd_image`): `what` names
/// it in a rejection.
pub fn image(rules: &Rules, b: &[u8], ifd: &Ifd, what: &str) -> Res<Image> {
    let g = |t: u16| ifd.int(b, t);
    let (width, height) = (g(256).unwrap_or(0), g(257).unwrap_or(0));
    let compression = g(259).unwrap_or(1);
    let samples = g(277).unwrap_or(1);
    let bits = ifd.ints(b, 258).and_then(|v| v.first().copied()).unwrap_or(1);
    let planar = g(284).unwrap_or(1);
    let format = ifd.ints(b, 339).and_then(|v| v.first().copied()).unwrap_or(1);
    let photometric = g(262).unwrap_or(if samples == 3 { 2 } else { 1 });
    let strips = segments(ifd, b, 273, 279).or_else(|| segments(ifd, b, 324, 325)).unwrap_or_default();
    rules.check(
        "ifd_image",
        &mut rules::env([
            ("ifd", num(ifd.index as f64)),
            ("what", rules::text(what)),
            ("width", num(width as f64)),
            ("height", num(height as f64)),
            ("compression", num(compression as f64)),
            ("samples", num(samples as f64)),
            ("bits", num(bits as f64)),
            ("planar", num(planar as f64)),
            ("strips", num(strips.len() as f64)),
            ("format", num(format as f64)),
        ]),
    )?;
    for &(o, n) in &strips {
        if n == 0 || o.checked_add(n).is_none_or(|e| e > b.len() as u64) {
            return Err(format!("IFD {} ({what}): a strip ({n} bytes at {o}) is outside the {}-byte .vsi file", ifd.index, b.len()));
        }
    }
    Ok(Image { index: ifd.index, width, height, samples, bits, compression, photometric, strips, prefix: jpeg_prefix(ifd, b, samples, photometric) })
}

/// Emits the IFDs whose bytes lie in the region `[start, end)` under `parent`, and gaps for
/// what the region has left: (the elements of each IFD's strips, by chain index).
pub fn emit_region(ir: &mut Ir, b: &[u8], ifds: &[Ifd], start: u64, end: u64, parent: u32, strips_of: &mut HashMap<usize, Vec<u32>>) -> Res<()> {
    let mut claimed: Vec<(u64, u64)> = Vec::new();
    for ifd in ifds.iter().filter(|f| start <= f.offset && f.end <= end) {
        let s = if ifd.exif {
            ir.struct_(parent, "exif/", ifd.index as u64, 0, Some((ifd.offset, ifd.end - ifd.offset)))?
        } else {
            ir.struct_(parent, "ifds/", ifd.index as u64, 0, Some((ifd.offset, ifd.end - ifd.offset)))?
        };
        let o = ifd.offset;
        ir.value(s, "entry_count", NO_INDEX, "<u2", 0, (o, 2), Some(&b[o as usize..o as usize + 2]))?;
        claimed.push((o, 2));
        let mut dups: HashMap<u16, u32> = HashMap::new();
        for (i, e) in ifd.entries.iter().enumerate() {
            let ea = o + 2 + 12 * i as u64;
            let raw = &b[ea as usize..ea as usize + 12];
            let dup = *dups.get(&e.tag).unwrap_or(&0);
            dups.insert(e.tag, dup + 1);
            let t = if dup == 0 {
                ir.struct_(s, "tags/", e.tag as u64, 0, Some((ea, 12)))?
            } else {
                ir.struct_(s, &format!("tags/{}~{dup}", e.tag), NO_INDEX, 0, Some((ea, 12)))?
            };
            match e.nbytes {
                None => {
                    ir.value(t, "entry", NO_INDEX, "{tag:<u2,type:<u2,count:<u4,field:bytes[4]}", 0, (ea, 12), Some(raw))?;
                }
                Some(nb) if e.inline => {
                    ir.value(t, "entry", NO_INDEX, "{tag:<u2,type:<u2,count:<u4}", 0, (ea, 8), Some(&raw[..8]))?;
                    let vt = value_type(e.typ, e.count);
                    if nb > 0 {
                        ir.value(t, "value", NO_INDEX, &vt, 0, (ea + 8, nb), Some(&raw[8..8 + nb as usize]))?;
                    }
                    if nb < 4 {
                        ir.value(t, "padding", NO_INDEX, &format!("bytes[{}]", 4 - nb), 0, (ea + 8 + nb, 4 - nb), Some(&raw[8 + nb as usize..12]))?;
                    }
                }
                Some(nb) => {
                    ir.value(t, "entry", NO_INDEX, "{tag:<u2,type:<u2,count:<u4,offset:<u4}", 0, (ea, 12), Some(raw))?;
                    if nb > 0 && e.off.checked_add(nb).is_some_and(|x| x <= b.len() as u64) {
                        let vt = value_type(e.typ, e.count);
                        ir.value(t, "value", NO_INDEX, &vt, 0, (e.off, nb), Some(&b[e.off as usize..(e.off + nb) as usize]))?;
                        if start <= e.off && e.off + nb <= end {
                            claimed.push((e.off, nb));
                        }
                    }
                }
            }
            claimed.push((ea, 12));
        }
        let na = ifd.end - 4;
        ir.value(s, "next_ifd", NO_INDEX, "<u4", 0, (na, 4), Some(&b[na as usize..na as usize + 4]))?;
        claimed.push((na, 4));
        // the strips (or tiles), as data elements
        if !ifd.exif {
            let samples = ifd.int(b, 277).unwrap_or(1);
            let photometric = ifd.int(b, 262).unwrap_or(if samples == 3 { 2 } else { 1 });
            let prefix = jpeg_prefix(ifd, b, samples, photometric);
            for (name, ot, ct) in [("strips/", 273u16, 279u16), ("tiles/", 324, 325)] {
                let Some(segs) = segments(ifd, b, ot, ct) else { continue };
                let rows = if ot == 273 { ifd.int(b, 278).or(ifd.int(b, 257)) } else { ifd.int(b, 323) };
                let cols = if ot == 273 { ifd.int(b, 256) } else { ifd.int(b, 322) };
                let base = json!({
                    "geometry": {"shape": [rows, cols], "samples": samples, "bits": ifd.ints(b, 258).and_then(|v| v.first().copied())},
                    "codec": {"compression": ifd.int(b, 259).unwrap_or(1)},
                });
                let shared = if prefix.is_empty() { None } else { Some(ir.share(&prefix)) };
                let plain = {
                    let mut j = base.clone();
                    j["recipe"] = json!([["src", 0, null]]);
                    j.to_string()
                };
                let framed = shared.map(|x| {
                    let mut j = base.clone();
                    j["recipe"] = json!([["src", 0, 2], ["shared", x], ["src", 2, null]]);
                    j.to_string()
                });
                let mut els = Vec::with_capacity(segs.len());
                for (k, &(so, sn)) in segs.iter().enumerate() {
                    if sn == 0 || so.checked_add(sn).is_none_or(|x| x > b.len() as u64) {
                        continue;
                    }
                    let form = match &framed {
                        Some(f) if sn > 2 => f.as_str(),
                        _ => plain.as_str(),
                    };
                    let d = ir.data(s, name, k as u64, 0, (so, sn), form)?;
                    els.push(d);
                    if start <= so && so + sn <= end {
                        claimed.push((so, sn));
                    }
                }
                if ot == 273 || !els.is_empty() {
                    strips_of.entry(ifd.index).or_default().extend(els);
                }
            }
        }
    }
    claimed.sort_unstable();
    let mut pos = start;
    for (o, n) in claimed {
        if o > pos {
            ir.add(GAP, parent, "gaps/", pos, "", 0, Some((pos, o - pos)))?;
        }
        pos = pos.max(o + n);
    }
    if end > pos {
        ir.add(GAP, parent, "gaps/", pos, "", 0, Some((pos, end - pos)))?;
    }
    Ok(())
}
