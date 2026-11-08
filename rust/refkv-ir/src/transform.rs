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
    /// SAFE: a zip entry's raw deflate stream (an XML document of a `.SAFE.zip`), whose space
    /// holds nothing
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

    /// Whether its space may hold values the view shows (conventions §8.8
    /// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#88-canonical-form>: only when a
    /// validator can derive its bytes again).
    pub fn may_hold_shown(self) -> bool {
        match self {
            Transform::Nd2LvZlib | Transform::XmlVariant | Transform::Gunzip | Transform::Bunzip2 => true,
            Transform::Gzip | Transform::Deflate => false,
        }
    }

    /// The derived bytes from the element's source bytes `raw` and its form, or None
    /// when a validator cannot derive them (the transform may then hold no shown value).
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
            Transform::Gzip | Transform::Deflate => None,
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
