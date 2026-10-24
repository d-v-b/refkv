//! A sans-IO parser of Olympus VSI slides (spec/virtualize/vsi.md, the profile's §9
//! <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/vsi.md#9-vsi-profile>) as a store
//! run ([`crate::store::StoreParser`]) whose objects are the `.vsi` file and the tile files
//! beside it, every one described in place (conventions §8.9
//! <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#89-stores>).
//!
//! **Batches.** The `.vsi` file, whole; then the tile files are **probed** (§9.2
//! <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/vsi.md#92-the-tile-files>): for each
//! stack the tree says has one, `_<name>_/stack<N>/frame_t.ets`, then `frame_t_0.ets`, and the
//! first that exists is the stack's tile file; then every tile file's 64-byte SIS header, in
//! one batch; then every one's ETS header and tile directory, in one batch. The schema
//! (`schema/vsi.json`) is checked as the profile meets each role. The IR is emitted at the end,
//! from what was read.

pub mod ets;
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
/// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/vsi.md#43-the-macro-image>), as
/// Bio-Formats names it.
pub const MACRO_NAME: &str = "macro image";

/// Whether a file's first bytes are a VSI file's (spec/virtualize.md §1.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize.md#12-input>): a little-endian
/// classic TIFF whose bytes 8 to 11 are a container header's size and version, `18 00 49 53`.
pub fn detect(head: &[u8]) -> bool {
    head.len() >= 12 && head[..4] == *b"II*\0" && head[8..12] == [0x18, 0x00, 0x49, 0x53]
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Phase {
    Start,
    Vsi,
    Probed,
    Heads,
    Dirs,
    Done,
}

/// A channel of a stack (§4.2 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/vsi.md#42-channels>).
#[derive(Clone, Debug, Default)]
struct Channel {
    /// the dimension name (the dye), else the channel name (the optical path)
    name: Option<String>,
    channel_name: Option<String>,
    color: Option<String>,
    display: Option<(f64, f64)>,
    wavelength: Option<f64>,
}

/// A dimension of a stack beyond x and y: its coordinate index in a tile record, and its
/// meaning (`z`, `t`, `c`, or the code's name).
#[derive(Clone, Debug)]
struct Dim {
    coord: usize,
    meaning: String,
}

#[derive(Clone, Debug, Default)]
struct Stack {
    id: u32,
    name: String,
    stack_type: Option<i64>,
    external: bool,
    width: Option<u64>,
    height: Option<u64>,
    dimension_sizes: Vec<i64>,
    dims: Vec<Dim>,
    default_ifd: Option<usize>,
    coarse_ifd: Option<usize>,
    scale: Option<(f64, f64)>,
    unit: Option<String>,
    origin: Option<(f64, f64)>,
    z_step: Option<f64>,
    z_unit: Option<String>,
    display: Option<(f64, f64)>,
    channels: Vec<Channel>,
    creation_time: Option<i64>,
    magnification: Option<f64>,
    /// the tile file's directory key, and the index of its file in `ets` once found
    dir: String,
    file: Option<usize>,
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
            return Err(format!("a VSI run starts from the .vsi file alone, not {} objects", keys.len()));
        }
        let key = &keys[0].0;
        let lower = key.to_ascii_lowercase();
        if !lower.ends_with(".vsi") || key.len() <= 4 || key.contains('/') {
            return Err(format!("the key {key:?} is not the name of a .vsi file"));
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
        self.got.remove(&k).ok_or_else(|| format!("internal: the read {k:?} was not fed"))
    }

    // ---- the .vsi file (§9.1 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/vsi.md#91-the-vsi-file>)

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
        // the tile files to probe (§9.2 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/vsi.md#92-the-tile-files>)
        let dir = format!("_{}_", self.base());
        let mut keys = Vec::new();
        for (k, s) in self.stacks.iter_mut().enumerate() {
            s.dir = format!("{dir}/stack{}", s.id);
            if s.external {
                for (c, name) in ["frame_t.ets", "frame_t_0.ets"].iter().enumerate() {
                    let key = format!("{}/{name}", s.dir);
                    self.probes.push((k, c, key.clone()));
                    keys.push(key);
                }
            }
        }
        Ok(keys)
    }

    /// The stacks of the tree: each `Multidimensional image volume` of the collection volume
    /// (§2.2 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/vsi.md#22-stacks>).
    fn find_stacks(&self, root: &Container) -> Res<Vec<Stack>> {
        let (rules, b) = (self.rules, self.file.as_slice());
        let mut out = Vec::new();
        let Some(coll) = root.volume(COLLECTION_VOLUME) else { return Ok(out) };
        for f in coll.fields.iter().filter(|f| f.tag == MULTIDIM_IMAGE_VOLUME && f.kind == Kind::Volume) {
            let Some(sc) = f.child() else { continue };
            let mut s = Stack { id: f.tag2.unwrap_or(0), ..Default::default() };
            s.dimension_sizes = tree::numbers_of(rules, b, sc, DIMENSION_SIZE).unwrap_or_default().iter().map(|v| *v as i64).collect();
            if let Some(fc) = sc.volume(IMAGE_FRAME_VOLUME) {
                if let Some(xp) = fc.volume(EXTERNAL_FILE_PROPERTIES) {
                    if let Some(v) = tree::numbers_of(rules, b, xp, IMAGE_BOUNDARY).filter(|v| v.len() >= 4) {
                        s.width = Some(v[2].max(0.0) as u64);
                        s.height = Some(v[3].max(0.0) as u64);
                    }
                    s.external = tree::number_of(rules, b, xp, HAS_EXTERNAL_FILE).is_some_and(|v| v != 0.0);
                }
                s.default_ifd = fc.fields.iter().find(|x| x.tag == DEFAULT_SAMPLE_IFD && x.kind == Kind::TiffIfd).and_then(|x| x.tag2).map(|x| x as usize);
                s.coarse_ifd = fc.fields.iter().find(|x| x.tag == COARSE_FRAME_IFD && x.kind == Kind::TiffIfd).and_then(|x| x.tag2).map(|x| x as usize);
            }
            if let Some(pc) = sc.volume(MULTIDIM_STACK_PROPERTIES) {
                s.name = tree::text_of(rules, b, pc, STACK_NAME).unwrap_or_default();
                s.stack_type = tree::number_of(rules, b, pc, STACK_TYPE).map(|v| v as i64);
                s.scale = tree::numbers_of(rules, b, pc, RWC_FRAME_SCALE).filter(|v| v.len() >= 2).map(|v| (v[0], v[1]));
                s.origin = tree::numbers_of(rules, b, pc, RWC_FRAME_ORIGIN).filter(|v| v.len() >= 2).map(|v| (v[0], v[1]));
                s.unit = tree::text_of(rules, b, pc, RWC_FRAME_UNIT);
                s.display = tree::numbers_of(rules, b, pc, DISPLAY_LIMITS).filter(|v| v.len() >= 2).map(|v| (v[0], v[1]));
                s.creation_time = tree::number_of(rules, b, pc, CREATION_TIME).map(|v| v as i64);
                s.magnification = find_number(rules, b, pc, OBJECTIVE_MAG);
            }
            // the dimensions (§2.2 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/vsi.md#22-stacks>): the description volumes, by their second tag
            for d in sc.fields.iter().filter(|x| x.tag == DIMENSION_DESCRIPTION_VOLUME && x.kind == Kind::Volume) {
                let Some(dc) = d.child() else { continue };
                let index = d.tag2.unwrap_or(0) as usize;
                let psets: Vec<&Container> = dc.fields.iter().filter(|x| x.tag == CHANNEL_PROPERTIES).filter_map(|x| x.child()).collect();
                let code = psets.iter().find_map(|p| tree::number_of(rules, b, p, DIMENSION_MEANING));
                let meaning = match code {
                    Some(c) => rules.table("dimension_meanings").get((c as i64).to_string()).and_then(|v| v.as_str()).map(str::to_string).unwrap_or_else(|| format!("meaning {}", c as i64)),
                    None if psets.iter().any(|p| p.field(Z_START).is_some()) => "z".into(),
                    None if psets.iter().any(|p| p.field(TIME_START).is_some() || p.field(DIMENSION_VALUE_ID).is_some()) => "t".into(),
                    None if psets.iter().any(|p| p.field(CHANNEL_NAME).is_some() || p.field(STACK_DISPLAY_LUT).is_some()) => "c".into(),
                    None => "unknown".into(),
                };
                if meaning == "z" {
                    for p in &psets {
                        if let Some((v, u)) = tree::mdim_value(rules, b, p, Z_INCREMENT) {
                            s.z_step = v.as_f64();
                            s.z_unit = u;
                        }
                    }
                }
                if meaning == "c" {
                    for p in &psets {
                        let mut ch = Channel { name: tree::text_of(rules, b, p, DIMENSION_NAME).or_else(|| tree::text_of(rules, b, p, CHANNEL_NAME)), channel_name: tree::text_of(rules, b, p, CHANNEL_NAME), ..Default::default() };
                        ch.display = tree::numbers_of(rules, b, p, DISPLAY_LIMITS).filter(|v| v.len() >= 2).map(|v| (v[0], v[1]));
                        ch.color = lut_color(rules, b, p);
                        ch.wavelength = tree::mdim_value(rules, b, p, CHANNEL_WAVELENGTH).and_then(|(v, _)| v.as_f64());
                        s.channels.push(ch);
                    }
                }
                s.dims.push(Dim { coord: 2 + index, meaning });
            }
            s.dims.sort_by_key(|d| d.coord);
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

    // ---- the tile files (§9.2 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/vsi.md#92-the-tile-files>)

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
                return Err(format!("{key} ({size} bytes) is too short for an ETS file"));
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
    /// (§9.3 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/vsi.md#93-tiles-and-levels>).
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

    // ---- the IR and the facts

    fn add_dirs(&mut self, key: &str) -> Res<u32> {
        let ir = self.ir.as_mut().ok_or("internal: no IR")?;
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

    /// The facts of the images (§4 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/vsi.md#4-the-images>):
    /// each stack with pixels (a tile file, or an IFD) in the tree's order, then the macro image.
    fn images(&self, tiles_of: &[Vec<u32>], strips_of: &HashMap<usize, Vec<u32>>) -> Res<Vec<J>> {
        let b = self.file.as_slice();
        let mut out = Vec::new();
        for s in &self.stacks {
            let mut im = json!({
                "kind": "stack", "id": s.id, "name": s.name, "type": s.stack_type,
                "type_name": s.stack_type.and_then(|t| self.rules.table("stack_types").get(t.to_string()).cloned()),
                "width": s.width, "height": s.height, "dimension_sizes": s.dimension_sizes,
                "dimensions": s.dims.iter().map(|d| json!({"coordinate": d.coord, "meaning": d.meaning})).collect::<Vec<_>>(),
                "scale": s.scale.map(|(x, y)| json!([x, y])), "unit": s.unit,
                "unit_name": s.unit.as_ref().and_then(|u| self.rules.table("units").get(u).cloned()),
                "origin": s.origin.map(|(x, y)| json!([x, y])),
                "z_step": s.z_step, "z_unit": s.z_unit,
                "z_unit_name": s.z_unit.as_ref().and_then(|u| self.rules.table("units").get(u).cloned()),
                "display": s.display.map(|(a, z)| json!([a, z])),
                "channels": s.channels.iter().map(|c| json!({"name": c.name, "channel_name": c.channel_name, "color": c.color, "display": c.display.map(|(a, z)| json!([a, z])), "wavelength": c.wavelength})).collect::<Vec<_>>(),
                "creation_time": s.creation_time, "magnification": s.magnification,
                "default_ifd": s.default_ifd, "coarse_ifd": s.coarse_ifd,
            });
            if let Some(f) = s.file {
                let file = &self.files[f];
                im["ets"] = self.ets_facts(file, &tiles_of[f])?;
            } else if let Some(k) = s.default_ifd {
                im["ifd"] = self.ifd_facts(k, &s.name, strips_of)?;
            } else {
                continue;
            }
            out.push(im);
        }
        // the macro image: the collection volume's default sample IFD
        let root = self.tree.as_ref().ok_or("internal: no tree")?;
        if let Some(coll) = root.volume(COLLECTION_VOLUME)
            && let Some(k) = coll.fields.iter().find(|x| x.tag == DEFAULT_SAMPLE_IFD && x.kind == Kind::TiffIfd).and_then(|x| x.tag2)
        {
            let _ = b;
            out.push(json!({"kind": "macro", "name": MACRO_NAME, "ifd": self.ifd_facts(k as usize, MACRO_NAME, strips_of)?}));
        }
        Ok(out)
    }

    fn ifd_facts(&self, k: usize, what: &str, strips_of: &HashMap<usize, Vec<u32>>) -> Res<J> {
        let ifd = self.ifds.iter().find(|f| f.index == k && !f.exif).ok_or_else(|| format!("{what}: the .vsi file has no IFD {k}"))?;
        let im = tiff::image(self.rules, &self.file, ifd, what)?;
        let els = strips_of.get(&k).ok_or_else(|| format!("IFD {k} ({what}) is not inside the metadata tree"))?;
        if els.len() != im.strips.len() {
            return Err(format!("internal: IFD {k} has {} strip elements for {} strips", els.len(), im.strips.len()));
        }
        Ok(json!({
            "index": k, "width": im.width, "height": im.height, "samples": im.samples, "bits": im.bits,
            "compression": im.compression, "photometric": im.photometric,
            "strips": im.strips.iter().zip(els).map(|(&(o, n), &e)| json!([e, o, n])).collect::<Vec<_>>(),
            "prefix": im.prefix,
        }))
    }

    /// A tile file's facts: its headers, the level sizes (§4.1
    /// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/vsi.md#41-the-pyramid>) and
    /// every tile as `[element, x, y, level, t, c, z]`.
    fn ets_facts(&self, file: &EtsFile, els: &[u32]) -> Res<J> {
        let s = &self.stacks[file.stack];
        let (sis, e) = (&file.sis, &file.ets);
        let nc = sis.coordinates as usize;
        let level_coord = e.use_pyramid.then_some(nc - 1);
        let coord_of = |m: &str| s.dims.iter().find(|d| d.meaning == m && Some(d.coord) != level_coord && d.coord < nc).map(|d| d.coord);
        let (tc, cc, zc) = (coord_of("t"), coord_of("c"), coord_of("z"));
        let size_of = |c: Option<usize>| c.map_or(1, |c| coord_size(e, c).max(1));
        let levels = if let Some(lc) = level_coord { file.tiles.iter().map(|t| t.coords[lc] as i64).max().unwrap_or(0) + 1 } else { 1 } as usize;
        let (tw, th) = (e.tile_width as u64, e.tile_height as u64);
        // the level sizes, as Bio-Formats computes them from the stack's size and the tiles
        let (w0, h0) = (s.width.unwrap_or(0), s.height.unwrap_or(0));
        let mut max_x = vec![0i64; levels];
        let mut max_y = vec![0i64; levels];
        for t in &file.tiles {
            let l = level_coord.map_or(0, |lc| t.coords[lc] as usize);
            max_x[l] = max_x[l].max(t.coords[0] as i64);
            max_y[l] = max_y[l].max(t.coords[1] as i64);
        }
        let mut lv: Vec<(u64, u64)> = vec![(w0, h0)];
        for l in 1..levels {
            let (pw, ph) = lv[l - 1];
            let (mx, my) = (tw * (max_x[l] as u64 + 1), th * (max_y[l] as u64 + 1));
            let mut w = pw / 2;
            if pw % 2 == 1 && w < mx {
                w += 1;
            } else if w > mx {
                w = mx;
            }
            let mut h = ph / 2;
            if ph % 2 == 1 && h < my {
                h += 1;
            } else if h > my {
                h = my;
            }
            lv.push((w, h));
        }
        let mut tiles = Vec::with_capacity(file.tiles.len());
        let mut outside = 0u64;
        for (k, t) in file.tiles.iter().enumerate() {
            let l = level_coord.map_or(0, |lc| t.coords[lc] as usize);
            let (w, h) = lv[l];
            let (x, y) = (t.coords[0] as u64, t.coords[1] as u64);
            if x >= w.div_ceil(tw) || y >= h.div_ceil(th) {
                outside += 1;
                continue;
            }
            let at = |c: Option<usize>| c.map_or(0, |c| t.coords[c] as u64);
            tiles.push(json!([els[k], x, y, l, at(tc), at(cc), at(zc)]));
        }
        let fill = fill_value(e);
        Ok(json!({
            "key": file.key, "object": file.object, "size": file.size,
            "header": ets::header_json(&file.key, sis, e, file.size),
            "data_type": e.data_type, "bytes": e.bytes, "samples": e.channels, "codec": e.codec,
            "bgr": e.component_order == 1 && e.compression == ets::RAW,
            "tile": [tw, th], "fill": fill, "levels": lv.iter().map(|&(w, h)| json!([w, h])).collect::<Vec<_>>(),
            "sizes": {"t": size_of(tc), "c": size_of(cc), "z": size_of(zc)},
            "coordinates": {"t": tc, "c": cc, "z": zc, "level": level_coord},
            "tiles": tiles, "tiles_outside": outside,
        }))
    }

    fn emit(&mut self) -> Res<J> {
        let rules = self.rules;
        let file = std::mem::take(&mut self.file);
        let tree = self.tree.take().ok_or("internal: no tree")?;
        let mut ir = self.ir.take().ok_or("internal: no IR")?;
        let vsi = self.elems[self.vsi];
        ir.describe_in_place(vsi)?;
        ir.value(vsi, "header", NO_INDEX, VSI_HEADER_TYPE, 0, (0, 8), Some(&file[..8]))?;
        let mut regions = Vec::new();
        tree::emit(rules, &mut ir, &file, &tree, vsi, "tree", &mut regions)?;
        let mut strips_of: HashMap<usize, Vec<u32>> = HashMap::new();
        for r in &regions {
            tiff::emit_region(&mut ir, &file, &self.ifds, r.start, r.end, r.parent, &mut strips_of)?;
        }
        let mut tiles_of = Vec::with_capacity(self.files.len());
        for f in &self.files {
            let o = self.elems[f.object];
            tiles_of.push(ets::emit(&mut ir, o, f.size, &f.head, &f.sis, &f.header, &f.ets, &f.dir, &f.tiles)?);
        }
        self.file = file;
        self.tree = Some(tree);
        let images = self.images(&tiles_of, &strips_of)?;
        let tree = self.tree.as_ref().unwrap();
        let facts = json!({
            "vsi": {"key": self.keys[self.vsi].0, "size": self.keys[self.vsi].1, "ifds": self.ifds.iter().filter(|f| !f.exif).count()},
            "tree": tree::container_json(rules, &self.file, tree),
            "images": images,
            "stacks": self.stacks.iter().map(|s| json!({
                "id": s.id, "name": s.name, "type": s.stack_type, "external": s.external, "width": s.width, "height": s.height,
                "tile_file": s.file.map(|f| self.files[f].key.clone()),
            })).collect::<Vec<_>>(),
            "counts": {"ifds": self.ifds.len(), "tileFiles": self.files.len(), "tiles": self.files.iter().map(|f| f.tiles.len()).sum::<usize>(), "batches": self.batches},
        });
        ir.sort_values();
        crate::check::finish(&mut ir)?;
        self.ir = Some(ir);
        Ok(facts)
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
        // object of the input (§9.2 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/vsi.md#92-the-tile-files>)
        if let Some(&(stack, c, _)) = self.probes.iter().find(|p| p.2 == key)
            && c > 0
            && self.probes.iter().any(|p| p.0 == stack && p.1 < c && self.found.contains_key(&p.2))
        {
            return Ok(None);
        }
        let parent = self.add_dirs(key)?;
        let ir = self.ir.as_mut().ok_or("internal: no IR")?;
        let seg = key.rsplit('/').next().unwrap_or(key);
        let (n, x) = key_name(seg, false);
        let e = ir.object(parent, &n, x, size)?;
        if index != self.keys.len() || index != self.elems.len() {
            return Err("internal: a found object out of order".into());
        }
        self.keys.push((key.to_string(), size));
        self.elems.push(e);
        self.found.insert(key.to_string(), (index, size));
        Ok(Some(e))
    }

    fn finish(mut self: Box<Self>) -> Res<(Ir, J)> {
        if self.phase != Phase::Done {
            return Err("internal: the parse is not done".into());
        }
        let facts = self.emit()?;
        Ok((self.ir.take().unwrap(), facts))
    }
}

/// The size of coordinate `c` (2 or more) of a tile file: the ETS header's, or 1 past the
/// dimensions it lists.
fn coord_size(e: &ets::Ets, c: usize) -> i64 {
    if c < e.dimensions.clamp(0, 10) as usize { e.dimension_sizes[c].max(0) as i64 } else { 1 }
}

/// The fill value of a tile file's arrays (§4.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/vsi.md#41-the-pyramid>): the
/// background color's first sample, as the pixel type reads it.
fn fill_value(e: &ets::Ets) -> J {
    let b = &e.background;
    match e.data_type.as_str() {
        "int8" => json!(b[0] as i8),
        "uint8" => json!(b[0]),
        "int16" => json!(i16::le_at(b, 0)),
        "uint16" => json!(u16::le_at(b, 0)),
        "int32" => json!(i32::le_at(b, 0)),
        "uint32" => json!(u32::le_at(b, 0)),
        "int64" => json!(i64::le_at(b, 0)),
        "uint64" => json!(u64::le_at(b, 0)),
        "float32" => {
            let v = f32::le_at(b, 0);
            if v.is_finite() { json!(v) } else { json!(0) }
        }
        _ => {
            let v = f64::le_at(b, 0);
            if v.is_finite() { json!(v) } else { json!(0) }
        }
    }
}

/// A channel's color (§4.2 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/vsi.md#42-channels>):
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

/// The first number of `tag` anywhere under `c` (depth first).
fn find_number(rules: &Rules, b: &[u8], c: &Container, tag: u32) -> Option<f64> {
    if let Some(v) = tree::number_of(rules, b, c, tag) {
        return Some(v);
    }
    c.fields.iter().filter_map(|f| f.child()).find_map(|child| find_number(rules, b, child, tag))
}

/// An Olympus VSI slide's file run: a `.vsi` file whose tile files lie beside it, virtualized as a
/// store run of the `vsi` store format (spec/virtualize/vsi.md §9.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/vsi.md#92-the-tile-files>), never
/// as a file run: `Format::sniff` names it so that a host sends the file to
/// [`crate::store::StoreRun`].
pub struct Profile;

impl crate::run::FileProfile for Profile {
    /// A TIFF whose byte 8 starts its metadata tree (`detect`), tested before TIFF.
    fn detect(&self, head: &[u8]) -> bool {
        detect(head)
    }
    fn parser(&self, _size: u64) -> crate::run::AnyParser {
        crate::run::AnyParser::refused("a VSI file is virtualized as a store run, with its tile files")
    }
    fn store_run(&self) -> bool {
        true
    }
    fn project(&self, _: &crate::ir::Ir, _: &serde_json::Value, _: &str, _: &crate::project::Copied) -> Result<crate::out::Out, String> {
        Err("a VSI input is virtualized as a store run".into())
    }
}
