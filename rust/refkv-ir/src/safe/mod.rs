//! A sans-IO parser of Sentinel-2 SAFE products into the IR (spec/virtualize/safe.md, the
//! profile's §8 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/safe.md#8-safe-profile>),
//! in both forms:
//!
//! - **the directory form**, a store ([`crate::store::StoreParser`]): a store IR whose objects
//!   are the listing's. The XML documents it reads whole are held (the product and tile
//!   metadata, and every other XML document of at most 2^16 bytes); the band files, read in
//!   ranges, are described in place (conventions §8.9
//!   <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#89-stores>); every other
//!   object is referenced.
//! - **the zip form**, a file ([`crate::Parser`]): a file IR of the `.SAFE.zip`, whose zip
//!   structures, entries and band files are elements of source 0.
//!
//! Batches: (zip form) the end records, the ZIP64 records, the central directory and the
//! local headers; then the product metadata; the tile metadata; then every band file's walk
//! ([`jp2::BandReader`]) in step, one read per band file a batch, with, in the first, the XML
//! documents held (directory form) or inflated to be checked (zip form). The schema
//! (`schema/safe.json`) is checked as the profile meets each role.

pub mod jp2;
pub mod product;
pub mod xmltree;
pub mod zip;

use crate::ir::*;
use crate::nd2::Step;
use crate::rules::{self, Rules, flag, num, text};
use crate::store::{StoreParser, StoreStep};
use jp2::{BandReader, Codestream};
use product::{Geocoding, Image};
use serde_json::{Value as J, json};
use std::collections::{HashMap, HashSet};
use xmltree::Element;

const MARKER: &str = "_$folder$";
/// The XML documents of at most this many bytes are held, besides the product and tile metadata.
pub const MAX_HELD_XML: u64 = 1 << 16;
/// A read of the zip file's own structures, not of an object.
const FILE: usize = usize::MAX;
pub const TILE_FORM: &str = r#"{"codec":"jpeg2000-tile-part","recipe":[["src",0,null]]}"#;
pub const DEFLATE_FORM: &str = r#"{"transform":"deflate"}"#;
const SOT_TYPE: &str = "{marker:>u2,lsot:>u2,isot:>u2,psot:>u4,tpsot:u1,tnsot:u1}";

pub fn is_xml(key: &str) -> bool {
    key == "manifest.safe" || key.ends_with(".xml") || key.ends_with(".xsd")
}

/// (the folder markers among `keys`, the empty directories, sorted) (spec/virtualize/safe.md
/// §2.1 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/safe.md#21-objects>): a folder
/// marker is a key `d_$folder$` with `d` not empty; `directories` are the keys, ending in `/`,
/// of a zip file's directory entries or of a listing's empty `d/` objects. The directories they
/// name are empty when no key (theirs and the markers' included) is under them.
pub fn folders(keys: &[&str], directories: &[String]) -> (HashSet<String>, Vec<String>) {
    let markers: HashSet<String> = keys.iter().filter(|k| k.ends_with(MARKER) && k.len() > MARKER.len()).map(|k| k.to_string()).collect();
    let mut named: HashSet<String> = markers.iter().map(|k| k[..k.len() - MARKER.len()].to_string()).collect();
    named.extend(directories.iter().filter(|d| d.len() > 1).map(|d| d[..d.len() - 1].to_string()));
    let mut parents: HashSet<&str> = HashSet::new();
    for k in keys.iter().copied().chain(named.iter().map(|s| s.as_str())) {
        for (at, _) in k.match_indices('/') {
            if at == 0 {
                break;
            }
            parents.insert(&k[..at]);
        }
    }
    let mut empty: Vec<String> = named.iter().filter(|d| !parents.contains(d.as_str())).cloned().collect();
    empty.sort();
    (markers, empty)
}

/// An object of the product: its key and size; in the zip form, its central directory entry.
struct Obj {
    key: String,
    size: u64,
    entry: usize,
}

#[derive(PartialEq, Eq, Clone, Copy, Debug)]
enum Phase {
    ZipTail,
    ZipLocator,
    ZipRecord,
    ZipCd,
    ZipLocal,
    Start,
    Product,
    Tile,
    Bands,
    Done,
}

/// A band of the product: its image file, object, structure, resolution and name.
pub struct Band {
    image: Image,
    obj: usize,
    cs: Codestream,
    resolution: u64,
    name: String,
}

pub struct Safe {
    rules: &'static Rules,
    store: bool,
    size: u64,
    phase: Phase,
    // the directory form: the listing's IR and its objects' elements, and the listing's empty
    // `d/` keys (its recorded ignored keys reach the projection through the store run's facts)
    ir: Option<Ir>,
    elems: Vec<u32>,
    listed_dirs: Vec<String>,
    // the zip form
    end: zip::End,
    entries: Vec<zip::Entry>,
    zprod: Option<zip::Product>,
    deflated: Vec<bool>,
    raw: HashMap<&'static str, Vec<u8>>,
    // both
    objs: Vec<Obj>,
    by_key: HashMap<String, usize>,
    markers: HashSet<usize>,
    empty_dirs: Vec<String>,
    asked: Vec<(usize, u64, u64)>,
    got: HashMap<(usize, u64, u64), Vec<u8>>,
    level: usize,
    product_obj: usize,
    tile_obj: usize,
    metadata: Option<Element>,
    tile: Option<Element>,
    images: Vec<Image>,
    geo: Option<Geocoding>,
    readers: Vec<BandReader>,
    reader_of: HashMap<usize, usize>,
    held: Vec<usize>,
    bands: Vec<Band>,
    pub batches: u64,
    begun: bool,
}

fn whole_key(o: usize, n: u64) -> (usize, u64, u64) {
    (o, 0, n)
}

impl Safe {
    fn blank(store: bool, size: u64) -> Safe {
        Safe {
            rules: rules::of("safe"), store, size, phase: if store { Phase::Start } else { Phase::ZipTail }, ir: None, elems: Vec::new(),
            listed_dirs: Vec::new(), end: zip::End::default(), entries: Vec::new(), zprod: None,
            deflated: Vec::new(), raw: HashMap::new(), objs: Vec::new(), by_key: HashMap::new(), markers: HashSet::new(),
            empty_dirs: Vec::new(), asked: Vec::new(), got: HashMap::new(), level: 0, product_obj: 0, tile_obj: 0, metadata: None,
            tile: None, images: Vec::new(), geo: None, readers: Vec::new(), reader_of: HashMap::new(), held: Vec::new(),
            bands: Vec::new(), batches: 0, begun: false,
        }
    }

    /// The zip form: a `.SAFE.zip` of `size` bytes.
    pub fn new(size: u64) -> Safe {
        Safe::blank(false, size)
    }

    /// The directory form: the listing's IR, its objects' elements and keys.
    pub fn new_store(ir: Ir, objects: Vec<u32>, keys: &[(String, u64)]) -> Safe {
        let mut s = Safe::blank(true, 0);
        s.objs = keys.iter().enumerate().map(|(k, (key, n))| Obj { key: key.clone(), size: *n, entry: k }).collect();
        s.ir = Some(ir);
        s.elems = objects;
        s
    }

    fn ask(&mut self, o: usize, off: u64, n: u64) {
        self.asked.push((o, off, n));
    }

    /// Reads object `o` whole (in the zip form, its compressed bytes when deflated).
    fn ask_whole(&mut self, o: usize) {
        let n = self.objs[o].size;
        if n == 0 {
            self.got.insert((o, 0, 0), Vec::new()); // nothing to read
        } else {
            self.ask(o, 0, n);
        }
    }

    /// Object `o`'s bytes, read whole (inflated and checked when deflated).
    fn whole(&mut self, o: usize) -> Result<Vec<u8>, String> {
        let n = self.objs[o].size;
        let b = self.got.remove(&whole_key(o, n)).ok_or("internal: an object not read")?;
        if !self.store && self.deflated[self.objs[o].entry] {
            return zip::inflate(&b, &self.entries[self.objs[o].entry]);
        }
        Ok(b)
    }

    fn has(&self, key: &str) -> bool {
        self.by_key.get(key).is_some_and(|o| !self.markers.contains(o))
    }

    // ---- the zip form's own structures (§8.4 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/safe.md#84-the-zip-form>)

    fn zip_step(&mut self) -> Result<(), String> {
        let n = self.size;
        match self.phase {
            Phase::ZipTail if !self.begun => {
                self.begun = true;
                if n == 0 {
                    return Err("not a zip file: no end of central directory record".into());
                }
                let t = n.min(zip::MAX_TAIL);
                self.ask(FILE, n - t, t);
            }
            Phase::ZipTail => {
                let t = n.min(zip::MAX_TAIL);
                let tail = self.got.remove(&(FILE, n - t, t)).ok_or("internal: no tail")?;
                self.end = zip::eocd(self.rules, &tail, n)?;
                self.raw.insert("tail", tail);
                if let Some((loc, _)) = self.end.zip64 {
                    self.phase = Phase::ZipLocator;
                    self.ask(FILE, loc, 20);
                } else {
                    self.ask_cd()?;
                }
            }
            Phase::ZipLocator => {
                let loc = self.end.eocd - 20;
                let b = self.got.remove(&(FILE, loc, 20)).ok_or("internal: no locator")?;
                let rec = zip::zip64_locator(&b, &self.end)?;
                self.raw.insert("locator", b);
                self.end.zip64 = Some((loc, rec));
                self.phase = Phase::ZipRecord;
                self.ask(FILE, rec, 56);
            }
            Phase::ZipRecord => {
                let rec = self.end.zip64.unwrap().1;
                let b = self.got.remove(&(FILE, rec, 56)).ok_or("internal: no ZIP64 record")?;
                zip::zip64_record(self.rules, &b, &mut self.end, rec)?;
                self.raw.insert("record", b);
                self.ask_cd()?;
            }
            Phase::ZipCd => {
                let (o, len) = (self.end.cd_offset, self.end.cd_size);
                let cd = if len == 0 { Vec::new() } else { self.got.remove(&(FILE, o, len)).ok_or("internal: no central directory")? };
                self.entries = zip::central_directory(self.rules, &cd, &self.end)?;
                self.raw.insert("cd", cd);
                let p = zip::product_entries(&self.entries)?;
                for (k, i) in &p.objects {
                    self.by_key.insert(k.clone(), self.objs.len());
                    self.objs.push(Obj { key: k.clone(), size: self.entries[*i].us, entry: *i });
                }
                self.deflated = zip::methods(self.rules, &self.entries, &p.objects, &is_xml)?;
                self.zprod = Some(p);
                let cd_offset = self.end.cd_offset;
                for k in 0..self.objs.len() {
                    let e = &self.entries[self.objs[k].entry];
                    if e.us == 0 {
                        continue;
                    }
                    if e.lho.checked_add(30).is_none_or(|x| x > cd_offset) {
                        return Err("a zip entry's local header is beyond the entries".into());
                    }
                    let lho = e.lho;
                    self.ask(FILE, lho, 30);
                }
                self.phase = Phase::ZipLocal;
                if self.asked.is_empty() {
                    self.phase = Phase::Start;
                }
            }
            Phase::ZipLocal => {
                let mut read = Vec::new();
                for k in 0..self.objs.len() {
                    let i = self.objs[k].entry;
                    if self.entries[i].us == 0 {
                        continue;
                    }
                    let lho = self.entries[i].lho;
                    let head = self.got.get(&(FILE, lho, 30)).ok_or("internal: no local header")?.clone();
                    zip::local_header(&head, &mut self.entries[i], self.end.cd_offset)?;
                    read.push(i);
                }
                self.got.clear();
                zip::disjoint(&self.entries, &read)?;
                self.phase = Phase::Start;
            }
            _ => {}
        }
        Ok(())
    }

    fn ask_cd(&mut self) -> Result<(), String> {
        let end = zip::cd_end(&self.end);
        if self.end.cd_offset.checked_add(self.end.cd_size) != Some(end) {
            return Err("a zip file's central directory does not end where its end records start".into());
        }
        self.phase = Phase::ZipCd;
        if self.end.cd_size > 0 {
            let (o, n) = (self.end.cd_offset, self.end.cd_size);
            self.ask(FILE, o, n);
        }
        Ok(())
    }

    // ---- the product (§2 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/safe.md#2-the-source>)

    fn start(&mut self) -> Result<(), String> {
        let keys: Vec<&str> = self.objs.iter().map(|o| o.key.as_str()).collect();
        let dirs = if self.store { self.listed_dirs.clone() } else { self.zprod.as_ref().map(|p| p.dirs.clone()).unwrap_or_default() };
        let (markers, empty) = folders(&keys, &dirs);
        if self.store {
            self.by_key = self.objs.iter().enumerate().map(|(k, o)| (o.key.clone(), k)).collect();
        }
        self.markers = markers.iter().filter_map(|m| self.by_key.get(m).copied()).collect();
        self.empty_dirs = empty;
        let has = |k: &str| self.has(k);
        self.level = product::product_level(self.rules, &has)?;
        let key = product::LEVELS[self.level].0;
        self.product_obj = self.by_key[key];
        self.rules.check("xml_size", &mut rules::env([("what", text(key)), ("size", num(self.objs[self.product_obj].size as f64))]))?;
        self.ask_whole(self.product_obj);
        self.phase = Phase::Product;
        Ok(())
    }

    fn product(&mut self) -> Result<(), String> {
        let key = product::LEVELS[self.level].0;
        let b = self.whole(self.product_obj)?;
        let root = product::parse_document(&b, key)?;
        let has = |k: &str| self.has(k);
        let (images, g) = product::images(self.rules, &root, self.level, &has)?;
        let tile_key = format!("GRANULE/{g}/MTD_TL.xml");
        let short: String = tile_key.chars().take(200).collect();
        self.rules.check("tile_metadata", &mut rules::env([("key", text(&short)), ("present", flag(self.has(&tile_key)))]))?;
        self.tile_obj = self.by_key[&tile_key];
        self.rules.check("xml_size", &mut rules::env([("what", text("the tile metadata")), ("size", num(self.objs[self.tile_obj].size as f64))]))?;
        self.metadata = Some(root);
        self.images = images;
        self.ask_whole(self.tile_obj);
        self.phase = Phase::Tile;
        Ok(())
    }

    fn tile(&mut self) -> Result<(), String> {
        let b = self.whole(self.tile_obj)?;
        let tile = product::parse_document(&b, "MTD_TL.xml")?;
        let expected = product::LEVELS[self.level].3;
        let found = tile.name.rsplit(':').next().unwrap_or("").to_string();
        self.rules.check("root_element", &mut rules::env([("what", text("the tile metadata")), ("found", text(&found)), ("expected", text(expected))]))?;
        self.geo = Some(product::geocoding(self.rules, &tile)?);
        self.tile = Some(tile);
        for (k, im) in self.images.iter().enumerate() {
            let o = self.by_key[&im.key];
            self.reader_of.insert(o, k);
            self.readers.push(BandReader::new(self.objs[o].size));
        }
        // the XML documents held (directory form), or inflated to be checked (zip form)
        let bands: HashSet<usize> = self.reader_of.keys().copied().collect();
        for o in 0..self.objs.len() {
            let (key, n) = (&self.objs[o].key, self.objs[o].size);
            if n == 0 || bands.contains(&o) || self.markers.contains(&o) || !is_xml(key) {
                continue;
            }
            if self.store {
                if n <= MAX_HELD_XML || o == self.product_obj || o == self.tile_obj {
                    self.held.push(o);
                    if o != self.product_obj && o != self.tile_obj {
                        self.ask_whole(o);
                    }
                }
            } else if self.deflated[self.objs[o].entry] && o != self.product_obj && o != self.tile_obj {
                self.ask_whole(o);
            }
        }
        self.phase = Phase::Bands;
        self.bands_step()
    }

    fn bands_step(&mut self) -> Result<(), String> {
        // the deflated XML documents, inflated one at a time and checked
        let fed: Vec<(usize, u64, u64)> = self.got.keys().copied().collect();
        for (o, off, n) in fed {
            if let Some(&k) = self.reader_of.get(&o) {
                let b = self.got.remove(&(o, off, n)).unwrap();
                self.readers[k].feed(off, b);
            } else if !self.store {
                self.whole(o)?;
            } else {
                self.got.remove(&(o, off, n));
            }
        }
        for k in 0..self.readers.len() {
            if let Some((off, n)) = self.readers[k].step(self.rules)? {
                let o = self.by_key[&self.images[k].key];
                self.ask(o, off, n);
            }
        }
        if self.asked.is_empty() {
            self.finish_bands()?;
            self.phase = Phase::Done;
        }
        Ok(())
    }

    /// Each band's resolution and band name, and its chunks' empty tiles, checked (§2.5
    /// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/safe.md#25-band-files>, §8.6 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/safe.md#86-chunks>).
    fn finish_bands(&mut self) -> Result<(), String> {
        let geo = self.geo.as_ref().unwrap();
        let mut names: HashSet<(u64, String)> = HashSet::new();
        let images = std::mem::take(&mut self.images);
        for (k, image) in images.into_iter().enumerate() {
            let cs = self.readers[k].cs.take().ok_or("internal: a band file not read")?;
            let found: Vec<u64> = geo.sizes.iter().filter(|s| s.1 == cs.height && s.2 == cs.width).map(|s| s.0).collect();
            let r = found.first().copied().unwrap_or(0);
            let (name, ascii) = product::band_name(&image.basename, r);
            let short_key: String = image.key.chars().take(200).collect();
            let short_name: String = name.chars().take(40).collect();
            let short_base: String = image.basename.chars().take(100).collect();
            let twice = found.len() == 1 && ascii && !names.insert((r, name.clone()));
            self.rules.check("band", &mut rules::env([("key", text(&short_key)), ("w", num(cs.width as f64)), ("h", num(cs.height as f64)),
                ("matches", num(found.len() as f64)), ("name", text(&format!("{short_name:?}"))), ("plain", text(&short_name)), ("basename", text(&format!("{short_base:?}"))),
                ("ascii", flag(ascii)), ("twice", flag(twice)), ("r", num(r as f64))]))?;
            let o = self.by_key[&image.key];
            self.bands.push(Band { image, obj: o, cs, resolution: r, name });
        }
        for b in &self.bands {
            let mut total = 0u64;
            for t in 0..b.cs.tiles.len() as u64 {
                total = total.saturating_add(jp2::tail_length(&b.cs, t)?);
            }
            let short: String = b.image.key.chars().take(200).collect();
            self.rules.check("tails", &mut rules::env([("key", text(&short)), ("total", num(total as f64)), ("n", num(b.cs.n as f64))]))?;
        }
        Ok(())
    }

    /// The next batch: (object or FILE, offset, length) ranges, object-relative; empty when done.
    fn advance(&mut self) -> Result<Vec<(usize, u64, u64)>, String> {
        loop {
            self.asked.clear();
            match self.phase {
                Phase::ZipTail | Phase::ZipLocator | Phase::ZipRecord | Phase::ZipCd | Phase::ZipLocal => self.zip_step()?,
                Phase::Start => self.start()?,
                Phase::Product => self.product()?,
                Phase::Tile => self.tile()?,
                Phase::Bands => self.bands_step()?,
                Phase::Done => return Ok(Vec::new()),
            }
            if !self.asked.is_empty() {
                self.batches += 1;
                return Ok(self.asked.clone());
            }
            if self.phase == Phase::Done {
                return Ok(Vec::new());
            }
        }
    }

    fn feed_obj(&mut self, o: usize, off: u64, data: &[u8]) {
        self.got.insert((o, off, data.len() as u64), data.to_vec());
    }

    // ---- the zip form as a file parser

    pub fn step(&mut self) -> Result<Step, String> {
        let asked = self.advance()?;
        if asked.is_empty() {
            return Ok(Step::Done);
        }
        let mut ranges = Vec::with_capacity(asked.len());
        for &(o, off, n) in &asked {
            ranges.push(self.absolute(o, off, n));
        }
        Ok(Step::Read(ranges))
    }

    /// The file's range of an object-relative read (the zip form): a whole deflated entry is
    /// its compressed bytes.
    fn absolute(&self, o: usize, off: u64, n: u64) -> (u64, u64) {
        if o == FILE {
            return (off, n);
        }
        let e = &self.entries[self.objs[o].entry];
        if n == 0 {
            return (0, 0);
        }
        if self.deflated[self.objs[o].entry] {
            return (e.ds, e.cs);
        }
        (e.ds + off, n)
    }

    pub fn feed(&mut self, offset: u64, data: Vec<u8>) {
        let asked = self.asked.clone();
        for (o, off, n) in asked {
            if self.absolute(o, off, n) == (offset, data.len() as u64) {
                self.got.insert((o, off, n), data.clone());
            }
        }
    }

    pub fn finish(mut self) -> Result<(Ir, J), String> {
        if self.phase != Phase::Done {
            return Err("internal: the parse is not done".into());
        }
        let mut ir = if self.store { self.ir.take().ok_or("internal: no IR")? } else { Ir::new(self.size) };
        if self.store {
            self.emit_store(&mut ir)?;
        } else {
            self.emit_zip(&mut ir)?;
        }
        ir.sort_values();
        crate::check::finish(&mut ir)?;
        Ok((ir, self.facts()))
    }

    // ---- the IR

    fn emit_store(&self, ir: &mut Ir) -> Result<(), String> {
        for b in &self.bands {
            let e = self.elems[b.obj];
            ir.describe_in_place(e)?;
            emit_band(ir, e, 0, &b.cs)?;
        }
        for &o in &self.held {
            ir.value(self.elems[o], "xml", NO_INDEX, &format!("ascii[{}]", self.objs[o].size), 0, (0, self.objs[o].size), None)?;
        }
        Ok(())
    }

    fn emit_zip(&self, ir: &mut Ir) -> Result<(), String> {
        let le = &self.end;
        let tail = &self.raw["tail"];
        let tail_at = self.size - tail.len() as u64;
        let at = |o: u64, n: u64| -> &[u8] { &tail[(o - tail_at) as usize..(o - tail_at + n) as usize] };
        if le.cd_size > 0 {
            let cd = &self.raw["cd"];
            let c = ir.struct_(0, "central_directory", NO_INDEX, 0, Some((le.cd_offset, le.cd_size)))?;
            for (i, e) in self.entries.iter().enumerate() {
                let p = (e.at - le.cd_offset) as usize;
                let s = ir.struct_(c, "entries/", i as u64, 0, Some((e.at, 46 + e.nn + e.ne + e.nc)))?;
                ir.value(s, "header", NO_INDEX, CD_TYPE, 0, (e.at, 46), Some(&cd[p..p + 46]))?;
                let mut o = e.at + 46;
                for (name, n, ty) in [("name", e.nn, "ascii"), ("extra", e.ne, "bytes"), ("comment", e.nc, "ascii")] {
                    if n > 0 {
                        let q = (o - le.cd_offset) as usize;
                        ir.value(s, name, NO_INDEX, &format!("{ty}[{n}]"), 0, (o, n), Some(&cd[q..q + n as usize]))?;
                    }
                    o += n;
                }
            }
        }
        if let Some((loc, rec)) = le.zip64 {
            ir.value(0, "zip64_end", NO_INDEX, ZIP64_END_TYPE, 0, (rec, 56), Some(&self.raw["record"]))?;
            ir.value(0, "zip64_locator", NO_INDEX, ZIP64_LOCATOR_TYPE, 0, (loc, 20), Some(&self.raw["locator"]))?;
        }
        ir.value(0, "end", NO_INDEX, END_TYPE, 0, (le.eocd, 22), Some(at(le.eocd, 22)))?;
        if le.comment > 0 {
            ir.value(0, "end_comment", NO_INDEX, &format!("ascii[{}]", le.comment), 0, (le.eocd + 22, le.comment), Some(at(le.eocd + 22, le.comment)))?;
        }
        let bands: HashMap<usize, &Band> = self.bands.iter().map(|b| (b.obj, b)).collect();
        for (o, obj) in self.objs.iter().enumerate() {
            let e = &self.entries[obj.entry];
            if e.us == 0 {
                continue;
            }
            let s = ir.struct_(0, "entries/", obj.entry as u64, 0, Some((e.lho, e.ds + e.cs - e.lho)))?;
            ir.value(s, "local_header", NO_INDEX, LOCAL_TYPE, 0, (e.lho, 30), Some(&e.local))?;
            if e.local_nn > 0 {
                ir.value(s, "name", NO_INDEX, &format!("ascii[{}]", e.local_nn), 0, (e.lho + 30, e.local_nn), None)?;
            }
            if e.local_ne > 0 {
                ir.value(s, "extra", NO_INDEX, &format!("bytes[{}]", e.local_ne), 0, (e.lho + 30 + e.local_nn, e.local_ne), None)?;
            }
            if let Some(b) = bands.get(&o) {
                let d = ir.struct_(s, "data", NO_INDEX, 0, Some((e.ds, e.us)))?;
                emit_band(ir, d, e.ds, &b.cs)?;
            } else if self.deflated[obj.entry] {
                ir.derived(s, "data", 0, (e.ds, e.cs), DEFLATE_FORM)?;
            } else {
                let ty = if is_xml(&obj.key) && !self.markers.contains(&o) { "ascii" } else { "bytes" };
                ir.value(s, "data", NO_INDEX, &format!("{ty}[{}]", e.us), 0, (e.ds, e.us), None)?;
            }
        }
        Ok(())
    }

    fn facts(&self) -> J {
        let geo = self.geo.as_ref().unwrap();
        let metadata = self.metadata.as_ref().unwrap();
        let l2a = self.level == 1;
        let bands: Vec<J> = self.bands.iter().map(|b| {
            let base = if self.store { 0 } else { self.entries[self.objs[b.obj].entry].ds };
            let siz_b64 = crate::mirror::base64(&b.cs.siz);
            json!({"key": b.image.key, "base": base, "resolution": b.resolution, "name": b.name,
                   "meta": product::band_metadata(metadata, l2a, &b.image.text, &b.name, &siz_b64),
                   "codestream": b.cs.to_facts()})
        }).collect();
        let band_objs: HashSet<usize> = self.bands.iter().map(|b| b.obj).collect();
        let held: HashSet<usize> = self.held.iter().copied().collect();
        let mut referenced = Vec::new();
        let mut empty = 0u64;
        for (o, obj) in self.objs.iter().enumerate() {
            if self.markers.contains(&o) || band_objs.contains(&o) || held.contains(&o) {
                continue;
            }
            if obj.size == 0 {
                empty += 1;
            } else {
                referenced.push(json!([obj.key, obj.size]));
            }
        }
        // the zip form's recorded keys; the directory form's are the store run's
        // (`StoreRun::ignore`), which it adds to these facts
        let ignored: Vec<String> = {
            let mut v = if self.store { Vec::new() } else { self.zprod.as_ref().map(|p| p.ignored.clone()).unwrap_or_default() };
            v.sort();
            v
        };
        json!({
            "level": product::LEVELS[self.level].1,
            "form": if self.store { "store" } else { "zip" },
            "root": product::root_metadata(metadata, self.tile.as_ref().unwrap(), geo),
            "geo": {"code": geo.code, "sizes": geo.sizes.iter().map(|s| [s.0, s.1, s.2]).collect::<Vec<_>>(),
                    "positions": geo.positions.iter().map(|(r, v)| json!([r, v])).collect::<Vec<_>>()},
            "bands": bands,
            "objects": if self.store { referenced.clone() } else { Vec::new() },
            "empty_dirs": self.empty_dirs,
            "ignored": ignored,
            "counts": {"objects": self.objs.len(), "folderMarkers": self.markers.len(), "emptyDirs": self.empty_dirs.len(),
                       "heldXml": self.held.len(), "otherObjects": referenced.len(), "emptyObjects": empty,
                       "tileParts": self.bands.iter().map(|b| b.cs.tiles.len()).sum::<usize>(),
                       "bandReads": self.readers.iter().map(|r| r.requests).sum::<u64>(), "batches": self.batches},
        })
    }
}

const CD_TYPE: &str = "{signature:<u4,version_made:<u2,version_needed:<u2,flags:<u2,method:<u2,time:<u2,date:<u2,crc32:<u4,compressed_size:<u4,size:<u4,name_length:<u2,extra_length:<u2,comment_length:<u2,disk:<u2,internal:<u2,external:<u4,local_header_offset:<u4}";
const LOCAL_TYPE: &str = "{signature:<u4,version:<u2,flags:<u2,method:<u2,time:<u2,date:<u2,crc32:<u4,compressed_size:<u4,size:<u4,name_length:<u2,extra_length:<u2}";
const ZIP64_END_TYPE: &str = "{signature:<u4,size:<u8,version_made:<u2,version_needed:<u2,disk:<u4,cd_disk:<u4,entries_here:<u8,entries:<u8,cd_size:<u8,cd_offset:<u8}";
const ZIP64_LOCATOR_TYPE: &str = "{signature:<u4,cd_disk:<u4,record_offset:<u8,disks:<u4}";
const END_TYPE: &str = "{signature:<u4,disk:<u2,cd_disk:<u2,entries_here:<u2,entries:<u2,cd_size:<u4,cd_offset:<u4,comment_length:<u2}";

/// A band file's elements under `parent`, its offsets from `base` (spec/virtualize/safe.md §6.3
/// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/safe.md#63-the-source-metadata-node>).
fn emit_band(ir: &mut Ir, parent: u32, base: u64, cs: &Codestream) -> Result<(), String> {
    for (i, b) in cs.boxes.iter().enumerate() {
        let bx = ir.struct_(parent, "boxes/", i as u64, 0, Some((base + b.at, b.len)))?;
        let hty = if b.header == 16 { "{lbox:>u4,tbox:ascii[4],xlbox:>u8}" } else { "{lbox:>u4,tbox:ascii[4]}" };
        ir.value(bx, "header", NO_INDEX, hty, 0, (base + b.at, b.header), Some(&b.raw))?;
        if &b.tbox != b"jp2c" {
            if b.len > b.header {
                ir.value(bx, "contents", NO_INDEX, &format!("bytes[{}]", b.len - b.header), 0, (base + b.at + b.header, b.len - b.header), None)?;
            }
            continue;
        }
        let c = ir.struct_(bx, "codestream", NO_INDEX, 0, Some((base + cs.c0, cs.n - cs.c0)))?;
        ir.value(c, "soc", NO_INDEX, ">u2", 0, (base + cs.c0, 2), Some(&[0xFF, 0x4F]))?;
        let siz_ty = "{marker:>u2,lsiz:>u2,rsiz:>u2,xsiz:>u4,ysiz:>u4,xosiz:>u4,yosiz:>u4,xtsiz:>u4,ytsiz:>u4,xtosiz:>u4,ytosiz:>u4,csiz:>u2,components:{ssiz:u1,xrsiz:u1,yrsiz:u1}[".to_string()
            + &cs.components.to_string() + "]}";
        ir.value(c, "siz", NO_INDEX, &siz_ty, 0, (base + cs.c0 + 2, cs.siz.len() as u64), Some(&cs.siz))?;
        for (m, &(pos, _, length)) in cs.markers.iter().enumerate() {
            let length = length as u64;
            let ms = ir.struct_(c, "markers/", m as u64, 0, Some((base + pos, 2 + length)))?;
            let r = (pos - cs.rest_at) as usize;
            ir.value(ms, "header", NO_INDEX, "{marker:>u2,length:>u2}", 0, (base + pos, 4), Some(&cs.rest[r..r + 4]))?;
            if length > 2 {
                ir.value(ms, "body", NO_INDEX, &format!("bytes[{}]", length - 2), 0, (base + pos + 4, length - 2), Some(&cs.rest[r + 4..r + 2 + length as usize]))?;
            }
        }
        for (k, (&(s, psot), sot)) in cs.tiles.iter().zip(&cs.sots).enumerate() {
            let t = ir.struct_(c, "tiles/", k as u64, 0, Some((base + s, psot)))?;
            ir.value(t, "sot", NO_INDEX, SOT_TYPE, 0, (base + s, 12), Some(sot))?;
            ir.data(t, "body", NO_INDEX, 0, (base + s + 12, psot - 12), TILE_FORM)?;
        }
        ir.value(c, "eoc", NO_INDEX, ">u2", 0, (base + cs.n - 2, 2), Some(&[0xFF, 0xD9]))?;
    }
    Ok(())
}

// ---- the directory form as a store parser

impl StoreParser for Safe {
    fn step(&mut self) -> Result<StoreStep, String> {
        let asked = self.advance()?;
        if asked.is_empty() {
            return Ok(StoreStep::Done);
        }
        Ok(StoreStep::Read(asked))
    }

    fn feed(&mut self, object: usize, offset: u64, data: &[u8]) {
        self.feed_obj(object, offset, data);
    }

    fn folders(&mut self, folders: &[String]) {
        self.listed_dirs = folders.to_vec();
    }

    fn finish(self: Box<Self>) -> Result<(Ir, J), String> {
        Safe::finish(*self)
    }
}

crate::parser!(Safe);

/// The SAFE profile's file run, of a product's zip form (`.SAFE.zip`; spec/virtualize/safe.md).
pub struct Profile;

impl crate::run::FileProfile for Profile {
    /// A zip local file header: any zip file is taken for a SAFE product's zip form, which
    /// then rejects one that is not (spec/virtualize/safe.md §8.1
    /// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/safe.md#81-choosing-the-profile>).
    fn detect(&self, head: &[u8]) -> bool {
        head.len() >= 4 && &head[..4] == b"PK\x03\x04"
    }
    fn parser(&self, size: u64) -> crate::run::AnyParser {
        crate::run::AnyParser::of::<Safe>(size)
    }
    /// It takes exactly the ranges it asked for.
    fn whole(&self) -> bool {
        false
    }
    fn project(&self, ir: &crate::ir::Ir, facts: &serde_json::Value, url: &str, _copied: &crate::project::Copied) -> Result<crate::out::Out, String> {
        crate::project::safe::project(ir, facts, url)
    }
    /// Its empty directories and ignored keys beside the mirror, as a SAFE directory's.
    fn mirrored(&self, out: &mut crate::out::Out, facts: &serde_json::Value) -> Result<(), String> {
        crate::project::store::record_ignored(out, facts, "safe")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn folder_markers_and_empty_directories() {
        let keys = ["a/b.xml", "AUX_DATA_$folder$", "a_$folder$", "x/_$folder$", "_$folder$"];
        let (m, e) = folders(&keys, &["q/".to_string(), "a/c/".to_string(), "/".to_string()]);
        let mut m: Vec<String> = m.into_iter().collect();
        m.sort();
        assert_eq!(m, vec!["AUX_DATA_$folder$", "a_$folder$", "x/_$folder$"]);
        assert_eq!(e, vec!["AUX_DATA", "a/c", "q", "x/"]);
    }
}
