//! The zip form of a SAFE product (spec/zarrify/safe.md §8.4
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safe.md#84-the-zip-form>): the ZIP
//! archive's end records, its central directory, its local headers, and its deflated XML
//! documents. The functions are pure: the parser reads the bytes they name.

use crate::fed::{Word, uint};
use crate::rules::{self, Rules, flag, num, text};

pub const EOCD: &[u8; 4] = b"PK\x05\x06";
pub const ZIP64_LOCATOR: &[u8; 4] = b"PK\x06\x07";
pub const ZIP64_EOCD: &[u8; 4] = b"PK\x06\x06";
pub const CENTRAL: &[u8; 4] = b"PK\x01\x02";
pub const LOCAL: &[u8; 4] = b"PK\x03\x04";
/// an EOCD record (22 bytes) and the longest comment
pub const MAX_TAIL: u64 = 65557;
/// the largest deflated XML document
pub const MAX_DEFLATED: u64 = 1 << 26;
/// the most bytes of all the deflated XML documents together
pub const MAX_INFLATED: u64 = 1 << 27;

/// A central directory file header, as §8.4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safe.md#84-the-zip-form> reads it, with where it is.
#[derive(Clone, Debug, Default)]
pub struct Entry {
    pub name: String,
    pub flags: u64,
    pub method: u64,
    pub crc: u32,
    /// compressed size
    pub cs: u64,
    /// uncompressed size
    pub us: u64,
    /// local header offset
    pub lho: u64,
    /// data start, from the local header
    pub ds: u64,
    /// the local header's 30 bytes, and its name and extra lengths
    pub local: Vec<u8>,
    pub local_nn: u64,
    pub local_ne: u64,
    /// the header's offset in the file, and its name, extra and comment lengths
    pub at: u64,
    pub nn: u64,
    pub ne: u64,
    pub nc: u64,
}

/// The end records: where the EOCD record is, its comment's length, and the ZIP64 records'
/// offsets when present.
#[derive(Clone, Debug, Default)]
pub struct End {
    pub eocd: u64,
    pub comment: u64,
    pub count: u64,
    pub cd_size: u64,
    pub cd_offset: u64,
    /// (locator offset, record offset), when the zip file is ZIP64
    pub zip64: Option<(u64, u64)>,
}

/// The EOCD record in the file's last `tail.len()` bytes (file of `n` bytes): the last
/// occurrence of its signature whose record and comment end exactly at the end of the file.
pub fn eocd(rules: &Rules, tail: &[u8], n: u64) -> Result<End, String> {
    let mut at = tail.len();
    let found = loop {
        let Some(k) = tail[..at].windows(4).rposition(|w| w == EOCD) else { break None };
        if k + 22 <= tail.len() && k + 22 + uint(tail, k + 20, 2, true) as usize == tail.len() {
            break Some(k);
        }
        at = k;
    };
    let k = found.ok_or("not a zip file: no end of central directory record")?;
    let eocd = n - tail.len() as u64 + k as u64;
    let (disk, cd_disk, here, count, cd_size, cd_offset) =
        (uint(tail, k + 4, 2, true), uint(tail, k + 6, 2, true), uint(tail, k + 8, 2, true), uint(tail, k + 10, 2, true), uint(tail, k + 12, 4, true), uint(tail, k + 16, 4, true));
    rules.check("zip_end", &mut rules::env([("disks", flag(disk == 0 && cd_disk == 0)), ("counts", flag(here == count))]))?;
    let zip64 = count == 0xFFFF || cd_size == 0xFFFF_FFFF || cd_offset == 0xFFFF_FFFF;
    if zip64 && eocd < 20 {
        return Err("a ZIP64 zip file has no ZIP64 end of central directory locator".into());
    }
    Ok(End { eocd, comment: uint(tail, k + 20, 2, true), count, cd_size, cd_offset, zip64: zip64.then_some((eocd - 20, 0)) })
}

/// The ZIP64 locator's record offset.
pub fn zip64_locator(loc: &[u8], end: &End) -> Result<u64, String> {
    let (rec, disks) = (u64::le_at(loc, 8), uint(loc, 16, 4, true));
    if &loc[0..4] != ZIP64_LOCATOR || uint(loc, 4, 4, true) != 0 || disks != 1 {
        return Err("a ZIP64 zip file has no valid ZIP64 end of central directory locator".into());
    }
    if rec.checked_add(56).is_none_or(|e| e > end.eocd - 20) {
        return Err("a zip file's ZIP64 end of central directory record is outside it".into());
    }
    Ok(rec)
}

/// The ZIP64 record's counts (`end` updated).
pub fn zip64_record(rules: &Rules, body: &[u8], end: &mut End, rec: u64) -> Result<(), String> {
    if &body[0..4] != ZIP64_EOCD {
        return Err("a zip file has no ZIP64 end of central directory record where its locator says".into());
    }
    let (d1, d2, here, count) = (uint(body, 16, 4, true), uint(body, 20, 4, true), u64::le_at(body, 24), u64::le_at(body, 32));
    rules.check("zip_end", &mut rules::env([("disks", flag(d1 == 0 && d2 == 0)), ("counts", flag(here == count))]))?;
    end.count = count;
    end.cd_size = u64::le_at(body, 40);
    end.cd_offset = u64::le_at(body, 48);
    end.zip64 = end.zip64.map(|(l, _)| (l, rec));
    Ok(())
}

/// Where the central directory must end: the ZIP64 record's start, or the EOCD's.
pub fn cd_end(end: &End) -> u64 {
    end.zip64.map(|z| z.1).unwrap_or(end.eocd)
}

/// The first `wanted` values of the ZIP64 extended information extra field.
fn zip64_extra(extra: &[u8], wanted: usize) -> Result<Vec<u64>, String> {
    let mut p = 0;
    while p + 4 <= extra.len() {
        let (hid, size) = (uint(extra, p, 2, true), uint(extra, p + 2, 2, true) as usize);
        if p + 4 + size > extra.len() {
            break;
        }
        if hid == 1 {
            if size < 8 * wanted {
                return Err("a zip entry's ZIP64 extra field is too short".into());
            }
            return Ok((0..wanted).map(|k| u64::le_at(extra, p + 4 + 8 * k)).collect());
        }
        p += 4 + size;
    }
    Err("a zip entry needs a ZIP64 extra field and has none".into())
}

/// The entries of the central directory `cd` (at `cd_offset`).
pub fn central_directory(rules: &Rules, cd: &[u8], end: &End) -> Result<Vec<Entry>, String> {
    let mut out = Vec::new();
    let mut p = 0usize;
    for _ in 0..end.count {
        if p + 46 > cd.len() || &cd[p..p + 4] != CENTRAL {
            return Err("a zip file's central directory has fewer entries than it says".into());
        }
        let (flags, method, crc, mut cs, mut us) = (uint(cd, p + 8, 2, true), uint(cd, p + 10, 2, true), uint(cd, p + 16, 4, true) as u32, uint(cd, p + 20, 4, true), uint(cd, p + 24, 4, true));
        let (nn, ne, nc, disk, mut lho) = (uint(cd, p + 28, 2, true), uint(cd, p + 30, 2, true), uint(cd, p + 32, 2, true), uint(cd, p + 34, 2, true), uint(cd, p + 42, 4, true));
        let len = 46 + (nn + ne + nc) as usize;
        if p + len > cd.len() {
            return Err("a zip file's central directory entry is truncated".into());
        }
        let raw = &cd[p + 46..p + 46 + nn as usize];
        let extra = &cd[p + 46 + nn as usize..p + 46 + (nn + ne) as usize];
        let wanted = [us == 0xFFFF_FFFF, cs == 0xFFFF_FFFF, lho == 0xFFFF_FFFF];
        let k = wanted.iter().filter(|&&w| w).count();
        if k > 0 {
            let mut values = zip64_extra(extra, k)?.into_iter();
            if wanted[0] {
                us = values.next().unwrap();
            }
            if wanted[1] {
                cs = values.next().unwrap();
            }
            if wanted[2] {
                lho = values.next().unwrap();
            }
        }
        let name = std::str::from_utf8(raw).ok();
        rules.check("zip_entry", &mut rules::env([("disk", num(disk as f64)), ("encrypted", flag(flags & 0x41 != 0)),
            ("utf8", flag(name.is_some()))]))?;
        out.push(Entry { name: name.unwrap().to_string(), flags, method, crc, cs, us, lho, at: end.cd_offset + p as u64, nn, ne, nc, ..Default::default() });
        p += len;
    }
    if p != cd.len() {
        return Err("a zip file's central directory has more bytes than its entries".into());
    }
    Ok(out)
}

/// The product's objects (indexes into `entries`, by key), the recorded keys of the
/// directory entries, and the keys of every directory entry but the root's.
pub struct Product {
    pub root: String,
    pub objects: Vec<(String, usize)>,
    pub ignored: Vec<String>,
    pub dirs: Vec<String>,
}

pub fn product_entries(entries: &[Entry]) -> Result<Product, String> {
    let mut names = std::collections::HashSet::new();
    let mut root: Option<String> = None;
    let (mut objects, mut ignored, mut dirs) = (Vec::new(), Vec::new(), Vec::new());
    for (i, e) in entries.iter().enumerate() {
        let short: String = e.name.chars().take(200).collect();
        if !names.insert(e.name.as_str()) {
            return Err(format!("a zip file has two entries named {short:?}"));
        }
        let (head, key) = match e.name.split_once('/') {
            Some((h, k)) if h.ends_with(".SAFE") => (h, k),
            _ => return Err(format!("a zip entry {short:?} is not under a .SAFE directory")),
        };
        match &root {
            None => root = Some(head.to_string()),
            Some(r) if r != head => return Err("a zip file has entries under two root directories".into()),
            _ => {}
        }
        if key.is_empty() || key.ends_with('/') {
            if e.us != 0 {
                ignored.push(key.to_string());
            }
            if !key.is_empty() {
                dirs.push(key.to_string());
            }
            continue;
        }
        if key.contains('\\') || key.split('/').any(|s| s.is_empty() || s == "." || s == "..") {
            return Err(format!("a zip entry's name {short:?} has an empty, . or .. segment, or a backslash"));
        }
        objects.push((key.to_string(), i));
    }
    let root = root.ok_or("an empty zip file")?;
    Ok(Product { root, objects, ignored, dirs })
}

/// The method checks of the objects (§8.4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safe.md#84-the-zip-form>, Methods), before anything is read: which are
/// deflated (an XML document each).
pub fn methods(rules: &Rules, entries: &[Entry], objects: &[(String, usize)], is_xml: &dyn Fn(&str) -> bool) -> Result<Vec<bool>, String> {
    let mut deflated = vec![false; entries.len()];
    let mut total = 0u64;
    for (k, i) in objects {
        let e = &entries[*i];
        if e.us == 0 {
            continue;
        }
        let short: String = k.chars().take(200).collect();
        rules.check("zip_method", &mut rules::env([("key", text(&short)), ("method", num(e.method as f64)),
            ("same_size", flag(e.cs == e.us)), ("xml", flag(is_xml(k))), ("size", num(e.us as f64))]))?;
        if e.method == 8 {
            deflated[*i] = true;
            total = total.saturating_add(e.us);
        }
    }
    rules.check("zip_inflated", &mut rules::env([("total", num(total as f64))]))?;
    Ok(deflated)
}

/// A local header's data start (`e` updated), checked against the central directory's offset.
pub fn local_header(head: &[u8], e: &mut Entry, cd_offset: u64) -> Result<(), String> {
    if &head[0..4] != LOCAL {
        return Err("a zip entry has no local header where the central directory says".into());
    }
    e.local = head[..30].to_vec();
    e.local_nn = uint(head, 26, 2, true);
    e.local_ne = uint(head, 28, 2, true);
    e.ds = e.lho + 30 + e.local_nn + e.local_ne;
    if e.ds.checked_add(e.cs).is_none_or(|x| x > cd_offset) {
        return Err("a zip entry's data reaches beyond the entries".into());
    }
    Ok(())
}

/// No two objects' local headers and data overlap.
pub fn disjoint(entries: &[Entry], read: &[usize]) -> Result<(), String> {
    let mut v: Vec<&Entry> = read.iter().map(|&i| &entries[i]).collect();
    v.sort_by_key(|e| e.lho);
    let mut end = 0;
    for e in v {
        if e.lho < end {
            return Err("two zip entries' local headers and data overlap".into());
        }
        end = e.ds + e.cs;
    }
    Ok(())
}

/// A deflated entry's bytes: the raw deflate stream must end exactly at its compressed size
/// and give exactly its size, with its CRC-32.
pub fn inflate(data: &[u8], e: &Entry) -> Result<Vec<u8>, String> {
    use miniz_oxide::inflate::stream::{InflateState, inflate as step};
    use miniz_oxide::{DataFormat, MZFlush, MZStatus};
    let mut st = InflateState::new_boxed(DataFormat::Raw);
    let mut out = vec![0u8; e.us as usize + 1];
    let r = step(&mut st, data, &mut out, MZFlush::Finish);
    let bad = || "a zip entry's deflate stream does not end at its compressed size with its size".to_string();
    match r.status {
        Ok(MZStatus::StreamEnd) => {}
        Ok(_) => return Err(bad()),
        Err(e) => return Err(format!("a zip entry's deflate stream is invalid: {e:?}")),
    }
    if r.bytes_consumed != data.len() || r.bytes_written as u64 != e.us {
        return Err(bad());
    }
    out.truncate(r.bytes_written);
    if crc32(&out) != e.crc {
        return Err("a zip entry's CRC-32 does not match its bytes".into());
    }
    Ok(out)
}

/// CRC-32 (ISO 3309, as zip uses it).
pub fn crc32(b: &[u8]) -> u32 {
    static TABLE: std::sync::OnceLock<[u32; 256]> = std::sync::OnceLock::new();
    let t = TABLE.get_or_init(|| {
        let mut t = [0u32; 256];
        for (i, x) in t.iter_mut().enumerate() {
            let mut c = i as u32;
            for _ in 0..8 {
                c = if c & 1 == 1 { 0xEDB8_8320 ^ (c >> 1) } else { c >> 1 };
            }
            *x = c;
        }
        t
    });
    let mut c = !0u32;
    for &x in b {
        c = t[((c ^ x as u32) & 0xFF) as usize] ^ (c >> 8);
    }
    !c
}
