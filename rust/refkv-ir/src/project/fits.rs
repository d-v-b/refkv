//! The FITS projection (spec/zarrify/fits.md §2
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/fits.md#2-hdus-and-nodes>) from a
//! valid IR: the root group with the table of HDUs and the primary header's cards, and one
//! node per HDU at the path of its index: an array for an image (its chunks cut from the
//! `data` element), a group with the arrays `rows` (a struct over the rows, or their bytes) and
//! `heap` for a table, and an empty group for an HDU without data, each declaring the convention
//! with its own cards. A compressed file's arrays have one chunk each, its whole stream.

use super::{contiguous, key, Copied};
use crate::fed::Word;
use crate::fits::{card, parse_cards, row_struct, tile_cut, Card, Value};
use crate::fits_tiles::{prefix, TileRecord};
use crate::ir::Ir;
use crate::out::{array_json, blob_chunks, bytes_codec, declare_array, declared_group, grid_chunks, int_json, item_size, json_size, Chunk, Chunking, Out, Part};
use crate::rules;
use std::collections::HashMap;
use crate::refs::Refs;
use serde_json::{json, Map, Value as J};

/// An array of an HDU with its chunks.
struct Node {
    path: String,
    data_type: String,
    shape: Vec<u64>,
    dims: Vec<String>,
    element: Option<u32>,
    grid: Vec<u64>,
    chunks: Vec<(Vec<u64>, Chunk)>,
    /// in a compressed file, the array's bytes of what it decodes to: (offset, length)
    within: Option<(u64, u64)>,
    /// a table's rows as a struct (§4
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/fits.md#4-tables>): its data type,
    /// its fill value and whether a field has a byte order
    row: Option<J>,
}

const PROFILE: &str = "fits";
/// The most bytes of a chunk of an image or of a table's rows (§3.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/fits.md#31-the-array>).
const CHUNK_BYTES: u64 = 1 << 20;
/// The most bytes of JSON a node's cards may take (§5.3
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/fits.md#53-cards>).
const CARDS_BUDGET: usize = 65536;

/// A card's value as JSON (§5.3
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/fits.md#53-cards>): an integer exactly, whatever its magnitude; a real as
/// its binary64 value; a complex as `[re, im]`; no value as null.
fn value_json(v: &Value) -> J {
    match v {
        Value::None => J::Null,
        Value::Logical(b) => json!(b),
        Value::Int(n) => int_json(*n),
        Value::BigInt(t) => big_json(t),
        Value::Real(x, t) => if x.is_finite() { json!(x) } else { json!({"text": t}) },
        Value::Str(s) => json!(s),
        Value::Complex(r, i) => json!([r, i]),
        Value::Unparsed(t) => json!({"text": t}),
    }
}

fn cards_json(cards: &[Card]) -> J {
    J::Array(cards.iter().map(|c| json!([c.keyword, value_json(&c.value), c.comment])).collect())
}

/// A card's numeric value, when it has one.
fn number(cards: &[Card], keyword: &str) -> J {
    match card(cards, keyword).map(|c| &c.value) {
        Some(Value::Int(n)) => int_json(*n),
        Some(Value::BigInt(t)) => big_json(t),
        Some(Value::Real(x, _)) if x.is_finite() => json!(x),
        _ => J::Null,
    }
}

/// An integer card past i128 (its digits, normalized), exactly: a value field holds 70 characters.
fn big_json(digits: &str) -> J {
    J::Number(digits.parse().expect("arbitrary_precision holds every integer"))
}

fn string(cards: &[Card], keyword: &str) -> J {
    match card(cards, keyword).map(|c| &c.value) {
        Some(Value::Str(s)) => json!(s),
        _ => J::Null,
    }
}

/// A card that is a string in one kind of table and a number in the other (TNULLn: an ASCII
/// table's null string, a binary table's null integer): whichever it holds.
fn string_or_number(cards: &[Card], keyword: &str) -> J {
    match string(cards, keyword) {
        J::Null => number(cards, keyword),
        s => s,
    }
}

/// The Zarr data type of the values of `bitpix` (§3.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/fits.md#31-the-array>), the profile's
/// table of BITPIX.
fn data_type(bitpix: &J) -> String {
    let table = rules::of(PROFILE).table("data_types");
    table[bitpix.as_i64().unwrap_or(8).to_string()]["dtype"].as_str().unwrap_or("uint8").to_string()
}

/// A binary table's rows as a struct, or why they stay bytes (§4.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/fits.md#41-rows-as-a-struct>), from its
/// header's `TFORMn` and `TTYPEn`; an ASCII table's stay bytes (`ascii`). None for an HDU that is
/// no table, or a tile-compressed image's table.
fn row_of(h: &J, cards: &[Card]) -> Option<Result<J, &'static str>> {
    if h["kind"] != "table" {
        return None;
    }
    if h["type"] != "bintable" {
        return Some(Err("ascii"));
    }
    let t = &h["table"];
    let forms: Vec<String> = t["tform"].as_array().into_iter().flatten().filter_map(|f| f.as_str().map(str::to_string)).collect();
    let r = rules::of(PROFILE);
    Some(row_struct(cards, &forms, t["row_bytes"].as_u64().unwrap_or(0), r.limit("max_row_fields") as u64, r.limit("max_row_bytes") as u64))
}

/// The elements of the tile-compressed images' heaps (`hdus/<i>/heap/<j>`), by their HDU's node
/// and `j`, which the projection's chunks are: the parser makes one per chunk of [`tile_cut`] that
/// holds a tile.
fn heap_chunks(ir: &Ir) -> HashMap<(u32, u64), u32> {
    (0..ir.len()).filter(|&i| ir.names.get(ir.name[i]) == "heap/").map(|i| ((ir.parent[i], ir.nidx[i]), i as u32)).collect()
}

/// The cards of HDU `h`, from its header element.
fn cards_of(ir: &Ir, h: &J) -> Result<Vec<Card>, String> {
    let id = h["elements"]["header"].as_u64().ok_or("internal: an HDU without a header")? as u32;
    let raw = ir.value_bytes(id).ok_or("internal: the header is not held")?;
    Ok(parse_cards(raw, h["cards"].as_u64().unwrap_or(0) as usize))
}

/// An HDU node's source metadata (§5.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/fits.md#52-an-hdus-node>): its index, its cards (null past the budget), an
/// image's scaling members, a table's columns.
fn own_json(h: &J, cards: &[Card], row: Option<&Result<J, &'static str>>) -> J {
    let mut own = Map::new();
    own.insert("index".into(), h["index"].clone());
    let cj = cards_json(cards);
    own.insert("cards".into(), if json_size(&cj) <= CARDS_BUDGET { cj } else { J::Null });
    match h["kind"].as_str() {
        Some("image") => {
            own.insert("bscale".into(), number(cards, "BSCALE"));
            own.insert("bzero".into(), number(cards, "BZERO"));
            own.insert("blank".into(), number(cards, "BLANK"));
            own.insert("bunit".into(), string(cards, "BUNIT"));
        }
        Some("table") => {
            let forms = h["table"]["tform"].as_array().cloned().unwrap_or_default();
            let fields = match row {
                Some(Ok(r)) => r["fields"].clone(),
                _ => J::Null,
            };
            let columns: Vec<J> = forms
                .iter()
                .enumerate()
                .map(|(k, form)| {
                    let n = k + 1;
                    json!({
                        "name": string(cards, &format!("TTYPE{n}")), "form": form,
                        "unit": string(cards, &format!("TUNIT{n}")), "dim": string(cards, &format!("TDIM{n}")),
                        "scale": number(cards, &format!("TSCAL{n}")), "zero": number(cards, &format!("TZERO{n}")),
                        "null": string_or_number(cards, &format!("TNULL{n}")), "disp": string(cards, &format!("TDISP{n}")),
                        "bcol": number(cards, &format!("TBCOL{n}")),
                        "field": fields[k],
                    })
                })
                .collect();
            own.insert("columns".into(), J::Array(columns));
            own.insert("not_struct".into(), match row {
                Some(Err(reason)) => json!(reason),
                _ => J::Null,
            });
        }
        _ => {}
    }
    J::Object(own)
}

/// The arrays of HDU `h`, cut as conventions §7
/// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#7-source-values-as-arrays> cuts
/// contiguous values; in a compressed file, each one chunk, the file's `length` bytes (§2.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/fits.md#21-a-compressed-file>).
fn arrays(ir: &Ir, h: &J, row: Option<&J>, threshold: usize, stream: &J) -> Result<Vec<Node>, String> {
    let mut out = cut_arrays(ir, h, row, threshold)?;
    if let Some(length) = stream["length"].as_u64() {
        for a in &mut out {
            let (start, bytes) = a.element.map_or((0, 0), |e| (ir.start[e as usize], ir.len[e as usize]));
            a.within = Some((start, bytes));
            a.grid = a.shape.iter().map(|&s| s.max(1)).collect();
            a.chunks = if bytes > 0 {
                vec![(vec![0; a.shape.len()], Chunk::Ranges(vec![Part::Src(0, length)]))]
            } else {
                Vec::new()
            };
        }
    }
    Ok(out)
}

fn cut_arrays(ir: &Ir, h: &J, row: Option<&J>, threshold: usize) -> Result<Vec<Node>, String> {
    let index = h["index"].as_u64().unwrap_or(0);
    let mut out = Vec::new();
    match h["kind"].as_str() {
        Some("image") => {
            let shape: Vec<u64> = h["naxis"].as_array().unwrap_or(&Vec::new()).iter().rev().map(|v| v.as_u64().unwrap_or(0)).collect();
            let dims: Vec<String> = (1..=shape.len()).rev().map(|n| format!("axis{n}")).collect();
            let item = h["bitpix"].as_i64().unwrap_or(8).unsigned_abs() / 8;
            let element = h["elements"]["data"].as_u64().map(|v| v as u32);
            let offset = element.map_or(0, |e| ir.start[e as usize]);
            let (grid, chunks) = grid_chunks(offset, &shape, item, CHUNK_BYTES, threshold).ok_or("internal: the data cannot be cut into chunks")?;
            out.push(Node { path: index.to_string(), data_type: data_type(&h["bitpix"]), shape, dims, element, grid, chunks, within: None, row: None });
        }
        Some("table") => {
            let t = &h["table"];
            let (rows, row_bytes, heap) = (t["rows"].as_u64().unwrap_or(0), t["row_bytes"].as_u64().unwrap_or(0), t["heap"].as_u64().unwrap_or(0));
            let element = h["elements"]["rows"].as_u64().map(|v| v as u32);
            let offset = element.map_or(0, |e| ir.start[e as usize]);
            let path = format!("{index}/rows");
            let row = row.cloned();
            // a struct's element is a row; bytes' a byte of a row
            let (shape, item, dims, data_type): (Vec<u64>, u64, Vec<String>, &str) = match row {
                Some(_) => (vec![rows], row_bytes, vec!["row".into()], "struct"),
                None => (vec![rows, row_bytes], 1, vec!["row".into(), "byte".into()], "uint8"),
            };
            let (grid, chunks) = grid_chunks(offset, &shape, item, CHUNK_BYTES, threshold).ok_or("internal: the rows cannot be cut into chunks")?;
            out.push(Node { path, data_type: data_type.into(), shape, dims, element, grid, chunks, within: None, row });
            if heap > 0 {
                let e = h["elements"]["heap"].as_u64().ok_or("internal: a heap without an element")? as u32;
                let (size, chunks) = blob_chunks(ir.start[e as usize], heap);
                out.push(Node { path: format!("{index}/heap"), data_type: "uint8".into(), shape: vec![heap], dims: vec!["byte".into()], element: Some(e), grid: vec![size], chunks, within: None, row: None });
            }
        }
        _ => {}
    }
    Ok(out)
}

/// The source's ranges of the edge chunks the projection copies.
pub fn reads(ir: &Ir, description: &J, chunking: &Chunking) -> Result<Vec<(u64, u64)>, String> {
    let threshold = chunking.copy_threshold;
    let mut out = Vec::new();
    for h in description["hdus"].as_array().into_iter().flatten() {
        if !h["compression"].is_null() {
            continue;
        }
        let cards = cards_of(ir, h)?;
        let row = row_of(h, &cards).and_then(Result::ok);
        for a in arrays(ir, h, row.as_ref(), threshold, &description["stream"])? {
            out.extend(contiguous::copy_reads(&a.chunks));
        }
    }
    Ok(out)
}

pub fn project(ir: &Ir, description: &J, url: &str, copied: &Copied, chunking: &Chunking) -> Result<Out, String> {
    let threshold = chunking.copy_threshold;
    let hdus = description["hdus"].as_array().cloned().unwrap_or_default();
    let stream = &description["stream"];
    let mut out = Out::new(url);
    // the root: the table of HDUs and the primary header's cards (§5.1
    // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/fits.md#51-the-root>)
    let mut table = Vec::with_capacity(hdus.len());
    let mut kinds: Map<String, J> = Map::new();
    let (mut cards_total, mut tables) = (0u64, 0u64);
    let mut docs: Vec<(String, J)> = Vec::new();
    let mut all_arrays = Vec::new();
    let mut tiled: Vec<(J, J)> = Vec::new();
    let mut primary_cards = J::Null;
    for h in &hdus {
        let cards = cards_of(ir, h)?;
        cards_total += cards.len() as u64;
        let kind = h["kind"].as_str().unwrap_or("empty");
        let n = kinds.get(kind).and_then(|v| v.as_u64()).unwrap_or(0);
        kinds.insert(kind.to_string(), json!(n + 1));
        if kind == "table" {
            tables += 1;
        }
        if h["index"] == 0 {
            let cj = cards_json(&cards);
            primary_cards = if json_size(&cj) <= CARDS_BUDGET { cj } else { J::Null };
        }
        table.push(json!({
            "index": h["index"], "type": h["type"], "kind": kind, "name": h["name"], "version": h["version"],
            "bitpix": h["bitpix"], "naxis": h["naxis"], "pcount": h["pcount"], "gcount": h["gcount"],
            "offset": h["offset"], "header_bytes": h["header_bytes"], "data_offset": h["data_offset"], "data_bytes": h["data_bytes"],
            "compression": compression_json(&h["compression"]),
        }));
        let row = row_of(h, &cards);
        let own = own_json(h, &cards, row.as_ref());
        let index = h["index"].as_u64().unwrap_or(0);
        if !h["compression"].is_null() {
            tiled.push((h.clone(), own));
            continue;
        }
        let node_arrays = arrays(ir, h, row.and_then(Result::ok).as_ref(), threshold, stream)?;
        if kind == "image" {
            // the array is the node itself: its document gets the declaration
        } else {
            docs.push((format!("{index}/zarr.json"), declared_group(Map::new(), PROFILE, None, Some(own.clone()))));
        }
        all_arrays.push((kind == "image", own, node_arrays));
    }
    let root_own = json!({"hdus": table, "cards": primary_cards, "stream": match stream.is_null() {
        true => J::Null,
        false => json!({"method": stream["method"], "size": stream["size"]}),
    }});
    out.json("zarr.json", &declared_group(Map::new(), PROFILE, Some(url), Some(root_own)));
    for (key, doc) in docs {
        out.json(&key, &doc);
    }
    let mut summary_arrays = Vec::new();
    let mut count = 0usize;
    // one zero data source for every array's padding
    let mut zeros = contiguous::Zeros::of(all_arrays.iter().flat_map(|(_, _, arrays)| arrays.iter().flat_map(|a| &a.chunks)));
    let mut refs = Refs::new(ir, out);
    for (is_image, own, node_arrays) in all_arrays {
        for node in node_arrays {
            let mut doc = node_doc(&node, stream);
            let Node { path, data_type, shape, element, grid, chunks, .. } = node;
            if is_image {
                declare_array(&mut doc, PROFILE, own.clone());
            }
            refs.out.json(&format!("{path}/zarr.json"), &doc);
            summary_arrays.push(json!({"path": path, "shape": shape, "dataType": data_type, "chunkShape": grid}));
            let (start, length, form) = match element {
                Some(e) => (ir.start[e as usize], ir.len[e as usize], ir.form_id(e).ok_or("internal: the data has no form")?),
                None => (0, 0, 0),
            };
            count += chunks.len();
            contiguous::emit(&mut refs, chunks, (start, length, form), copied, &[&format!("{path}/c/")], |c| c, &mut zeros)?;
        }
    }
    // the tile-compressed images (§3.3
    // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/fits.md#33-tile-compressed-images>):
    // the array's chunks are a literal prefix of the tiles' records, then the heap element's
    // ranges; the `refkv.fits_tiles` codec decodes them
    let heaps = if tiled.is_empty() { HashMap::new() } else { heap_chunks(ir) };
    for (h, own) in tiled {
        let Tiled { path, mut doc, chunks } = tiled_array(ir, &h, &heaps)?;
        declare_array(&mut doc, PROFILE, own);
        refs.out.json(&format!("{path}/zarr.json"), &doc);
        summary_arrays.push(json!({"path": path, "shape": doc["shape"], "dataType": doc["data_type"],
                                   "chunkShape": doc["chunk_grid"]["configuration"]["chunk_shape"], "compression": h["compression"]["type"]}));
        for (coords, e, prefix) in chunks {
            let (start, length, form) = (ir.start[e as usize], ir.len[e as usize], ir.form_id(e).ok_or("internal: a heap chunk has no form")?);
            let mut parts = refs.parts(start, length, form)?;
            parts.insert(0, Part::Lit(prefix));
            count += 1;
            refs.chunk(key(&format!("{path}/c/"), coords), start, length, form, Some(parts))?;
        }
    }
    let mut out = refs.out;
    out.summary = json!({"hdus": hdus.len(), "kinds": kinds, "arrays": summary_arrays, "chunks": count, "cards": cards_total, "tables": tables, "stream": stream["method"]});
    Ok(out)
}

/// An array's `zarr.json`, but for its declaration (§3.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/fits.md#31-the-array>, §4
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/fits.md#4-tables>): its values
/// big-endian, a table's rows as their struct where they are one, and in a compressed file the
/// codecs that decode the file and skip to the array.
fn node_doc(node: &Node, stream: &J) -> J {
    let item = node.row.as_ref().map_or(item_size(&node.data_type), |r| if r["ordered"] == true { 2 } else { 1 });
    let mut codecs = vec![bytes_codec(item, "big")];
    if let Some((length, keep)) = node.within {
        codecs.push(json!({"name": "refkv.skip", "configuration": {"length": length, "keep": keep}}));
        codecs.push(match stream["method"].as_str() {
            Some("gzip") => json!({"name": "gzip", "configuration": {"level": 1}}),
            _ => json!({"name": "numcodecs.bz2", "configuration": {"level": 1}}),
        });
    }
    let dim_refs: Vec<&str> = node.dims.iter().map(|s| s.as_str()).collect();
    let mut doc = array_json(&node.shape, &node.data_type, json!(node.grid), J::Array(codecs), &dim_refs);
    if let Some(r) = &node.row {
        doc["data_type"] = r["data_type"].clone();
        doc["fill_value"] = r["fill_value"].clone();
    }
    doc
}

/// A tile-compressed image (§3.3
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/fits.md#33-tile-compressed-images>): its
/// path, its `zarr.json` but for its declaration, and its chunks, each its coordinates, the heap
/// element that holds its tiles, and the literal prefix of their records the `refkv.fits_tiles`
/// codec reads before them. A chunk that holds no tile has no element, and is the fill value.
struct Tiled {
    path: String,
    doc: J,
    chunks: Vec<(Vec<u64>, u32, Vec<u8>)>,
}

fn tiled_array(ir: &Ir, h: &J, heaps: &HashMap<(u32, u64), u32>) -> Result<Tiled, String> {
    let c = &h["compression"];
    let path = h["index"].as_u64().unwrap_or(0).to_string();
    let ints = |v: &J| -> Vec<u64> { v.as_array().into_iter().flatten().map(|v| v.as_u64().unwrap_or(0)).collect() };
    let (shape, tile) = (ints(&c["shape"]), ints(&c["tile"]));
    let dims: Vec<String> = (1..=shape.len()).rev().map(|n| format!("axis{n}")).collect();
    let dim_refs: Vec<&str> = dims.iter().map(|s| s.as_str()).collect();
    let data_type = data_type(&c["zbitpix"]);
    let item = c["zbitpix"].as_i64().unwrap_or(8).unsigned_abs() / 8;
    let (chunk_shape, _, cut) = tile_cut(&shape, &tile, item);
    let node = h["elements"]["header"].as_u64().map(|e| ir.parent[e as usize]).ok_or("internal: an HDU without a header")?;
    let config = json!({
        "compression": c["type"], "parameters": c["parameters"], "zbitpix": c["zbitpix"],
        "shape": c["shape"], "tile_shape": c["tile"], "quantize": c["quantize"],
        "dither_seed": c["dither_seed"].as_i64().unwrap_or(0),
    });
    let codecs = json!([{"name": "refkv.fits_tiles", "configuration": config}]);
    let doc = array_json(&shape, &data_type, json!(chunk_shape), codecs, &dim_refs);
    let table = h["elements"]["table"].as_u64().and_then(|t| ir.value_bytes(t as u32)).unwrap_or(&[]);
    let records = tile_records(c, table)?;
    let mut chunks = Vec::new();
    for (j, ch) in cut.into_iter().enumerate() {
        let Some(&e) = heaps.get(&(node, j as u64)) else { continue };
        let (first, n) = (ch.first as usize, ch.count as usize);
        let tiles = records.get(first..first + n).ok_or("internal: a chunk past the tile table")?;
        chunks.push((ch.coords, e, prefix(first as u64, tiles)));
    }
    Ok(Tiled { path, doc, chunks })
}

/// The cards of HDU `index` of a FITS file as the convention writes them in its source metadata
/// (§5.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/fits.md#53-cards>): each
/// `[keyword, value, comment]`, whatever their JSON's size (the source metadata leaves them out
/// past its budget).
pub fn hdu_cards(ir: &Ir, description: &J, index: u64) -> Result<J, String> {
    Ok(cards_json(&cards_of(ir, hdu(description, index)?)?))
}

/// HDU `index` of the description, which lists the HDUs in the file's order, each at its index.
fn hdu(description: &J, index: u64) -> Result<&J, String> {
    let h = usize::try_from(index).ok().and_then(|i| description["hdus"].get(i)).ok_or_else(|| format!("no HDU {index}"))?;
    if h["index"] != index {
        return Err(format!("internal: the description's HDU {index} is not at its index"));
    }
    Ok(h)
}

/// The elements of a FITS file's tile-compressed images' heaps, by their HDU's node and chunk
/// (`hdu_arrays`): built once for a file, for every HDU of it.
pub struct Heaps(HashMap<(u32, u64), u32>);

impl Heaps {
    pub fn of(ir: &Ir) -> Heaps {
        Heaps(heap_chunks(ir))
    }
}

/// A part of a chunk of an HDU's array (`hdu_arrays`): bytes of the file, literal bytes, zeros
/// that pad an edge chunk, or bytes of the file to be copied (an edge chunk whose padded reference
/// would not fit in a payload).
#[derive(Debug, PartialEq)]
pub enum Piece {
    Src(u64, u64),
    Lit(Vec<u8>),
    Zeros(u64),
    Copy(u64, u64),
}

/// An array of an HDU as the convention writes it (`hdu_arrays`): its path under the HDU's node
/// (`""` for the node itself, an image; `rows` and `heap` for a table), its `zarr.json` but for
/// its declaration, and its chunks by their coordinates.
pub struct HduArray {
    pub path: String,
    pub doc: J,
    pub chunks: Vec<(Vec<u64>, Vec<Piece>)>,
}

/// The arrays of HDU `index` of a FITS file, from its IR and the parser's description, as the
/// projection writes them but for their declarations (the routine of
/// design/proposals/profile-plugins.md "The routines" for FITS): an image's, a table's rows and
/// heap, a tile-compressed image's. None for an HDU of no data. `heaps` is the file's (`Heaps::of`).
pub fn hdu_arrays(ir: &Ir, description: &J, index: u64, heaps: &Heaps, chunking: &Chunking) -> Result<Vec<HduArray>, String> {
    let h = hdu(description, index)?;
    let under = |path: &str| path.strip_prefix(&index.to_string()).unwrap_or(path).trim_start_matches('/').to_string();
    if !h["compression"].is_null() {
        let Tiled { path, doc, chunks } = tiled_array(ir, h, &heaps.0)?;
        let mut refs = Refs::new(ir, Out::new(""));
        let mut out = Vec::with_capacity(chunks.len());
        for (coords, e, prefix) in chunks {
            let (start, length, form) = (ir.start[e as usize], ir.len[e as usize], ir.form_id(e).ok_or("internal: a heap chunk has no form")?);
            let mut pieces = vec![Piece::Lit(prefix)];
            for part in refs.parts(start, length, form)? {
                pieces.push(match part {
                    Part::Src(o, n) => Piece::Src(o, n),
                    Part::Lit(b) => Piece::Lit(b),
                    _ => return Err("internal: a heap chunk's recipe names no range of the file".into()),
                });
            }
            out.push((coords, pieces));
        }
        return Ok(vec![HduArray { path: under(&path), doc, chunks: out }]);
    }
    let cards = cards_of(ir, h)?;
    let mut out = Vec::new();
    let row = row_of(h, &cards).and_then(Result::ok);
    for node in arrays(ir, h, row.as_ref(), chunking.copy_threshold, &description["stream"])? {
        let doc = node_doc(&node, &description["stream"]);
        let chunks = node.chunks.into_iter().map(|(coords, chunk)| {
            let pieces = match chunk {
                Chunk::Ranges(parts) => {
                    let pad = contiguous::zero_tail(&parts);
                    let mut pieces: Vec<Piece> = parts.into_iter().take(if pad.is_some() { 1 } else { usize::MAX }).map(|p| match p {
                        Part::Src(o, n) => Ok(Piece::Src(o, n)),
                        Part::Lit(b) => Ok(Piece::Lit(b)),
                        _ => Err("internal: a chunk of no range of the file".to_string()),
                    }).collect::<Result<_, _>>()?;
                    pieces.extend(pad.map(Piece::Zeros));
                    pieces
                }
                Chunk::Copy(o, n, pad) => {
                    let mut pieces = vec![Piece::Copy(o, n)];
                    if pad > 0 {
                        pieces.push(Piece::Zeros(pad));
                    }
                    pieces
                }
                Chunk::Bytes(b) => vec![Piece::Lit(b)],
            };
            Ok((coords, pieces))
        }).collect::<Result<Vec<_>, String>>()?;
        out.push(HduArray { path: under(&node.path), doc, chunks });
    }
    Ok(out)
}

/// The root table's `compression` member of a tile-compressed image (§5.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/fits.md#51-the-root>): null for
/// any other HDU.
fn compression_json(c: &J) -> J {
    if c.is_null() {
        return J::Null;
    }
    json!({"type": c["type"], "parameters": c["parameters"], "zbitpix": c["zbitpix"], "tile": c["tile"],
           "quantize": c["quantize"], "dither_seed": c["dither_seed"], "zblank": c["zblank"], "tiles": c["tiles"]})
}

/// Every tile's record for the chunks' prefixes (§3.3
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/fits.md#33-tile-compressed-images>),
/// from the main table's rows: its
/// stream's length and column, its quantization.
fn tile_records(c: &J, table: &[u8]) -> Result<Vec<TileRecord>, String> {
    let cols = &c["columns"];
    let row_bytes = c["row_bytes"].as_u64().unwrap_or(0) as usize;
    let rows = c["rows"].as_u64().unwrap_or(0) as usize;
    if table.len() < rows * row_bytes {
        return Err("internal: the tile table is not held".into());
    }
    let descriptor = |col: &J, r: usize| -> (u64, u64) {
        let Some(at) = col["offset"].as_u64() else { return (0, 0) };
        let o = r * row_bytes + at as usize;
        if col["q"].as_bool().unwrap_or(false) {
            (i64::be_at(table, o) as u64, i64::be_at(table, o + 8) as u64)
        } else {
            (i32::be_at(table, o) as u64, i32::be_at(table, o + 4) as u64)
        }
    };
    let f64_at = |col: &J, r: usize| -> Option<f64> {
        let at = col.as_u64()? as usize;
        let o = r * row_bytes + at;
        Some(f64::be_at(table, o))
    };
    let scaled = cols["ZSCALE"].is_u64() && cols["ZZERO"].is_u64();
    let zblank_keyword = c["zblank"].as_i64();
    let mut out = Vec::with_capacity(rows);
    for r in 0..rows {
        let (n, _) = descriptor(&cols["COMPRESSED_DATA"], r);
        let (gn, _) = descriptor(&cols["GZIP_COMPRESSED_DATA"], r);
        let (length, kind) = if n != 0 { (n, 0) } else { (gn, 1) };
        let zblank_column = cols["ZBLANK"].as_u64().map(|at| {
            let o = r * row_bytes + at as usize;
            i32::be_at(table, o)
        });
        let (zblank, has_blank) = match (zblank_column, zblank_keyword) {
            (Some(b), _) => (b, true),
            (None, Some(b)) => (b as i32, true),
            _ => (0, false),
        };
        out.push(TileRecord {
            length: length as u32,
            kind,
            zblank,
            flags: (scaled as u32) | ((has_blank as u32) << 1),
            zscale: if scaled { f64_at(&cols["ZSCALE"], r).unwrap_or(0.0) } else { 0.0 },
            zzero: if scaled { f64_at(&cols["ZZERO"], r).unwrap_or(0.0) } else { 0.0 },
        });
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 80-byte cards from `KEYWORD= value` texts.
    fn records(cards: &[&str]) -> Vec<u8> {
        cards.iter().flat_map(|c| format!("{c:<80}").into_bytes()).collect()
    }

    /// A table's `null` member is TNULLn as the card holds it: an ASCII table's string, a
    /// binary table's integer, exact whatever its magnitude (spec/zarrify/fits.md §5.2
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/fits.md#52-an-hdus-node>).
    #[test]
    fn a_column_s_null_is_tnull_as_the_card_holds_it() {
        let raw = records(&[
            "TNULL1  = 'N/A     '",
            "TNULL2  =                  -99",
            "TNULL3  = -9223372036854775808",
            "TNULL4  = -170141183460469231731687303715884105728",
            "TNULL5  = -1234567890123456789012345678901234567890",
        ]);
        let h = json!({"index": 1, "kind": "table", "table": {"tform": ["I8", "J", "K", "K", "K"]}});
        let own = own_json(&h, &parse_cards(&raw, 5), None);
        let columns = own["columns"].as_array().unwrap();
        assert_eq!(columns[0]["null"], json!("N/A"));
        assert_eq!(columns[1]["null"], json!(-99));
        assert_eq!(columns[2]["null"], json!(i64::MIN));
        // i128::MIN, whose magnitude no i128 holds
        assert_eq!(crate::out::to_text(&columns[3]["null"]), "-170141183460469231731687303715884105728");
        // past i128, exactly
        assert_eq!(crate::out::to_text(&columns[4]["null"]), "-1234567890123456789012345678901234567890");
    }

    /// A card's integer past i128 (40 digits; a value field holds 70) is exact, in the cards and in
    /// an image's `bscale`, `bzero` and `blank` (spec/zarrify/fits.md §5.3
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/fits.md#53-cards>).
    #[test]
    fn an_integer_card_past_i128_is_exact() {
        let digits = "1234567890123456789012345678901234567890";
        let cards = parse_cards(&records(&[&format!("BLANK   = {digits}"), &format!("HUGE    = -000{digits}")]), 2);
        assert_eq!(crate::out::to_text(&cards_json(&cards)), format!(r#"[["BLANK",{digits},null],["HUGE",-{digits},null]]"#));
        assert_eq!(crate::out::to_text(&number(&cards, "BLANK")), digits);
    }
}
