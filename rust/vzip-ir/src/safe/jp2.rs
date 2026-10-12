//! JPEG 2000 band files (spec/virtualize/safe.md §12.3
//! <https://github.com/d-v-b/vzip/blob/main/spec/virtualize/safe.md#123-band-files>), read sans-IO,
//! and their chunks (§12.6 <https://github.com/d-v-b/vzip/blob/main/spec/virtualize/safe.md#126-chunks>).
//!
//! A band file is a JP2 file whose codestream has one tile-part per tile, in raster order.
//! [`BandReader`] walks one: its boxes and main header from 64 KiB blocks, then each
//! tile-part's SOT segment, a 12-byte read each (a 64 KiB block after a small tile-part),
//! each depending on the one before. Each tile becomes a standalone codestream: the file's
//! main header with a SIZ marker of its own, the tile's tile-part, and, at the array's right
//! and bottom edges, empty tiles that fill the chunk ([`chunk_parts`]).

use crate::fed::Fed;
use crate::rules::{self, Rules, flag, num, text};

pub const MAX_BOXES: usize = 1024;
pub const MAX_REST: u64 = 1 << 16;
pub const MAX_TAIL: u64 = 1 << 12;
const BLOCK: u64 = 1 << 16;
const SMALL_TILE: u64 = 1 << 14;
const SOC: u16 = 0xFF4F;
const SIZ: u16 = 0xFF51;
const SOT: u16 = 0xFF90;
const EOC: u16 = 0xFFD9;
const COD: u16 = 0xFF52;
const COC: u16 = 0xFF53;
const QCD: u16 = 0xFF5C;
/// The markers a main header may have after SIZ.
const ALLOWED: [u16; 8] = [COD, COC, QCD, 0xFF5D, 0xFF5E, 0xFF5F, 0xFF63, 0xFF64];

/// A component's coding: (decomposition levels N, [(PPx_r, PPy_r) for r = 0..N]).
pub type Coding = (u64, Vec<(u64, u64)>);
/// A chunk's parts: (SOC + SIZ', SOT, the body's (offset, length), the tail, whether it has empty tiles).
pub type ChunkParts = (Vec<u8>, Vec<u8>, (u64, u64), Vec<u8>, bool);

/// A box before the codestream, or the `jp2c` box: (offset, length, header length, type).
#[derive(Clone, Debug)]
pub struct Jp2Box {
    pub at: u64,
    pub len: u64,
    pub header: u64,
    pub tbox: [u8; 4],
    /// its header's bytes (8 or 16)
    pub raw: Vec<u8>,
}

/// A band file's structure (§12.3 <https://github.com/d-v-b/vzip/blob/main/spec/virtualize/safe.md#123-band-files>).
#[derive(Clone, Debug, Default)]
pub struct Codestream {
    pub n: u64,
    pub boxes: Vec<Jp2Box>,
    /// where the codestream starts: the JP2 header is [0, c0)
    pub c0: u64,
    /// the SIZ segment, from its marker
    pub siz: Vec<u8>,
    /// the main header after the SIZ segment, up to the first SOT, and where it starts
    pub rest: Vec<u8>,
    pub rest_at: u64,
    /// the marker segments of the rest: (offset, marker, length L)
    pub markers: Vec<(u64, u16, u16)>,
    pub width: u64,
    pub height: u64,
    pub tile_w: u64,
    pub tile_h: u64,
    pub components: u64,
    pub precision: u64,
    pub layers: u64,
    /// per component: (decomposition levels N, [(PPx_r, PPy_r) for r = 0..N])
    pub coding: Vec<Coding>,
    /// (s_k, Psot) per tile
    pub tiles: Vec<(u64, u64)>,
    /// the SOT segments, 12 bytes each
    pub sots: Vec<[u8; 12]>,
}

impl Codestream {
    pub fn grid(&self) -> (u64, u64) {
        (self.width.div_ceil(self.tile_w), self.height.div_ceil(self.tile_h))
    }
}

fn u16_be(b: &[u8], at: usize) -> u64 {
    u16::from_be_bytes([b[at], b[at + 1]]) as u64
}

fn u32_be(b: &[u8], at: usize) -> u64 {
    u32::from_be_bytes([b[at], b[at + 1], b[at + 2], b[at + 3]]) as u64
}

/// What a walk needs to go on: a read (offset, length, read-ahead), or a rejection.
enum Need {
    Read(u64, u64, u64),
    Reject(String),
}

impl From<String> for Need {
    fn from(s: String) -> Need {
        Need::Reject(s)
    }
}

type W<T> = Result<T, Need>;

/// The bytes read so far of a band file of `n` bytes.
struct Cache<'a> {
    fed: &'a Fed,
    n: u64,
}

impl Cache<'_> {
    fn read(&self, o: u64, len: u64, ahead: u64) -> W<&[u8]> {
        if o.checked_add(len).is_none_or(|e| e > self.n) {
            return Err(Need::Reject(format!("a band file read of [{o}, {}) is outside its {} bytes", o.saturating_add(len), self.n)));
        }
        self.fed.get(o, len).ok_or(Need::Read(o, len, ahead))
    }

    fn get(&self, o: u64, len: u64) -> W<&[u8]> {
        self.read(o, len, BLOCK)
    }
}

/// The boxes, up to and including the `jp2c` box, which must end at the end of the file.
fn boxes(c: &Cache) -> W<Vec<Jp2Box>> {
    let (n, mut o) = (c.n, 0u64);
    let mut out = Vec::new();
    for i in 0..MAX_BOXES {
        if o + 8 > n {
            return Err("a band file is not a JP2 file: its boxes end without a jp2c box".to_string().into());
        }
        let head = c.get(o, 8)?;
        let lbox = u32_be(head, 0);
        let tbox: [u8; 4] = head[4..8].try_into().unwrap();
        if i == 0 && (lbox != 12 || &tbox != b"jP  " || c.get(o + 8, 4)? != b"\r\n\x87\n") {
            return Err("a band file is not a JP2 file: no JPEG 2000 signature box".to_string().into());
        }
        let (length, hl) = if lbox == 1 {
            if o + 16 > n {
                return Err("a JP2 box's XLBox is beyond the file".to_string().into());
            }
            let x = c.get(o + 8, 8)?;
            let length = u64::from_be_bytes(x.try_into().unwrap());
            if length < 16 {
                return Err(format!("a JP2 box's XLBox is {length}, less than 16").into());
            }
            (length, 16)
        } else if lbox == 0 {
            (n - o, 8)
        } else {
            if lbox < 8 {
                return Err(format!("a JP2 box's LBox is {lbox}").into());
            }
            (lbox, 8)
        };
        if o.checked_add(length).is_none_or(|e| e > n) {
            return Err("a JP2 box reaches beyond the file".to_string().into());
        }
        if i == 1 && &tbox != b"ftyp" {
            return Err("a band file's second box is not ftyp".to_string().into());
        }
        let raw = c.get(o, hl)?.to_vec();
        out.push(Jp2Box { at: o, len: length, header: hl, tbox, raw });
        if &tbox == b"jp2c" {
            if o + length != n {
                return Err("a band file's jp2c box does not end at the end of the file".to_string().into());
            }
            return Ok(out);
        }
        o += length;
    }
    Err(format!("a band file has no jp2c box in its first {MAX_BOXES} boxes").into())
}

/// (N, [(PPx_r, PPy_r)]) from the SPcod/SPcoc fields at `at`.
fn coding(rules: &Rules, seg: &[u8], at: usize, scod: u64, what: &str) -> W<Coding> {
    let levels = seg[at] as u64;
    rules.check("levels", &mut rules::env([("what", text(what)), ("levels", num(levels as f64))]))?;
    let pp = if scod & 1 == 1 {
        seg[at + 5..at + 6 + levels as usize].iter().map(|&b| ((b & 0x0F) as u64, (b >> 4) as u64)).collect()
    } else {
        vec![(15, 15); levels as usize + 1]
    };
    Ok((levels, pp))
}

/// The SIZ segment and the main header rest of the codestream at c0.
fn main_header(rules: &Rules, c: &Cache, boxes: Vec<Jp2Box>, c0: u64) -> W<Codestream> {
    let c1 = c.n;
    if c0 + 4 > c1 || u16_be(c.get(c0, 2)?, 0) != SOC as u64 {
        return Err("a band file's codestream does not start with SOC".to_string().into());
    }
    if u16_be(c.get(c0 + 2, 2)?, 0) != SIZ as u64 {
        return Err("a band file's codestream has no SIZ marker after SOC".to_string().into());
    }
    if c0 + 6 > c1 {
        return Err("a band file's SIZ marker is truncated".to_string().into());
    }
    let lsiz = u16_be(c.get(c0 + 4, 2)?, 0);
    if c0 + 4 + lsiz > c1 || lsiz < 41 {
        return Err("a band file's SIZ segment is truncated".to_string().into());
    }
    let siz = c.get(c0 + 2, 2 + lsiz)?.to_vec();
    let (rsiz, xs, ys, xo, yo, xt, yt, xto, yto, csiz) = (u16_be(&siz, 4), u32_be(&siz, 6), u32_be(&siz, 10), u32_be(&siz, 14),
        u32_be(&siz, 18), u32_be(&siz, 22), u32_be(&siz, 26), u32_be(&siz, 30), u32_be(&siz, 34), u16_be(&siz, 38));
    let comps: Vec<&[u8]> = (0..csiz as usize).filter(|k| 40 + 3 * k + 3 <= siz.len()).map(|k| &siz[40 + 3 * k..43 + 3 * k]).collect();
    let same = comps.iter().all(|x| x[0] == comps[0][0]);
    let ssiz = comps.first().map(|x| x[0]).unwrap_or(0) as u64;
    rules.check("siz", &mut rules::env([
        ("lsiz", num(lsiz as f64)), ("csiz", num(csiz as f64)), ("rsiz", num(rsiz as f64)), ("part2", flag(rsiz & 0xC000 != 0)),
        ("origin", flag(xo != 0 || yo != 0 || xto != 0 || yto != 0)), ("empty", flag(xs == 0 || ys == 0 || xt == 0 || yt == 0)),
        ("same", flag(same)), ("signed", flag(ssiz & 0x80 != 0)), ("precision", num(((ssiz & 0x7F) + 1) as f64)),
        ("subsampled", flag(comps.iter().any(|x| x[1] != 1 || x[2] != 1))),
    ]))?;
    let precision = (ssiz & 0x7F) + 1;
    let mut pos = c0 + 4 + lsiz;
    let mut cod: Option<(u64, Coding)> = None;
    let mut qcd = 0;
    let mut cocs: Vec<Option<Coding>> = vec![None; csiz as usize];
    let mut markers = Vec::new();
    let rest_at = c0 + 4 + lsiz;
    loop {
        // the rest's bound, before reading on: a long run of segments is refused at 64 KiB
        if pos - rest_at > MAX_REST {
            return Err(format!("a band file's main header is more than {MAX_REST} bytes").into());
        }
        if pos + 2 > c1 {
            return Err("a band file's main header has no SOT".to_string().into());
        }
        let marker = u16_be(c.get(pos, 2)?, 0) as u16;
        if marker == SOT {
            break;
        }
        let name = match marker {
            0xFF55 => "TLM".to_string(),
            0xFF57 => "PLM".to_string(),
            0xFF60 => "PPM".to_string(),
            0xFF50 => "CAP".to_string(),
            0xFF59 => "CPF".to_string(),
            m => format!("{m:04X}"),
        };
        rules.check("marker", &mut rules::env([("allowed", flag(ALLOWED.contains(&marker))), ("name", text(&name))]))?;
        if pos + 4 > c1 {
            return Err("a band file's main header marker segment is truncated".to_string().into());
        }
        let length = u16_be(c.get(pos + 2, 2)?, 0);
        if length < 2 || pos + 2 + length > c1 {
            return Err("a band file's main header marker segment is truncated".to_string().into());
        }
        let seg = c.get(pos + 4, length - 2)?.to_vec();
        if marker == COD {
            let (scod, layers) = if length >= 12 { (seg[0] as u64, u16_be(&seg, 2)) } else { (0, 0) };
            let levels = if length >= 12 { seg[5] as u64 } else { 0 };
            rules.check("cod", &mut rules::env([("twice", flag(cod.is_some())), ("length", num(length as f64)),
                ("scod", num(scod as f64)), ("layers", num(layers as f64)), ("levels", num(levels as f64))]))?;
            cod = Some((layers, coding(rules, &seg, 5, scod, "a band file's COD")?));
        } else if marker == COC {
            let (comp, scoc, levels) = if length >= 9 { (seg[0] as u64, seg[1] as u64, seg[2] as u64) } else { (0, 0, 0) };
            let named = length >= 9 && comp < csiz && cocs[comp as usize].is_none();
            rules.check("coc", &mut rules::env([("length", num(length as f64)), ("named", flag(named)),
                ("scoc", num(scoc as f64)), ("levels", num(levels as f64))]))?;
            cocs[comp as usize] = Some(coding(rules, &seg, 2, scoc, "a band file's COC")?);
        } else if marker == QCD {
            qcd += 1;
        }
        markers.push((pos, marker, length as u16));
        pos += 2 + length;
    }
    let (nx, ny) = (xs.div_ceil(xt), ys.div_ceil(yt));
    rules.check("main_header", &mut rules::env([("cod", flag(cod.is_some())), ("qcd", num(qcd as f64)),
        ("rest", num((pos - rest_at) as f64)), ("tiles", num((nx * ny) as f64))]))?;
    let rest = c.get(rest_at, pos - rest_at)?.to_vec();
    let (layers, cod_coding) = cod.unwrap();
    Ok(Codestream {
        n: c.n, boxes, c0, siz, rest, rest_at, markers, width: xs, height: ys, tile_w: xt, tile_h: yt, components: csiz,
        precision, layers, coding: (0..csiz as usize).map(|k| cocs[k].clone().unwrap_or_else(|| cod_coding.clone())).collect(),
        tiles: vec![(pos, 0)], sots: Vec::new(),
    })
}

enum Phase {
    Header,
    /// the walk of the tile-parts: tile k at s, read-ahead `ahead`
    Tiles { k: u64, s: u64, ahead: u64 },
    Done,
}

/// The sans-IO walk of one band file of `n` bytes: `step` gives the next read (offset,
/// length) or None when done, `feed` its bytes.
pub struct BandReader {
    pub n: u64,
    fed: Fed,
    phase: Phase,
    pub cs: Option<Codestream>,
    pub requests: u64,
    pub bytes: u64,
}

impl BandReader {
    pub fn new(n: u64) -> BandReader {
        BandReader { n, fed: Fed::default(), phase: Phase::Header, cs: None, requests: 0, bytes: 0 }
    }

    pub fn feed(&mut self, offset: u64, data: Vec<u8>) {
        if matches!(self.phase, Phase::Tiles { .. }) {
            self.fed.clear(); // a walk keeps one window, as it moves on
        }
        self.requests += 1;
        self.bytes += data.len() as u64;
        self.fed.feed(offset, data);
    }

    fn need(&self, o: u64, len: u64, ahead: u64) -> Option<(u64, u64)> {
        Some((o, (self.n - o).min(len.max(ahead))))
    }

    /// The next read, None when the walk is done, or the rejection.
    pub fn step(&mut self, rules: &Rules) -> Result<Option<(u64, u64)>, String> {
        loop {
            match self.phase {
                Phase::Done => return Ok(None),
                Phase::Header => {
                    let c = Cache { fed: &self.fed, n: self.n };
                    let got = boxes(&c).and_then(|b| {
                        let last = b.last().unwrap();
                        let c0 = last.at + last.header;
                        main_header(rules, &c, b, c0)
                    });
                    match got {
                        Ok(cs) => {
                            let s = cs.tiles[0].0;
                            self.cs = Some(cs);
                            self.phase = Phase::Tiles { k: 0, s, ahead: BLOCK };
                        }
                        Err(Need::Read(o, len, ahead)) => return Ok(self.need(o, len, ahead)),
                        Err(Need::Reject(e)) => return Err(e),
                    }
                }
                Phase::Tiles { k, s, ahead } => {
                    let cs = self.cs.as_mut().unwrap();
                    let c1 = self.n;
                    let (nx, ny) = cs.grid();
                    if k == 0 {
                        cs.tiles.clear();
                    }
                    if k < nx * ny {
                        if s + 12 > c1 {
                            return Err(format!("a band file's tile-part {k} is beyond the codestream"));
                        }
                        let Some(head) = self.fed.get(s, 12) else { return Ok(self.need(s, 12, ahead)) };
                        let head: [u8; 12] = head.try_into().unwrap();
                        let (marker, lsot, isot, psot, tpsot, tnsot) =
                            (u16_be(&head, 0), u16_be(&head, 2), u16_be(&head, 4), u32_be(&head, 6), head[10], head[11]);
                        rules.check("tile_part", &mut rules::env([("k", num(k as f64)), ("sot", flag(marker == SOT as u64 && lsot == 10)),
                            ("isot", num(isot as f64)), ("psot", num(psot as f64)), ("one", flag(tpsot == 0 && tnsot == 1)),
                            ("within", flag(s + psot <= c1 - 2))]))?;
                        cs.tiles.push((s, psot));
                        cs.sots.push(head);
                        self.phase = Phase::Tiles { k: k + 1, s: s + psot, ahead: if psot <= SMALL_TILE { BLOCK } else { 12 } };
                        continue;
                    }
                    if s + 2 != c1 {
                        return Err("a band file's codestream does not end with EOC after its last tile-part".into());
                    }
                    let Some(e) = self.fed.get(s, 2).map(|b| u16_be(b, 0)) else { return Ok(self.need(s, 2, 2)) };
                    if e != EOC as u64 {
                        return Err("a band file's codestream does not end with EOC after its last tile-part".into());
                    }
                    self.fed.clear();
                    self.phase = Phase::Done;
                }
            }
        }
    }
}

fn nprec(z0: u64, z1: u64, d: u64, e: u64) -> u64 {
    let (a, b) = (z0.div_ceil(1 << d), z1.div_ceil(1 << d));
    if b > a { b.div_ceil(1 << e) - (a >> e) } else { 0 }
}

/// e_k: the packets of a tile of area [x0, x1) × [y0, y1).
pub fn empty_packets(cs: &Codestream, x0: u64, x1: u64, y0: u64, y1: u64) -> u64 {
    let mut n = 0u64;
    for (levels, pp) in &cs.coding {
        for res in 0..=*levels {
            let (ppx, ppy) = pp[res as usize];
            let d = levels - res;
            n = n.saturating_add(nprec(x0, x1, d, ppx).saturating_mul(nprec(y0, y1, d, ppy)));
        }
    }
    cs.layers.saturating_mul(n)
}

/// (x0, y0, w, h, a, b) of the chunk of tile t, or None when its image area reaches beyond 2^32 − 1.
fn area(cs: &Codestream, t: u64) -> Option<(u64, u64, u64, u64, u64, u64)> {
    let (nx, _) = cs.grid();
    let (tw, th) = (cs.tile_w, cs.tile_h);
    let (x0, y0) = ((t % nx) * tw, (t / nx) * th);
    let (w, h) = (tw.min(cs.width - x0), th.min(cs.height - y0));
    if x0 + tw > 0xFFFF_FFFF || y0 + th > 0xFFFF_FFFF {
        return None;
    }
    Some((x0, y0, w, h, tw.div_ceil(w), th.div_ceil(h)))
}

/// [e_1, …, e_{a×b−1}], the empty packets of the empty tiles of the chunk of tile t, once
/// their tail, of 2 + Σ (14 + e_k) bytes, is known to be at most MAX_TAIL; else the rejection.
pub fn tail_packets(cs: &Codestream, t: u64) -> Result<Vec<u64>, String> {
    let (x0, y0, w, h, a, b) = area(cs, t).ok_or("a chunk's image area reaches beyond 2^32 − 1")?;
    let (tw, th) = (cs.tile_w, cs.tile_h);
    let too_long = || format!("the empty tiles of a chunk of tile {t} would take more than {MAX_TAIL} bytes");
    if (a * b - 1).saturating_mul(14).saturating_add(2) > MAX_TAIL {
        return Err(too_long());
    }
    let mut out = Vec::new();
    let mut length = 2u64;
    for k in 1..a * b {
        let (i, j) = (k % a, k / a);
        let e = empty_packets(cs, x0 + i * w, (x0 + (i + 1) * w).min(x0 + tw), y0 + j * h, (y0 + (j + 1) * h).min(y0 + th));
        length = length.saturating_add(14).saturating_add(e);
        if length > MAX_TAIL {
            return Err(too_long());
        }
        out.push(e);
    }
    Ok(out)
}

/// The length of the chunk of tile t's empty tiles and EOC.
pub fn tail_length(cs: &Codestream, t: u64) -> Result<u64, String> {
    Ok(2 + tail_packets(cs, t)?.iter().map(|e| 14 + e).sum::<u64>())
}

/// The chunk of tile t (§12.6 <https://github.com/d-v-b/vzip/blob/main/spec/virtualize/safe.md#126-chunks>):
/// (SOC + SIZ', SOT, (offset, length) of the body in the band file, the empty tiles and
/// EOC, whether it has empty tiles).
pub fn chunk_parts(cs: &Codestream, t: u64) -> Result<ChunkParts, String> {
    let (x0, y0, w, h, a, b) = area(cs, t).ok_or("a chunk's image area reaches beyond 2^32 − 1")?;
    let packets = tail_packets(cs, t)?;
    let mut siz = cs.siz.clone();
    siz[4..6].copy_from_slice(&0u16.to_be_bytes());
    for (k, v) in [x0 + cs.tile_w, y0 + cs.tile_h, x0, y0, w, h, x0, y0].iter().enumerate() {
        siz[6 + 4 * k..10 + 4 * k].copy_from_slice(&(*v as u32).to_be_bytes());
    }
    let (s, psot) = cs.tiles[t as usize];
    let sot_seg = |isot: u64, psot: u64| -> Vec<u8> {
        let mut v = vec![0xFF, 0x90, 0x00, 0x0A];
        v.extend_from_slice(&(isot as u16).to_be_bytes());
        v.extend_from_slice(&(psot as u32).to_be_bytes());
        v.extend_from_slice(&[0, 1]);
        v
    };
    let mut tail = Vec::new();
    for (k, &e) in packets.iter().enumerate() {
        tail.extend(sot_seg(k as u64 + 1, 14 + e));
        tail.extend_from_slice(&[0xFF, 0x93]);
        tail.extend(std::iter::repeat_n(0u8, e as usize));
    }
    tail.extend_from_slice(&[0xFF, 0xD9]);
    let mut head = vec![0xFF, 0x4F];
    head.extend(siz);
    Ok((head, sot_seg(0, psot), (s + 12, psot - 12), tail, a * b > 1))
}

fn hex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect()
}

fn unhex(s: &str) -> Vec<u8> {
    (0..s.len() / 2).map(|k| u8::from_str_radix(&s[2 * k..2 * k + 2], 16).unwrap_or(0)).collect()
}

impl Codestream {
    /// What the projection needs of the codestream to make its chunks, as the facts hold it.
    pub fn to_facts(&self) -> serde_json::Value {
        serde_json::json!({
            "n": self.n, "c0": self.c0, "siz": hex(&self.siz), "rest": hex(&self.rest),
            "width": self.width, "height": self.height, "tile_w": self.tile_w, "tile_h": self.tile_h,
            "components": self.components, "precision": self.precision, "layers": self.layers,
            "coding": self.coding.iter().map(|(n, pp)| serde_json::json!([n, pp.iter().map(|p| [p.0, p.1]).collect::<Vec<_>>()])).collect::<Vec<_>>(),
            "tiles": self.tiles.iter().map(|t| [t.0, t.1]).collect::<Vec<_>>(),
        })
    }

    /// The codestream the facts hold ([`Codestream::to_facts`]).
    pub fn from_facts(f: &serde_json::Value) -> Codestream {
        let n = |k: &str| f[k].as_u64().unwrap_or(0);
        let pair = |v: &serde_json::Value| (v[0].as_u64().unwrap_or(0), v[1].as_u64().unwrap_or(0));
        let arr = |v: &serde_json::Value| v.as_array().cloned().unwrap_or_default();
        Codestream {
            n: n("n"), c0: n("c0"), siz: unhex(f["siz"].as_str().unwrap_or("")), rest: unhex(f["rest"].as_str().unwrap_or("")),
            width: n("width"), height: n("height"), tile_w: n("tile_w"), tile_h: n("tile_h"), components: n("components"),
            precision: n("precision"), layers: n("layers"),
            coding: arr(&f["coding"]).iter().map(|c| (c[0].as_u64().unwrap_or(0), arr(&c[1]).iter().map(pair).collect())).collect(),
            tiles: arr(&f["tiles"]).iter().map(pair).collect(),
            ..Default::default()
        }
    }
}
