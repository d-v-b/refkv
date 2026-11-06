//! The coding routines a layout plugin calls (`refkv.zarrify.coding`, spec/layout.md
//! <https://github.com/d-v-b/refkv/blob/main/spec/layout.md>): a format's coding parameters as
//! the Zarr codecs and data type its profile writes, each by the function the profile itself
//! calls, so that a plugin and refkv cannot disagree. `call` takes a routine's name and its
//! parameters as JSON, and gives its result as JSON.

use crate::out::tiff_codecs;
use crate::expr::V;
use crate::rules::{self, num};
use serde_json::{json, Map, Value as J};

/// Why a routine gave no result.
#[derive(Debug)]
pub enum Refused {
    /// the format's section rejects the parameters: no codec undoes them
    NoCodec(String),
    /// the parameters are not the routine's (a missing or mistyped member)
    Bad(String),
}

type R<T> = Result<T, Refused>;

fn bad<T>(m: impl Into<String>) -> R<T> {
    Err(Refused::Bad(m.into()))
}

/// The routine `name` of the parameters `p` (spec/layout.md).
pub fn call(name: &str, p: &J) -> R<J> {
    match name {
        "tiff_data_type" => tiff_data_type(obj(p, "tags")?).map(J::String),
        "tiff_codecs" => {
            let little = p.get("little_endian").and_then(J::as_bool).map_or_else(|| bad("little_endian is a bool"), Ok)?;
            let x = uint(p, "x_axis")?;
            let row = uint(p, "row_length")?;
            let chunk = match p.get("chunk_shape") {
                None | Some(J::Null) => None,
                Some(J::Array(a)) if a.len() == 2 && a.iter().all(|v| v.as_u64().is_some()) => {
                    Some([a[0].as_u64().unwrap_or(0), a[1].as_u64().unwrap_or(0)])
                }
                Some(_) => return bad("chunk_shape is [rows, columns]"),
            };
            tiff_codecs_of(obj(p, "tags")?, little, x as usize, row, chunk).map(J::Array)
        }
        "czi_codecs" => {
            let (pt, comp) = (int_of(p, "pixel_type")?, int_of(p, "compression")?);
            let flag = p.get("flag").and_then(J::as_bool).map_or_else(|| bad("flag is a bool"), Ok)?;
            let (data_type, _) = czi_type(pt)?;
            if !CZI_COMPRESSIONS.contains(&comp) {
                return Err(Refused::NoCodec(format!("unsupported compression {comp}")));
            }
            Ok(J::Array(crate::project::czi::chain((pt as i32, comp as i32, flag), data_type)))
        }
        "vsi_codecs" => {
            let codec = text(p, "codec")?;
            // the codecs of the ETS header's compressions, the profile's table
            let known = rules::of("vsi").table("compressions").as_object().is_some_and(|t| t.values().any(|v| v == codec));
            if !known {
                return Err(Refused::NoCodec(format!("unsupported codec {codec}")));
            }
            Ok(J::Array(vec![crate::project::vsi::chain(codec, uint(p, "item_size")?)]))
        }
        "dicom_codecs" => {
            let uid = text(p, "transfer_syntax")?;
            let rules = rules::of("dicom");
            let (enc, codec) = crate::dicom::syntax(rules, uid.as_bytes())
                .ok_or_else(|| Refused::NoCodec(format!("unsupported transfer syntax {uid}")))?;
            let data_type = text(p, "data_type")?;
            let endian = if enc.little { "little" } else { "big" };
            Ok(J::Array(vec![crate::project::dicom::chain(codec, data_type, uint(p, "samples")?, endian)]))
        }
        "gzip_codecs" => {
            let level = uint(p, "level")?;
            let level = u8::try_from(level).ok().filter(|l| *l <= 9).map_or_else(|| bad("level is 0 to 9"), Ok)?;
            Ok(J::Array(crate::out::gzip_codecs(uint(p, "skip")?, level)))
        }
        _ => bad(format!("no routine {name}")),
    }
}

/// The compressions the CZI profile reads (spec/zarrify/czi.md §3.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/czi.md#31-pixel-types-and-codecs>).
const CZI_COMPRESSIONS: [i64; 7] = [
    crate::coding::UNCOMPRESSED as i64, crate::coding::JPEG as i64, crate::coding::LJPEG as i64,
    crate::coding::LZW as i64, crate::coding::JPEGXR as i64, crate::coding::ZSTD0 as i64, crate::coding::ZSTD1 as i64,
];

/// A CZI pixel type's (data type, samples a pixel), or NoCodec.
fn czi_type(pt: i64) -> R<(&'static str, u64)> {
    let t = i32::try_from(pt).ok().and_then(crate::coding::pixel_type);
    t.map(|(dt, p, _)| (dt, p)).ok_or_else(|| Refused::NoCodec(format!("unsupported pixel type {pt}")))
}

fn int_of(p: &J, k: &str) -> R<i64> {
    p.get(k).and_then(J::as_i64).map_or_else(|| bad(format!("{k} is an integer")), Ok)
}

fn text<'a>(p: &'a J, k: &str) -> R<&'a str> {
    p.get(k).and_then(J::as_str).map_or_else(|| bad(format!("{k} is a string")), Ok)
}

fn obj<'a>(p: &'a J, k: &str) -> R<&'a Map<String, J>> {
    p.get(k).and_then(J::as_object).map_or_else(|| bad(format!("{k} is an object")), Ok)
}

fn uint(p: &J, k: &str) -> R<u64> {
    p.get(k).and_then(J::as_u64).map_or_else(|| bad(format!("{k} is a natural number")), Ok)
}

/// A TIFF tag's integers, as the IR's value holds them: one integer, or a list of them (a count
/// above one); None when the tags do not have it.
fn ints(tags: &Map<String, J>, tag: u16) -> R<Option<Vec<u64>>> {
    let Some(v) = tags.get(&tag.to_string()) else { return Ok(None) };
    let list: Vec<&J> = match v {
        J::Array(a) => a.iter().collect(),
        one => vec![one],
    };
    list.iter()
        .map(|x| x.as_u64().map_or_else(|| bad(format!("tag {tag} is not integers: {v}")), Ok))
        .collect::<R<Vec<u64>>>()
        .map(Some)
}

/// A tag's one integer (its first: every sample's BitsPerSample must agree), else `default`.
fn int(tags: &Map<String, J>, tag: u16, default: u64) -> R<u64> {
    let Some(v) = ints(tags, tag)? else { return Ok(default) };
    match v.first() {
        None => bad(format!("tag {tag} is empty")),
        Some(&first) if v.iter().all(|&x| x == first) => Ok(first),
        Some(_) if matches!(tag, 258 | 339) => Err(Refused::NoCodec(format!("samples of different formats (tag {tag}: {v:?})"))),
        Some(&first) => Ok(first),
    }
}

/// The data type of TIFF samples of SampleFormat `sample_format` and BitsPerSample `bits`
/// (spec/zarrify/tiff.md §4.3
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#43-data-types-and-codecs>):
/// packed samples, of 1 or 12 bits, held in the smallest type that takes them. The profile's
/// checks have accepted them.
pub fn tiff_data_type_of(sample_format: u64, bits: u64) -> Result<String, String> {
    let kind = match sample_format {
        1 => "uint",
        2 => "int",
        3 => "float",
        f => return Err(format!("internal: sample format {f}")),
    };
    let bits = match bits {
        1 => 8,
        12 => 16,
        bits => bits,
    };
    Ok(format!("{kind}{bits}"))
}

/// The image's format, as the TIFF profile checks it (its `image` role): BitsPerSample,
/// SamplesPerPixel, SampleFormat, PlanarConfiguration, Compression and PhotometricInterpretation.
struct Format {
    bits: u64,
    spp: u64,
    sample_format: u64,
    planar: u64,
    compression: u64,
    photometric: Option<u64>,
}

fn format(tags: &Map<String, J>) -> R<Format> {
    // the IFD's sample format (the `format` role), its tags' values whole: BitsPerSample and
    // SampleFormat agree across samples
    let all = |tag: u16, default: u64| -> R<Vec<V>> {
        Ok(ints(tags, tag)?.unwrap_or_else(|| vec![default]).iter().map(|&v| num(v as f64)).collect())
    };
    let spp0 = int(tags, 277, 1).unwrap_or(1);
    rules::of("tiff").check("format", rules::env([
        ("bits", V::List(all(258, 1)?)),
        ("formats", V::List(all(339, 1)?)),
        ("spp", num(spp0 as f64)),
        ("planar", num(if spp0 == 1 { 1 } else { ints(tags, 284)?.and_then(|v| v.first().copied()).unwrap_or(1) } as f64)),
    ])).map_err(Refused::NoCodec)?;
    let f = Format {
        bits: int(tags, 258, 1)?,
        spp: int(tags, 277, 1)?,
        sample_format: int(tags, 339, 1)?,
        planar: int(tags, 284, 1)?,
        compression: int(tags, 259, 1)?,
        photometric: ints(tags, 262)?.and_then(|v| v.first().copied()),
    };
    let checked = rules::of("tiff").check("image", rules::env([
        ("bits", num(f.bits as f64)),
        ("sample_format", num(f.sample_format as f64)),
        ("spp", num(f.spp as f64)),
        ("planar", num(f.planar as f64)),
        ("compression", num(f.compression as f64)),
        ("photometric", f.photometric.map(|p| num(p as f64)).unwrap_or(V::Absent)),
    ]));
    checked.map_err(Refused::NoCodec)?;
    Ok(f)
}

/// `tiff_data_type(tags)`: the data type of an IFD's samples, from its tags by number.
fn tiff_data_type(tags: &Map<String, J>) -> R<String> {
    let f = format(tags)?;
    tiff_data_type_of(f.sample_format, f.bits).map_err(Refused::Bad)
}

/// `tiff_codecs(tags, little_endian, x_axis, row_length)`: the codecs after any `transpose` of an
/// IFD's chunks, as the TIFF profile writes them, its tags checked as the profile checks them
/// (the `image`, `samples` and `predictor` roles); `x` is the index of `x` in a chunk as stored,
/// `row` the samples of a stored row of a chunk, which packed samples need.
fn tiff_codecs_of(tags: &Map<String, J>, little: bool, x: usize, row: u64, chunk: Option<[u64; 2]>) -> R<Vec<J>> {
    let f = format(tags)?;
    let tiff = rules::of("tiff");
    let sub = ints(tags, 530)?.unwrap_or_else(|| vec![2, 2]);
    tiff.check("samples", rules::env([
        ("offset", rules::text("(the IFD given)")),
        ("compression", num(f.compression as f64)),
        ("fill_order", num(int(tags, 266, 1)? as f64)),
        ("photometric", num(f.photometric.unwrap_or(0) as f64)),
        ("subsampling", V::List(sub.iter().map(|&s| num(s as f64)).collect())),
    ])).map_err(Refused::NoCodec)?;
    let predictor = int(tags, 317, 1)?;
    tiff.check("predictor", rules::env([
        ("compression", num(f.compression as f64)),
        ("predictor", num(predictor as f64)),
        ("sample_format", num(f.sample_format as f64)),
        ("bits", num(f.bits as f64)),
    ])).map_err(Refused::NoCodec)?;
    if matches!(f.compression, 65000..=65002) {
        // EER's codec decodes one shape of chunk, `chunk`, with the bits of the compression's (or,
        // for 65002, tags 65007 to 65009's, each 7, 2 and 2 without its tag)
        let Some([rows, columns]) = chunk else {
            return Err(Refused::NoCodec("EER's codec follows the chunk shape: give chunk_shape".to_string()));
        };
        // one shape for every chunk: a level in strips whose last is short has none
        let length = int(tags, 257, 0)?;
        if !tags.contains_key("322") && rows > 0 && length % rows != 0 {
            return Err(Refused::NoCodec(format!("an EER image of {length} rows in strips of {rows}: its last strip is short")));
        }
        let (skip, horz, vert) = crate::tiff::eer_bits(f.compression, |t, default| int(tags, t, default).unwrap_or(default));
        return Ok(vec![json!({"name": "imagecodecs_eer", "configuration": {
            "shape": [rows, columns], "skipbits": skip, "horzbits": horz, "vertbits": vert
        }})]);
    }
    if f.spp > 1 && f.planar == 1 && x == 0 {
        return bad("x_axis is the index of x in a chunk as stored, before its interleaved samples");
    }
    Ok(tiff_codecs(f.compression, predictor, f.bits, f.spp, little, x, row))
}

/// The routines' results as zarrs' typed Zarr v3 metadata (the `zarrs` feature), for a Rust
/// caller that builds arrays with zarrs.
#[cfg(feature = "zarrs")]
pub mod typed {
    use super::{call, Refused, R};
    use serde_json::Value as J;
    use zarrs_metadata::v3::MetadataV3;

    /// The codecs the routine `name` gives for the parameters `p`, as zarrs' `MetadataV3`.
    pub fn codecs(name: &str, p: &J) -> R<Vec<MetadataV3>> {
        let got = call(name, p)?;
        serde_json::from_value(got).map_err(|e| Refused::Bad(format!("{name} gave codecs zarrs does not read: {e}")))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tags(pairs: &[(u16, J)]) -> J {
        J::Object(pairs.iter().map(|(t, v)| (t.to_string(), v.clone())).collect())
    }

    #[test]
    fn tiff_routines_give_the_profile_s_codecs_and_types() {
        let lzw = tags(&[(258, json!([16])), (259, json!([5])), (277, json!([1])), (317, json!([2]))]);
        let p = json!({"tags": lzw, "little_endian": true, "x_axis": 1, "row_length": 64});
        assert_eq!(call("tiff_data_type", &json!({"tags": lzw})).unwrap(), json!("uint16"));
        assert_eq!(call("tiff_codecs", &p).unwrap(), json!([
            {"name": "imagecodecs_delta", "configuration": {"axis": 1, "dist": 1}},
            {"name": "bytes", "configuration": {"endian": "little"}},
            {"name": "refkv.tiff_lzw"}
        ]));
        let packed = tags(&[(258, json!(1)), (259, json!(1))]);
        assert_eq!(call("tiff_data_type", &json!({"tags": packed})).unwrap(), json!("uint8"));
    }

    #[test]
    fn tiff_codecs_refuse_what_the_profile_rejects() {
        for t in [
            tags(&[(259, json!([6]))]),                                      // old-style JPEG
            tags(&[(258, json!([8, 16])), (277, json!([2]))]),               // samples of two sizes
            tags(&[(258, json!([16])), (259, json!([5])), (317, json!([3]))]), // Predictor 3 of integers
            tags(&[(259, json!([65000]))]),                                  // EER, without a chunk shape
        ] {
            let p = json!({"tags": t, "little_endian": true, "x_axis": 1, "row_length": 8});
            assert!(matches!(call("tiff_codecs", &p), Err(Refused::NoCodec(_))), "{t}");
        }
        assert!(matches!(call("tiff_codecs", &json!({"tags": {}})), Err(Refused::Bad(_))));
        let eer = json!({"tags": tags(&[(259, json!([65001]))]), "little_endian": true, "x_axis": 1, "row_length": 8,
                         "chunk_shape": [4, 8]});
        assert_eq!(call("tiff_codecs", &eer).unwrap(), json!([{"name": "imagecodecs_eer", "configuration": {
            "shape": [4, 8], "skipbits": 7, "horzbits": 2, "vertbits": 2}}]));
    }

    #[test]
    fn the_other_routines_give_their_profiles_codecs() {
        let cases = [
            ("czi_codecs", json!({"pixel_type": 1, "compression": 6, "flag": true}), json!([
                {"name": "bytes", "configuration": {"endian": "little"}},
                {"name": "numcodecs.shuffle", "configuration": {"elementsize": 2}},
                {"name": "zstd", "configuration": {"level": 0, "checksum": false}}
            ])),
            ("vsi_codecs", json!({"codec": "jpeg", "item_size": 1}), json!([{"name": "imagecodecs_jpeg"}])),
            ("vsi_codecs", json!({"codec": "raw", "item_size": 2}), json!([{"name": "bytes", "configuration": {"endian": "little"}}])),
            ("dicom_codecs", json!({"transfer_syntax": "1.2.840.10008.1.2.2", "data_type": "int16", "samples": 1}),
             json!([{"name": "bytes", "configuration": {"endian": "big"}}])),
            ("dicom_codecs", json!({"transfer_syntax": "1.2.840.10008.1.2.4.50", "data_type": "uint8", "samples": 3}),
             json!([{"name": "imagecodecs_jpeg"}])),
            ("gzip_codecs", json!({"skip": 128, "level": 6}), json!([
                {"name": "refkv.skip", "configuration": {"length": 128}}, {"name": "gzip", "configuration": {"level": 6}}
            ])),
        ];
        for (name, p, want) in cases {
            assert_eq!(call(name, &p).unwrap(), want, "{name} {p}");
            #[cfg(feature = "zarrs")]
            assert!(typed::codecs(name, &p).is_ok(), "{name}: zarrs does not read its codecs");
        }
    }

    #[test]
    fn the_other_routines_refuse_what_their_profiles_reject() {
        for (name, p) in [
            ("czi_codecs", json!({"pixel_type": 7, "compression": 0, "flag": false})),
            ("czi_codecs", json!({"pixel_type": 0, "compression": 99, "flag": false})),
            ("vsi_codecs", json!({"codec": "webp", "item_size": 1})),
            ("dicom_codecs", json!({"transfer_syntax": "1.2.840.10008.1.2.4.51", "data_type": "uint8", "samples": 1})),
        ] {
            assert!(matches!(call(name, &p), Err(Refused::NoCodec(_))), "{name} {p}");
        }
        assert!(matches!(call("gzip_codecs", &json!({"skip": 0, "level": 10})), Err(Refused::Bad(_))));
    }
}
