//! The `refkv.grib` codec (spec/conventions.md §12
//! <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#12-grib-messages>): decodes one
//! field of a GRIB edition 2 message (WMO-No. 306, FM 92 GRIB edition 2), the chunk's bytes, into
//! the values of its grid as binary64, NaN where its bitmap or its complex packing's missing
//! values leave a point out. The values are computed as eccodes computes them, bit for bit:
//! `(X × 2^E + R) × 10^−D`, with 10^−D as eccodes' `codes_power` makes it (D divisions of 1 by
//! 10, or −D multiplications), and `R` alone for a bit width of 0 (or a complex packing of no
//! groups), as eccodes, NCEP's g2c and gribberish read a constant field, whose `R` their
//! encoders write as the value itself. One implementation serves Python and the browser.

mod aec;
mod png;

use super::sections::{Field, Section, fields, indicator, sections, signed, uint};
use serde_json::Value as J;

type Res<T> = Result<T, String>;

fn err<T>(m: impl std::fmt::Display) -> Res<T> {
    Err(format!("refkv.grib: {m}"))
}

/// The most points a grid may have (spec/zarrify/grib.md's budget).
const MAX_POINTS: u64 = 1 << 31;

/// The codec's configuration (the array's `codecs` entry): the field of the message to decode,
/// from 0 in the order of its section 7s.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Config {
    pub field: usize,
}

impl Config {
    /// The configuration object: `field` (optional, a non-negative integer, 0 when absent), and
    /// no other member.
    pub fn from_json(v: &J) -> Res<Config> {
        let Some(o) = v.as_object() else {
            return err("the configuration is not an object");
        };
        if let Some(k) = o.keys().find(|k| *k != "field") {
            return err(format!("the configuration has an unknown member {k:?}"));
        }
        let field = match o.get("field") {
            None => 0,
            Some(f) => match f.as_u64().and_then(|f| usize::try_from(f).ok()) {
                Some(f) => f,
                None => return err(format!("the configuration's field is {f}, not a non-negative integer")),
            },
        };
        Ok(Config { field })
    }
}

/// A message's bytes and its fields.
struct Message<'a> {
    b: &'a [u8],
    fields: Vec<Field>,
}

impl<'a> Message<'a> {
    /// The message whose bytes are the whole of `b`: its sections and fields.
    fn parse(b: &'a [u8]) -> Res<Self> {
        let ind = indicator(b).or_else(err)?;
        let len = b.len() as u64;
        if len < ind.length {
            return err(format!("the chunk is {len} bytes, shorter than its message's length, {}: the message is truncated", ind.length));
        }
        if len > ind.length {
            return err(format!("the chunk is {len} bytes, longer than its message's length, {}", ind.length));
        }
        if &b[b.len() - 4..] != b"7777" {
            return err("the message does not end with 7777");
        }
        let s = sections(b, ind.length).or_else(err)?;
        let fields = fields(&s).or_else(err)?;
        Ok(Message { b, fields })
    }

    fn field(&self, k: usize) -> Res<&Field> {
        match self.fields.get(k) {
            Some(f) => Ok(f),
            None => err(format!("field {k} was asked for, and the message has {} fields", self.fields.len())),
        }
    }

    /// A section's bytes, from its length on (octet n of the section is `[n − 1]`).
    fn sec(&self, s: Option<Section>, number: u8) -> Res<&'a [u8]> {
        match s {
            Some(s) => Ok(&self.b[s.offset as usize..(s.offset + s.length) as usize]),
            None => err(format!("the field has no section {number}")),
        }
    }
}

/// `s` holds at least `n` octets.
fn need(s: &[u8], n: usize, what: &str) -> Res<()> {
    if s.len() < n {
        return err(format!("section {} is {} octets, too short for {what}", s[4], s.len()));
    }
    Ok(())
}

/// A grid of section 3: Ni points along a row (i), Nj rows (j), and the scanning mode (flag table
/// 3.4).
#[derive(Clone, Copy, Debug)]
struct Grid {
    ni: u64,
    nj: u64,
    scan: u8,
}

/// The grid of section 3 `s3`, for the grid templates whose Ni (or Nx) and Nj (or Ny) are at
/// octets 31–34 and 35–38: 3.0, 3.1 and 3.40 (scanning mode at octet 72), 3.10 (octet 60), 3.20
/// and 3.30 (octet 65).
fn grid(s3: &[u8]) -> Res<Grid> {
    need(s3, 14, "its grid template number")?;
    let template = uint(s3, 12, 2);
    let scan_at = match template {
        0 | 1 | 40 => 71,
        10 => 59,
        20 | 30 => 64,
        t => return err(format!("grid template 3.{t} is not decoded")),
    };
    need(s3, scan_at + 1, &format!("grid template 3.{template}"))?;
    if s3[10] != 0 {
        return err("a grid with a list of numbers of points (section 3, octet 11) is not decoded");
    }
    let (ni, nj, points) = (uint(s3, 30, 4), uint(s3, 34, 4), uint(s3, 6, 4));
    if ni == 0 || nj == 0 || ni.checked_mul(nj) != Some(points) {
        return err(format!("the grid is {ni} × {nj} points, and section 3 gives {points}"));
    }
    if points > MAX_POINTS {
        return err(format!("the grid has {points} points, more than {MAX_POINTS}"));
    }
    let scan = s3[scan_at];
    if scan & 0x0f != 0 {
        return err(format!("scanning mode 0x{scan:02x} (rows or columns offset) is not decoded"));
    }
    Ok(Grid { ni, nj, scan })
}

/// The grid of field `field` of `message`: its rows and the points of each, (ny, nx) = (Nj, Ni),
/// the shape `decode` gives its values.
pub fn grid_shape(message: &[u8], field: usize) -> Res<(u64, u64)> {
    let m = Message::parse(message)?;
    section3_shape(m.sec(m.field(field)?.s3, 3)?)
}

/// The grid of a section 3 alone (its bytes from its length on), (ny, nx) = (Nj, Ni), or why the
/// codec does not decode it: for a reader of a message's head, which does not hold section 7.
pub fn section3_shape(s3: &[u8]) -> Res<(u64, u64)> {
    let g = grid(s3)?;
    Ok((g.nj, g.ni))
}

/// eccodes' `codes_power(s, n)`: n^s by |s| multiplications, or divisions, of 1 (exact for the
/// powers of 2, and for 10^s up to s = 22).
fn power(s: i64, n: f64) -> f64 {
    let mut v = 1.0;
    for _ in 0..s.unsigned_abs() {
        v = if s < 0 { v / n } else { v * n };
    }
    v
}

/// A field's scaling (FM 92 regulation 92.9.4): the reference value R (binary32), 2^E and 10^−D,
/// from section 5's octets 12–19.
struct Scale {
    r: f64,
    bscale: f64,
    dscale: f64,
}

impl Scale {
    fn of(s5: &[u8]) -> Res<Scale> {
        need(s5, 21, "its packing's scaling")?;
        let r = f32::from_bits(uint(s5, 11, 4) as u32) as f64;
        Ok(Scale { r, bscale: power(signed(s5, 15, 2), 2.0), dscale: power(-signed(s5, 17, 2), 10.0) })
    }

    fn value(&self, x: f64) -> f64 {
        (x * self.bscale + self.r) * self.dscale
    }
}

/// Bits read most significant first.
struct Bits<'a> {
    b: &'a [u8],
    pos: u64,
}

impl<'a> Bits<'a> {
    fn at(b: &'a [u8], octet: u64) -> Bits<'a> {
        Bits { b, pos: octet * 8 }
    }

    /// The next `n` bits (n ≤ 64) as an unsigned integer.
    fn read(&mut self, n: u32) -> Res<u64> {
        let end = self.pos + n as u64;
        if end > self.b.len() as u64 * 8 {
            return err("section 7 ends before its packed values do");
        }
        let mut v = 0u64;
        while self.pos < end {
            let off = (self.pos & 7) as u32;
            let take = (8 - off).min((end - self.pos) as u32);
            let byte = self.b[(self.pos >> 3) as usize] as u64;
            v = (v << take) | ((byte >> (8 - off - take)) & ((1 << take) - 1));
            self.pos += take as u64;
        }
        Ok(v)
    }

    /// The next `n` bits as a sign-and-magnitude integer (regulation 92.1.5).
    fn read_signed(&mut self, n: u32) -> Res<i64> {
        if n == 0 {
            return Ok(0);
        }
        let v = self.read(n)?;
        let sign = 1u64 << (n - 1);
        let m = (v & (sign - 1)) as i64;
        Ok(if v & sign != 0 { -m } else { m })
    }
}

/// 2^w − 1, the largest integer of `w` bits.
fn ones(w: u32) -> u64 {
    if w >= 64 { u64::MAX } else { (1 << w) - 1 }
}

/// Template 5.0 (and 7.0): `n` integers of the bit width.
fn simple(s5: &[u8], data: &[u8], n: usize) -> Res<Vec<f64>> {
    let scale = Scale::of(s5)?;
    let bits = s5[19] as u32;
    if bits == 0 {
        return Ok(vec![scale.r; n]);
    }
    if bits > 64 {
        return err(format!("a bit width of {bits} is not decoded"));
    }
    if (n as u64) * bits as u64 > data.len() as u64 * 8 {
        return err(format!("section 7 holds {} octets, fewer than {n} values of {bits} bits", data.len()));
    }
    let mut b = Bits::at(data, 0);
    (0..n).map(|_| Ok(scale.value(b.read(bits)? as f64))).collect()
}

/// A complex packing's missing value, in its integers.
const GONE: i64 = i64::MIN;

/// Templates 5.2 and 5.3 (and 7.2, 7.3): groups of integers, each a reference plus values of the
/// group's width; with 5.3, spatial differences of order 1 or 2 (code table 5.6). Missing values
/// (octet 23, code table 5.5: 1 primary, 2 primary and secondary) are each group's largest
/// integers of its width (of the reference's width for a group of width 0), and NaN. Read as
/// eccodes reads them: the first values of 5.3's extra descriptors unsigned, the overall minimum
/// of the differences signed; the differences restart at the first values past missing points.
fn complex(s5: &[u8], data: &[u8], n: usize, spatial: bool) -> Res<Vec<f64>> {
    let t = if spatial { 3 } else { 2 };
    need(s5, if spatial { 49 } else { 47 }, &format!("template 5.{t}"))?;
    let scale = Scale::of(s5)?;
    let bits = s5[19] as u32;
    let mvm = s5[22];
    if mvm > 2 {
        return err(format!("missing value management {mvm} (template 5.{t}, octet 23) is not decoded"));
    }
    let groups = uint(s5, 31, 4);
    let (wref, wbits) = (s5[35] as u64, s5[36] as u32);
    let (lref, linc, last, lbits) = (uint(s5, 37, 4), s5[41] as u64, uint(s5, 42, 4), s5[46] as u32);
    let (order, ndes) = if spatial { (s5[47] as u32, s5[48] as u32) } else { (0, 0) };
    if spatial && !(1..=2).contains(&order) {
        return err(format!("spatial differencing of order {order} (template 5.3, octet 48) is not decoded"));
    }
    if groups == 0 {
        return Ok(vec![scale.r; n]);
    }
    if groups > n as u64 {
        return err(format!("{groups} groups for {n} values"));
    }
    if bits > 64 || wbits > 64 || lbits > 64 || ndes > 8 {
        return err("a width of the complex packing's integers is past 64 bits");
    }
    // the streams of section 7: the extra descriptors, then the groups' references, widths and
    // lengths, each from an octet
    let desc = order as u64 * 8 * ndes as u64 + if spatial { 8 * ndes as u64 } else { 0 };
    let octets = |bits: u64| bits.div_ceil(8);
    let at_widths = octets(desc + groups * bits as u64);
    let at_lengths = at_widths + octets(groups * wbits as u64);
    let at_values = at_lengths + octets(groups * lbits as u64);
    if at_values > data.len() as u64 {
        return err("section 7 ends before its groups' descriptions do");
    }
    let mut refs = Bits::at(data, 0);
    let mut extras = [0i64; 2];
    for e in extras.iter_mut().take(order as usize) {
        *e = refs.read(8 * ndes)? as i64;
    }
    let bias = if spatial { refs.read_signed(8 * ndes)? } else { 0 };
    let mut widths = Bits::at(data, at_widths);
    let mut lengths = Bits::at(data, at_lengths);
    let mut values = Bits::at(data, at_values);
    let mut x: Vec<i64> = Vec::with_capacity(n);
    for g in 0..groups {
        let gref = refs.read(bits)?;
        let w = widths.read(wbits)? + wref;
        let scaled = lengths.read(lbits)?;
        let len = if g == groups - 1 { last } else { scaled.wrapping_mul(linc).wrapping_add(lref) };
        if len > (n - x.len()) as u64 {
            return err(format!("the groups hold more than section 5's {n} values"));
        }
        if w > 64 {
            return err(format!("a group's width of {w} bits is not decoded"));
        }
        let w = w as u32;
        // a group of width 0 is missing when its reference is
        let whole = w == 0 && mvm > 0 && (gref == ones(bits) || (mvm == 2 && gref == ones(bits).wrapping_sub(1)));
        for _ in 0..len {
            let v = values.read(w)?;
            let gone = whole || (w > 0 && mvm > 0 && (v == ones(w) || (mvm == 2 && v == ones(w) - 1)));
            x.push(if gone { GONE } else { gref.wrapping_add(v) as i64 });
        }
    }
    if x.len() != n {
        return err(format!("the groups hold {} values, and section 5 gives {n}", x.len()));
    }
    if spatial {
        undifference(&mut x, order, extras, bias);
    }
    Ok(x.into_iter().map(|v| if v == GONE { f64::NAN } else { scale.value(v as f64) }).collect())
}

/// Spatial differences of order 1 or 2 summed back (FM 92 template 7.3), over the values that are
/// not missing, as eccodes' `post_process`.
fn undifference(x: &mut [i64], order: u32, extras: [i64; 2], bias: i64) {
    let mut present = x.iter_mut().filter(|v| **v != GONE);
    let (mut penultimate, mut last) = (0i64, 0i64);
    if order == 1 {
        let Some(first) = present.next() else { return };
        *first = extras[0];
        last = extras[0];
        for v in present {
            *v = v.wrapping_add(last).wrapping_add(bias);
            last = *v;
        }
    } else {
        for (k, e) in extras.iter().enumerate() {
            let Some(v) = present.next() else { return };
            *v = *e;
            if k == 0 { penultimate = *e } else { last = *e }
        }
        for v in present {
            *v = v.wrapping_add(bias).wrapping_add(last).wrapping_add(last).wrapping_sub(penultimate);
            penultimate = last;
            last = *v;
        }
    }
}

/// Template 5.4: the values themselves, IEEE binary32 or binary64 (octet 12, code table 5.7),
/// big-endian.
fn ieee(s5: &[u8], data: &[u8], n: usize) -> Res<Vec<f64>> {
    need(s5, 12, "template 5.4")?;
    let size = match s5[11] {
        1 => 4,
        2 => 8,
        3 => return err("IEEE binary128 values (template 5.4, precision 3) are not decoded"),
        p => return err(format!("template 5.4's precision {p} is not decoded")),
    };
    if data.len() < n * size {
        return err(format!("section 7 holds {} octets, fewer than {n} values of {size}", data.len()));
    }
    Ok(data[..n * size]
        .chunks(size)
        .map(|c| if size == 4 { f32::from_be_bytes(c.try_into().unwrap()) as f64 } else { f64::from_be_bytes(c.try_into().unwrap()) })
        .collect())
}

/// Template 5.41: a PNG image whose samples, each pixel's bits as one big-endian integer, are
/// the integers (grayscale of 1, 2, 4, 8 or 16 bits, 8-bit RGB as 24 bits, 8-bit RGBA as 32).
fn png_values(s5: &[u8], data: &[u8], n: usize) -> Res<Vec<f64>> {
    let scale = Scale::of(s5)?;
    if s5[19] == 0 {
        return Ok(vec![scale.r; n]);
    }
    let x = png::decode(data).or_else(err)?;
    if x.len() != n {
        return err(format!("the PNG image has {} pixels, and section 5 gives {n} values", x.len()));
    }
    Ok(x.into_iter().map(|v| scale.value(v as f64)).collect())
}

/// Template 5.42: a CCSDS 121.0-B adaptive entropy coded stream (WMO-No. 306 FM 92, template
/// 7.42) of the integers, with section 5's flags (octet 22), block size (23) and reference sample
/// interval (24–25).
fn ccsds(s5: &[u8], data: &[u8], n: usize) -> Res<Vec<f64>> {
    need(s5, 25, "template 5.42")?;
    let scale = Scale::of(s5)?;
    let bits = s5[19] as u32;
    if bits == 0 {
        return Ok(vec![scale.r; n]);
    }
    let x = aec::decode(data, bits, s5[22] as u32, uint(s5, 23, 2) as u32, s5[21], n).or_else(err)?;
    Ok(x.into_iter().map(|v| scale.value(v as f64)).collect())
}

/// The `n` values of a field, by its section 5's template, from its section 7's data (after the
/// section's first five octets); NaN where missing.
fn unpack(s5: &[u8], data: &[u8], n: usize) -> Res<Vec<f64>> {
    need(s5, 11, "its template number")?;
    match uint(s5, 9, 2) {
        0 => simple(s5, data, n),
        2 => complex(s5, data, n, false),
        3 => complex(s5, data, n, true),
        4 => ieee(s5, data, n),
        40 => err("JPEG 2000 packing (5.40) is not decoded"),
        41 => png_values(s5, data, n),
        42 => ccsds(s5, data, n),
        t => err(format!("data representation template 5.{t} is not decoded")),
    }
}

/// A field's bitmap (section 6, code table 6.0): indicator 0, the bits that follow; 254, the
/// bitmap of the message's last earlier field that has one (indicator 0); 255, none.
fn bitmap<'a>(m: &Message<'a>, k: usize) -> Res<Option<&'a [u8]>> {
    let s6 = m.sec(m.fields[k].s6, 6)?;
    need(s6, 6, "its bitmap indicator")?;
    match s6[5] {
        0 => Ok(Some(&s6[6..])),
        254 => {
            for f in m.fields[..k].iter().rev() {
                let s = m.sec(f.s6, 6)?;
                if s.len() >= 6 && s[5] == 0 {
                    return Ok(Some(&s[6..]));
                }
            }
            err("bitmap indicator 254, and no earlier field of the message has a bitmap")
        }
        255 => Ok(None),
        i => err(format!("a predefined bitmap (indicator {i}) is not decoded")),
    }
}

/// The bit of point `p` of a bitmap.
fn set(b: &[u8], p: usize) -> bool {
    b[p >> 3] & (0x80 >> (p & 7)) != 0
}

/// Values in the file's order, as [Nj][Ni]: every second line (rows of Ni points, or columns of Nj
/// points when 0x20 says adjacent points in j are consecutive) reversed back when 0x10 says
/// they alternate direction, then columns transposed to rows (0x20). Rows keep the file's order
/// and direction (0x80, 0x40).
fn order(mut v: Vec<f64>, g: Grid) -> Vec<f64> {
    let (ni, nj) = (g.ni as usize, g.nj as usize);
    let len = if g.scan & 0x20 != 0 { nj } else { ni };
    if g.scan & 0x10 != 0 {
        for line in v.chunks_mut(len).skip(1).step_by(2) {
            line.reverse();
        }
    }
    if g.scan & 0x20 == 0 {
        return v;
    }
    let mut out = vec![0.0; v.len()];
    for i in 0..ni {
        for j in 0..nj {
            out[j * ni + i] = v[i * nj + j];
        }
    }
    out
}

/// Decodes field `cfg.field` of the message `message` (the chunk's bytes, sections 0 to 8) into
/// the values of its grid, Nj rows of Ni points in the order `order` gives, NaN where missing;
/// `shape` is the chunk's shape, whose product must be the grid's points.
pub fn decode(message: &[u8], cfg: &Config, shape: &[u64]) -> Res<Vec<f64>> {
    let m = Message::parse(message)?;
    let f = *m.field(cfg.field)?;
    let g = grid(m.sec(f.s3, 3)?)?;
    let points = (g.ni * g.nj) as usize;
    let chunk = shape.iter().try_fold(1u64, |a, &d| a.checked_mul(d));
    if chunk != Some(points as u64) {
        return err(format!("the chunk shape {shape:?} does not hold the grid's {} × {} points", g.nj, g.ni));
    }
    let s5 = m.sec(f.s5, 5)?;
    need(s5, 9, "its number of values")?;
    let n = uint(s5, 5, 4) as usize;
    let mask = bitmap(&m, cfg.field)?;
    if let Some(b) = mask {
        if b.len() * 8 < points {
            return err(format!("the bitmap holds {} bits, fewer than the grid's {points} points", b.len() * 8));
        }
        let marked = (0..points).filter(|&p| set(b, p)).count();
        if marked != n {
            return err(format!("the bitmap marks {marked} points, and section 5 gives {n} values"));
        }
    } else if n != points {
        return err(format!("section 5 gives {n} values for the grid's {points} points"));
    }
    let s7 = m.sec(f.s7, 7)?;
    let values = unpack(s5, &s7[5..], n)?;
    let placed = match mask {
        None => values,
        Some(b) => {
            let mut it = values.into_iter();
            (0..points).map(|p| if set(b, p) { it.next().unwrap_or(f64::NAN) } else { f64::NAN }).collect()
        }
    };
    Ok(order(placed, g))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn power_is_eccodes_codes_power() {
        assert_eq!(power(0, 10.0), 1.0);
        assert_eq!(power(3, 10.0), 1000.0);
        assert_eq!(power(-2, 10.0), 0.01);
        // the nearest binary64 to 1e-6 is not what six divisions give
        assert_eq!(power(-6, 10.0), 1.0000000000000002e-6);
        assert_eq!(power(-3, 2.0), 0.125);
    }

    #[test]
    fn order_reverses_alternate_lines_then_transposes() {
        let g = |scan| Grid { ni: 3, nj: 2, scan };
        let v: Vec<f64> = (0..6).map(|x| x as f64).collect();
        assert_eq!(order(v.clone(), g(0x00)), v);
        assert_eq!(order(v.clone(), g(0x10)), [0., 1., 2., 5., 4., 3.]);
        // columns of 2 points: [0 1] [2 3] [4 5]
        assert_eq!(order(v.clone(), g(0x20)), [0., 2., 4., 1., 3., 5.]);
        assert_eq!(order(v, g(0x30)), [0., 3., 4., 1., 2., 5.]);
    }
}
