//! A NIfTI header-and-image pair, a `.hdr` file and the `.img` file beside it (spec/zarrify/nifti.md §7.5
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nifti.md#75-header-and-image-pairs>), which a
//! file run finds by its magic and starts over as a store run (`Step::Beside`).
//!
//! Rounds: the header file's first 552 bytes; the rest of it, when it has more, as the mirror holds it; the probe
//! for the image file; and the edge chunks the projection copies. The IR is a store's: the header file held,
//! with the single file's `header`, `extender` and `extensions/<i>`, and the image file described in place,
//! with `voxels`. The checks are the single file's, but that the voxel offset may be 0 and bounds the image file.

use crate::ir::*;
use crate::nifti::{chain_values, check_header, detect_pair};
use crate::rules::{self, Rules, flag, num, text};
use crate::store::{StoreParser, StoreStep};
use serde_json::{Value as J, json};
use std::collections::HashMap;

/// The bytes of the header file read first.
const HEAD: u64 = 552;

#[derive(Clone, Copy, PartialEq, Debug)]
enum Phase {
    Head,
    Whole,
    Probed,
    Copies,
    Done,
}

pub struct NiftiPair {
    rules: &'static Rules,
    phase: Phase,
    ir: Option<Ir>,
    keys: Vec<(String, u64)>,
    elems: Vec<u32>,
    /// the header file's bytes: its first, then all
    head: Vec<u8>,
    version: u8,
    vox: u64,
    total: u64,
    copies: Vec<(u64, u64)>,
    copied: Vec<(u64, u64, Vec<u8>)>,
    got: HashMap<(usize, u64), Vec<u8>>,
    copy_threshold: usize,
}

/// The image file's key of the header file `key`: `.hdr` as `.img`, in the same case, or None for another name.
fn image_key(key: &str) -> Option<String> {
    let (stem, suffix) = key.split_at_checked(key.len().checked_sub(4)?)?;
    let image = match suffix {
        ".hdr" => ".img",
        ".HDR" => ".IMG",
        _ => return None,
    };
    Some(format!("{stem}{image}"))
}

impl NiftiPair {
    /// The listing's IR, its objects' elements and keys: one key, the header file.
    pub fn new(ir: Ir, objects: Vec<u32>, keys: &[(String, u64)]) -> Res<NiftiPair> {
        if keys.len() != 1 || keys[0].0.contains('/') {
            return Err(format!("a NIfTI pair's run starts from the header file alone, not {} objects", keys.len()));
        }
        Ok(NiftiPair {
            rules: rules::of("nifti"),
            phase: Phase::Head,
            ir: Some(ir),
            keys: keys.to_vec(),
            elems: objects,
            head: Vec::new(),
            version: 1,
            vox: 0,
            total: 0,
            copies: Vec::new(),
            copied: Vec::new(),
            got: HashMap::new(),
            copy_threshold: crate::out::copy_threshold(),
        })
    }

    /// The header's checks, from the file's first bytes.
    fn check_first(&mut self, first: &[u8]) -> Res<()> {
        let (key, size) = self.keys[0].clone();
        self.version = detect_pair(first).ok_or("not a NIfTI header-and-image pair's header file")?;
        self.rules.check("pair_name", rules::env([("key", text(&key)), ("named", flag(image_key(&key).is_some()))]))?;
        let c = check_header(self.rules, first, self.version, size, |_| 0)?;
        self.rules.check("pair_header", rules::env([("key", text(&key)), ("size", num(size as f64))]))?;
        self.vox = c.vox;
        self.rules.check("voxel_size", rules::env([("total", num(c.total as f64)), ("total_shown", text(&c.total.to_string()))]))?;
        self.total = c.total as u64;
        Ok(())
    }

    /// The image file's checks, and the edge chunks to copy.
    fn check_image(&mut self) -> Res<()> {
        let key = self.keys[0].0.clone();
        let image = image_key(&key).ok_or("internal: a header file of no image file")?;
        self.rules.check("pair_image", rules::env([("key", text(&image)), ("found", flag(self.keys.len() == 2))]))?;
        let size = self.keys[1].1;
        self.rules.check(
            "voxel_data",
            rules::env([
                ("total", num(self.total as f64)),
                ("vox", num(self.vox as f64)),
                ("size", num(size as f64)),
                ("total_shown", text(&self.total.to_string())),
            ]),
        )?;
        let h = crate::nifti::header(&self.head, self.version);
        let threshold = json!({"copy_threshold": self.copy_threshold});
        self.copies = crate::project::nifti::copy_ranges(&h, self.vox, &threshold)?;
        Ok(())
    }

    /// The IR (spec/zarrify/nifti.md §7.5
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nifti.md#75-header-and-image-pairs>) and the facts.
    fn emit(&mut self) -> Res<J> {
        let mut ir = self.ir.take().ok_or("internal: no IR")?;
        let (header_file, image_file) = (self.elems[0], self.elems[1]);
        let (size, image_size) = (self.keys[0].1, self.keys[1].1);
        let h = crate::nifti::header(&self.head, self.version);
        let length = h.raw.len() as u64;
        let header = ir.value(header_file, "header", NO_INDEX, &h.record_type(), 0, (0, length), Some(h.raw))?;
        let mut end = length;
        let mut extended = false;
        if size >= length + 4 {
            let extender = &self.head[length as usize..length as usize + 4];
            extended = extender[0] != 0;
            ir.value(header_file, "extender", NO_INDEX, "u1[4]", 0, (length, 4), Some(extender))?;
            end = length + 4;
            if extended {
                let mut k = 0;
                let (took, _) = chain_values(&mut ir, header_file, &self.head[end as usize..], end, size, h.little, &mut k)?;
                end += took;
            }
        }
        if size > end {
            ir.add(GAP, header_file, "gaps/", end, "", 0, Some((end, size - end)))?;
        }
        ir.describe_in_place(image_file)?;
        if self.vox > 0 {
            ir.add(GAP, image_file, "gaps/", 0, "", 0, Some((0, self.vox)))?;
        }
        let spec = &self.rules.table("datatypes")[h.get("datatype").i().to_string()];
        let endian = if h.little { "little" } else { "big" };
        let form = json!({"geometry": {"data_type": spec["dtype"], "samples": spec["samples"]}, "codec": {"endian": endian},
                          "recipe": [["src", 0, null]]});
        let voxels = ir.data(image_file, "voxels", NO_INDEX, 0, (self.vox, self.total), &form.to_string())?;
        let after = self.vox + self.total;
        if image_size > after {
            ir.add(GAP, image_file, "gaps/", after, "", 0, Some((after, image_size - after)))?;
        }
        ir.sort_values();
        crate::check::finish(&mut ir)?;
        self.ir = Some(ir);
        let copied: Vec<J> = self.copied.iter().map(|(o, n, b)| json!([o, n, b])).collect();
        Ok(json!({"version": self.version, "header": header, "voxels": voxels, "extended": extended,
                  "start": self.vox, "length": self.total, "header_key": self.keys[0].0, "data_key": self.keys[1].0,
                  "copied": copied, "copy_threshold": self.copy_threshold}))
    }
}

impl StoreParser for NiftiPair {
    fn step(&mut self) -> Res<StoreStep> {
        loop {
            let size = self.keys[0].1;
            match self.phase {
                Phase::Head => {
                    let want = size.min(HEAD);
                    let Some(first) = self.got.remove(&(0, 0)) else { return Ok(StoreStep::Read(vec![(0, 0, want)])) };
                    self.check_first(&first)?;
                    self.head = first;
                    self.phase = Phase::Whole;
                    if size > want {
                        return Ok(StoreStep::Read(vec![(0, 0, size)]));
                    }
                }
                Phase::Whole => {
                    if let Some(whole) = self.got.remove(&(0, 0)) {
                        self.head = whole;
                    }
                    self.phase = Phase::Probed;
                    let image = image_key(&self.keys[0].0).ok_or("internal: a header file of no image file")?;
                    return Ok(StoreStep::Probe(vec![image]));
                }
                Phase::Probed => {
                    self.check_image()?;
                    self.phase = Phase::Copies;
                    if !self.copies.is_empty() {
                        return Ok(StoreStep::Read(self.copies.iter().map(|&(o, n)| (1, o, n)).collect()));
                    }
                }
                Phase::Copies => {
                    for (o, n) in std::mem::take(&mut self.copies) {
                        let b = self.got.remove(&(1, o)).ok_or("internal: a copied chunk was not read")?;
                        self.copied.push((o, n, b));
                    }
                    self.phase = Phase::Done;
                }
                Phase::Done => return Ok(StoreStep::Done),
            }
        }
    }

    fn feed(&mut self, object: usize, offset: u64, data: &[u8]) {
        self.got.insert((object, offset), data.to_vec());
    }

    fn found(&mut self, key: &str, index: usize, size: Option<u64>) -> Res<Option<u32>> {
        let Some(size) = size else { return Ok(None) };
        let ir = self.ir.as_mut().ok_or("internal: no IR")?;
        let (name, x) = key_name(key, false);
        let e = ir.object(0, &name, x, size)?;
        if index != self.keys.len() {
            return Err("internal: a found object out of order".into());
        }
        self.keys.push((key.to_string(), size));
        self.elems.push(e);
        Ok(Some(e))
    }

    fn finish(mut self: Box<Self>) -> Res<(Ir, J)> {
        if self.phase != Phase::Done {
            return Err("internal: the parse is not done".into());
        }
        let facts = self.emit()?;
        Ok((self.ir.take().ok_or("internal: no IR")?, facts))
    }
}

#[cfg(test)]
mod tests {
    use super::image_key;

    #[test]
    fn a_header_files_key_gives_its_image_files() {
        for (key, want) in [("a.hdr", Some("a.img")), ("a.b.HDR", Some("a.b.IMG")), ("a.Hdr", None), ("a.nii", None), ("hdr", None), ("é.hdr", Some("é.img"))] {
            assert_eq!(image_key(key).as_deref(), want, "{key}");
        }
    }
}
