//! The archive an output is written as: its source table, its references numbered by it, and its
//! bytes entries, each marked late (read when the archive opens) or not, in the order written; the
//! page size of its index; and the ranges whose bytes a written archive carries checksums of. Every
//! host writes an output this way, so that the same output is the same archive from each.

use super::{urls::object_url, Fail};
use crate::out::{Out, Part};
use std::collections::HashMap;

/// The page size of a zarrified archive's page index (spec/bindings/vzip.md §12
/// <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#12-page-index-optional>).
pub const PAGE_SIZE: u64 = 1 << 16;

/// Whether the bytes entry `key` of an output whose lazy prefixes are `lazy` is late: read when the
/// archive opens (a pinned entry, spec/bindings/vzip.md §14.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#142-recommended-layout-informative>),
/// and written uncompressed. Those are the hierarchy's metadata documents: a key that is `zarr.json`
/// or ends in `/zarr.json`, the key of a node's metadata in Zarr v3
/// (<https://zarr-specs.readthedocs.io/en/latest/v3/core/index.html#metadata-keys>), and not under a
/// lazy prefix (the source metadata node's, and those of nodes a profile has one of per tile, which
/// are read only when asked for, like chunks).
pub fn late(key: &str, lazy: &[String]) -> bool {
    (key == "zarr.json" || key.ends_with("/zarr.json")) && !lazy.iter().any(|p| key.starts_with(p.as_str()))
}

/// A source of the archive's table.
#[derive(Debug, Clone, PartialEq)]
pub enum Source {
    /// a url source: the input's file (whose pins the host adds), or a store's object, its size
    /// pinned
    Url { url: String, size: Option<u64> },
    /// a data source
    Data(Vec<u8>),
}

/// A range of a reference, numbered by the archive's table.
#[derive(Debug, Clone, PartialEq)]
pub enum Range {
    Of { source: u32, offset: u64, length: u64 },
    Literal(Vec<u8>),
}

/// An output as the archive holds it: the source table, the references (key, ranges) in order of
/// their keys' UTF-8 bytes, and the bytes entries (key, bytes, late) in that order too. `objects`
/// is the object key of each url source of a store's output.
#[derive(Debug, Default, PartialEq)]
pub struct Layout {
    pub sources: Vec<Source>,
    pub refs: Vec<(String, Vec<Range>)>,
    pub entries: Vec<(String, Vec<u8>, bool)>,
    pub objects: Vec<String>,
}

fn sorted<T>(mut v: Vec<(String, T)>) -> Vec<(String, T)> {
    v.sort_unstable_by(|a, b| a.0.as_bytes().cmp(b.0.as_bytes()));
    v
}

fn entries(out: &mut Out) -> Vec<(String, Vec<u8>, bool)> {
    sorted(std::mem::take(&mut out.entries)).into_iter().map(|(k, v)| {
        let l = late(&k, &out.lazy);
        (k, v, l)
    }).collect()
}

/// The archive of a file's output (spec/zarrify.md §1.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>): source 0 the file, then
/// the data sources 1, 2, ... in order of first use.
pub fn file_layout(mut out: Out) -> Result<Layout, Fail> {
    let mut sources = vec![Source::Url { url: out.url.clone(), size: None }];
    sources.extend(std::mem::take(&mut out.data).into_iter().map(Source::Data));
    let refs = sorted(std::mem::take(&mut out.refs))
        .into_iter()
        .map(|(k, parts)| {
            let ranges = parts
                .into_iter()
                .map(|p| match p {
                    Part::Src(a, n) => Ok(Range::Of { source: 0, offset: a, length: n }),
                    Part::Data(s, a, n) => Ok(Range::Of { source: s, offset: a, length: n }),
                    Part::Lit(b) => Ok(Range::Literal(b)),
                    Part::Obj(..) => Err(Fail::Bad(format!("{k}: a reference to a store's object in a file's output"))),
                })
                .collect::<Result<Vec<_>, _>>()?;
            Ok((k, ranges))
        })
        .collect::<Result<Vec<_>, Fail>>()?;
    Ok(Layout { sources, refs, entries: entries(&mut out), objects: Vec::new() })
}

/// The archive of a store's output (spec/zarrify.md §1.4
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>): one url source per
/// object some reference uses, its size in `sizes` pinned, in order of first use (the references in
/// order of their keys, each one's ranges in order), then the data sources, also in order of first
/// use.
pub fn store_layout(mut out: Out, sizes: &HashMap<String, u64>) -> Result<Layout, Fail> {
    let refs = sorted(std::mem::take(&mut out.refs));
    let mut objects: Vec<String> = Vec::new();
    let mut object_at: HashMap<String, u32> = HashMap::new();
    for (_, parts) in &refs {
        for p in parts {
            if let Part::Obj(k, ..) = p
                && !object_at.contains_key(k)
            {
                object_at.insert(k.clone(), objects.len() as u32);
                objects.push(k.clone());
            }
        }
    }
    let mut data: Vec<u32> = Vec::new(); // the output's data source numbers, in order of first use
    let mut data_at: HashMap<u32, u32> = HashMap::new();
    let mut numbered = Vec::with_capacity(refs.len());
    for (k, parts) in refs {
        let mut ranges = Vec::with_capacity(parts.len());
        for p in parts {
            ranges.push(match p {
                Part::Lit(b) => Range::Literal(b),
                Part::Obj(o, a, n) => Range::Of { source: object_at[&o], offset: a, length: n },
                Part::Data(s, a, n) => {
                    let i = *data_at.entry(s).or_insert_with(|| {
                        data.push(s);
                        (objects.len() + data.len() - 1) as u32
                    });
                    Range::Of { source: i, offset: a, length: n }
                }
                Part::Src(..) => return Err(Fail::Bad(format!("{k}: a reference to source 0 in a store's output"))),
            });
        }
        numbered.push((k, ranges));
    }
    let mut sources: Vec<Source> = objects.iter().map(|k| Source::Url { url: object_url(&out.url, k), size: sizes.get(k).copied() }).collect();
    for s in &data {
        let d = out.data.get((*s as usize).wrapping_sub(1)).ok_or_else(|| Fail::Bad(format!("no data source {s}")))?;
        sources.push(Source::Data(d.clone()));
    }
    Ok(Layout { sources, refs: numbered, entries: entries(&mut out), objects })
}

impl Layout {
    /// The ranges whose bytes a written archive carries the CRC-32C of when it carries checksums
    /// (spec/refkv.md §4.2 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#42-range>): each
    /// range of a url source, as (reference, range) indexes.
    pub fn checksummed(&self) -> Vec<(usize, usize)> {
        let mut out = Vec::new();
        for (i, (_, ranges)) in self.refs.iter().enumerate() {
            for (j, r) in ranges.iter().enumerate() {
                if let Range::Of { source, .. } = r
                    && matches!(self.sources.get(*source as usize), Some(Source::Url { .. }))
                {
                    out.push((i, j));
                }
            }
        }
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn lazy() -> Vec<String> {
        vec!["refkv_source/".into()]
    }

    #[test]
    fn late_documents() {
        assert!(late("zarr.json", &lazy()));
        assert!(late("a/b/zarr.json", &lazy()));
        assert!(!late("foozarr.json", &lazy()));
        assert!(!late("a/foozarr.json", &lazy()));
        assert!(!late("refkv_source/zarr.json", &lazy()));
        assert!(!late("a/zarr.json/c", &lazy()));
    }

    #[test]
    fn layouts() {
        let mut out = Out::new("http://h/s/");
        out.refs("b", vec![Part::Obj("o2".into(), 0, 4), Part::Data(1, 0, 2)]);
        out.refs("a", vec![Part::Obj("o1".into(), 1, 2), Part::Lit(vec![7]), Part::Data(2, 0, 1)]);
        out.refs("c", vec![Part::Obj("o1".into(), 0, 3)]);
        out.shared(b"xy");
        out.shared(b"z");
        out.json("zarr.json", &serde_json::json!({}));
        out.json("refkv_source/zarr.json", &serde_json::json!({}));
        out.bytes("c/0", vec![1]);
        let sizes = HashMap::from([("o1".to_string(), 3), ("o2".to_string(), 4)]);
        let l = store_layout(out, &sizes).unwrap();
        assert_eq!(l.objects, vec!["o1", "o2"]);
        assert_eq!(
            l.sources,
            vec![
                Source::Url { url: "http://h/s/o1".into(), size: Some(3) },
                Source::Url { url: "http://h/s/o2".into(), size: Some(4) },
                Source::Data(b"z".to_vec()),
                Source::Data(b"xy".to_vec()),
            ]
        );
        let of = |source, offset, length| Range::Of { source, offset, length };
        assert_eq!(l.refs[0], ("a".into(), vec![of(0, 1, 2), Range::Literal(vec![7]), of(2, 0, 1)]));
        assert_eq!(l.refs[1], ("b".into(), vec![of(1, 0, 4), of(3, 0, 2)]));
        let marks: Vec<(&str, bool)> = l.entries.iter().map(|(k, _, l)| (k.as_str(), *l)).collect();
        assert_eq!(marks, vec![("c/0", false), ("refkv_source/zarr.json", false), ("zarr.json", true)]);
        assert_eq!(l.checksummed(), vec![(0, 0), (1, 0), (2, 0)]);

        let mut out = Out::new("f.tif");
        out.refs("c/1", vec![Part::Src(4, 2), Part::Data(1, 0, 1)]);
        out.refs("c/0", vec![Part::Lit(vec![1])]);
        out.shared(b"q");
        let l = file_layout(out).unwrap();
        assert_eq!(l.sources, vec![Source::Url { url: "f.tif".into(), size: None }, Source::Data(b"q".to_vec())]);
        assert_eq!(l.refs, vec![("c/0".into(), vec![Range::Literal(vec![1])]), ("c/1".into(), vec![of(0, 4, 2), of(1, 0, 1)])]);
        assert_eq!(l.checksummed(), vec![(1, 0)]);
    }

    #[test]
    fn refuses_source_0_in_a_store_output() {
        let mut out = Out::new("http://h/s/");
        out.refs("a", vec![Part::Src(0, 1)]);
        assert!(matches!(store_layout(out, &HashMap::new()), Err(Fail::Bad(_))));
    }

    #[test]
    fn refuses_an_object_in_a_file_output() {
        let mut out = Out::new("f.tif");
        out.refs("a", vec![Part::Obj("o".into(), 0, 1)]);
        assert!(matches!(file_layout(out), Err(Fail::Bad(_))));
    }

    #[test]
    fn refuses_a_data_source_the_output_lacks() {
        let mut out = Out::new("http://h/s/");
        out.refs("a", vec![Part::Data(3, 0, 1)]);
        assert!(matches!(store_layout(out, &HashMap::new()), Err(Fail::Bad(_))));
    }
}
