//! A sans-IO parser of Olympus VSI slides (spec/zarrify/vsi.md, the profile's §9
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#9-vsi-profile>) as a store
//! run ([`crate::store::StoreParser`]) whose objects are the `.vsi` file and the tile files
//! beside it, every one described in place (conventions §8.9
//! <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#89-stores>).
//!
//! **Batches.** The `.vsi` file, whole; then the tile files are **probed** (§9.2
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#92-the-tile-files>): for each
//! stack the tree says has one, `_<name>_/stack<N>/frame_t.ets`, in one batch, then
//! `frame_t_0.ets` of each stack whose `frame_t.ets` is absent, in a second, and the first
//! that exists is the stack's tile file; then every tile file's 64-byte SIS header, in
//! one batch; then every one's ETS header and tile directory, in one batch. The schema
//! (`schema/vsi.json`) is checked as the profile meets each role. The IR is emitted at the end,
//! from what was read, and its arrays are then described (`stored.rs`; spec/stored-array.md §7
//! <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>): each tile
//! file's levels, and the images of the `.vsi` file's IFDs.

use crate::error::Error;

mod describe;
pub mod ets;
pub mod plan;
mod stored;
pub mod tiff;
pub mod tree;

use crate::fed::Word;
use crate::ir::{Ir, NO_INDEX, Res, key_name};
use crate::rules::{self, Rules, flag, num, text};
use crate::store::{StoreParser, StoreStep};
use serde_json::{Value as J, json};
use std::collections::{HashMap, HashSet};
use tree::{Container, Kind};

// tags of the metadata tree (Bio-Formats' names)
const COLLECTION_VOLUME: u32 = 2000;
const MULTIDIM_IMAGE_VOLUME: u32 = 2001;
const IMAGE_FRAME_VOLUME: u32 = 2002;
const DIMENSION_SIZE: u32 = 2003;
const MULTIDIM_STACK_PROPERTIES: u32 = 2005;
const DIMENSION_DESCRIPTION_VOLUME: u32 = 2007;
const CHANNEL_PROPERTIES: u32 = 2008;
const Z_START: u32 = 2012;
const Z_INCREMENT: u32 = 2013;
const CREATION_TIME: u32 = 2015;
const DEFAULT_SAMPLE_IFD: u32 = 2016;
const RWC_FRAME_ORIGIN: u32 = 2018;
const EXTERNAL_FILE_PROPERTIES: u32 = 2018;
const RWC_FRAME_SCALE: u32 = 2019;
const COARSE_FRAME_IFD: u32 = 2019;
const RWC_FRAME_UNIT: u32 = 2020;
const DIMENSION_NAME: u32 = 2021;
const DIMENSION_MEANING: u32 = 2023;
const DIMENSION_VALUE_ID: u32 = 2027;
const STACK_NAME: u32 = 2030;
const STACK_TYPE: u32 = 2074;
const TIME_START: u32 = 2100;
const DISPLAY_LIMITS: u32 = 2003;
const STACK_DISPLAY_LUT: u32 = 2004;
const IMAGE_BOUNDARY: u32 = 2053;
const HAS_EXTERNAL_FILE: u32 = 20005;
const CHANNEL_WAVELENGTH: u32 = 2417;
const CHANNEL_NAME: u32 = 2419;
const OBJECTIVE_MAG: u32 = 120060;

pub const VSI_HEADER_TYPE: &str = "{byte_order:ascii[2],magic:<u2,first_ifd:<u4}";
/// The name of the image the root's default sample IFD gives (§4.3
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#43-the-macro-image>), as
/// Bio-Formats names it.
pub const MACRO_NAME: &str = "macro image";

/// Whether a file's first bytes are a VSI file's (spec/zarrify.md §1.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>): a little-endian
/// classic TIFF whose bytes 8 to 11 are a container header's size and version, `18 00 49 53`.
pub fn detect(head: &[u8]) -> bool {
    head.len() >= 12 && head[..4] == *b"II*\0" && head[8..12] == [0x18, 0x00, 0x49, 0x53]
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Phase {
    Start,
    Vsi,
    /// the first candidates probed: probe the second of the stacks whose first is absent
    Probed,
    /// every candidate a stack needs probed: resolve the tile files
    Fallback,
    Heads,
    Dirs,
    Done,
}

/// A dimension of a stack beyond x and y: its coordinate index in a tile record, and its
/// meaning (`z`, `t`, `c`, or the code's name).
#[derive(Clone, Debug)]
struct Dim {
    coord: usize,
    meaning: String,
}

/// A stack, as the parser checks it and describes its arrays (§2.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#22-stacks>); the rest of its fields
/// are the description's (`described`).
#[derive(Clone, Debug, Default)]
struct Stack {
    id: u32,
    name: String,
    external: bool,
    width: Option<u64>,
    height: Option<u64>,
    dims: Vec<Dim>,
    /// the tile file's directory key, and the index of its file in `ets` once found
    dir: String,
    file: Option<usize>,
    /// the stack's fields as the description gives them (`describe`), the tree's values as read
    described: J,
}

/// A tile file: its listing index, key, size, and what was read of it.
struct EtsFile {
    stack: usize,
    object: usize,
    key: String,
    size: u64,
    head: Vec<u8>,
    sis: ets::Sis,
    header: Vec<u8>,
    ets: ets::Ets,
    dir: Vec<u8>,
    tiles: Vec<ets::Tile>,
}

pub struct Vsi {
    rules: &'static Rules,
    phase: Phase,
    ir: Option<Ir>,
    keys: Vec<(String, u64)>,
    elems: Vec<u32>,
    dirs: HashMap<String, u32>,
    vsi: usize,
    file: Vec<u8>,
    tree: Option<Container>,
    ifds: Vec<tiff::Ifd>,
    stacks: Vec<Stack>,
    /// per probed key: (stack index, candidate order), and what the host found
    probes: Vec<(usize, usize, String)>,
    found: HashMap<String, (usize, u64)>,
    files: Vec<EtsFile>,
    asked: Vec<(usize, u64, u64)>,
    got: HashMap<(usize, u64, u64), Vec<u8>>,
    batches: u64,
}

impl Vsi {
    /// The listing's IR, its objects' elements and keys: one key, the `.vsi` file.
    pub fn new(ir: Ir, objects: Vec<u32>, keys: &[(String, u64)]) -> Res<Vsi> {
        if keys.len() != 1 {
            return Err(Error::rejected(format!("a VSI run starts from the .vsi file alone, not {} objects", keys.len())));
        }
        let key = &keys[0].0;
        let lower = key.to_ascii_lowercase();
        if !lower.ends_with(".vsi") || key.len() <= 4 || key.contains('/') {
            return Err(Error::rejected(format!("the key {key:?} is not the name of a .vsi file")));
        }
        Ok(Vsi {
            rules: rules::of("vsi"),
            phase: Phase::Start,
            ir: Some(ir),
            keys: keys.to_vec(),
            elems: objects,
            dirs: HashMap::new(),
            vsi: 0,
            file: Vec::new(),
            tree: None,
            ifds: Vec::new(),
            stacks: Vec::new(),
            probes: Vec::new(),
            found: HashMap::new(),
            files: Vec::new(),
            asked: Vec::new(),
            got: HashMap::new(),
            batches: 0,
        })
    }

    fn base(&self) -> &str {
        let k = &self.keys[self.vsi].0;
        &k[..k.len() - 4]
    }

    fn take(&mut self, k: (usize, u64, u64)) -> Res<Vec<u8>> {
        self.got.remove(&k).ok_or_else(|| Error::internal(format!("the read {k:?} was not fed")))
    }

    // ---- the .vsi file (§9.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#91-the-vsi-file>)

    fn read_vsi(&mut self) -> Res<Vec<String>> {
        let size = self.keys[self.vsi].1;
        let b = self.take((self.vsi, 0, size))?;
        self.rules.check("file", rules::env([("size", num(size as f64))]))?;
        let order = String::from_utf8_lossy(&b[..2]).to_string();
        let (magic, first) = (u16::le_at(&b, 2), u32::le_at(&b, 4) as u64);
        self.rules.check("tiff_header", rules::env([("order", text(&order)), ("magic", num(magic as f64)), ("first_ifd", num(first as f64)), ("size", num(size as f64))]))?;
        let root = tree::parse(self.rules, &b, 8, size, 0)?;
        self.ifds = tiff::chain(self.rules, &b, first)?;
        self.file = b;
        self.stacks = self.find_stacks(&root)?;
        self.tree = Some(root);
        // the tile files to probe (§9.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#92-the-tile-files>)
        let dir = format!("_{}_", self.base());
        let mut keys = Vec::new();
        for (k, s) in self.stacks.iter_mut().enumerate() {
            s.dir = format!("{dir}/stack{}", s.id);
            if s.external {
                for (c, name) in ["frame_t.ets", "frame_t_0.ets"].iter().enumerate() {
                    let key = format!("{}/{name}", s.dir);
                    self.probes.push((k, c, key.clone()));
                    // the second candidate only if the first is absent (Phase::Probed)
                    if c == 0 {
                        keys.push(key);
                    }
                }
            }
        }
        Ok(keys)
    }

    /// The stacks of the tree: each `Multidimensional image volume` of the collection volume
    /// (§2.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#22-stacks>).
    fn find_stacks(&self, root: &Container) -> Res<Vec<Stack>> {
        let (rules, b) = (self.rules, self.file.as_slice());
        let mut out = Vec::new();
        let Some(coll) = root.volume(COLLECTION_VOLUME) else { return Ok(out) };
        for f in coll.fields.iter().filter(|f| f.tag == MULTIDIM_IMAGE_VOLUME && f.kind == Kind::Volume) {
            let Some(sc) = f.child() else { continue };
            let mut s = Stack { id: f.tag2.unwrap_or(0), ..Default::default() };
            // the description's fields (`describe`): each the first value field of its tag, as read
            let value = |c: Option<&Container>, tag: u32| c.and_then(|c| tree::value_of(rules, b, c, tag)).unwrap_or(J::Null);
            let fc = sc.volume(IMAGE_FRAME_VOLUME);
            let xp = fc.and_then(|fc| fc.volume(EXTERNAL_FILE_PROPERTIES));
            let pc = sc.volume(MULTIDIM_STACK_PROPERTIES);
            let ifd_of = |tag: u32| fc.and_then(|fc| fc.fields.iter().find(|x| x.tag == tag && x.kind == Kind::TiffIfd)).and_then(|x| x.tag2);
            let stack_type = value(pc, STACK_TYPE);
            let mut described = json!({
                "id": s.id, "name": value(pc, STACK_NAME), "type": stack_type,
                "type_name": stack_type.as_f64().map(|t| t as i64).and_then(|t| rules.table("stack_types").get(t.to_string()).cloned()),
                "has_external_file": value(xp, HAS_EXTERNAL_FILE), "image_boundary": value(xp, IMAGE_BOUNDARY),
                "dimension_sizes": value(Some(sc), DIMENSION_SIZE),
                "default_ifd": ifd_of(DEFAULT_SAMPLE_IFD), "coarse_ifd": ifd_of(COARSE_FRAME_IFD),
                "frame_scale": value(pc, RWC_FRAME_SCALE), "frame_origin": value(pc, RWC_FRAME_ORIGIN),
                "frame_unit": value(pc, RWC_FRAME_UNIT), "display_limits": value(pc, DISPLAY_LIMITS),
                "creation_time": value(pc, CREATION_TIME),
                "magnification": pc.and_then(|pc| find_value(rules, b, pc, OBJECTIVE_MAG)),
            });
            if let Some(xp) = xp {
                if let Some(v) = tree::numbers_of(rules, b, xp, IMAGE_BOUNDARY).filter(|v| v.len() >= 4) {
                    s.width = Some(v[2].max(0.0) as u64);
                    s.height = Some(v[3].max(0.0) as u64);
                }
                s.external = tree::number_of(rules, b, xp, HAS_EXTERNAL_FILE).is_some_and(|v| v != 0.0);
            }
            if let Some(pc) = pc {
                s.name = tree::text_of(rules, b, pc, STACK_NAME).unwrap_or_default();
            }
            // the dimensions (§2.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#22-stacks>): the description volumes, by their second tag
            let mut dims = Vec::new();
            let mut z_increment = J::Null;
            let mut channels = Vec::new();
            for d in sc.fields.iter().filter(|x| x.tag == DIMENSION_DESCRIPTION_VOLUME && x.kind == Kind::Volume) {
                let Some(dc) = d.child() else { continue };
                let index = d.tag2.unwrap_or(0) as usize;
                let psets: Vec<&Container> = dc.fields.iter().filter(|x| x.tag == CHANNEL_PROPERTIES).filter_map(|x| x.child()).collect();
                let code = psets.iter().find_map(|p| tree::value_of(rules, b, p, DIMENSION_MEANING).filter(J::is_number));
                let meaning = match code.as_ref().and_then(J::as_f64) {
                    Some(c) => rules.table("dimension_meanings").get((c as i64).to_string()).and_then(|v| v.as_str()).map(str::to_string).unwrap_or_else(|| format!("meaning {}", c as i64)),
                    None if psets.iter().any(|p| p.field(Z_START).is_some()) => "z".into(),
                    None if psets.iter().any(|p| p.field(TIME_START).is_some() || p.field(DIMENSION_VALUE_ID).is_some()) => "t".into(),
                    None if psets.iter().any(|p| p.field(CHANNEL_NAME).is_some() || p.field(STACK_DISPLAY_LUT).is_some()) => "c".into(),
                    None => "unknown".into(),
                };
                if meaning == "z" {
                    for p in &psets {
                        if let Some((v, u)) = tree::mdim_value(rules, b, p, Z_INCREMENT) {
                            z_increment = json!({"value": v, "unit": u});
                        }
                    }
                }
                if meaning == "c" {
                    for p in &psets {
                        channels.push(json!({
                            "dimension_name": tree::text_of(rules, b, p, DIMENSION_NAME), "channel_name": tree::text_of(rules, b, p, CHANNEL_NAME),
                            "lut_color": lut_color(rules, b, p), "display_limits": value(Some(p), DISPLAY_LIMITS),
                            "wavelength": tree::mdim_value(rules, b, p, CHANNEL_WAVELENGTH).map_or(J::Null, |(v, _)| v),
                        }));
                    }
                }
                dims.push(json!({"index": index, "meaning_code": code, "meaning": meaning}));
                s.dims.push(Dim { coord: 2 + index, meaning });
            }
            s.dims.sort_by_key(|d| d.coord);
            described["dimensions"] = J::Array(dims);
            described["z_increment"] = z_increment;
            described["channels"] = J::Array(channels);
            s.described = described;
            self.rules.check(
                "stack",
                rules::env([
                    ("id", num(s.id as f64)),
                    ("name", text(&s.name)),
                    ("external", flag(s.external)),
                    ("width", num(s.width.unwrap_or(0) as f64)),
                    ("height", num(s.height.unwrap_or(0) as f64)),
                ]),
            )?;
            out.push(s);
        }
        Ok(out)
    }

    // ---- the tile files (§9.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#92-the-tile-files>)

    /// After the probes: each stack's tile file, and the reads of their SIS headers.
    fn resolve(&mut self) -> Res<()> {
        for k in 0..self.stacks.len() {
            if !self.stacks[k].external {
                continue;
            }
            let mut found: Vec<(usize, String, usize, u64)> = Vec::new();
            for (sk, c, key) in &self.probes {
                if *sk == k && let Some(&(index, size)) = self.found.get(key) {
                    found.push((*c, key.clone(), index, size));
                }
            }
            found.sort_by_key(|f| f.0);
            let s = &self.stacks[k];
            self.rules.check("tile_file", rules::env([("id", num(s.id as f64)), ("name", text(&s.name)), ("dir", text(&s.dir)), ("found", num(found.len() as f64))]))?;
            let (_, key, index, size) = found.remove(0);
            if size < ets::SIS_HEADER {
                return Err(Error::rejected(format!("{key} ({size} bytes) is too short for an ETS file")));
            }
            self.stacks[k].file = Some(self.files.len());
            self.files.push(EtsFile { stack: k, object: index, key, size, head: Vec::new(), sis: Default::default(), header: Vec::new(), ets: Default::default(), dir: Vec::new(), tiles: Vec::new() });
            self.asked.push((index, 0, ets::SIS_HEADER));
        }
        Ok(())
    }

    fn read_heads(&mut self) -> Res<()> {
        for f in 0..self.files.len() {
            let (o, size) = (self.files[f].object, self.files[f].size);
            let head = self.take((o, 0, ets::SIS_HEADER))?;
            let sis = ets::sis(self.rules, &self.files[f].key, &head, size)?;
            self.asked.push((o, sis.ets_offset as u64, sis.ets_size as u64));
            let n = sis.record() * sis.tiles as u64;
            if n > 0 {
                self.asked.push((o, sis.tiles_offset as u64, n));
            }
            self.files[f].head = head;
            self.files[f].sis = sis;
        }
        Ok(())
    }

    fn read_dirs(&mut self) -> Res<()> {
        for f in 0..self.files.len() {
            let o = self.files[f].object;
            let sis = self.files[f].sis.clone();
            let header = self.take((o, sis.ets_offset as u64, sis.ets_size as u64))?;
            let e = ets::ets(self.rules, &self.files[f].key, &sis, &header)?;
            let n = sis.record() * sis.tiles as u64;
            let dir = if n > 0 { self.take((o, sis.tiles_offset as u64, n))? } else { Vec::new() };
            let tiles = ets::directory(&sis, &dir);
            self.files[f].header = header;
            self.files[f].ets = e;
            self.files[f].dir = dir;
            self.files[f].tiles = tiles;
            self.check_tiles(f)?;
        }
        Ok(())
    }

    /// The sizes of a stack's coordinates beyond x and y (the ETS header's dimension sizes; 1
    /// past them), the level coordinate when the file has a pyramid, and each tile checked
    /// (§9.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#93-tiles-and-levels>).
    fn check_tiles(&self, f: usize) -> Res<()> {
        let file = &self.files[f];
        let s = &self.stacks[file.stack];
        let (sis, e) = (&file.sis, &file.ets);
        let nc = sis.coordinates as usize;
        let level_coord = e.use_pyramid.then_some(nc - 1);
        let sizes: Vec<i64> = (0..nc).map(|c| if c < 2 || Some(c) == level_coord { i64::MAX } else { coord_size(e, c) }).collect();
        for d in &s.dims {
            let size = if Some(d.coord) == level_coord || d.coord >= nc { 1 } else { sizes[d.coord] };
            self.rules.check("dimension", rules::env([("id", num(s.id as f64)), ("index", num(d.coord as f64)), ("meaning", text(&d.meaning)), ("size", num(size as f64))]))?;
        }
        let c_size = s.dims.iter().find(|d| d.meaning == "c").map_or(1, |d| sizes.get(d.coord).copied().unwrap_or(1));
        self.rules.check("image", rules::env([("id", num(s.id as f64)), ("samples", num(e.channels as f64)), ("channels", num(c_size as f64))]))?;
        let raw = if e.compression == ets::RAW { e.tile_width as u64 * e.tile_height as u64 * e.channels as u64 * e.bytes } else { 0 };
        let mut seen: HashSet<&[i32]> = HashSet::with_capacity(file.tiles.len());
        for (k, t) in file.tiles.iter().enumerate() {
            let inside = t.coords.iter().enumerate().all(|(c, &v)| v >= 0 && (v as i64) < sizes[c]);
            let dup = !seen.insert(&t.coords);
            self.rules.check(
                "tile",
                rules::env([
                    ("key", text(&file.key)),
                    ("index", num(k as f64)),
                    ("offset", num(t.offset as f64)),
                    ("bytes", num(t.size as f64)),
                    ("size", num(file.size as f64)),
                    ("inside", flag(inside)),
                    ("duplicate", flag(dup)),
                    ("raw", num(raw as f64)),
                ]),
            )?;
        }
        Ok(())
    }

    // ---- the IR and the description

    fn add_dirs(&mut self, key: &str) -> Res<u32> {
        let ir = self.ir.as_mut().ok_or_else(|| Error::internal("no IR"))?;
        let mut parent = 0u32;
        let mut from = 0;
        for (at, _) in key.match_indices('/') {
            let dir = &key[..at];
            parent = match self.dirs.get(dir) {
                Some(&p) => p,
                None => {
                    let (n, x) = key_name(&key[from..at], false);
                    let p = ir.struct_(parent, &n, x, 0, None)?;
                    self.dirs.insert(dir.to_string(), p);
                    p
                }
            };
            from = at + 1;
        }
        Ok(parent)
    }

    fn emit(&mut self) -> Res<J> {
        let rules = self.rules;
        let file = std::mem::take(&mut self.file);
        let tree = self.tree.take().ok_or_else(|| Error::internal("no tree"))?;
        let mut ir = self.ir.take().ok_or_else(|| Error::internal("no IR"))?;
        let vsi = self.elems[self.vsi];
        ir.describe_in_place(vsi)?;
        let header = ir.value(vsi, "header", NO_INDEX, VSI_HEADER_TYPE, 0, (0, 8), Some(&file[..8]))?;
        let mut regions = Vec::new();
        tree::emit(rules, &mut ir, &file, &tree, vsi, "tree", &mut regions)?;
        let mut made: HashMap<usize, tiff::Emitted> = HashMap::new();
        for r in &regions {
            tiff::emit_region(&mut ir, &file, &self.ifds, r.start, r.end, r.parent, &mut made)?;
        }
        // each tile file's `ets` element, and its tiles' by index
        let mut tiles_of = Vec::with_capacity(self.files.len());
        for f in &self.files {
            let o = self.elems[f.object];
            tiles_of.push(ets::emit(&mut ir, o, f.size, &f.head, &f.sis, &f.header, &f.ets, &f.dir, &f.tiles)?);
        }
        self.file = file;
        self.tree = Some(tree);
        ir.sort_values();
        crate::check::finish(&mut ir)?;
        // the arrays, described on the finished IR (`stored.rs`): the parser folds no run, and
        // names each unit by the element it made of it
        let (arrays, chains, map) = self.described(&ir, header, &made, &tiles_of)?;
        ir.describe(arrays, chains, map)?;
        // the description of the slide (`describe`); the run's counts are apart
        // (`StoreParser::counts`)
        let description = self.description(header, &made, &tiles_of);
        self.ir = Some(ir);
        Ok(description)
    }

    fn advance(&mut self) -> Res<StoreStep> {
        loop {
            self.asked.clear();
            match self.phase {
                Phase::Start => {
                    self.phase = Phase::Vsi;
                    let size = self.keys[self.vsi].1;
                    self.asked.push((self.vsi, 0, size));
                }
                Phase::Vsi => {
                    let keys = self.read_vsi()?;
                    self.phase = Phase::Probed;
                    if !keys.is_empty() {
                        self.batches += 1;
                        return Ok(StoreStep::Probe(keys));
                    }
                }
                Phase::Probed => {
                    // a stack's later candidates, probed only when every earlier one is absent,
                    // so that a host is not asked for a file it does not have when the stack's
                    // tile file is already found (a host whose 404 a browser cannot read, one
                    // without CORS headers, would fail the run)
                    let keys: Vec<String> = self
                        .probes
                        .iter()
                        .filter(|(k, c, _)| *c > 0 && !self.probes.iter().any(|p| p.0 == *k && p.1 < *c && self.found.contains_key(&p.2)))
                        .map(|p| p.2.clone())
                        .collect();
                    self.phase = Phase::Fallback;
                    if !keys.is_empty() {
                        self.batches += 1;
                        return Ok(StoreStep::Probe(keys));
                    }
                }
                Phase::Fallback => {
                    self.resolve()?;
                    self.phase = Phase::Heads;
                }
                Phase::Heads => {
                    self.read_heads()?;
                    self.phase = Phase::Dirs;
                }
                Phase::Dirs => {
                    self.read_dirs()?;
                    self.phase = Phase::Done;
                    return Ok(StoreStep::Done);
                }
                Phase::Done => return Ok(StoreStep::Done),
            }
            if !self.asked.is_empty() {
                self.batches += 1;
                return Ok(StoreStep::Read(self.asked.clone()));
            }
        }
    }
}

impl StoreParser for Vsi {
    fn step(&mut self) -> Res<StoreStep> {
        self.advance()
    }

    fn feed(&mut self, object: usize, offset: u64, data: &[u8]) {
        self.got.insert((object, offset, data.len() as u64), data.to_vec());
    }

    fn found(&mut self, key: &str, index: usize, size: Option<u64>) -> Res<Option<u32>> {
        let Some(size) = size else { return Ok(None) };
        // the first candidate found is the stack's tile file: a later one is not read, nor an
        // object of the input (§9.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#92-the-tile-files>)
        if let Some(&(stack, c, _)) = self.probes.iter().find(|p| p.2 == key)
            && c > 0
            && self.probes.iter().any(|p| p.0 == stack && p.1 < c && self.found.contains_key(&p.2))
        {
            return Ok(None);
        }
        let parent = self.add_dirs(key)?;
        let ir = self.ir.as_mut().ok_or_else(|| Error::internal("no IR"))?;
        let seg = key.rsplit('/').next().unwrap_or(key);
        let (n, x) = key_name(seg, false);
        let e = ir.object(parent, &n, x, size)?;
        if index != self.keys.len() || index != self.elems.len() {
            return Err(Error::internal("a found object out of order"));
        }
        self.keys.push((key.to_string(), size));
        self.elems.push(e);
        self.found.insert(key.to_string(), (index, size));
        Ok(Some(e))
    }

    /// The run's counts, which the summary holds (spec/zarrify/vsi.md §9.5
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#95-summary>): the IFDs read,
    /// the tile files, their tiles and the read batches.
    fn counts(&self) -> J {
        json!({"ifds": self.ifds.len(), "tileFiles": self.files.len(), "tiles": self.files.iter().map(|f| f.tiles.len()).sum::<usize>(), "batches": self.batches})
    }

    fn finish(mut self: Box<Self>) -> Res<(Ir, J)> {
        if self.phase != Phase::Done {
            return Err(Error::internal("the parse is not done"));
        }
        let description = self.emit()?;
        Ok((self.ir.take().unwrap(), description))
    }
}

/// The size of coordinate `c` (2 or more) of a tile file: the ETS header's, or 1 past the
/// dimensions it lists.
fn coord_size(e: &ets::Ets, c: usize) -> i64 {
    if c < e.dimensions.clamp(0, 10) as usize { e.dimension_sizes[c].max(0) as i64 } else { 1 }
}

/// A channel's color (§4.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#42-channels>):
/// the last entry of its display LUT (a BGR array), as `RRGGBB`.
fn lut_color(rules: &Rules, b: &[u8], p: &Container) -> Option<String> {
    let v = p.fields.iter().find(|f| f.tag == STACK_DISPLAY_LUT && f.kind == Kind::MdimVolume)?.child()?;
    let f = v.fields.iter().find(|f| f.tag == 268435458 && f.kind == Kind::Value)?;
    let d = &b[f.data.0 as usize..f.data.1 as usize];
    let _ = rules;
    if d.len() < 3 || d.len() % 3 != 0 {
        return None;
    }
    let last = &d[d.len() - 3..];
    let (r, g, bl) = if f.real == 269 { (last[0], last[1], last[2]) } else { (last[2], last[1], last[0]) };
    Some(format!("{r:02X}{g:02X}{bl:02X}"))
}

/// The first value of `tag` anywhere under `c` (depth first) that is a number.
fn find_value(rules: &Rules, b: &[u8], c: &Container, tag: u32) -> Option<J> {
    if let Some(v) = tree::value_of(rules, b, c, tag).filter(|v| v.is_number()) {
        return Some(v);
    }
    c.fields.iter().filter_map(|f| f.child()).find_map(|child| find_value(rules, b, child, tag))
}

/// An Olympus VSI slide's file run: a `.vsi` file whose tile files lie beside it, zarrified as a
/// store run of the `vsi` store format (spec/zarrify/vsi.md §9.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#92-the-tile-files>), never
/// as a file run: `Format::sniff` names it so that a host sends the file to
/// [`crate::store::StoreRun`].
pub struct Profile;

impl crate::run::FileProfile for Profile {
    /// A TIFF whose byte 8 starts its metadata tree (`detect`), tested before TIFF.
    fn detect(&self, head: &[u8]) -> bool {
        detect(head)
    }
    fn parser(&self, _size: u64) -> crate::run::AnyParser {
        crate::run::AnyParser::refused("a VSI file is zarrified as a store run, with its tile files")
    }
    fn project(&self, _: &crate::ir::Ir, _: &serde_json::Value, _: &str, _: &crate::project::Copied) -> crate::error::Res<crate::out::Out> {
        Err(Error::rejected("a VSI input is zarrified as a store run"))
    }
}

/// The parser of the profile's store run (`StoreFormat`; crate::store).
pub fn store_parser(ir: crate::ir::Ir, objects: Vec<u32>, keys: &[(String, u64)]) -> crate::ir::Res<Box<dyn crate::store::StoreParser>> {
    Ok(Box::new(Vsi::new(ir, objects, keys)?))
}
