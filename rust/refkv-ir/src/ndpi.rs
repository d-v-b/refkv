//! What NDPI adds to TIFF (spec/zarrify/ndpi.md §7
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#7-ndpi-profile>), beyond its
//! IFD layout (which `tiff.rs` reads in its NDPI variant): a level's strip is one JPEG stream
//! whose restart intervals McuStarts locates, and each chunk is a JPEG stream rebuilt from the
//! strip's header (its parts around SOF0 shared, a literal SOF0 of the chunk's size), `a × b`
//! intervals of the strip, and literal restart markers and EOI.
//!
//! The parser reads the header and checks it (`header`), and chooses each level's chunk size
//! (`Grid::new`); the projection (`project/ndpi.rs`) writes the chunks (`Grid::parts`). Both
//! use the code here, so the payload the parser checks is the one the projection writes.

use crate::fed::Word;
use crate::ir::Ir;
use crate::out::{payload_size, Part};
use crate::rules::{self, flag, num, text, Res, Rules};
use serde_json::{json, Value as J};

/// The largest chunk width or height a frame header holds, and the target chunk size.
pub const MAX_SIDE: u64 = 65535;
pub const CHUNK: u64 = 1024;

/// A strip's JPEG header, its first `M[0]` bytes: where its SOF0 segment is, the MCU's size
/// and the restart interval.
#[derive(Clone, Debug, PartialEq)]
pub struct Header {
    pub sof: (usize, usize),
    pub mw: u64,
    pub mh: u64,
    pub restart: u64,
}

/// Reads and checks a strip's header (the `jpeg_segment` and `jpeg_header` roles): SOI, then
/// marker segments to SOS, which ends the header; one SOF0, baseline only, a DRI of 4 bytes.
pub fn header(h: &[u8], rules: &Rules) -> Res<Header> {
    let soi = h.len() >= 2 && h[..2] == [0xFF, 0xD8];
    let start = |done: bool, has_sof0: bool, restart: u64, sof0_ok: bool, sampling: bool| {
        rules.check(
            "jpeg_header",
            &mut rules::env([
                ("soi", flag(soi)),
                ("done", flag(done)),
                ("has_sof0", flag(has_sof0)),
                ("restart", num(restart as f64)),
                ("sof0_ok", flag(sof0_ok)),
                ("sampling", flag(sampling)),
            ]),
        )
    };
    start(false, false, 0, false, false)?;
    let (mut pos, mut sof, mut dri, mut sofs) = (2usize, None, 0u64, 0u64);
    loop {
        let whole0 = pos + 4 <= h.len() && h[pos] == 0xFF;
        let marker = if whole0 { h[pos + 1] } else { 0 };
        let length = if whole0 { u16::be_at(h, pos + 2) as usize } else { 0 };
        let end = pos + 2 + length;
        let whole = whole0 && length >= 2 && end <= h.len();
        rules.check(
            "jpeg_segment",
            &mut rules::env([
                ("whole", flag(whole)),
                ("marker", num(marker as f64)),
                ("hex", text(&format!("{marker:02X}"))),
                ("sofs", num(sofs as f64)),
                ("length", num(length as f64)),
                ("ends_header", flag(end == h.len())),
            ]),
        )?;
        match marker {
            0xC0 => {
                sofs += 1;
                sof = Some((pos, end));
            }
            0xDD => dri = u16::be_at(h, pos + 4) as u64,
            0xDA => break,
            _ => {}
        }
        pos = end;
    }
    let Some((a, b)) = sof else {
        start(true, false, dri, false, false)?;
        unreachable!("the schema rejects a header without SOF0");
    };
    let seg = &h[a..b];
    let nf = seg.get(9).copied().unwrap_or(0) as usize;
    let sof0_ok = nf > 0 && seg.len() == 10 + 3 * nf;
    let (mut hs, mut vs) = (0u64, 0u64);
    if sof0_ok {
        for k in 0..nf {
            hs = hs.max((seg[11 + 3 * k] >> 4) as u64);
            vs = vs.max((seg[11 + 3 * k] & 15) as u64);
        }
    }
    start(true, true, dri, sof0_ok, hs > 0 && vs > 0)?;
    Ok(Header { sof: (a, b), mw: 8 * hs, mh: 8 * vs, restart: dri })
}

/// The first bytes of a strip without McuStarts read for its frame header, and the most read.
pub const FRAME_READ: u64 = 1024;
pub const MAX_FRAME_READ: u64 = 1 << 20;

/// The frame header (SOF0) of a strip without McuStarts: where its height field is in the
/// strip (the width's follows it), the frame's height and width, and the MCU's size.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Frame {
    pub at: u64,
    pub height: u64,
    pub width: u64,
    pub mw: u64,
    pub mh: u64,
}

impl Frame {
    /// Whether a `height` × `width` image needs as many MCU rows and columns as the frame, and
    /// a frame header holds its size.
    pub fn covers(&self, height: u64, width: u64) -> bool {
        height <= MAX_SIDE
            && width <= MAX_SIDE
            && height.div_ceil(self.mh) == self.height.div_ceil(self.mh)
            && width.div_ceil(self.mw) == self.width.div_ceil(self.mw)
    }
}

/// What the first bytes of a strip without McuStarts say of its frame header.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Scan {
    /// its SOF0 segment, well formed
    Frame(Frame),
    /// the header continues past them: its first `n` bytes are needed
    Need(u64),
    /// none: no SOI, a malformed segment, a marker that is not a segment's, or SOS or another
    /// SOFn before an SOF0, or a malformed SOF0
    None,
}

/// Scans the first bytes `b` of a strip without McuStarts (spec/zarrify/ndpi.md §7
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#7-ndpi-profile>): SOI, then
/// marker segments (each an `FF`, the marker and a length of at least 2, after any `FF` fill
/// bytes) up to the first SOF0 segment. Unlike a McuStarts header (`header`), it never rejects.
pub fn frame(b: &[u8]) -> Scan {
    if b.len() < 2 {
        return Scan::Need(2);
    }
    if b[..2] != [0xFF, 0xD8] {
        return Scan::None;
    }
    let mut pos = 2usize;
    loop {
        while pos + 1 < b.len() && b[pos] == 0xFF && b[pos + 1] == 0xFF {
            pos += 1;
        }
        if pos + 4 > b.len() {
            return Scan::Need(pos as u64 + 4);
        }
        let (marker, length) = (b[pos + 1], u16::be_at(b, pos + 2) as usize);
        if b[pos] != 0xFF || length < 2 {
            return Scan::None;
        }
        let end = pos + 2 + length;
        match marker {
            0xC0 => {
                let Some(seg) = b.get(pos..end) else { return Scan::Need(end as u64) };
                let nf = seg.get(9).copied().unwrap_or(0) as usize;
                if nf == 0 || seg.len() != 10 + 3 * nf {
                    return Scan::None;
                }
                let factors = (0..nf).map(|k| seg[11 + 3 * k]);
                let (hs, vs) = factors.fold((0, 0), |(h, v), f| (h.max((f >> 4) as u64), v.max((f & 15) as u64)));
                if hs == 0 || vs == 0 {
                    return Scan::None;
                }
                let be = |i: usize| u16::be_at(seg, i) as u64;
                return Scan::Frame(Frame { at: pos as u64 + 5, height: be(5), width: be(7), mw: 8 * hs, mh: 8 * vs });
            }
            // a segment of the header: tables, application data, comments, DRI
            0xC4 | 0xC8 | 0xCC | 0xDB..=0xDF | 0xE0..=0xEF | 0xF0..=0xFD | 0xFE => pos = end,
            _ => return Scan::None,
        }
    }
}

/// Hamamatsu's Compression for JPEG XR (tifffile's `JPEGXR_NDPI`).
pub const JPEGXR: u64 = 22610;
/// The first bytes of a JPEG XR level's first stream read for its header, and the most read.
pub const STREAM_READ: u64 = 1024;
pub const MAX_STREAM_READ: u64 = 1 << 16;

/// The data type and samples of a JPEG XR pixel format the NDPI profile reads, by its GUID
/// (spec/zarrify/ndpi.md §3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#3-levels>):
/// Gray8, Gray16, 24bppBGR, 24bppRGB and 48bppRGB.
pub fn pixel_format(guid: &[u8; 16]) -> Option<(&'static str, u64)> {
    if guid[..15] != crate::coding::WIC {
        return None;
    }
    Some(match guid[15] {
        0x08 => ("uint8", 1),
        0x0B => ("uint16", 1),
        0x0C | 0x0D => ("uint8", 3),
        0x15 => ("uint16", 3),
        _ => return None,
    })
}

/// A GUID as its bytes in hex, as stored.
pub fn hex(guid: &[u8; 16]) -> String {
    guid.iter().map(|b| format!("{b:02X}")).collect()
}

/// A JPEG XR stream's header: its pixel format's GUID, its width and its height.
pub type Stream = ([u8; 16], u64, u64);

/// What the first bytes `b` of a JPEG XR stream of `n` bytes say of its header (spec/zarrify/ndpi.md
/// §7 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#7-ndpi-profile>): its pixel
/// format, width and height; None when there is none to read within `MAX_STREAM_READ` bytes;
/// Err the bytes from the start to read next.
pub fn stream(b: &[u8], n: u64) -> Result<Option<Stream>, u64> {
    let cap = n.min(MAX_STREAM_READ);
    match crate::coding::jpegxr_header(&crate::coding::Head { have: b, bound: cap }) {
        Ok(h) => Ok(h),
        Err(k) => Err(cap.min(k.max(2 * b.len() as u64))),
    }
}

/// A McuStarts level's intervals and chunk grid. `starts` are offsets in the strip
/// (with McuStartsHighBytes); interval `i` is `[starts[i], ends[i])` of the strip.
#[derive(Clone, Debug)]
pub struct Grid {
    pub strip: u64,
    pub starts: Vec<u64>,
    pub ends: Vec<u64>,
    /// intervals per row and rows of intervals
    pub q: u64,
    pub r: u64,
    /// intervals across and down a chunk
    pub a: u64,
    pub b: u64,
    pub header: Vec<u8>,
    pub h: Header,
}

impl Grid {
    /// The chunk shape `[3, b × mh, a × R × mw]`.
    pub fn chunk_shape(&self) -> [u64; 3] {
        [3, self.b * self.h.mh, self.a * self.h.restart * self.h.mw]
    }

    /// Chunks down and across.
    pub fn chunks(&self) -> (u64, u64) {
        (self.r.div_ceil(self.b), self.q.div_ceil(self.a))
    }

    /// The header's SOF0 segment with the chunk's height and width (`b` intervals down).
    pub fn sof(&self, b: u64) -> Vec<u8> {
        let mut s = self.header[self.h.sof.0..self.h.sof.1].to_vec();
        s[5..7].copy_from_slice(&((b * self.h.mh) as u16).to_be_bytes());
        s[7..9].copy_from_slice(&((self.a * self.h.restart * self.h.mw) as u16).to_be_bytes());
        s
    }

    /// The header before and after SOF0, which every chunk shares (data sources).
    pub fn pieces(&self) -> (&[u8], &[u8]) {
        (&self.header[..self.h.sof.0], &self.header[self.h.sof.1..])
    }

    /// Chunk `(u, v)`'s parts at `b` intervals down: the shared header before SOF0, the
    /// chunk's SOF0, the header after it, then the intervals row by row (clamped at the
    /// image's edges), restart markers between them, and EOI.
    pub fn parts(&self, u: u64, v: u64, b: u64, before: &Part, sof: &[u8], after: &Part) -> Vec<Part> {
        let mut out = Vec::with_capacity(3 + 2 * (self.a * b) as usize);
        out.push(before.clone());
        out.push(Part::Lit(sof.to_vec()));
        out.push(after.clone());
        let mut t = 0u64;
        for y in 0..b {
            for x in 0..self.a {
                let i = ((u * b + y).min(self.r - 1) * self.q + (v * self.a + x).min(self.q - 1)) as usize;
                if t > 0 {
                    out.push(Part::Lit(vec![0xFF, 0xD0 + ((t - 1) % 8) as u8]));
                }
                out.push(Part::Src(self.strip + self.starts[i], self.ends[i] - self.starts[i]));
                t += 1;
            }
        }
        out.push(Part::Lit(vec![0xFF, 0xD9]));
        out
    }

    /// The largest payload of a chunk at `b` intervals down.
    fn largest(&self, b: u64, before: &Part, after: &Part) -> usize {
        let sof = self.sof(b);
        let (down, across) = (self.r.div_ceil(b), self.q.div_ceil(self.a));
        let mut m = 0;
        for u in 0..down {
            for v in 0..across {
                m = m.max(payload_size(&self.parts(u, v, b, before, &sof, after)));
            }
        }
        m
    }

    /// The intervals of a strip of `length` bytes at `strip`, of an image `w × h`, its
    /// header `header` and McuStarts `starts` (`intervals` and `chunks` roles); `b` the largest
    /// from 1 to `min(r, 1024 / mh)` whose chunks' payloads are within the limit, with the
    /// header's pieces as `before` and `after`.
    #[allow(clippy::too_many_arguments)]
    pub fn new(strip: u64, length: u64, w: u64, hgt: u64, header: Vec<u8>, h: Header, starts: Vec<u64>,
               before: &Part, after: &Part, rules: &Rules) -> Res<Grid> {
        let q = w.div_ceil(h.restart * h.mw);
        let r = hgt.div_ceil(h.mh);
        let matches = q.checked_mul(r).is_some_and(|n| n == starts.len() as u64)
            && starts.last().is_some_and(|&l| l < length)
            && starts.windows(2).all(|p| p[1] > p[0]);
        let mut ends: Vec<u64> = Vec::new();
        let mut nonempty = true;
        if matches {
            ends = starts[1..].iter().map(|&s| s - 2).chain([length - 2]).collect();
            nonempty = starts.iter().zip(&ends).all(|(s, e)| e > s);
        }
        rules.check("intervals", &mut rules::env([("matches", flag(matches)), ("nonempty", flag(nonempty))]))?;
        let a = q.min((CHUNK / (h.restart * h.mw)).max(1));
        let chunks = |width: u64, height: u64, payload: usize| {
            rules.check(
                "chunks",
                &mut rules::env([("width", num(width as f64)), ("height", num(height as f64)), ("payload", num(payload as f64))]),
            )
        };
        chunks(a * h.restart * h.mw, 0, 0)?;
        let mut g = Grid { strip, starts, ends, q, r, a, b: 1, header, h };
        let mut b = r.min(CHUNK / g.h.mh).max(1);
        let mut largest;
        loop {
            largest = g.largest(b, before, after);
            if b == 1 || largest <= crate::out::MAX_PAYLOAD {
                break;
            }
            b -= 1;
        }
        g.b = b;
        chunks(g.a * g.h.restart * g.h.mw, b * g.h.mh, largest)?;
        Ok(g)
    }

    /// What the projection needs of the grid, as facts (the starts are the IR's).
    pub fn facts(&self) -> J {
        json!({
            "strip": self.strip, "q": self.q, "r": self.r, "a": self.a, "b": self.b,
            "header": self.header, "sof": [self.h.sof.0, self.h.sof.1],
            "mw": self.h.mw, "mh": self.h.mh, "restart": self.h.restart,
        })
    }

    /// The grid from its facts and the level's McuStarts as the IR holds them (`starts`,
    /// with McuStartsHighBytes), its strip being `length` bytes long.
    pub fn from_facts(f: &J, starts: Vec<u64>, length: u64) -> Grid {
        let n = |k: &str| f[k].as_u64().unwrap_or(0);
        let header: Vec<u8> = f["header"].as_array().map(|a| a.iter().map(|x| x.as_u64().unwrap_or(0) as u8).collect()).unwrap_or_default();
        let ends = starts.iter().skip(1).map(|&s| s - 2).chain([length - 2]).collect();
        Grid {
            strip: n("strip"),
            starts,
            ends,
            q: n("q"),
            r: n("r"),
            a: n("a"),
            b: n("b"),
            header,
            h: Header {
                sof: (f["sof"][0].as_u64().unwrap_or(0) as usize, f["sof"][1].as_u64().unwrap_or(0) as usize),
                mw: n("mw"),
                mh: n("mh"),
                restart: n("restart"),
            },
        }
    }
}

/// Unsigned integers of field type `typ`, little-endian.
pub fn uints(b: &[u8], typ: u16) -> Vec<u64> {
    let z = crate::tiff::type_size(typ).unwrap_or(1) as usize;
    b.chunks_exact(z)
        .map(|c| match z {
            1 => c[0] as u64,
            2 => u16::le_at(c, 0) as u64,
            4 => u32::le_at(c, 0) as u64,
            _ => u64::le_at(c, 0),
        })
        .collect()
}

/// A tag's integer values as the IR holds them: its `value` element's, or, for an in-line
/// LONG or IFD of count 1, the value plus its `high` element's word times 2^32.
pub fn tag_values(ir: &Ir, value: u32, typ: u16, high: Option<u32>) -> Vec<u64> {
    let mut v = ir.value_bytes(value).map(|b| uints(b, typ)).unwrap_or_default();
    if let (Some(h), 1, true) = (high, v.len(), matches!(typ, 4 | 13)) {
        if let Some(hb) = ir.value_bytes(h) {
            v[0] += (u32::le_at(hb, 0) as u64) << 32;
        }
    }
    v
}

/// McuStarts with McuStartsHighBytes: `M[i] + H[i] × 2^32`, saturating at 2^64 − 1 (an
/// offset that large is outside every strip, which the `mcu_starts` and `intervals` roles
/// reject, as the profile rejects the exact value).
pub fn combine(starts: Vec<u64>, high: Option<&[u64]>) -> Vec<u64> {
    match high {
        None => starts,
        Some(h) => starts
            .iter()
            .zip(h)
            .map(|(&s, &x)| x.checked_mul(1 << 32).and_then(|x| s.checked_add(x)).unwrap_or(u64::MAX))
            .collect(),
    }
}

/// The NDPI profile's file run: a TIFF the TIFF parser found to be NDPI (spec/zarrify/ndpi.md §7
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#7-ndpi-profile>), so hosts
/// never test for NDPI themselves. Its files are not sniffed: they are TIFFs.
pub struct Profile;

impl crate::run::FileProfile for Profile {
    fn parser(&self, size: u64) -> crate::run::AnyParser {
        crate::run::AnyParser::of::<crate::tiff::Tiff>(size)
    }
    fn project(&self, ir: &crate::ir::Ir, facts: &serde_json::Value, url: &str, _copied: &crate::project::Copied) -> Result<crate::out::Out, String> {
        crate::project::ndpi::project(ir, facts, url)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// SOI, SOF0 for a 16 × 32 image with the given sampling factors, DRI `r`, SOS.
    fn jpeg(factors: &[u8], r: u16) -> Vec<u8> {
        let mut sof = vec![8, 0, 16, 0, 32, factors.len() as u8];
        for (k, &f) in factors.iter().enumerate() {
            sof.extend([k as u8, f, 0]);
        }
        let mut sos = vec![factors.len() as u8];
        for k in 0..factors.len() {
            sos.extend([k as u8, 0]);
        }
        sos.extend([0, 0x3f, 0]);
        let mut h = vec![0xFF, 0xD8, 0xFF, 0xC0];
        h.extend(((2 + sof.len()) as u16).to_be_bytes());
        h.extend(sof);
        h.extend([0xFF, 0xDD, 0, 4]);
        h.extend(r.to_be_bytes());
        h.extend([0xFF, 0xDA]);
        h.extend(((2 + sos.len()) as u16).to_be_bytes());
        h.extend(sos);
        h
    }

    fn rejects(h: &[u8], msg: &str) {
        assert_eq!(header(h, crate::rules::of("ndpi")), Err(msg.to_string()));
    }

    #[test]
    fn the_header_gives_the_mcu_size_and_restart_interval() {
        for (factors, mw, mh) in [
            (&[0x11u8, 0x11, 0x11][..], 8, 8),
            (&[0x21, 0x11, 0x11][..], 16, 8),
            (&[0x22, 0x11, 0x11][..], 16, 16),
            (&[0x12, 0x11, 0x11][..], 8, 16),
            (&[0x11][..], 8, 8),
        ] {
            let h = jpeg(factors, 4);
            let got = header(&h, crate::rules::of("ndpi")).unwrap();
            assert_eq!((got.sof.0, got.mw, got.mh, got.restart), (2, mw, mh, 4));
            assert_eq!(&h[got.sof.0..got.sof.0 + 2], &[0xFF, 0xC0]);
        }
    }

    #[test]
    fn a_header_without_soi_is_rejected() {
        let mut h = jpeg(&[0x11], 4);
        h[1] = 0xD9;
        rejects(&h, "an NDPI strip does not start with a JPEG SOI marker");
    }

    #[test]
    fn a_truncated_header_is_rejected() {
        let h = jpeg(&[0x11], 4);
        rejects(&h[..h.len() - 3], "malformed JPEG header in an NDPI strip");
    }

    #[test]
    fn the_frame_is_the_first_sof0() {
        let h = jpeg(&[0x22, 0x11, 0x11], 4);
        // a fill byte and a comment before the SOF0 move it by 1 + 6 bytes
        let mut b = h[..2].to_vec();
        b.extend([0xFF, 0xFF, 0xFE, 0, 4, b'h', b'i']);
        b.extend(&h[2..]);
        let want = Frame { at: 2 + 7 + 5, height: 16, width: 32, mw: 16, mh: 16 };
        assert_eq!(frame(&b), Scan::Frame(want));
        assert_eq!(frame(&b[..12]), Scan::Need(13));
        assert_eq!(frame(&b[..5]), Scan::Need(7));
        assert!(want.covers(15, 17) && !want.covers(17, 32) && !want.covers(16, 33));
        let big = Frame { height: 65535, width: 16, ..want };
        assert!(!big.covers(65536, 16)); // as many MCU rows, but more than a frame header holds
    }

    #[test]
    fn a_strip_without_a_frame_has_none() {
        let h = jpeg(&[0x11], 4);
        assert_eq!(frame(&h[1..]), Scan::None); // no SOI
        let sos = [0xFF, 0xD8, 0xFF, 0xDA, 0, 2];
        assert_eq!(frame(&sos), Scan::None);
        let mut progressive = h.clone();
        progressive[3] = 0xC2;
        assert_eq!(frame(&progressive), Scan::None);
        let mut zero = h.clone();
        zero[2 + 4 + 7] = 0x01; // the component's sampling factors 0 × 1
        assert_eq!(frame(&zero), Scan::None);
    }

    #[test]
    fn a_second_sof0_is_rejected() {
        let h = jpeg(&[0x11], 4);
        let sof = h[2..2 + 2 + 11].to_vec();
        let mut two = h[..2].to_vec();
        two.extend(&sof);
        two.extend(&h[2..]);
        rejects(&two, "an NDPI strip has two SOF0 segments");
    }

    #[test]
    fn a_progressive_header_is_rejected() {
        let mut h = jpeg(&[0x11], 4);
        h[3] = 0xC2;
        rejects(&h, "an NDPI strip is not baseline JPEG (marker FFC2)");
    }

    #[test]
    fn a_dri_of_the_wrong_length_is_rejected() {
        let mut h = jpeg(&[0x11], 4);
        let at = h.windows(2).position(|w| w == [0xFF, 0xDD]).unwrap();
        h.splice(at + 2..at + 6, [0, 2]);
        rejects(&h, "malformed DRI segment in an NDPI strip");
    }

    #[test]
    fn an_sos_before_the_end_is_rejected() {
        let mut h = jpeg(&[0x11], 4);
        h.push(0);
        rejects(&h, "an NDPI strip's SOS does not end at McuStarts[0]");
    }

    #[test]
    fn a_header_without_restart_interval_is_rejected() {
        rejects(&jpeg(&[0x11], 0), "an NDPI strip has no SOF0 or no restart interval");
    }

    #[test]
    fn a_malformed_sof0_is_rejected() {
        let mut h = jpeg(&[0x11], 4);
        h[2 + 2 + 7] = 2; // two components, the bytes of one
        rejects(&h, "malformed SOF0 segment in an NDPI strip");
    }

    #[test]
    fn a_sampling_factor_of_0_is_rejected() {
        rejects(&jpeg(&[0x10], 4), "an NDPI strip has a sampling factor of 0");
    }

    /// A JPEG XR container of a `w` × `h` image of pixel format GUID `last` (after the common
    /// prefix): the header, an image directory of 3 entries, then the GUID.
    fn jxr(last: u8, w: u32, h: u16) -> Vec<u8> {
        let mut b = vec![0x49, 0x49, 0xBC, 0x01, 8, 0, 0, 0, 3, 0];
        let guid_at = 10 + 3 * 12 + 4;
        for (tag, typ, value) in [(0xBC01u16, 1u16, guid_at as u32), (0xBC80, 4, w), (0xBC81, 3, h as u32)] {
            b.extend(tag.to_le_bytes());
            b.extend(typ.to_le_bytes());
            b.extend((if typ == 1 { 16u32 } else { 1 }).to_le_bytes());
            b.extend(value.to_le_bytes());
        }
        b.extend([0; 4]); // the next directory's offset
        b.extend(crate::coding::WIC);
        b.push(last);
        b
    }

    #[test]
    fn a_jpegxr_stream_header_gives_the_pixels() {
        for (last, want) in [(0x08, Some(("uint8", 1))), (0x0B, Some(("uint16", 1))), (0x0C, Some(("uint8", 3))),
                             (0x0D, Some(("uint8", 3))), (0x15, Some(("uint16", 3))), (0x11, None)] {
            let b = jxr(last, 896, 448);
            let (guid, w, h) = stream(&b, 1 << 20).unwrap().unwrap();
            assert_eq!((w, h, pixel_format(&guid)), (896, 448, want));
            assert!(hex(&guid).ends_with(&format!("{last:02X}")));
        }
        // the header continues past the bytes read: at least twice as many next, up to the stream
        let b = jxr(0x0B, 8, 8);
        assert_eq!(stream(&b[..16], 1 << 20), Err(32));
        assert_eq!(stream(&b[..16], 30), Err(30));
        // not a JPEG XR container, a size of 0, or a header past the stream's end: none
        assert_eq!(stream(&[0xFF, 0xD8, 0xFF, 0xE0, 0, 0, 0, 0], 8), Ok(None));
        assert_eq!(stream(&jxr(0x0B, 0, 8), 1 << 20), Ok(None));
        assert_eq!(stream(&b[..20], 20), Ok(None));
    }

    /// A strip of a 16 × 8 image of MCUs 8 × 8 and restart interval 1: two intervals.
    fn grid(starts: Vec<u64>, length: u64, w: u64) -> Res<Grid> {
        let h = jpeg(&[0x11], 1);
        let hd = header(&h, crate::rules::of("ndpi"))?;
        Grid::new(100, length, w, 8, h, hd, starts, &Part::Data(1, 0, 2), &Part::Data(2, 0, 10), crate::rules::of("ndpi"))
    }

    #[test]
    fn a_grid_cuts_the_intervals_into_chunks() {
        let g = grid(vec![40, 50], 60, 16).unwrap();
        assert_eq!((g.q, g.r, g.a, g.b, g.chunk_shape()), (2, 1, 2, 1, [3, 8, 16]));
        assert_eq!(g.ends, vec![48, 58]);
        let parts = g.parts(0, 0, 1, &Part::Data(1, 0, 2), &g.sof(1), &Part::Data(2, 0, 10));
        assert_eq!(parts[3..], [Part::Src(140, 8), Part::Lit(vec![0xFF, 0xD0]), Part::Src(150, 8), Part::Lit(vec![0xFF, 0xD9])]);
    }

    #[test]
    fn high_bytes_combine_and_saturate() {
        assert_eq!(combine(vec![5, 6], None), vec![5, 6]);
        assert_eq!(combine(vec![5, 6], Some(&[0, 1])), vec![5, 6 + (1 << 32)]);
        assert_eq!(combine(vec![5, u64::MAX - 1], Some(&[1 << 32, 1])), vec![u64::MAX, u64::MAX]);
    }

    #[test]
    fn mcu_starts_that_do_not_match_are_rejected() {
        assert_eq!(grid(vec![40], 60, 16).unwrap_err(), "McuStarts does not match the strip's intervals");
        assert_eq!(grid(vec![50, 40], 60, 16).unwrap_err(), "McuStarts does not match the strip's intervals");
    }

    #[test]
    fn an_empty_interval_is_rejected() {
        assert_eq!(grid(vec![40, 42], 60, 16).unwrap_err(), "an NDPI restart interval is empty");
    }

    #[test]
    fn a_chunk_wider_than_a_frame_header_is_rejected() {
        let h = jpeg(&[0x11], 8192);
        let hd = header(&h, crate::rules::of("ndpi")).unwrap();
        let g = Grid::new(0, 60, 65536, 8, h, hd, vec![40], &Part::Data(1, 0, 2), &Part::Data(2, 0, 10), crate::rules::of("ndpi"));
        assert_eq!(g.unwrap_err(), "an NDPI chunk would be 65536 pixels wide, more than 65535");
    }
}
