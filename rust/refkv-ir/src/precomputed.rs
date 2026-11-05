//! The Neuroglancer precomputed store format on the IR (spec/zarrify/precomputed.md
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/precomputed.md>), sans-IO: from the
//! listing's IR, it reads one document, `info`, in one read, checks it by the schema
//! (`schema/precomputed.json`: the role `info` rejects the input) and holds it, one value per
//! top-level member. It reads no chunk and no shard: the layout comes from `info` and the
//! listing's keys alone, which is what makes a volume of a billion chunks a few hundred
//! thousand references. Each scale is read into a [`Scale`], or a reason it is not presented
//! (the roles `scale` and `sharding`, and the rules of [`read_scale`]); the projection
//! (`project/precomputed.rs`) reads the document back from the IR.

use crate::expr::V;
use crate::ir::{Ir, Res};
use crate::json::{self, Doc, Jv};
use crate::rules::{self, num, text, Vars};
use crate::store::{StoreParser, StoreStep};
use serde_json::{json, Value as J};

pub const DOCUMENT: &str = "info";

pub static PRECOMPUTED_SCHEMA: &str = include_str!("../schema/precomputed.json");

/// The precomputed source model (`schema/precomputed.json`).
pub fn schema() -> &'static rules::Rules {
    static S: std::sync::OnceLock<rules::Rules> = std::sync::OnceLock::new();
    S.get_or_init(|| rules::Rules::new(PRECOMPUTED_SCHEMA).expect("the precomputed schema is valid"))
}

/// A presented scale's shards (spec/zarrify/precomputed.md §4
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/precomputed.md#4-shards>).
#[derive(Clone, Debug, PartialEq)]
pub struct Sharding {
    pub preshift_bits: u32,
    pub minishard_bits: u32,
    pub shard_bits: u32,
    pub minishard_index_encoding: String,
    pub data_encoding: String,
    /// the chunks a shard's box holds along x, y and z, each a power of two
    pub box_shape: [u64; 3],
    /// for each bit of a chunk's id, from the lowest, the axis it comes from
    pub axes: Vec<u8>,
}

/// A scale the layout presents, its numbers checked.
#[derive(Clone, Debug, PartialEq)]
pub struct Scale {
    pub key: String,
    pub size: [u64; 3],
    pub chunk: [u64; 3],
    pub offset: [i64; 3],
    pub resolution: [f64; 3],
    pub encoding: String,
    pub block: Option<[u64; 3]>,
    pub sharding: Option<Sharding>,
}

impl Scale {
    /// The chunk grid's shape along x, y and z.
    pub fn grid(&self) -> [u64; 3] {
        [0, 1, 2].map(|a| self.size[a].div_ceil(self.chunk[a]))
    }
}

/// The bits of the id along an axis of `n` chunks: `ceil(log2(n))`.
fn bits_of(n: u64) -> u32 {
    if n <= 1 { 0 } else { 64 - (n - 1).leading_zeros() }
}

/// For each bit of a compressed Morton id, from the lowest, its axis: x, y and z in turn, an
/// axis left out once its bits are used up (spec/zarrify/precomputed.md §4
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/precomputed.md#4-shards>).
fn morton_axes(grid: [u64; 3]) -> Vec<u8> {
    let bits = grid.map(bits_of);
    let mut used = [0u32; 3];
    let mut out = Vec::new();
    let total: u32 = bits.iter().sum();
    let mut a = 0usize;
    while (out.len() as u32) < total {
        if used[a] < bits[a] {
            used[a] += 1;
            out.push(a as u8);
        }
        a = (a + 1) % 3;
    }
    out
}

/// An info member's value, or absent.
fn field(v: &Jv, k: &str) -> V {
    v.get(k).map(Jv::to_v).unwrap_or(V::Absent)
}

fn ints3(v: Option<&Jv>, lo: i64, hi: i64) -> Option<[i64; 3]> {
    let items = v?.items()?;
    if items.len() != 3 {
        return None;
    }
    Some([items[0].int(lo, hi)?, items[1].int(lo, hi)?, items[2].int(lo, hi)?])
}

fn uints3(v: Option<&Jv>) -> Option<[u64; 3]> {
    // the schema bounded them below 2^53
    ints3(v, 0, i64::MAX).map(|a| a.map(|x| x as u64))
}

/// The role `scale` and the role `sharding` for one scale, then the rules no role can state:
/// the Scale, or the reason it is not presented.
pub fn read_scale(value: &Jv, data_type: &str, channels: i64) -> Result<Scale, String> {
    let first = value.get("chunk_sizes").and_then(Jv::items).and_then(<[Jv]>::first);
    let rules = schema();
    rules.check("scale", &mut Vars(vec![
        ("value", value.to_v()),
        ("key", field(value, "key")),
        ("size", field(value, "size")),
        ("resolution", field(value, "resolution")),
        ("chunk_sizes", field(value, "chunk_sizes")),
        ("first_chunk", first.map(Jv::to_v).unwrap_or(V::Absent)),
        ("voxel_offset", field(value, "voxel_offset")),
        ("encoding", field(value, "encoding")),
        ("block", field(value, "compressed_segmentation_block_size")),
        ("data_type", text(data_type)),
        ("num_channels", num(channels as f64)),
    ]))?;
    let key = value.get("key").and_then(Jv::as_str).unwrap_or("").to_string();
    if key.is_empty() || key.split('/').any(|s| s.is_empty() || s == "." || s == "..") {
        return Err(format!("its key {key:?} is not a path within the store"));
    }
    let size = uints3(value.get("size")).ok_or("internal: size")?;
    let chunk = uints3(first).ok_or("internal: chunk size")?;
    let offset = match value.get("voxel_offset") {
        Some(o) => ints3(Some(o), i64::MIN, i64::MAX).ok_or("internal: voxel_offset")?,
        None => [0; 3],
    };
    let r = value.get("resolution").and_then(Jv::items).ok_or("internal: resolution")?;
    let number = |i: usize| r.get(i).and_then(Jv::num).filter(|x| x.is_finite()).ok_or("its resolution is not 3 finite numbers");
    let resolution = [number(0)?, number(1)?, number(2)?];
    let scale = Scale {
        key,
        size,
        chunk,
        offset,
        resolution,
        encoding: value.get("encoding").and_then(Jv::as_str).unwrap_or("").to_string(),
        block: uints3(value.get("compressed_segmentation_block_size")),
        sharding: None,
    };
    let grid = scale.grid();
    if grid.iter().any(|&n| n > u32::MAX as u64) {
        return Err("its chunk grid is 2^32 chunks or more along an axis".into());
    }
    let Some(sh) = value.get("sharding") else { return Ok(scale) };
    let enc = |k: &str| sh.get(k).and_then(Jv::as_str).unwrap_or("raw").to_string();
    rules.check("sharding", &mut Vars(vec![
        ("value", sh.to_v()),
        ("type_", field(sh, "@type")),
        ("hash", field(sh, "hash")),
        ("preshift_bits", field(sh, "preshift_bits")),
        ("minishard_bits", field(sh, "minishard_bits")),
        ("shard_bits", field(sh, "shard_bits")),
        ("minishard_index_encoding", field(sh, "minishard_index_encoding")),
        ("data_encoding", field(sh, "data_encoding")),
        ("chunk_sizes", field(value, "chunk_sizes")),
    ]))?;
    let bits = |k: &str| sh.get(k).and_then(|v| v.int(0, 64)).unwrap_or(0) as u32;
    let (preshift_bits, minishard_bits, shard_bits) = (bits("preshift_bits"), bits("minishard_bits"), bits("shard_bits"));
    let axes = morton_axes(grid);
    let total = axes.len() as u32;
    if total > 64 {
        return Err(format!("its chunk ids need {total} bits, more than 64"));
    }
    let low = preshift_bits + minishard_bits;
    if low + shard_bits < total {
        return Err(format!("its shards wrap: chunk ids have {total} bits, and preshift_bits, minishard_bits and \
                            shard_bits name {} of them, so that one shard holds several boxes of chunks", low + shard_bits));
    }
    let mut box_shape = [1u64; 3];
    for &a in axes.iter().take(low as usize) {
        box_shape[a as usize] *= 2;
    }
    Ok(Scale {
        sharding: Some(Sharding {
            preshift_bits,
            minishard_bits,
            shard_bits,
            minishard_index_encoding: enc("minishard_index_encoding"),
            data_encoding: enc("data_encoding"),
            box_shape,
            axes,
        }),
        ..scale
    })
}

/// The box of the shard `number`, on the grid of boxes: the shard's bits are the id's bits past
/// `preshift_bits + minishard_bits`. None when a bit set in `number` is past the id's.
pub fn shard_box(sh: &Sharding, number: u64) -> Option<[u64; 3]> {
    let low = (sh.preshift_bits + sh.minishard_bits) as usize;
    let high = sh.axes.get(low..).unwrap_or(&[]);
    if high.len() < 64 && number >> high.len() != 0 {
        return None;
    }
    let mut at = [0u32; 3];
    let mut out = [0u64; 3];
    for (j, &a) in high.iter().enumerate() {
        let a = a as usize;
        out[a] |= (number >> j & 1) << at[a];
        at[a] += 1;
    }
    Some(out)
}

pub struct Precomputed {
    ir: Ir,
    objects: Vec<u32>,
    sizes: Vec<u64>,
    info: Option<usize>,
    asked: bool,
    got: Option<Vec<u8>>,
    doc: Option<Doc>,
    error: Option<String>,
}

impl Precomputed {
    pub fn new(ir: Ir, objects: Vec<u32>, keys: &[(String, u64)]) -> Precomputed {
        Precomputed {
            ir,
            objects,
            sizes: keys.iter().map(|k| k.1).collect(),
            info: keys.iter().position(|k| k.0 == DOCUMENT),
            asked: false,
            got: None,
            doc: None,
            error: None,
        }
    }
}

impl StoreParser for Precomputed {
    fn step(&mut self) -> Res<StoreStep> {
        if let Some(e) = self.error.take() {
            return Err(e);
        }
        let Some(k) = self.info else {
            // the host chose the profile by the root's info (spec/zarrify.md §1.4
            // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>)
            return Err(format!("not a Neuroglancer precomputed store: the root has no {DOCUMENT}"));
        };
        let rules = schema();
        if !self.asked {
            self.asked = true;
            let size = self.sizes[k];
            rules.check("candidate", &mut Vars(vec![("key", text(DOCUMENT)), ("size", num(size as f64))]))?;
            return Ok(StoreStep::Read(vec![(k, 0, size)]));
        }
        let b = self.got.take().ok_or("internal: info not read")?;
        let doc = json::parse(&b).map_err(|e| format!("{DOCUMENT}: {e}"))?;
        let v = &doc.value;
        rules.check("info", &mut Vars(vec![
            ("value", v.to_v()),
            ("type_", field(v, "@type")),
            ("data_type", field(v, "data_type")),
            ("num_channels", field(v, "num_channels")),
            ("kind_", field(v, "type")),
            ("scales", field(v, "scales")),
        ]))?;
        self.doc = Some(doc);
        Ok(StoreStep::Done)
    }

    fn feed(&mut self, object: usize, offset: u64, data: &[u8]) {
        if offset != 0 || data.len() as u64 != self.sizes[object] {
            self.error.get_or_insert_with(|| "internal: the document read in part".into());
            return;
        }
        self.got = Some(data.to_vec());
    }

    fn finish(self: Box<Self>) -> Res<(Ir, J)> {
        let Precomputed { mut ir, objects, info, doc, error, .. } = *self;
        if let Some(e) = error {
            return Err(e);
        }
        let k = info.ok_or("internal: no info")?;
        let doc = doc.ok_or("internal: info not read")?;
        let o = objects[k];
        let n = ir.len[o as usize];
        json::hold(&mut ir, o, n, &doc.members)?;
        crate::check::finish(&mut ir)?;
        Ok((ir, json!({})))
    }
}

/// The parser of the profile's store run (`StoreFormat`; crate::store).
pub fn store_parser(ir: crate::ir::Ir, objects: Vec<u32>, keys: &[(String, u64)]) -> crate::ir::Res<Box<dyn crate::store::StoreParser>> {
    Ok(Box::new(Precomputed::new(ir, objects, keys)))
}
