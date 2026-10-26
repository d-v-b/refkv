//! The FITS projection (spec/zarrify/fits.md §2
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/fits.md#2-hdus-and-nodes>) from a
//! valid IR: the root group with the table of HDUs and the primary header's cards, and one
//! node per HDU at the path of its index: an array for an image (its chunks cut from the
//! `data` element), a group with the arrays `rows` and `heap` for a table, and an empty
//! group for an HDU without data, each declaring the convention with its own cards.

use super::{contiguous, key, Copied};
use crate::fed::Word;
use crate::fits::{card, parse_cards, Card, Value};
use crate::fits_tiles::{prefix, TileRecord};
use crate::ir::Ir;
use crate::out::{array_json, blob_chunks, bytes_codec, declare_array, declared_group, grid_chunks, int_json, item_size, json_size, Chunk, Out, Part};
use crate::refs::Refs;
use serde_json::{json, Map, Value as J};

const PROFILE: &str = "fits";
/// The most bytes of a chunk of an image or of a table's rows (§3.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/fits.md#31-the-array>).
const CHUNK_BYTES: u64 = 1 << 20;
/// The most bytes of JSON a node's cards may take (§5.3
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/fits.md#53-cards>).
const CARDS_BUDGET: usize = 65536;

/// A card's value as JSON (§5.3
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/fits.md#53-cards>): an integer exactly, as a string past 2^53 − 1; a real as
/// its binary64 value; a complex as `[re, im]`; no value as null.
fn value_json(v: &Value) -> J {
    match v {
        Value::None => J::Null,
        Value::Logical(b) => json!(b),
        Value::Int(n) => int_json(*n),
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
        Some(Value::Real(x, _)) if x.is_finite() => json!(x),
        _ => J::Null,
    }
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

/// The cards of HDU `h`, from its header element.
fn cards_of(ir: &Ir, h: &J) -> Result<Vec<Card>, String> {
    let id = h["elements"]["header"].as_u64().ok_or("internal: an HDU without a header")? as u32;
    let raw = ir.value_bytes(id).ok_or("internal: the header is not held")?;
    Ok(parse_cards(raw, h["cards"].as_u64().unwrap_or(0) as usize))
}

/// An HDU node's source metadata (§5.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/fits.md#52-an-hdus-node>): its index, its cards (null past the budget), an
/// image's scaling members, a table's columns.
fn own_json(h: &J, cards: &[Card]) -> J {
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
                    })
                })
                .collect();
            own.insert("columns".into(), J::Array(columns));
        }
        _ => {}
    }
    J::Object(own)
}

/// The arrays of HDU `h` with their chunks: (path, data type, shape, dimension names, element
/// id, chunk shape, chunks), cut as conventions §7
/// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#7-source-values-as-arrays> cuts
/// contiguous values.
fn arrays(ir: &Ir, h: &J, threshold: usize) -> Result<Vec<(String, String, Vec<u64>, Vec<String>, Option<u32>, Vec<u64>, Vec<(Vec<u64>, Chunk)>)>, String> {
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
            out.push((index.to_string(), h["data_type"].as_str().unwrap_or("uint8").to_string(), shape, dims, element, grid, chunks));
        }
        Some("table") => {
            let t = &h["table"];
            let (rows, row_bytes, heap) = (t["rows"].as_u64().unwrap_or(0), t["row_bytes"].as_u64().unwrap_or(0), t["heap"].as_u64().unwrap_or(0));
            let element = h["elements"]["rows"].as_u64().map(|v| v as u32);
            let offset = element.map_or(0, |e| ir.start[e as usize]);
            let shape = vec![rows, row_bytes];
            let (grid, chunks) = grid_chunks(offset, &shape, 1, CHUNK_BYTES, threshold).ok_or("internal: the rows cannot be cut into chunks")?;
            out.push((format!("{index}/rows"), "uint8".into(), shape, vec!["row".into(), "byte".into()], element, grid, chunks));
            if heap > 0 {
                let e = h["elements"]["heap"].as_u64().ok_or("internal: a heap without an element")? as u32;
                let (size, chunks) = blob_chunks(ir.start[e as usize], heap);
                out.push((format!("{index}/heap"), "uint8".into(), vec![heap], vec!["byte".into()], Some(e), vec![size], chunks));
            }
        }
        _ => {}
    }
    Ok(out)
}

/// The source's ranges of the edge chunks the projection copies.
pub fn reads(ir: &Ir, facts: &J) -> Result<Vec<(u64, u64)>, String> {
    let threshold = contiguous::threshold(facts);
    let mut out = Vec::new();
    for h in facts["hdus"].as_array().into_iter().flatten() {
        for (_, _, _, _, _, _, chunks) in arrays(ir, h, threshold)? {
            out.extend(contiguous::copy_reads(&chunks));
        }
    }
    Ok(out)
}

pub fn project(ir: &Ir, facts: &J, url: &str, copied: &Copied) -> Result<Out, String> {
    let threshold = contiguous::threshold(facts);
    let hdus = facts["hdus"].as_array().cloned().unwrap_or_default();
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
        let own = own_json(h, &cards);
        let index = h["index"].as_u64().unwrap_or(0);
        if !h["compression"].is_null() {
            tiled.push((h.clone(), own));
            continue;
        }
        let node_arrays = arrays(ir, h, threshold)?;
        if kind == "image" {
            // the array is the node itself: its document gets the declaration
        } else {
            docs.push((format!("{index}/zarr.json"), declared_group(Map::new(), PROFILE, None, Some(own.clone()))));
        }
        all_arrays.push((kind == "image", own, node_arrays));
    }
    let root_own = json!({"hdus": table, "cards": primary_cards});
    out.json("zarr.json", &declared_group(Map::new(), PROFILE, Some(url), Some(root_own)));
    for (key, doc) in docs {
        out.json(&key, &doc);
    }
    let mut summary_arrays = Vec::new();
    let mut count = 0usize;
    // one zero data source for every array's padding
    let mut zeros = contiguous::Zeros::of(all_arrays.iter().flat_map(|(_, _, arrays)| arrays.iter().flat_map(|a| &a.6)));
    let mut refs = Refs::new(ir, out);
    for (is_image, own, node_arrays) in all_arrays {
        for (path, data_type, shape, dims, element, grid, chunks) in node_arrays {
            let codecs = json!([bytes_codec(item_size(&data_type), "big")]);
            let dim_refs: Vec<&str> = dims.iter().map(|s| s.as_str()).collect();
            let mut doc = array_json(&shape, &data_type, json!(grid), codecs, &dim_refs);
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
    for (h, own) in tiled {
        let c = &h["compression"];
        let index = h["index"].as_u64().unwrap_or(0);
        let path = index.to_string();
        let shape: Vec<u64> = c["shape"].as_array().unwrap_or(&Vec::new()).iter().map(|v| v.as_u64().unwrap_or(0)).collect();
        let dims: Vec<String> = (1..=shape.len()).rev().map(|n| format!("axis{n}")).collect();
        let dim_refs: Vec<&str> = dims.iter().map(|s| s.as_str()).collect();
        let data_type = c["data_type"].as_str().unwrap_or("uint8").to_string();
        let config = json!({
            "compression": c["type"], "parameters": c["parameters"], "zbitpix": c["zbitpix"],
            "shape": c["shape"], "tile_shape": c["tile"], "quantize": c["quantize"],
            "dither_seed": c["dither_seed"].as_i64().unwrap_or(0),
        });
        let codecs = json!([{"name": "refkv.fits_tiles", "configuration": config}]);
        let mut doc = array_json(&shape, &data_type, c["chunk_shape"].clone(), codecs, &dim_refs);
        declare_array(&mut doc, PROFILE, own);
        refs.out.json(&format!("{path}/zarr.json"), &doc);
        summary_arrays.push(json!({"path": path, "shape": shape, "dataType": data_type, "chunkShape": c["chunk_shape"], "compression": c["type"]}));
        let table = h["elements"]["table"].as_u64().and_then(|t| ir.value_bytes(t as u32)).unwrap_or(&[]);
        let records = tile_records(c, table)?;
        for (piece, ch) in c["chunks"].as_array().into_iter().flatten().enumerate() {
            let Some(e) = ch["element"].as_u64() else { continue };
            let (first, n) = (ch["first"].as_u64().unwrap_or(0) as usize, ch["count"].as_u64().unwrap_or(0) as usize);
            let (start, length, form) = (ir.start[e as usize], ir.len[e as usize], ir.form_id(e as u32).ok_or("internal: a heap chunk has no form")?);
            let mut parts = refs.parts(start, length, form)?;
            parts.insert(0, Part::Lit(prefix(first as u64, &records[first..first + n])));
            let coords = ch["coords"].as_array().into_iter().flatten().map(super::n);
            count += 1;
            refs.chunk(key(&format!("{path}/c/"), coords), start, length, form, Some(parts), piece as u64)?;
        }
    }
    let mut out = refs.out;
    out.summary = json!({"hdus": hdus.len(), "kinds": kinds, "arrays": summary_arrays, "chunks": count, "cards": cards_total, "tables": tables});
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
    /// binary table's integer, the string of its digits past 2^53 − 1 (spec/zarrify/fits.md §5.2
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/fits.md#52-an-hdus-node>).
    #[test]
    fn a_column_s_null_is_tnull_as_the_card_holds_it() {
        let raw = records(&["TNULL1  = 'N/A     '", "TNULL2  =                  -99", "TNULL3  = -9223372036854775808"]);
        let h = json!({"index": 1, "kind": "table", "table": {"tform": ["I8", "J", "K"]}});
        let own = own_json(&h, &parse_cards(&raw, 3));
        let columns = own["columns"].as_array().unwrap();
        assert_eq!(columns[0]["null"], json!("N/A"));
        assert_eq!(columns[1]["null"], json!(-99));
        assert_eq!(columns[2]["null"], json!("-9223372036854775808"));
    }
}
