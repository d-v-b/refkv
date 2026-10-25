//! The `omero` windows sampled from the data (spec/zarrify/tiff.md §4.4
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#44-arrays>,
//! spec/zarrify/qptiff.md §4.3
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/qptiff.md#43-arrays>): for the
//! compressions the core decodes, one chunk per channel of the coarsest level, the middle one,
//! whose values inside the array have 5th and 99.9th percentiles, leaving out the fill value,
//! that are the window's start and end. The run reads the chunks (`reads`) after the parser, as it reads the chunks NIfTI
//! and MRC copy; the projection decodes them (`windows`).

use super::{key, Copied};
use crate::out::{omero_window, type_window, Out, Part};
use serde_json::{json, Value as J};
use std::collections::HashMap;

/// The most bytes a sampled chunk has, encoded or decoded: a larger one is not sampled.
pub const MAX_SAMPLE: u64 = 1 << 26;

/// Whether the core decodes the chunks of a TIFF compression: none (1), LZW (5), Deflate (8,
/// 32946). Not JPEG, JPEG 2000 or zstd.
pub fn decodable(compression: u64) -> bool {
    matches!(compression, 1 | 5 | 8 | 32946)
}

/// The chunk a channel's window samples: its key; the rows and columns of it inside the array
/// (`rows` × `cols` of a chunk `width` values wide); and where the channel's values are among a
/// pixel's (the `offset`-th of `stride`, when the samples are interleaved).
#[derive(Clone, Debug, PartialEq)]
pub struct Sample {
    pub key: String,
    rows: u64,
    cols: u64,
    width: u64,
    offset: u64,
    stride: u64,
}

/// The samples of channels 0 to `count` − 1 of the array whose chunk keys start with `prefix`
/// (axes `axes`, ending `y`, `x`; shape `shape`; chunk shape `chunk`, 1 along `t` and `z`;
/// `item` bytes a value): along `t` the first chunk, along `c` the channel's, along every other
/// axis the middle one (the chunk holding index `floor(n / 2)`). None at all when a chunk
/// decodes to more than `MAX_SAMPLE` bytes.
pub fn samples(prefix: &str, axes: &[&str], shape: &[u64], chunk: &[u64], count: u64, item: u64) -> Vec<Sample> {
    let d = axes.len();
    if d < 2 || chunk.iter().try_fold(item, |a, &k| a.checked_mul(k)).is_none_or(|n| n > MAX_SAMPLE) {
        return Vec::new();
    }
    let c = axes.iter().position(|a| *a == "c");
    let stride = c.map_or(1, |i| chunk[i]);
    let middle = |i: usize| shape[i] / 2 / chunk[i];
    // the rows and columns of the middle chunk that are inside the array
    let inside = |i: usize| chunk[i].min(shape[i] - middle(i) * chunk[i]);
    (0..count)
        .map(|ch| {
            let coords = (0..d).map(|i| match axes[i] {
                "t" => 0,
                "c" => ch / chunk[i],
                _ => middle(i),
            });
            Sample { key: key(prefix, coords), rows: inside(d - 2), cols: inside(d - 1), width: chunk[d - 1], offset: ch % stride, stride }
        })
        .collect()
}

/// The bytes the parts of a reference name, in all.
fn encoded_len(parts: &[Part]) -> u64 {
    parts.iter().map(|p| match p {
        Part::Src(_, n) | Part::Data(_, _, n) | Part::Obj(_, _, n) => *n,
        Part::Lit(b) => b.len() as u64,
    }).fold(0, u64::saturating_add)
}

/// The source's ranges that the samples' chunks reference (none for an absent chunk, or one
/// of more than `MAX_SAMPLE` bytes).
pub fn reads(out: &Out, samples: &[Sample]) -> Vec<(u64, u64)> {
    samples
        .iter()
        .filter_map(|s| out.reference(&s.key))
        .filter(|parts| encoded_len(parts) <= MAX_SAMPLE)
        .flatten()
        .filter_map(|p| match p {
            Part::Src(o, n) => Some((*o, *n)),
            _ => None,
        })
        .collect()
}

/// A chunk's encoded bytes from its reference: the source's from `copied`, the data
/// sources' and literals' from `out`. None when it is absent, too large, or a range was not read.
fn encoded(out: &Out, key: &str, copied: &Copied) -> Option<Vec<u8>> {
    let parts = out.reference(key)?;
    if encoded_len(parts) > MAX_SAMPLE {
        return None;
    }
    let mut b = Vec::new();
    for p in parts {
        match p {
            Part::Src(o, n) => b.extend_from_slice(copied.get(&(*o, *n))?),
            Part::Data(i, o, n) => {
                let d = out.data.get((*i as usize).checked_sub(1)?)?;
                b.extend_from_slice(d.get(*o as usize..(*o + *n) as usize)?);
            }
            Part::Lit(l) => b.extend_from_slice(l),
            Part::Obj(..) => return None,
        }
    }
    Some(b)
}

/// A chunk's decoded bytes (`size` of them) under a TIFF compression; None when it does not
/// decode to exactly `size` bytes.
fn decode(compression: u64, b: Vec<u8>, size: u64) -> Option<Vec<u8>> {
    let d = match compression {
        1 => b,
        5 => crate::lzw::decode(&b, size).ok()?,
        8 | 32946 => miniz_oxide::inflate::decompress_to_vec_zlib_with_limit(&b, size as usize).ok()?,
        _ => return None,
    };
    (d.len() as u64 == size).then_some(d)
}

/// A channel's values of a decoded chunk.
enum Values {
    Int(Vec<i128>),
    Float(Vec<f64>),
}

/// The bytes of a value of a data type.
fn type_size(data_type: &str) -> Option<u64> {
    let bits: u64 = data_type.trim_start_matches(char::is_alphabetic).parse().ok()?;
    Some(bits / 8)
}

/// A sample's values of its decoded chunk `b` (row-major, a pixel's `stride` values
/// interleaved), of data type `data_type` in the byte order `little`: those of the rows and
/// columns inside the array.
fn values(b: &[u8], data_type: &str, little: bool, s: &Sample) -> Option<Values> {
    fn take<'a, const W: usize>(b: &'a [u8], s: &'a Sample, little: bool) -> impl Iterator<Item = [u8; W]> + 'a {
        (0..s.rows).flat_map(move |r| (0..s.cols).map(move |c| ((r * s.width + c) * s.stride + s.offset) as usize)).map(move |i| {
            let mut a: [u8; W] = b[i * W..(i + 1) * W].try_into().unwrap();
            if !little {
                a.reverse();
            }
            a
        })
    }
    let size = type_size(data_type)?;
    let last = (s.rows.checked_sub(1)? * s.width + s.cols.checked_sub(1)?) * s.stride + s.offset;
    if (last + 1) * size > b.len() as u64 {
        return None;
    }
    let l = little;
    Some(match data_type {
        "uint8" => Values::Int(take::<1>(b, s, l).map(|a| a[0] as i128).collect()),
        "int8" => Values::Int(take::<1>(b, s, l).map(|a| a[0] as i8 as i128).collect()),
        "uint16" => Values::Int(take::<2>(b, s, l).map(|a| u16::from_le_bytes(a) as i128).collect()),
        "int16" => Values::Int(take::<2>(b, s, l).map(|a| i16::from_le_bytes(a) as i128).collect()),
        "uint32" => Values::Int(take::<4>(b, s, l).map(|a| u32::from_le_bytes(a) as i128).collect()),
        "int32" => Values::Int(take::<4>(b, s, l).map(|a| i32::from_le_bytes(a) as i128).collect()),
        "uint64" => Values::Int(take::<8>(b, s, l).map(|a| u64::from_le_bytes(a) as i128).collect()),
        "int64" => Values::Int(take::<8>(b, s, l).map(|a| i64::from_le_bytes(a) as i128).collect()),
        "float32" => Values::Float(take::<4>(b, s, l).map(|a| f32::from_le_bytes(a) as f64).collect()),
        "float64" => Values::Float(take::<8>(b, s, l).map(f64::from_le_bytes).collect()),
        _ => return None,
    })
}

/// The 5th and 99.9th percentiles of sorted values: those at indices `floor(n × 5 / 100)` and
/// `min(n − 1, floor(n × 999 / 1000))`; None when there are none, or they are equal.
fn percentiles<T: Copy + PartialOrd>(v: &[T]) -> Option<(T, T)> {
    let n = v.len() as u64;
    if n == 0 {
        return None;
    }
    let (lo, hi) = (v[(n * 5 / 100) as usize], v[(n * 999 / 1000).min(n - 1) as usize]);
    (lo < hi).then_some((lo, hi))
}

/// An integer of a data type as JSON.
fn int_json(v: i128) -> J {
    i64::try_from(v).map(J::from).unwrap_or_else(|_| json!(v as u64))
}

/// A channel's window from its values, leaving out the fill value 0 (and, of a float type, the
/// values that are not finite): `start` and `end` their percentiles; `min` and `max` an integer
/// type's range, or the least and greatest of a float type's values.
fn window(v: Values, data_type: &str) -> Option<J> {
    match v {
        Values::Int(mut v) => {
            v.retain(|&x| x != 0);
            v.sort_unstable();
            let (lo, hi) = percentiles(&v)?;
            let mut w = type_window(data_type)?;
            w["start"] = int_json(lo);
            w["end"] = int_json(hi);
            Some(w)
        }
        Values::Float(mut v) => {
            v.retain(|x| x.is_finite() && *x != 0.0);
            v.sort_unstable_by(f64::total_cmp);
            let (lo, hi) = percentiles(&v)?;
            Some(omero_window(json!(v[0]), json!(v[v.len() - 1]), json!(lo), json!(hi)))
        }
    }
}

/// Each sample's window (`window`), None where its chunk is absent or not read, or does not
/// decode to `size` bytes under `compression`, or its values give none. The chunks hold
/// `data_type` in the byte order `little`; `copied` holds the bytes `reads` gave.
pub fn windows(out: &Out, samples: &[Sample], copied: &Copied, compression: u64, data_type: &str, little: bool, size: u64) -> Vec<Option<J>> {
    let mut decoded: HashMap<&str, Option<Vec<u8>>> = HashMap::new();
    samples
        .iter()
        .map(|s| {
            let d = decoded
                .entry(&s.key)
                .or_insert_with(|| encoded(out, &s.key, copied).and_then(|b| decode(compression, b, size)));
            window(values(d.as_ref()?, data_type, little, s)?, data_type)
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn samples_take_the_middle_chunk_of_each_channel() {
        let s = samples("2/c/", &["t", "c", "z", "y", "x"], &[3, 2, 5, 100, 70], &[1, 1, 1, 32, 32], 2, 2);
        assert_eq!(s.iter().map(|s| s.key.as_str()).collect::<Vec<_>>(), ["2/c/0/0/2/1/1", "2/c/0/1/2/1/1"]);
        assert_eq!((s[0].rows, s[0].cols, s[0].width), (32, 32, 32));
        let s = samples("0/c/", &["c", "y", "x"], &[3, 10, 20], &[3, 16, 16], 3, 1);
        assert_eq!(s[2], Sample { key: "0/c/0/0/0".into(), rows: 10, cols: 16, width: 16, offset: 2, stride: 3 });
        assert_eq!(samples("0/c/", &["y", "x"], &[1 << 20, 1 << 20], &[1 << 14, 1 << 14], 1, 1), vec![]);
    }

    #[test]
    fn windows_are_the_percentiles_without_the_fill_value() {
        let ints: Vec<i128> = (0..=1000).collect();
        let w = window(Values::Int(ints), "uint16").unwrap();
        assert_eq!(w, json!({"min": 0, "max": 65535, "start": 51, "end": 1000}));
        let w = window(Values::Float(vec![0.0, -2.5, f64::NAN, 1.0, 4.0, f64::INFINITY]), "float32").unwrap();
        assert_eq!(w, json!({"min": -2.5, "max": 4.0, "start": -2.5, "end": 4.0}));
        assert_eq!(window(Values::Int(vec![0, 7, 7]), "uint8"), None);
        assert_eq!(window(Values::Float(vec![0.0, f64::NAN]), "float64"), None);
        let w = window(Values::Int(vec![u64::MAX as i128, 1]), "uint64").unwrap();
        assert_eq!(w["end"], json!(u64::MAX));
    }

    #[test]
    fn values_read_the_byte_order_the_interleave_and_the_inside() {
        // a 2 × 2 chunk of 2 interleaved uint16 samples, whose second column is outside
        let b = [1u8, 0, 2, 0, 3, 0, 4, 0, 5, 0, 6, 0, 7, 0, 8, 0];
        let s = Sample { key: String::new(), rows: 2, cols: 1, width: 2, offset: 1, stride: 2 };
        let Some(Values::Int(v)) = values(&b, "uint16", true, &s) else { panic!() };
        assert_eq!(v, [2, 6]);
        let s = Sample { offset: 0, ..s };
        let Some(Values::Int(v)) = values(&b, "int16", false, &s) else { panic!() };
        assert_eq!(v, [256, 1280]);
    }
}
