//! What the GRIB profile reads of a field's sections 1, 3, 4, 5 and 6 (spec/zarrify/grib.md §2
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/grib.md#2-the-source>): the reference
//! time, the grid, the product's identity and its dimension coordinates (the forecast step, the
//! level, the ensemble member), the packing and the bitmap indicator. Octets are numbered from 1
//! at the start of their section, as WMO-No. 306 FM 92 numbers them; `o(n)` is octet `n`'s index.

use super::sections::{signed, uint};

/// The index in a section's bytes of its octet `n`.
const fn o(n: usize) -> usize {
    n - 1
}

/// `v × 10^−k`: `10^|k|` by multiplications (exact to 10^22), then one division or product, so
/// that every host gets the same bits.
fn scale(v: f64, k: i64) -> f64 {
    let p = (0..k.unsigned_abs()).fold(1.0f64, |x, _| x * 10.0);
    if k >= 0 { v / p } else { v * p }
}

/// All ones in `n` octets: GRIB's missing value.
fn missing(v: u64, n: usize) -> bool {
    v == if n >= 8 { u64::MAX } else { (1u64 << (8 * n)) - 1 }
}

/// Seconds since 1970-01-01T00:00:00Z of a proleptic Gregorian date and time (days from civil,
/// H. Hinnant's algorithm), or None for a date or time out of range.
pub fn epoch_seconds(y: i64, m: u64, d: u64, hh: u64, mm: u64, ss: u64) -> Option<i64> {
    if !(1..=12).contains(&m) || !(1..=31).contains(&d) || hh > 23 || mm > 59 || ss > 60 {
        return None;
    }
    let y = if m <= 2 { y - 1 } else { y };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let mp = (m as i64 + 9) % 12;
    let doy = (153 * mp + 2) / 5 + d as i64 - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    let days = era * 146097 + doe - 719468;
    Some(days * 86400 + (hh * 3600 + mm * 60 + ss) as i64)
}

/// A date and time of 7 octets at `at` (year in 2, then month, day, hour, minute, second), as
/// seconds since the epoch.
fn time_at(b: &[u8], at: usize) -> Option<i64> {
    if b.len() < at + 7 {
        return None;
    }
    epoch_seconds(uint(b, at, 2) as i64, b[at + 2] as u64, b[at + 3] as u64, b[at + 4] as u64, b[at + 5] as u64, b[at + 6] as u64)
}

/// Section 1, the identification section: the reference time, and the octets that identify the
/// field beside it (all but the reference time's, octets 13–19).
#[derive(Clone, Debug, PartialEq)]
pub struct Ident {
    pub centre: u64,
    pub subcentre: u64,
    pub master_tables: u8,
    pub local_tables: u8,
    pub significance: u8,
    pub reference: i64,
    pub status: u8,
    pub kind: u8,
    /// the identifying octets: the section from octet 6, the reference time's octets left out
    pub key: Vec<u8>,
}

pub fn ident(s1: &[u8]) -> Result<Ident, String> {
    if s1.len() < 21 {
        return Err(format!("a GRIB section 1 of {} octets, fewer than 21", s1.len()));
    }
    let reference = time_at(s1, o(13)).ok_or("a GRIB section 1's reference time is not a date and time")?;
    let mut key = s1[o(6)..o(13)].to_vec();
    key.extend_from_slice(&s1[o(20)..]);
    Ok(Ident {
        centre: uint(s1, o(6), 2),
        subcentre: uint(s1, o(8), 2),
        master_tables: s1[o(10)],
        local_tables: s1[o(11)],
        significance: s1[o(12)],
        reference,
        status: s1[o(20)],
        kind: s1[o(21)],
        key,
    })
}

/// The shape of the Earth (code table 3.2): a sphere's radius, or an ellipsoid's semi-major
/// axis and inverse flattening, in meters.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Earth {
    Sphere(f64),
    Ellipsoid { a: f64, inverse_flattening: f64 },
}

/// A scaled value of section 3: `value × 10^−factor`, the factor 1 signed octet, the value 4
/// unsigned ones; None when either is missing.
fn scaled(b: &[u8], at: usize) -> Option<f64> {
    let f = b[at];
    let v = uint(b, at + 1, 4);
    if f == 0xff || missing(v, 4) {
        return None;
    }
    Some(scale(v as f64, signed(b, at, 1)))
}

/// The Earth of shape `shape` and the octets 15–30 of a grid template that has them, or None for
/// a shape this profile does not name.
fn earth(s3: &[u8]) -> Option<Earth> {
    let shape = s3[o(15)];
    let radius = scaled(s3, o(16));
    let (major, minor) = (scaled(s3, o(21)), scaled(s3, o(26)));
    let ellipsoid = |a: f64, b: f64| Earth::Ellipsoid { a, inverse_flattening: a / (a - b) };
    Some(match shape {
        0 => Earth::Sphere(6_367_470.0),
        1 => Earth::Sphere(radius?),
        2 => Earth::Ellipsoid { a: 6_378_160.0, inverse_flattening: 297.0 },
        3 => ellipsoid(major? * 1000.0, minor? * 1000.0),
        4 => Earth::Ellipsoid { a: 6_378_137.0, inverse_flattening: 298.257_222_101 },
        5 => Earth::Ellipsoid { a: 6_378_137.0, inverse_flattening: 298.257_223_563 },
        6 => Earth::Sphere(6_371_229.0),
        7 => ellipsoid(major?, minor?),
        8 => Earth::Sphere(6_371_200.0),
        _ => return None,
    })
}

/// A grid of the templates this profile presents (spec/zarrify/grib.md §2.4
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/grib.md#24-grids>).
#[derive(Clone, Debug, PartialEq)]
pub enum Projection {
    /// 3.0: the first point's latitude and longitude, and the increments, in degrees, each
    /// increment with the direction the scanning mode gives it
    LatLon { lat0: f64, lon0: f64, dlat: f64, dlon: f64 },
    /// 3.30: the first point's latitude and longitude, LaD, LoV, the increments in meters (with
    /// their directions), the two secants' latitudes, and whether the projection's pole is the
    /// south pole
    Lambert { lat0: f64, lon0: f64, lad: f64, lov: f64, dx: f64, dy: f64, latin1: f64, latin2: f64, south: bool },
}

/// Section 3, the grid definition section.
#[derive(Clone, Debug, PartialEq)]
pub struct Grid {
    pub template: u64,
    pub points: u64,
    pub nx: u64,
    pub ny: u64,
    pub scan: u8,
    pub earth: Option<Earth>,
    /// None for a template this profile does not present, or one whose values it cannot use
    pub projection: Option<Projection>,
    /// why the grid is not presented, when it is not
    pub reason: Option<&'static str>,
    /// the identifying octets: the section from octet 6
    pub key: Vec<u8>,
}

/// An angle of a lat/lon template: `v` (a signed integer) in units of `basic / subdivisions`
/// degrees, or 10^−6 degree when either is 0 or missing.
fn angle(v: i64, basic: u64, subdivisions: u64) -> f64 {
    if basic == 0 || subdivisions == 0 || missing(basic, 4) || missing(subdivisions, 4) {
        v as f64 / 1e6
    } else {
        v as f64 * basic as f64 / subdivisions as f64
    }
}

pub fn grid(s3: &[u8]) -> Result<Grid, String> {
    if s3.len() < 14 {
        return Err(format!("a GRIB section 3 of {} octets, fewer than 14", s3.len()));
    }
    let template = uint(s3, o(13), 2);
    let points = uint(s3, o(7), 4);
    let mut g = Grid { template, points, nx: 0, ny: 0, scan: 0, earth: None, projection: None, reason: None, key: s3[o(6)..].to_vec() };
    // a list of the number of points along each row or column (a reduced grid)
    if s3[o(6)] != 0 {
        g.reason = Some("grid_source");
        return Ok(g);
    }
    if s3[o(11)] != 0 {
        g.reason = Some("reduced");
        return Ok(g);
    }
    let (min_len, scan_at) = match template {
        0 => (72, 72),
        30 => (81, 65),
        _ => {
            g.reason = Some("grid");
            return Ok(g);
        }
    };
    if s3.len() < min_len {
        return Err(format!("a GRIB section 3 of template 3.{template} has {} octets, fewer than {min_len}", s3.len()));
    }
    g.nx = uint(s3, o(31), 4);
    g.ny = uint(s3, o(35), 4);
    g.scan = s3[o(scan_at)];
    // the codec puts the points in one order whatever bits 3 and 4 say (conventions §12
    // <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#12-grib-messages>), so two grids
    // that differ in those bits alone are one
    g.key[o(scan_at) - o(6)] &= !0x30;
    g.earth = earth(s3);
    if g.nx == 0 || g.ny == 0 || missing(g.nx, 4) || missing(g.ny, 4) || g.nx.checked_mul(g.ny) != Some(points) {
        g.reason = Some("grid_size");
        return Ok(g);
    }
    // +i is west to east unless bit 1 (0x80); +j north to south unless bit 2 (0x40)
    let (si, sj) = (if g.scan & 0x80 != 0 { -1.0 } else { 1.0 }, if g.scan & 0x40 != 0 { 1.0 } else { -1.0 });
    g.projection = match template {
        0 => {
            let (basic, sub) = (uint(s3, o(39), 4), uint(s3, o(43), 4));
            let a = |at: usize| angle(signed(s3, o(at), 4), basic, sub);
            let (lat0, lon0, lat1, lon1) = (a(47), a(51), a(56), a(60));
            let (di, dj) = (uint(s3, o(64), 4), uint(s3, o(68), 4));
            let flags = s3[o(55)];
            // the increments when given (flag bits 3 and 4), else from the corners
            let dlon = if flags & 0x20 != 0 && !missing(di, 4) {
                si * angle(di as i64, basic, sub)
            } else if g.nx > 1 {
                let mut span = lon1 - lon0;
                if si > 0.0 && span < 0.0 {
                    span += 360.0;
                } else if si < 0.0 && span > 0.0 {
                    span -= 360.0;
                }
                span / (g.nx - 1) as f64
            } else {
                0.0
            };
            let dlat = if flags & 0x10 != 0 && !missing(dj, 4) {
                sj * angle(dj as i64, basic, sub)
            } else if g.ny > 1 {
                (lat1 - lat0) / (g.ny - 1) as f64
            } else {
                0.0
            };
            Some(Projection::LatLon { lat0, lon0, dlat, dlon })
        }
        _ => {
            let d = |at: usize| signed(s3, o(at), 4) as f64 / 1e6;
            let (dx, dy) = (uint(s3, o(56), 4), uint(s3, o(60), 4));
            let flags = s3[o(64)];
            if g.earth.is_none() || flags & 0x40 != 0 {
                // a shape this profile cannot model, or a bipolar projection
                g.reason = Some("earth");
                None
            } else {
                Some(Projection::Lambert {
                    lat0: d(39), lon0: d(43), lad: d(48), lov: d(52),
                    dx: si * dx as f64 / 1e3, dy: sj * dy as f64 / 1e3,
                    latin1: d(66), latin2: d(70), south: flags & 0x80 != 0,
                })
            }
        }
    };
    Ok(g)
}

/// The seconds of a unit of time (code table 4.4), or None for a month, year, decade, normal
/// or century, which have none.
pub fn unit_seconds(u: u8) -> Option<i64> {
    Some(match u {
        0 => 60,
        1 => 3600,
        2 => 86400,
        10 => 3 * 3600,
        11 => 6 * 3600,
        12 => 12 * 3600,
        13 => 1,
        _ => return None,
    })
}

/// A fixed surface: its type (code table 4.5) and its value, when it has one.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Surface {
    pub kind: u8,
    pub value: Option<f64>,
}

/// A product template's layout: the octet of its member's number, and the octets of its
/// statistic's end time and count of missing values.
type Layout = (Option<usize>, Option<(usize, usize)>);

/// The product templates whose dimensions this profile names (spec/zarrify/grib.md §2.3
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/grib.md#23-dimensions>): each with
/// octets 10–34 of template 4.0, the octet of its ensemble member's number, if any, and the
/// octets of its statistic's end time and its count of missing values, if any.
fn layout(template: u64) -> Option<Layout> {
    Some(match template {
        0 | 2 | 5 | 6 => (None, None),
        1 => (Some(36), None),
        8 => (None, Some((35, 43))),
        9 => (None, Some((48, 56))),
        10 => (None, Some((36, 44))),
        11 => (Some(36), Some((38, 46))),
        12 => (None, Some((37, 45))),
        _ => return None,
    })
}

/// Section 4, the product definition section.
#[derive(Clone, Debug, PartialEq)]
pub struct Product {
    pub template: u64,
    pub category: u8,
    pub number: u8,
    pub first: Surface,
    pub second: Surface,
    /// seconds from the reference time to the field's time: a statistic's end
    pub step: Option<i64>,
    /// the ensemble member's number
    pub member: Option<u8>,
    /// a statistic's process (code table 4.10) and the length of its interval in seconds
    pub statistic: Option<(u8, Option<i64>)>,
    /// what a probability, percentile or derived ensemble product is of, as a name's part
    /// (spec/zarrify/grib.md §3.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/grib.md#32-names>)
    pub qualifier: Option<String>,
    pub reason: Option<&'static str>,
    /// the identifying octets: the section from octet 6, the dimensions' octets zeroed
    pub key: Vec<u8>,
}

pub fn product(s4: &[u8], reference: i64) -> Result<Product, String> {
    if s4.len() < 9 {
        return Err(format!("a GRIB section 4 of {} octets, fewer than 9", s4.len()));
    }
    let template = uint(s4, o(8), 2);
    let none = Surface { kind: 255, value: None };
    let mut p = Product { template, category: 0, number: 0, first: none, second: none, step: None, member: None, statistic: None,
                          qualifier: None, reason: None, key: Vec::new() };
    let Some((member, interval)) = layout(template) else {
        p.reason = Some("product");
        p.key = s4[o(6)..].to_vec();
        return Ok(p);
    };
    let need = interval.map_or(34, |(_, m)| m + 15).max(member.unwrap_or(0));
    if s4.len() < need {
        return Err(format!("a GRIB section 4 of template 4.{template} has {} octets, fewer than {need}", s4.len()));
    }
    p.category = s4[o(10)];
    p.number = s4[o(11)];
    let surface = |at: usize| {
        let kind = s4[o(at)];
        let (f, v) = (s4[o(at + 1)], uint(s4, o(at + 2), 4));
        let value = (kind != 255 && f != 0xff && !missing(v, 4)).then(|| scale(v as f64, signed(s4, o(at + 1), 1)));
        Surface { kind, value }
    };
    p.first = surface(23);
    p.second = surface(29);
    p.qualifier = qualifier(template, s4);
    let unit = s4[o(18)];
    let forecast = uint(s4, o(19), 4) as i64;
    let mut key = s4[o(6)..].to_vec();
    // the dimensions' octets: the unit and forecast time, the surfaces' scale factors and values
    for r in [o(18)..o(23), o(24)..o(29), o(30)..o(35)] {
        key[r.start - o(6)..r.end - o(6)].fill(0);
    }
    if let Some(m) = member {
        p.member = Some(s4[o(m)]);
        key[o(m) - o(6)] = 0;
    }
    match interval {
        None => p.step = unit_seconds(unit).map(|s| s * forecast),
        Some((end, nmissing)) => {
            p.step = time_at(s4, o(end)).map(|t| t - reference);
            let n = s4[o(nmissing - 1)];
            if n != 1 {
                p.reason = Some("statistic");
            }
            let at = o(nmissing + 4);
            let length = unit_seconds(s4[at + 2]).map(|s| s * uint(s4, at + 3, 4) as i64);
            p.statistic = Some((s4[at], length));
            for r in [o(end)..o(end) + 7, o(nmissing)..o(nmissing) + 4] {
                key[r.start - o(6)..r.end - o(6)].fill(0);
            }
        }
    }
    if p.step.is_none() && p.reason.is_none() {
        p.reason = Some("time");
    }
    p.key = key;
    Ok(p)
}

/// A number as a name's part: its shortest decimal, `p` for the point and `m` for a minus.
fn name_number(v: f64) -> String {
    format!("{v}").replace('-', "m").replace('.', "p")
}

/// What a product of template 4.2 or 4.12 (a derived forecast, code table 4.7), 4.5 or 4.9 (a
/// probability, code table 4.9, of its limits) or 4.6 or 4.10 (a percentile) is of.
fn qualifier(template: u64, s4: &[u8]) -> Option<String> {
    match template {
        2 | 12 => Some(match s4[o(35)] {
            0 => "ensmean".into(),
            1 => "enswmean".into(),
            2 => "enssd".into(),
            3 => "enssdn".into(),
            4 => "ensspread".into(),
            5 => "enslai".into(),
            6 => "enscmean".into(),
            7 => "ensiqr".into(),
            8 => "ensmin".into(),
            9 => "ensmax".into(),
            d => format!("ens{d}"),
        }),
        5 | 9 => {
            let limit = |at: usize| {
                let (f, v) = (s4[o(at)], uint(s4, o(at + 1), 4));
                (f != 0xff && !missing(v, 4)).then(|| name_number(scale(signed(s4, o(at + 1), 4) as f64, signed(s4, o(at), 1))))
            };
            let (lo, up) = (limit(38), limit(43));
            let t = s4[o(37)];
            let q = match t {
                0 => lo.map(|l| format!("lt{l}")),
                1 => up.map(|u| format!("gt{u}")),
                2 => lo.zip(up).map(|(l, u)| format!("{l}to{u}")),
                3 => lo.map(|l| format!("gt{l}")),
                4 => up.map(|u| format!("lt{u}")),
                _ => None,
            }
            .unwrap_or_else(|| format!("type{t}"));
            Some(format!("prob{q}"))
        }
        6 | 10 => Some(format!("p{}", s4[o(35)])),
        _ => None,
    }
}

/// Section 5, the data representation section: its number of values and its template.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Packing {
    pub values: u64,
    pub template: u64,
}

/// The packing templates `refkv.grib` decodes (conventions §12
/// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#12-grib-messages>).
pub const DECODED: [u64; 6] = [0, 2, 3, 4, 41, 42];

pub fn packing(s5: &[u8]) -> Result<Packing, String> {
    if s5.len() < 11 {
        return Err(format!("a GRIB section 5 of {} octets, fewer than 11", s5.len()));
    }
    Ok(Packing { values: uint(s5, o(6), 4), template: uint(s5, o(10), 2) })
}

/// Section 6's bitmap indicator.
pub fn bitmap(s6: &[u8]) -> Result<u8, String> {
    s6.get(o(6)).copied().ok_or_else(|| "a GRIB section 6 of fewer than 6 octets".into())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn epoch_seconds_are_unix_time() {
        assert_eq!(epoch_seconds(1970, 1, 1, 0, 0, 0), Some(0));
        assert_eq!(epoch_seconds(2026, 10, 29, 6, 0, 0), Some(1_793_253_600));
        assert_eq!(epoch_seconds(2000, 2, 29, 0, 0, 0), Some(951_782_400));
        assert_eq!(epoch_seconds(2026, 13, 1, 0, 0, 0), None);
    }
}
