//! The transforms of derived elements (conventions §8.5
//! <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#85-forms-and-recipes>): every one a
//! parser emits, by
//! name, and whether a validator can derive its bytes again from the source (the view
//! check, conventions §8.8
//! <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#88-canonical-form>). A derived
//! transform may hold shown values only if it can.

use serde_json::Value as J;
use std::io::Read;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Transform {
    /// ND2: a compressed LV record (a 12-byte header, then a zlib stream)
    Nd2LvZlib,
    /// ND2: an XML variant chunk (its bytes, as UTF-8 text)
    XmlVariant,
    /// CZI: a compressed attachment (`Zip-Comp`, `ZIP`), whose space holds nothing
    Gzip,
    /// A zip entry's raw deflate stream. SAFE's (an XML document of a `.SAFE.zip`) has a space
    /// that holds nothing. NumPy's (a deflated `.npz` member) gives a `prefix` in its form, the
    /// bytes at the start of what it inflates to that hold its values (the member's preamble and
    /// header), which a validator inflates again
    Deflate,
    /// FITS: a whole file of gzip members, whose space holds the file it decodes to
    Gunzip,
    /// FITS: a whole file of bzip2 streams, whose space holds the file it decodes to
    Bunzip2,
}

impl Transform {
    /// Every transform; `index` keeps it complete (each variant at its index).
    pub const ALL: [Transform; 6] =
        [Transform::Nd2LvZlib, Transform::XmlVariant, Transform::Gzip, Transform::Deflate, Transform::Gunzip, Transform::Bunzip2];

    pub fn index(self) -> usize {
        match self {
            Transform::Nd2LvZlib => 0,
            Transform::XmlVariant => 1,
            Transform::Gzip => 2,
            Transform::Deflate => 3,
            Transform::Gunzip => 4,
            Transform::Bunzip2 => 5,
        }
    }

    pub fn name(self) -> &'static str {
        match self {
            Transform::Nd2LvZlib => "nd2-lv-zlib",
            Transform::XmlVariant => "xml-variant",
            Transform::Gzip => "gzip",
            Transform::Deflate => "deflate",
            Transform::Gunzip => "gunzip",
            Transform::Bunzip2 => "bunzip2",
        }
    }

    /// The transform a form names, if one of these.
    pub fn of_form(form: &str) -> Option<Transform> {
        let v: J = serde_json::from_str(form).ok()?;
        let t = v["transform"].as_str()?;
        Transform::ALL.into_iter().find(|x| x.name() == t)
    }

    /// The `prefix` of a form (conventions §8.5
    /// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#85-forms-and-recipes>): the
    /// bytes at the start of a `deflate` element's derived bytes that hold its values.
    pub fn prefix(form: &J) -> Option<u64> {
        form.get("prefix").and_then(J::as_u64)
    }

    /// Whether the space of an element of this transform and `form` may hold values the view shows
    /// (conventions §8.8
    /// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#88-canonical-form>: only when a
    /// validator can derive its bytes again). A `deflate` space may when its form gives a `prefix`,
    /// for the values that end at or before it.
    pub fn may_hold_shown(self, form: &J) -> bool {
        match self {
            Transform::Nd2LvZlib | Transform::XmlVariant | Transform::Gunzip | Transform::Bunzip2 => true,
            Transform::Deflate => Transform::prefix(form).is_some(),
            Transform::Gzip => false,
        }
    }

    /// How many of the element's source bytes a validator reads to derive its space, given the
    /// `have` it has read so far (0 at first) of the element's `length`: all of them, but for a
    /// `deflate` element with a `prefix`, which is read in windows that grow until the prefix is
    /// inflated, so that validating a header does not read a member's whole stream. None when
    /// `have` is all there is to read.
    pub fn window(self, form: &J, have: u64, length: u64) -> Option<u64> {
        if have >= length {
            return None;
        }
        match (self, Transform::prefix(form)) {
            (Transform::Deflate, Some(p)) if have == 0 => Some(length.min(p.saturating_add(p / 8).saturating_add(1 << 16))),
            (Transform::Deflate, Some(_)) => Some(length.min(have.saturating_mul(2))),
            _ => Some(length),
        }
    }

    /// The derived bytes from the element's source bytes `raw` (all of them, or the first of them,
    /// as `window` reads them) and its form, or None when a validator cannot derive them (the
    /// transform may then hold no shown value). A `deflate` element with a `prefix` gives its
    /// first `prefix` bytes; `Err` while `raw` does not inflate to that many.
    pub fn rederive(self, raw: &[u8], form: &J) -> Option<Result<Vec<u8>, String>> {
        match self {
            Transform::Nd2LvZlib if raw.len() < 12 => Some(Err("a compressed LV record of fewer than 12 bytes".into())),
            Transform::Nd2LvZlib => Some(crate::lv::inflate(raw, form["size"].as_u64())),
            Transform::XmlVariant => Some(Ok(raw.to_vec())),
            Transform::Gunzip | Transform::Bunzip2 => {
                let mut out = Vec::with_capacity(form["size"].as_u64().unwrap_or(0) as usize);
                let decoded = self.reader(raw).read_to_end(&mut out);
                Some(decoded.map(|_| out).map_err(|e| format!("a {} stream: {e}", self.name())))
            }
            Transform::Deflate => {
                let prefix = Transform::prefix(form)?;
                let (out, _) = crate::gz::inflate_prefix(raw, prefix);
                Some(if out.len() as u64 == prefix {
                    Ok(out)
                } else {
                    Err(format!("a deflate stream that inflates to {} bytes, fewer than its prefix of {prefix}", out.len()))
                })
            }
            Transform::Gzip => None,
        }
    }

    /// The bytes a whole file of gzip members (`Gunzip`) or bzip2 streams decodes to, as they
    /// decode; any other transform reads as nothing, since no file is one.
    pub fn reader<'a, R: Read + Send + Sync + 'a>(self, raw: R) -> Box<dyn Read + Send + Sync + 'a> {
        match self {
            Transform::Gunzip => Box::new(flate2::read::MultiGzDecoder::new(raw)),
            Transform::Bunzip2 => Box::new(bzip2::read::MultiBzDecoder::new(raw)),
            _ => Box::new(std::io::empty()),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    /// A raw deflate stream of `data`.
    fn deflated(data: &[u8]) -> Vec<u8> {
        miniz_oxide::deflate::compress_to_vec(data, 6)
    }

    /// A `deflate` element with a `prefix` derives the first `prefix` bytes of what its stream
    /// inflates to, from all of the stream or from enough of its start; one without derives
    /// nothing, whatever its other members.
    #[test]
    fn a_deflate_prefix_is_derived_from_the_start_of_the_stream() {
        let data: Vec<u8> = (0..200_000u32).flat_map(|i| i.wrapping_mul(2_654_435_761).to_le_bytes()).collect();
        let stream = deflated(&data);
        for prefix in [0u64, 1, 128, 70_000] {
            let form = json!({"transform": "deflate", "prefix": prefix, "size": data.len()});
            assert!(Transform::Deflate.may_hold_shown(&form));
            assert_eq!(Transform::Deflate.rederive(&stream, &form), Some(Ok(data[..prefix as usize].to_vec())), "{prefix}");
            // the first window is enough for data that does not compress
            let n = Transform::Deflate.window(&form, 0, stream.len() as u64).unwrap() as usize;
            assert!(n < stream.len() / 2, "{prefix}: the first window is {n} of {} bytes", stream.len());
            assert_eq!(Transform::Deflate.rederive(&stream[..n], &form), Some(Ok(data[..prefix as usize].to_vec())), "{prefix}");
        }
        for form in [json!({"transform": "deflate"}), json!({"transform": "deflate", "size": 9})] {
            assert!(!Transform::Deflate.may_hold_shown(&form));
            assert_eq!(Transform::Deflate.rederive(&stream, &form), None);
        }
    }

    /// The windows a validator reads of a `deflate` element with a `prefix` grow until they are
    /// the element; every other element is read whole at once.
    #[test]
    fn a_deflate_prefix_is_read_in_windows_that_grow() {
        let form = json!({"transform": "deflate", "prefix": 800});
        let t = Transform::Deflate;
        assert_eq!(t.window(&form, 0, 1 << 30), Some(800 + 100 + (1 << 16)));
        assert_eq!(t.window(&form, 66_436, 1 << 30), Some(132_872));
        assert_eq!(t.window(&form, 1 << 29, (1 << 29) + 5), Some((1 << 29) + 5));
        assert_eq!(t.window(&form, 0, 50), Some(50));
        assert_eq!(t.window(&form, 50, 50), None);
        assert_eq!(t.window(&form, 0, 0), None);
        assert_eq!(Transform::Gunzip.window(&json!({"transform": "gunzip", "size": 9}), 0, 1 << 30), Some(1 << 30));
        assert_eq!(t.window(&json!({"transform": "deflate"}), 0, 1 << 30), Some(1 << 30));
    }

    #[test]
    fn a_deflate_stream_shorter_than_its_prefix_is_an_error() {
        let stream = deflated(b"only these bytes");
        let form = json!({"transform": "deflate", "prefix": 17});
        let e = Transform::Deflate.rederive(&stream, &form).unwrap().unwrap_err();
        assert_eq!(e, "a deflate stream that inflates to 16 bytes, fewer than its prefix of 17");
    }

    #[test]
    fn a_deflate_stream_that_is_not_one_is_an_error() {
        let form = json!({"transform": "deflate", "prefix": 4});
        assert!(Transform::Deflate.rederive(&[0xff; 32], &form).unwrap().is_err());
    }
}
