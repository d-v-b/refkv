//! What the TIFF family's projections share (spec/zarrify/tiff.md §4
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#4-the-image>, spec/zarrify/qptiff.md
//! §4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/qptiff.md#4-the-image>, and NDPI's
//! levels): one array per level whose chunks are the tiles of each plane's IFD, the samples'
//! data type and codecs, the `omero` windows sampled from the coarsest level, and the image's
//! OME-NGFF object with the facts' translation at every level.

use super::contiguous::Zeros;
use super::{arr, key, n, obj, strs, translations, truthy, window, Copied};
use crate::ir::Ir;
use crate::out::{array_json, image_ome, tiff_codecs, transpose_codec, Out, Part};
use crate::refs::Refs;
use serde_json::{json, Value as J};
use std::collections::HashMap;

/// The longest channel name taken for a label.
pub const MAX_NAME: usize = 256;

/// The data type of the samples (spec/zarrify/tiff.md §4.3
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#43-data-types-and-codecs>).
pub fn data_type(facts: &J) -> Result<String, String> {
    let kind = match n(&facts["sample_format"]) {
        1 => "uint",
        2 => "int",
        3 => "float",
        f => return Err(format!("internal: sample format {f}")),
    };
    Ok(format!("{kind}{}", n(&facts["bits"])))
}

/// Whether a pixel's samples are interleaved (more than one, PlanarConfiguration 1): a chunk
/// then holds them all.
pub fn contig(facts: &J) -> bool {
    n(&facts["spp"]) > 1 && n(&facts["planar"]) == 1
}

/// A tile's coordinate along `c`: 0 for interleaved samples, the sample plane `s` for planar
/// ones, else the channel `c` of its IFD.
pub fn channel(facts: &J, s: u64, c: u64) -> u64 {
    if n(&facts["spp"]) > 1 {
        if contig(facts) { 0 } else { s }
    } else {
        c
    }
}

/// The levels' arrays and chunks (every level's, or only level `only`'s), and their shapes:
/// each level's zarr.json, of the shape and chunk shape `shape(level)`, then its chunks
/// (`tiles(refs, index, level)`).
pub fn arrays(ir: &Ir, facts: &J, url: &str, only: Option<usize>, shape: impl Fn(&J) -> (Vec<u64>, Vec<u64>),
              mut tiles: impl FnMut(&mut Refs, usize, &J) -> Result<(), String>) -> Result<(Out, Vec<Vec<u64>>), String> {
    let axes = strs(&facts["axes"]);
    let mut codecs = if contig(facts) { vec![transpose_codec(&axes)] } else { vec![] };
    // `x` in stored order: the last axis, or the one before the samples when they are interleaved
    let x = axes.len() - if contig(facts) { 2 } else { 1 };
    codecs.extend(tiff_codecs(n(&facts["compression"]), n(&facts["predictor"]), n(&facts["bits"]),
                              facts["little"].as_bool().unwrap_or(false), x));
    let codecs = J::Array(codecs);
    let data_type = data_type(facts)?;
    let mut refs = Refs::new(ir, Out::new(url));
    let levels = arr(&facts["levels"]);
    let mut shapes = Vec::with_capacity(levels.len());
    for (li, lv) in levels.iter().enumerate() {
        if only.is_some_and(|l| l != li) {
            continue;
        }
        let (shape, chunk) = shape(lv);
        refs.out.json(&format!("{li}/zarr.json"), &array_json(&shape, &data_type, json!(chunk), codecs.clone(), &axes));
        shapes.push(shape);
        tiles(&mut refs, li, lv)?;
    }
    Ok((refs.out, shapes))
}

/// The chunks of one IFD's tiles, the members of its element `e` (none without one): member k
/// is tile k % `per` of the sample plane s = k / `per`, at row and column of `across` tiles a
/// row, keyed `prefix` and `coords(s, row, col)`; its ranges are `parts(k, row, start, length)`,
/// or the element's recipe.
#[allow(clippy::too_many_arguments)]
pub fn plane(refs: &mut Refs, prefix: &str, e: Option<u64>, per: u64, across: u64, coords: impl Fn(u64, u64, u64) -> Vec<u64>,
             parts: impl Fn(u64, u64, u64, u64) -> Option<Vec<Part>>) -> Result<(), String> {
    let mut members = e.map(|e| refs.ir.members(e as u32)).unwrap_or_default();
    members.sort_unstable();
    for (k, start, length, form) in members {
        let (s, j) = (k / per, k % per);
        let (row, col) = (j / across, j % across);
        refs.chunk(key(prefix, coords(s, row, col)), start, length, form, parts(k, row, start, length), 0)?;
    }
    Ok(())
}

/// The output's one zero data source (spec/zarrify.md §1.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>) of a TIFF's or QPTIFF's
/// padded strips: as many zero bytes as the most any strip of the facts' levels is padded with.
pub fn zero_source(facts: &J) -> Zeros {
    let most = arr(&facts["levels"]).iter().flat_map(|lv| arr(&lv["pad"]["zeros"])).map(|p| n(&p["zeros"])).max();
    Zeros::sized(most.unwrap_or(0))
}

/// How a level's short last strip is padded to the chunk (spec/zarrify/tiff.md §4.5
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#45-strips>): the facts' `pad`,
/// zero bytes after plane `p`'s uncompressed strip `k`, or a literal tail spliced into its LZW
/// stream.
pub struct Padding {
    last: Option<u64>,
    zeros: HashMap<(u64, u64), Part>,
    lzw: HashMap<(u64, u64), (u64, Vec<u8>)>,
}

impl Padding {
    /// The padding of level `lv` (none unless it is in strips and has a `pad`): its zero bytes a
    /// prefix of the output's zero data source `zeros` (`zero_source`).
    pub fn of(refs: &mut Refs, lv: &J, zeros: &mut Zeros) -> Result<Self, String> {
        let strips = lv["strips"].as_bool().unwrap_or(false);
        let pad = &lv["pad"];
        let at = |p: &J| (n(&p["c"]), n(&p["k"]));
        let mut by_strip = HashMap::new();
        for p in arr(&pad["zeros"]) {
            by_strip.insert(at(p), zeros.range(&mut refs.out, n(&p["zeros"]))?);
        }
        Ok(Padding {
            last: strips.then(|| n(&lv["h"]).div_ceil(n(&lv["th"])) - 1),
            zeros: by_strip,
            lzw: arr(&pad["lzw"])
                .iter()
                .map(|p| (at(p), (n(&p["prefix"]), arr(&p["tail"]).iter().map(|b| n(b) as u8).collect())))
                .collect(),
        })
    }

    /// The ranges of plane `p`'s strip `k` at chunk row `row`, of `length` bytes at `start`, when it
    /// is a padded last strip; else None.
    pub fn parts(&self, p: u64, k: u64, row: u64, start: u64, length: u64) -> Option<Vec<Part>> {
        if self.last != Some(row) {
            return None;
        }
        if let Some(z) = self.zeros.get(&(p, k)) {
            return Some(vec![Part::Src(start, length), z.clone()]);
        }
        self.lzw.get(&(p, k)).map(|(prefix, tail)| vec![Part::Src(start, *prefix), Part::Lit(tail.clone())])
    }
}

/// The source's ranges the windows sample (`samples`, of the coarsest level): their chunks'
/// bytes, from that level's references (`arrays`), which the run reads after the parser.
pub fn reads(ir: &Ir, facts: &J, samples: &[window::Sample], arrays: impl Fn(&Ir, &J, &str, Option<usize>) -> Result<(Out, Vec<Vec<u64>>), String>)
             -> Result<Vec<(u64, u64)>, String> {
    if samples.is_empty() {
        return Ok(Vec::new());
    }
    let last = arr(&facts["levels"]).len() - 1;
    Ok(window::reads(&arrays(ir, facts, "", Some(last))?.0, samples))
}

/// The windows `samples` give, decoded from the chunks of `out` whose bytes `copied` holds (a
/// chunk the coarsest level's chunk shape, `chunk(level)`); none without samples.
pub fn sampled(out: &Out, facts: &J, samples: &[window::Sample], chunk: impl Fn(&J) -> Vec<u64>, data_type: &str, copied: &Copied) -> Vec<Option<J>> {
    match arr(&facts["levels"]).last() {
        Some(lv) if !samples.is_empty() => {
            let size = chunk(lv).iter().product::<u64>() * (n(&facts["bits"]) / 8);
            let little = facts["little"].as_bool().unwrap_or(false);
            window::windows(out, samples, copied, n(&facts["compression"]), n(&facts["predictor"]), data_type, little, size)
        }
        _ => Vec::new(),
    }
}

/// The image's OME-NGFF object: the facts' axes, units and scales, with the facts' translation
/// at every level when it has one.
pub fn ome(facts: &J, axes: &[&str], name: Option<&J>) -> Result<J, String> {
    let tr = &facts["translation"];
    let trs = J::Array(if truthy(tr) { vec![tr.clone(); arr(&facts["levels"]).len()] } else { vec![] });
    image_ome(axes, &obj(&facts["units"]), arr(&facts["scales"]), name, translations(&trs))
}

/// The root's source metadata: the byte order, and whether the file is a BigTIFF.
pub fn own(facts: &J) -> J {
    let little = facts["little"].as_bool().unwrap_or(false);
    json!({"byte_order": if little { "little" } else { "big" }, "bigtiff": facts["bigtiff"]})
}
