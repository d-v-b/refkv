//! A DICOM file's data elements (spec/virtualize/dicom.md §7.1
//! <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/dicom.md#71-elements>–§7.3
//! <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/dicom.md#73-datasets-and-sequences>):
//! the bytes a parser was fed, element headers, and the profile's walk, which rejects.

use crate::fed::{Fed, Overlap, Word};
use std::collections::HashMap;

pub const UNDEFINED: u32 = 0xFFFF_FFFF;
pub const ITEM: u32 = 0xFFFE_E000;
pub const ITEM_END: u32 = 0xFFFE_E00D;
pub const SEQUENCE_END: u32 = 0xFFFE_E0DD;
pub const PIXEL_DATA: u32 = 0x7FE0_0010;
/// The sequences of defined length the profile walks: Shared Functional Groups and
/// Pixel Measures (spec/virtualize/dicom.md §7.3
/// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/dicom.md#73-datasets-and-sequences>, rule 3).
pub const WALKED: [u32; 2] = [0x5200_9229, 0x0028_9110];
pub const MAX_DEPTH: u32 = 64;

pub const LONG_VRS: [&str; 13] = ["OB", "OD", "OF", "OL", "OV", "OW", "SQ", "SV", "UC", "UN", "UR", "UT", "UV"];
pub const SHORT_VRS: [&str; 21] =
    ["AE", "AS", "AT", "CS", "DA", "DS", "DT", "FD", "FL", "IS", "LO", "LT", "PN", "SH", "SL", "SS", "ST", "TM", "UI", "UL", "US"];

/// A VR, two ASCII letters.
pub type Vr = [u8; 2];

pub fn vr_str(v: &Vr) -> &str {
    std::str::from_utf8(v).unwrap_or("??")
}

pub fn tag_name(tag: u32) -> String {
    format!("({:04X},{:04X})", tag >> 16, tag & 0xFFFF)
}

pub fn tag_key(tag: u32) -> String {
    format!("{tag:08X}")
}

/// An encoding: explicit or implicit VR, little or big endian.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub struct Enc {
    pub explicit: bool,
    pub little: bool,
}

pub const IMPLICIT_LE: Enc = Enc { explicit: false, little: true };
pub const EXPLICIT_LE: Enc = Enc { explicit: true, little: true };

/// An element's header: where it starts (`at`), its tag, VR (None in implicit VR and
/// for group FFFE), where its value starts, its length (or UNDEFINED), and the byte order of
/// its value.
#[derive(Clone, Copy, Debug)]
pub struct El {
    pub at: u64,
    pub tag: u32,
    pub vr: Option<Vr>,
    pub value: u64,
    pub length: u32,
    pub little: bool,
}

impl El {
    pub fn undefined(&self) -> bool {
        self.length == UNDEFINED
    }
    /// The end of a value of defined length.
    pub fn end(&self) -> u64 {
        self.value + self.length as u64
    }
    pub fn vr_is(&self, s: &str) -> bool {
        self.vr.is_some_and(|v| vr_str(&v) == s)
    }
    pub fn vr_text(&self) -> String {
        self.vr.map(|v| vr_str(&v).to_string()).unwrap_or_else(|| "None".into())
    }
}

/// Why a walk stopped: the input is rejected, or bytes it needs were not fed.
#[derive(Clone, Debug, PartialEq)]
pub enum Fail {
    Reject(String),
    Miss(u64, u64),
}

pub type R<T> = Result<T, Fail>;

pub fn reject<T>(m: impl Into<String>) -> R<T> {
    Err(Fail::Reject(m.into()))
}

/// The bytes fed, as disjoint ranges (adjacent and overlapping ones merged), held until
/// the parse is done.
pub struct Bytes {
    fed: Fed,
    pub size: u64,
}

impl Bytes {
    pub fn new(size: u64) -> Bytes {
        Bytes { fed: Fed::new(Overlap::MergeKeep), size }
    }

    pub fn feed(&mut self, o: u64, data: Vec<u8>) {
        self.fed.feed(o, data)
    }

    pub fn held(&self) -> u64 {
        self.fed.held()
    }

    /// The bytes [o, o + n), or Miss.
    pub fn get(&self, o: u64, n: u64) -> R<&[u8]> {
        self.fed.get(o, n).ok_or(Fail::Miss(o, n))
    }

    pub fn has(&self, o: u64, n: u64) -> bool {
        self.fed.has(o, n)
    }

    /// Where the fed range holding `o` ends (o itself when none does).
    pub fn reach(&self, o: u64) -> u64 {
        self.fed.reach(o)
    }

    pub fn u16(&self, o: u64, little: bool) -> R<u16> {
        Ok(u16::at(self.get(o, 2)?, 0, little))
    }

    pub fn u32(&self, o: u64, little: bool) -> R<u32> {
        Ok(u32::at(self.get(o, 4)?, 0, little))
    }

    /// The element at `pos`, whose header must end by `end` (spec/virtualize/dicom.md §7.1
    /// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/dicom.md#71-elements>).
    pub fn header(&self, pos: u64, end: u64, enc: Enc) -> R<El> {
        if pos + 8 > end {
            return reject(format!("the element at {pos} runs past its container"));
        }
        let b = self.get(pos, 8)?;
        let le = enc.little;
        let (g, e) = (u16::at(b, 0, le), u16::at(b, 2, le));
        let tag = (g as u32) << 16 | e as u32;
        if g == 0xFFFE || !enc.explicit {
            let length = u32::at(b, 4, le);
            return Ok(El { at: pos, tag, vr: None, value: pos + 8, length, little: le });
        }
        let v = [b[4], b[5]];
        let s = std::str::from_utf8(&v).unwrap_or("");
        if LONG_VRS.contains(&s) {
            if pos + 12 > end {
                return reject(format!("the element at {pos} runs past its container"));
            }
            let length = self.u32(pos + 8, le)?;
            return Ok(El { at: pos, tag, vr: Some(v), value: pos + 12, length, little: le });
        }
        if !SHORT_VRS.contains(&s) {
            let shown: String = v
                .iter()
                .map(|&c| if (0x20..0x7f).contains(&c) && c != b'\'' && c != b'\\' { (c as char).to_string() } else { format!("\\x{c:02x}") })
                .collect();
            return reject(format!("unknown VR '{shown}' at {pos}"));
        }
        let length = u16::at(b, 6, le) as u32;
        Ok(El { at: pos, tag, vr: Some(v), value: pos + 8, length, little: le })
    }
}

/// A walked element: its header, and a walked sequence's items.
#[derive(Clone, Debug)]
pub struct Walked {
    pub el: El,
    pub items: Option<Vec<Dataset>>,
}

/// A dataset as the profile reads it: the first element of each tag.
pub type Dataset = HashMap<u32, Walked>;

/// The profile's walk (spec/virtualize/dicom.md §7.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/dicom.md#72-file>, §7.3
/// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/dicom.md#73-datasets-and-sequences>):
/// every element it walks is checked, and a structure that breaks a rule rejects.
pub struct Walker<'a> {
    pub b: &'a Bytes,
}

impl Walker<'_> {
    /// The File Meta Information, and where the dataset starts.
    pub fn meta(&self) -> R<(Dataset, u64)> {
        let size = self.b.size;
        let mut out = Dataset::new();
        let mut pos = 132;
        while pos + 2 <= size && self.b.u16(pos, true)? == 2 {
            let el = self.b.header(pos, size, EXPLICIT_LE)?;
            if el.undefined() {
                return reject(format!("meta element {} has an undefined length", tag_name(el.tag)));
            }
            if el.end() > size {
                return reject(format!("meta element {} runs past the end of the file", tag_name(el.tag)));
            }
            out.entry(el.tag).or_insert(Walked { el, items: None });
            pos = el.end();
        }
        Ok((out, pos))
    }

    /// The elements from `pos`: to `end` (defined), or to an item delimiter within `end`.
    /// The top-level dataset stops at Pixel Data, whose header is the last one read.
    pub fn dataset(&self, mut pos: u64, end: u64, defined: bool, enc: Enc, depth: u32, top: bool) -> R<(Dataset, u64)> {
        let mut out = Dataset::new();
        loop {
            if defined && pos == end {
                return Ok((out, pos));
            }
            if top && pos >= end {
                return reject("no Pixel Data element");
            }
            let el = self.b.header(pos, end, enc)?;
            if el.tag >> 16 == 0xFFFE {
                if el.tag == ITEM_END && !defined && !top {
                    if el.length != 0 {
                        return reject(format!("the item delimiter at {pos} has a nonzero length"));
                    }
                    return Ok((out, el.value));
                }
                return reject(format!("unexpected {} at {pos}", tag_name(el.tag)));
            }
            if top && el.tag == PIXEL_DATA {
                out.entry(el.tag).or_insert(Walked { el, items: None });
                return Ok((out, el.value));
            }
            let mut items = None;
            if el.undefined() {
                if enc.explicit && !el.vr_is("SQ") && !el.vr_is("UN") && !(el.tag == PIXEL_DATA && (el.vr_is("OB") || el.vr_is("OW"))) {
                    return reject(format!("{} with VR {} has an undefined length", tag_name(el.tag), el.vr_text()));
                }
                if el.tag == PIXEL_DATA {
                    pos = self.fragments(el.value, end, enc.little)?.1;
                } else {
                    let inner = if el.vr_is("UN") { IMPLICIT_LE } else { enc };
                    let (it, p) = self.sequence(el.value, None, end, inner, depth)?;
                    items = Some(it);
                    pos = p;
                }
            } else {
                if el.end() > end {
                    return reject(format!("{} at {pos} runs past its container", tag_name(el.tag)));
                }
                if WALKED.contains(&el.tag) {
                    if enc.explicit && !el.vr_is("SQ") {
                        return reject(format!("{} has VR {}, not SQ", tag_name(el.tag), el.vr_text()));
                    }
                    items = Some(self.sequence(el.value, Some(el.end()), end, enc, depth)?.0);
                }
                pos = el.end();
            }
            out.entry(el.tag).or_insert(Walked { el, items });
        }
    }

    /// A sequence's items: to `end` if its length is defined, else to a sequence
    /// delimiter within `limit`.
    pub fn sequence(&self, mut pos: u64, end: Option<u64>, limit: u64, enc: Enc, depth: u32) -> R<(Vec<Dataset>, u64)> {
        let container = end.unwrap_or(limit);
        let mut items = Vec::new();
        loop {
            if end == Some(pos) {
                return Ok((items, pos));
            }
            let el = self.b.header(pos, container, enc)?;
            if end.is_none() && el.tag == SEQUENCE_END {
                if el.length != 0 {
                    return reject(format!("the sequence delimiter at {pos} has a nonzero length"));
                }
                return Ok((items, el.value));
            }
            if el.tag != ITEM {
                return reject(format!("expected an item at {pos}, found {}", tag_name(el.tag)));
            }
            if depth + 1 > MAX_DEPTH {
                return reject(format!("items nested more than {MAX_DEPTH} deep"));
            }
            let ds = if el.undefined() {
                let (ds, p) = self.dataset(el.value, container, false, enc, depth + 1, false)?;
                pos = p;
                ds
            } else {
                if el.end() > container {
                    return reject(format!("the item at {pos} runs past its container"));
                }
                let (ds, _) = self.dataset(el.value, el.end(), true, enc, depth + 1, false)?;
                pos = el.end();
                ds
            };
            items.push(ds);
        }
    }

    /// The items of a fragment sequence at `pos`, as (item offset, data offset, data
    /// length), and where the sequence ends (spec/virtualize/dicom.md §7.5
    /// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/dicom.md#75-frames>).
    pub fn fragments(&self, mut pos: u64, limit: u64, little: bool) -> R<(Vec<(u64, u64, u32)>, u64)> {
        let mut items = Vec::new();
        loop {
            match fragment(self.b, pos, limit, little)? {
                Frag::End(e) => return Ok((items, e)),
                Frag::Item(n) => {
                    items.push((pos, pos + 8, n));
                    pos += 8 + n as u64;
                }
            }
        }
    }
}

pub enum Frag {
    /// an item of this data length
    Item(u32),
    /// the sequence delimiter, and where the sequence ends
    End(u64),
}

/// The fragment item (or delimiter) at `pos`, within `limit`.
pub fn fragment(b: &Bytes, pos: u64, limit: u64, little: bool) -> R<Frag> {
    if pos + 8 > limit {
        return reject(format!("the fragment sequence runs past its container at {pos}"));
    }
    let tag = (b.u16(pos, little)? as u32) << 16 | b.u16(pos + 2, little)? as u32;
    let length = b.u32(pos + 4, little)?;
    if tag == SEQUENCE_END {
        if length != 0 {
            return reject(format!("the sequence delimiter at {pos} has a nonzero length"));
        }
        return Ok(Frag::End(pos + 8));
    }
    if tag != ITEM {
        return reject(format!("expected a fragment item at {pos}, found {}", tag_name(tag)));
    }
    if length == UNDEFINED || pos + 8 + length as u64 > limit {
        return reject(format!("the fragment at {pos} has an undefined length or runs past its container"));
    }
    Ok(Frag::Item(length))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bytes_merge_and_find_ranges() {
        let file: Vec<u8> = (0..100u8).collect();
        let mut b = Bytes::new(100);
        b.feed(10, file[10..20].to_vec());
        b.feed(30, file[30..40].to_vec());
        assert_eq!(b.get(12, 4).unwrap(), &file[12..16]);
        assert_eq!(b.get(18, 4), Err(Fail::Miss(18, 4)));
        b.feed(20, file[20..30].to_vec()); // joins both
        assert_eq!(b.get(15, 20).unwrap(), &file[15..35]);
        assert_eq!((b.fed.ranges(), b.held()), (1, 30));
        b.feed(5, file[5..45].to_vec()); // covers all, past both ends
        assert_eq!((b.fed.ranges(), b.held()), (1, 40));
        assert_eq!(b.get(5, 40).unwrap(), &file[5..45]);
        b.feed(12, file[12..14].to_vec()); // inside: nothing changes
        assert_eq!((b.fed.ranges(), b.held(), b.reach(7)), (1, 40, 45));
    }
}
