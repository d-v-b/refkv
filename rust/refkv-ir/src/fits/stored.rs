//! The stored arrays of a FITS file (spec/stored-array.md §7
//! <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>): for every HDU
//! that holds data, descriptions keyed by its header. An image is one unit, its data; a table is
//! its rows, a record each, and its heap, each one unit; a tile-compressed image is its pixels, a
//! tile a unit in the chain of its compression, and its table's rows and heap. Built from the
//! facts and the IR the parser made, before the IR is finished: it reads no byte the parser did
//! not.

use super::{card, field_of, parse_card, parse_cards, str_of, tile_at, Card, Value, CARD};
use crate::array::{AxisUnits, Chain, ChunkMap, Codec, Edge, Fill, Grid, Param, Place, Row, Scalar, StoredArray, UnitParameter};
use crate::fed::Word;
use crate::ir::{Ir, Res};
use serde_json::{json, Value as J};
use std::collections::BTreeMap;

/// The descriptions, their chains and their rows, as they are built.
#[derive(Default)]
struct Built {
    arrays: Vec<StoredArray>,
    chains: Vec<Chain>,
    chain_index: BTreeMap<String, u32>,
    map: ChunkMap,
}

fn whole_place(element: u32) -> Place {
    Place::Element { element, member: None, range: None }
}

fn codec<const N: usize>(name: &str, parameters: [(&str, Option<Param>); N]) -> Codec {
    Codec { name: name.into(), parameters: parameters.into_iter().filter_map(|(k, v)| Some((k.to_string(), v?))).collect() }
}

fn size(v: &J) -> u64 {
    v.as_u64().unwrap_or(0)
}

fn element(h: &J, name: &str) -> Option<u32> {
    h["elements"][name].as_u64().map(|v| v as u32)
}

/// Whether a row of the binary table whose facts are `t` is its columns' fields: every TFORMn one
/// the parser reads, and their bytes NAXIS1 in all. Of any other table, readers do not agree on
/// where a row is: the standard's rows are NAXIS1 bytes each, and astropy's the fields' bytes.
fn fields_fill_rows(t: &J) -> bool {
    let mut bytes = 0u64;
    for form in t["tform"].as_array().into_iter().flatten() {
        match field_of(form.as_str().unwrap_or("")) {
            Ok(Some(f)) => bytes = bytes.saturating_add(f.bytes.saturating_mul(f.count)),
            Ok(None) => {}
            Err(_) => return false,
        }
    }
    bytes == size(&t["row_bytes"])
}

/// The place of the first card of `keyword` in the header `header`, whose bytes are `raw` and
/// whose cards before END are `count`: the card's 80 bytes.
fn card_place(header: u32, raw: &[u8], count: usize, keyword: &str) -> Option<Place> {
    let k = (0..count).find(|&k| {
        let rec = &raw[k * CARD..(k + 1) * CARD];
        // a card's keyword is its first 8 bytes, but a HIERARCH card's
        (rec[..8].trim_ascii_end() == keyword.as_bytes() || rec.starts_with(b"HIERARCH")) && parse_card(rec).keyword == keyword
    })?;
    Some(Place::Element { element: header, member: None, range: Some(((k * CARD) as u64, CARD as u64)) })
}

impl Built {
    /// The position of `chain` in the chain table.
    fn chain(&mut self, chain: Chain) -> u32 {
        let key = J::Array(chain.iter().map(Codec::to_json).collect()).to_string();
        *self.chain_index.entry(key).or_insert_with(|| {
            self.chains.push(chain);
            (self.chains.len() - 1) as u32
        })
    }

    /// An array the source stores as it is, in one unit: array `index` of the header `key`, of
    /// `shape`, its elements of `bits` bits (None: of no bytes, which no grid holds), its bytes
    /// `unit`, an element and the range of it that is the array's where that is not all of it
    /// (None: an array of no bytes).
    fn whole(&mut self, key: u32, index: u64, shape: Vec<u64>, bits: Option<u64>, unit: Option<(u32, Option<(u64, u64)>)>) -> Res<()> {
        let array = self.arrays.len() as u32;
        let rank = shape.len();
        // the source lists its axes fastest first, NAXIS1 to NAXISn
        let axes = (rank >= 2).then(|| (0..rank as u32).rev().collect());
        let grid = bits.map(|_| Grid {
            axes: shape.iter().map(|&n| if n == 0 { AxisUnits::List(Vec::new()) } else { AxisUnits::Each(n) }).collect(),
            edge: Edge::Short,
        });
        let chains = if grid.is_some() { vec![self.chain(Vec::new())] } else { Vec::new() };
        let described = grid.is_some();
        self.arrays.push(StoredArray {
            element: key,
            index: Some(index),
            declarations: vec![whole_place(key)],
            shape: Some(shape),
            axes,
            item_bits: bits,
            pad_from: None,
            grid,
            chains,
            fill: Fill::Undefined,
            unit_parameters: Vec::new(),
        });
        if let (Some((element, range)), true) = (unit, described) {
            self.map.push(Row { array, coords: vec![0; rank], element: Some(element), range, ..Row::default() })?;
        }
        Ok(())
    }

    /// A table's rows, array `index` of the header `key`: a record a row, in the element `rows`;
    /// `fields`, whether a row's bytes are its columns' fields.
    fn rows(&mut self, key: u32, index: u64, t: &J, rows: Option<u32>, fields: bool) -> Res<()> {
        let (n1, n2) = (size(&t["row_bytes"]), size(&t["rows"]));
        let unit = rows.filter(|_| n1 > 0 && n2 > 0).map(|e| (e, None));
        self.whole(key, index, vec![n2], (n1 > 0 && fields).then_some(8 * n1), unit)
    }

    /// The pixels of the tile-compressed image of the HDU `h`, array 0 of its header `key`, whose
    /// bytes are `raw`: `records` cards before END, which are `cards` with CONTINUE cards joined;
    /// `fields`, whether a row of its table is its columns' fields.
    fn tiled(&mut self, ir: &Ir, h: &J, key: u32, (cards, raw, records): (&[Card], &[u8], usize), fields: bool) -> Res<()> {
        let c = &h["compression"];
        let cols = &c["columns"];
        let list = |v: &J| -> Vec<u64> { v.as_array().into_iter().flatten().map(size).collect() };
        let (shape, tile) = (list(&c["shape"]), list(&c["tile"]));
        let rank = shape.len();
        let zbitpix = c["zbitpix"].as_i64().unwrap_or(8);
        let (float, item) = (zbitpix < 0, zbitpix.unsigned_abs() / 8);
        let scaled = cols["ZSCALE"].is_u64() && cols["ZZERO"].is_u64();
        let quantized = float && scaled;
        let blank_column = cols["ZBLANK"].as_u64();
        let has = |k: &str| card(cards, k).is_some();
        let place = |k: &str| card_place(key, raw, records, k);
        // the blank value of a quantized tile's integers: the row's, the header's ZBLANK, or its
        // BLANK, as astropy reads it
        let blank = if blank_column.is_some() {
            Ok(None)
        } else if has("ZBLANK") {
            Ok(place("ZBLANK"))
        } else {
            match card(cards, "BLANK").map(|c| &c.value) {
                None => Ok(None),
                Some(Value::Int(_)) => Ok(place("BLANK")),
                // a BLANK that is no integer is one readers take each their own way
                Some(_) => Err(()),
            }
        };
        // what readers do not agree on, and the IR does not model: quantization that an integer
        // image's columns or a float image's cards alone give; a blank value of pixels that are
        // not quantized; and rows that are not their columns' bytes
        let agreed = if quantized {
            blank.is_ok()
        } else {
            !scaled && !(float && (has("ZSCALE") || has("ZZERO") || has("ZBLANK") || has("BLANK"))) && blank_column.is_none()
        };
        let modeled = agreed && fields;
        let array = self.arrays.len() as u32;
        let mut described = StoredArray {
            element: key,
            index: Some(0),
            declarations: vec![whole_place(key)],
            shape: Some(shape.clone()),
            axes: (rank >= 2).then(|| (0..rank as u32).rev().collect()),
            item_bits: Some(8 * item),
            pad_from: None,
            grid: None,
            chains: Vec::new(),
            fill: Fill::Undefined,
            unit_parameters: Vec::new(),
        };
        if !modeled {
            self.arrays.push(described);
            return Ok(());
        }
        // the ZVALn cards the parser read BLOCKSIZE and BYTEPIX from: the last of each name
        let (mut block, mut bytes) = (None, None);
        for k in 1..=9 {
            let value = || place(&format!("ZVAL{k}")).map(Param::Source);
            match card(cards, &format!("ZNAME{k}")).map(str_of).as_deref() {
                Some("BLOCKSIZE") => block = value(),
                Some("BYTEPIX") => bytes = value(),
                _ => {}
            }
        }
        let bytepix = size(&c["parameters"]["BYTEPIX"]);
        let gzip = || codec("gzip", []);
        // the steps after quantization; None where the list's entry cannot state what the stream
        // decodes to for this image: Rice of integers of another width than the elements'
        let compressor: Option<Chain> = match c["type"].as_str() {
            Some("RICE_1") => {
                let fits = if quantized { bytepix == 4 } else { !float && bytepix == item };
                fits.then(|| vec![codec("fits_rice", [("block_size", block), ("byte_pix", bytes)])])
            }
            Some("GZIP_1") => Some(vec![gzip()]),
            Some("GZIP_2") => {
                let width = if quantized { 4 } else { item };
                Some(vec![codec("shuffle", [("element_size", Some(Param::Value(json!(width))))]), gzip()])
            }
            Some("NOCOMPRESS") => Some(Vec::new()),
            _ => None,
        };
        let dithered = quantized && c["quantize"].as_str().is_some_and(|q| q != "NO_DITHER");
        let (first, columns) = match compressor {
            Some(rest) if quantized => {
                let method = place("ZQUANTIZ").map(Param::Source);
                let quantize = codec("fits_quantize", [("method", method), ("zblank", blank.ok().flatten().map(Param::Source))]);
                let mut names = vec!["scale", "zero"];
                names.extend(dithered.then_some("seed"));
                names.extend(blank_column.map(|_| "zblank"));
                (std::iter::once(quantize).chain(rest).collect(), names)
            }
            Some(rest) => (rest, Vec::new()),
            None => {
                let id = card(cards, "ZCMPTYPE").map(str_of).unwrap_or_default();
                (vec![codec("source", [("format", Some(Param::Value(json!("fits")))), ("id", Some(Param::Value(json!(id))))])], Vec::new())
            }
        };
        // a tile in GZIP_COMPRESSED_DATA is a gzip stream of its elements, whatever the image's
        // compression
        let fallback = vec![gzip()];
        let second = cols["GZIP_COMPRESSED_DATA"].is_object() && first != fallback;
        described.chains = vec![self.chain(first)];
        if second {
            described.chains.push(self.chain(fallback));
        }
        described.grid = Some(Grid { axes: tile.iter().map(|&n| AxisUnits::Each(n.max(1))).collect(), edge: Edge::Short });
        described.unit_parameters = columns.iter().map(|n| UnitParameter { chain: 0, codec: 0, name: n.to_string() }).collect();
        self.arrays.push(described);
        // the tiles, from the table's rows, each in its chunk of the heap
        let (rows, row_bytes) = (size(&c["rows"]), size(&c["row_bytes"]));
        let Some(table) = element(h, "table").and_then(|t| ir.value_bytes(t)).filter(|t| t.len() as u64 >= rows * row_bytes) else {
            return if rows == 0 { Ok(()) } else { Err(format!("internal: HDU {}'s tile table is not held", h["index"])) };
        };
        let mut chunks: Vec<(u64, u64, u32)> = Vec::new();
        for ch in c["chunks"].as_array().into_iter().flatten() {
            if let Some(e) = ch["element"].as_u64() {
                chunks.push((size(&ch["first"]), size(&ch["count"]), e as u32));
            }
        }
        chunks.sort_unstable();
        let t = &h["table"];
        let heap = size(&h["data_offset"]) + size(&t["row_bytes"]) * size(&t["rows"]) + size(&t["gap"]);
        let descriptor = |col: &J, r: u64| -> (i64, i64) {
            let Some(at) = col["offset"].as_u64() else { return (0, 0) };
            let o = (r * row_bytes + at) as usize;
            if col["q"].as_bool().unwrap_or(false) {
                (i64::be_at(table, o), i64::be_at(table, o + 8))
            } else {
                (i32::be_at(table, o) as i64, i32::be_at(table, o + 4) as i64)
            }
        };
        let seed = c["dither_seed"].as_i64().unwrap_or(0);
        for r in 0..rows {
            let (n, o) = descriptor(&cols["COMPRESSED_DATA"], r);
            let (gn, go) = descriptor(&cols["GZIP_COMPRESSED_DATA"], r);
            let (n, o, gzipped) = if n != 0 { (n as u64, o as u64, false) } else if gn != 0 { (gn as u64, go as u64, true) } else { continue };
            // the chunk whose element holds the tile
            let k = chunks.partition_point(|&(first, _, _)| first <= r);
            let Some(&(_, _, e)) = k.checked_sub(1).and_then(|k| chunks.get(k)).filter(|&&(first, count, _)| r < first + count) else {
                return Err(format!("internal: HDU {}'s tile {r} is in no chunk of its heap", h["index"]));
            };
            let (start, length) = (ir.start[e as usize], ir.len[e as usize]);
            let at = (heap + o).checked_sub(start).filter(|at| at + n <= length);
            let Some(at) = at else {
                return Err(format!("internal: HDU {}'s tile {r} is outside its chunk of the heap", h["index"]));
            };
            // a tile that is all of its element is the element (spec/stored-array.md §4.2
            // <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#42-rows>): the
            // recipe of an element of one tile is the identity
            let range = (n != length).then_some((at, n));
            let mut params = Vec::new();
            if !gzipped {
                let field = |col: &J| (r * row_bytes + size(col)) as usize;
                for name in &columns {
                    let value = match *name {
                        "scale" => Scalar::Float64(f64::be_at(table, field(&cols["ZSCALE"]))),
                        "zero" => Scalar::Float64(f64::be_at(table, field(&cols["ZZERO"]))),
                        "seed" => Scalar::Int64(r as i64 + seed),
                        _ => Scalar::Int64(i32::be_at(table, field(&cols["ZBLANK"])) as i64),
                    };
                    params.push((format!("p.fits_quantize.{name}"), value));
                }
            }
            let chain = (gzipped && second).then_some(1);
            self.map.push(Row { array, coords: tile_at(&shape, &tile, r).0, element: Some(e), range, chain, params, ..Row::default() })?;
        }
        Ok(())
    }
}

/// The descriptions of the arrays of the HDUs whose facts are `hdus`, their chains and their rows.
pub(super) fn described(ir: &Ir, hdus: &[J]) -> Res<(Vec<StoredArray>, Vec<Chain>, ChunkMap)> {
    let mut b = Built::default();
    for h in hdus {
        // an HDU of no axes holds no array
        let Some(key) = h["declared_by"].as_u64().map(|v| v as u32) else { continue };
        let t = &h["table"];
        if t.is_null() {
            // an image: its values, NAXIS1 fastest; where the HDU's data is longer than they
            // are (PCOUNT or GCOUNT say so), they are its first bytes
            let shape: Vec<u64> = h["naxis"].as_array().into_iter().flatten().rev().map(size).collect();
            let bits = h["bitpix"].as_i64().unwrap_or(8).unsigned_abs();
            let bytes = shape.iter().fold((bits / 8) as u128, |a, &n| a.saturating_mul(n as u128));
            let unit = element(h, "data").filter(|&e| bytes > 0 && bytes <= ir.len[e as usize] as u128).map(|e| (e, (bytes as u64 != ir.len[e as usize]).then_some((0, bytes as u64))));
            b.whole(key, 0, shape, Some(bits), unit)?;
            continue;
        }
        let heap = size(&t["heap"]);
        if h["compression"].is_null() {
            if h["kind"] == "image" {
                return Err(format!("internal: HDU {}'s tile table was not read", h["index"]));
            }
            // a table: its rows, then its heap. An ASCII table's row is NAXIS1 characters, which
            // its fields need not fill
            b.rows(key, 0, t, element(h, "rows"), h["type"] == "table" || fields_fill_rows(t))?;
            b.whole(key, 1, vec![heap], Some(8), element(h, "heap").filter(|_| heap > 0).map(|e| (e, None)))?;
            continue;
        }
        // a tile-compressed image: its pixels, then its table's rows and heap. The heap is in
        // elements of tiles, so it is a range of its HDU
        let raw = ir.value_bytes(key).ok_or("internal: a header is not held")?;
        let records = (size(&h["cards"]) as usize).min(raw.len() / CARD);
        let fields = fields_fill_rows(t);
        b.tiled(ir, h, key, (&parse_cards(raw, records), raw, records), fields)?;
        b.rows(key, 1, t, element(h, "table"), fields)?;
        let node = ir.parent[key as usize];
        let at = size(&h["data_offset"]) + size(&t["row_bytes"]) * size(&t["rows"]) + size(&t["gap"]);
        let within = at.checked_sub(ir.start[node as usize]).filter(|_| heap > 0).map(|o| (node, Some((o, heap))));
        b.whole(key, 2, vec![heap], Some(8), within)?;
    }
    Ok((b.arrays, b.chains, b.map))
}
