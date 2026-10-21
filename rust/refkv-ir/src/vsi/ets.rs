//! A tile file (`frame_t.ets`, spec/virtualize/vsi.md §3
//! <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/vsi.md#3-the-tile-files>): the
//! 64-byte SIS header, the 228-byte ETS header it points at (pixel type, samples, compression,
//! tile size, background color, pyramid flag, dimension sizes) and the tile directory, one
//! record per tile (its coordinates, offset and size). Its IR elements (§6.2
//! <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/vsi.md#62-the-ir-mirror>): the
//! headers and the directory as values, each tile as a data element.

use crate::ir::{GAP, Ir, NO_INDEX, Res};
use crate::rules::{self, Rules, num, text};
use serde_json::{Value as J, json};

pub const SIS_HEADER: u64 = 64;
pub const ETS_HEADER: u64 = 228;
pub const SIS_TYPE: &str = "{magic:ascii[4],header_size:<i4,version:<i4,coordinates:<i4,ets_offset:<i8,ets_size:<i4,reserved:<i4,tiles_offset:<i8,tiles:<i4,reserved2:<i4,size:<i8,reserved3:bytes[8]}";
pub const ETS_TYPE: &str = "{magic:ascii[4],version:<i4,pixel_type:<i4,channels:<i4,colorspace:<i4,compression:<i4,quality:<i4,tile_width:<i4,tile_height:<i4,tile_depth:<i4,pixel_info_hints:<i4[17],background:bytes[40],component_order:<i4,use_pyramid:<i4,reserved:bytes[28],dimensions:<i4,dimension_sizes:<i4[10]}";

impl Default for Ets {
    fn default() -> Self {
        Ets {
            version: 0, pixel_type: 0, channels: 0, colorspace: 0, compression: 0, quality: 0, tile_width: 0, tile_height: 0, tile_depth: 0,
            background: [0; 40], component_order: 0, use_pyramid: false, dimensions: 0, dimension_sizes: Vec::new(),
            data_type: String::new(), bytes: 0, codec: String::new(),
        }
    }
}

pub const RAW: i32 = 0;
pub const JPEG: i32 = 2;
pub const JPEG2000: i32 = 3;

fn i32_at(b: &[u8], o: usize) -> i32 {
    i32::from_le_bytes(b[o..o + 4].try_into().unwrap())
}
fn i64_at(b: &[u8], o: usize) -> i64 {
    i64::from_le_bytes(b[o..o + 8].try_into().unwrap())
}

/// The SIS header (the file's first 64 bytes).
#[derive(Clone, Debug, Default)]
pub struct Sis {
    pub header_size: i32,
    pub version: i32,
    /// the coordinates per tile (the directory's dimension count)
    pub coordinates: i32,
    pub ets_offset: i64,
    pub ets_size: i32,
    pub tiles_offset: i64,
    pub tiles: i32,
    pub size: i64,
}

impl Sis {
    /// A tile record's bytes.
    pub fn record(&self) -> u64 {
        20 + 4 * self.coordinates as u64
    }
}

/// Reads and checks the SIS header of the tile file `key` of `size` bytes.
pub fn sis(rules: &Rules, key: &str, head: &[u8], size: u64) -> Res<Sis> {
    let magic = String::from_utf8_lossy(&head[..3]).to_string();
    let s = Sis {
        header_size: i32_at(head, 4),
        version: i32_at(head, 8),
        coordinates: i32_at(head, 12),
        ets_offset: i64_at(head, 16),
        ets_size: i32_at(head, 24),
        tiles_offset: i64_at(head, 32),
        tiles: i32_at(head, 40),
        size: i64_at(head, 48),
    };
    rules.check(
        "sis",
        &mut rules::env([
            ("key", text(key)),
            ("magic", text(&magic)),
            ("header_size", num(s.header_size as f64)),
            ("coordinates", num(s.coordinates as f64)),
            ("ets_offset", num(s.ets_offset as f64)),
            ("ets_size", num(s.ets_size as f64)),
            ("tiles_offset", num(s.tiles_offset as f64)),
            ("tiles", num(s.tiles as f64)),
            ("size", num(size as f64)),
        ]),
    )?;
    Ok(s)
}

/// The ETS header.
#[derive(Clone, Debug)]
pub struct Ets {
    pub version: i32,
    pub pixel_type: i32,
    /// samples per pixel
    pub channels: i32,
    pub colorspace: i32,
    pub compression: i32,
    pub quality: i32,
    pub tile_width: i32,
    pub tile_height: i32,
    pub tile_depth: i32,
    pub background: [u8; 40],
    pub component_order: i32,
    pub use_pyramid: bool,
    /// the dimension count and the sizes of the first `dimensions` coordinates (x, y, then the
    /// others), as the header lists them
    pub dimensions: i32,
    pub dimension_sizes: Vec<i32>,
    pub data_type: String,
    pub bytes: u64,
    pub codec: String,
}

/// Reads and checks the ETS header of the tile file `key`, whose SIS header is `s`.
pub fn ets(rules: &Rules, key: &str, s: &Sis, h: &[u8]) -> Res<Ets> {
    let magic = String::from_utf8_lossy(&h[..3]).to_string();
    let mut e = Ets {
        version: i32_at(h, 4),
        pixel_type: i32_at(h, 8),
        channels: i32_at(h, 12),
        colorspace: i32_at(h, 16),
        compression: i32_at(h, 20),
        quality: i32_at(h, 24),
        tile_width: i32_at(h, 28),
        tile_height: i32_at(h, 32),
        tile_depth: i32_at(h, 36),
        background: h[108..148].try_into().unwrap(),
        component_order: i32_at(h, 148),
        use_pyramid: i32_at(h, 152) != 0,
        dimensions: i32_at(h, 184),
        dimension_sizes: (0..10).map(|k| i32_at(h, 188 + 4 * k)).collect(),
        ..Default::default()
    };
    rules.check(
        "ets",
        &mut rules::env([
            ("key", text(key)),
            ("magic", text(&magic)),
            ("pixel_type", num(e.pixel_type as f64)),
            ("channels", num(e.channels as f64)),
            ("compression", num(e.compression as f64)),
            ("tile_width", num(e.tile_width as f64)),
            ("tile_height", num(e.tile_height as f64)),
            ("tile_depth", num(e.tile_depth as f64)),
            ("dimensions", num(e.dimensions as f64)),
            ("coordinates", num(s.coordinates as f64)),
        ]),
    )?;
    let p = &rules.table("pixel_types")[e.pixel_type.to_string()];
    e.data_type = p["dtype"].as_str().unwrap_or("uint8").to_string();
    e.bytes = p["bytes"].as_u64().unwrap_or(1);
    e.codec = rules.table("compressions")[e.compression.to_string()].as_str().unwrap_or("raw").to_string();
    Ok(e)
}

/// One tile of the directory.
#[derive(Clone, Debug)]
pub struct Tile {
    pub coords: Vec<i32>,
    pub offset: i64,
    pub size: i32,
    pub index: i32,
}

/// The tile directory's records (unchecked: `check_tiles` checks them once the dimensions are
/// known).
pub fn directory(s: &Sis, d: &[u8]) -> Vec<Tile> {
    let rec = s.record() as usize;
    let n = s.coordinates as usize;
    (0..s.tiles as usize)
        .map(|k| {
            let o = k * rec;
            Tile {
                coords: (0..n).map(|i| i32_at(d, o + 4 + 4 * i)).collect(),
                offset: i64_at(d, o + 4 + 4 * n),
                size: i32_at(d, o + 12 + 4 * n),
                index: i32_at(d, o + 16 + 4 * n),
            }
        })
        .collect()
}

/// The form of the tiles of a file: their geometry and codec, read in place.
pub fn form(e: &Ets) -> String {
    json!({
        "geometry": {"shape": [e.tile_height, e.tile_width], "samples": e.channels, "bits": 8 * e.bytes},
        "codec": {"compression": e.codec},
        "recipe": [["src", 0, null]],
    })
    .to_string()
}

/// Emits the tile file's elements under its object `o` (described in place): the headers, the
/// directory, each tile (`tiles/<k>`, its element returned by index) and the gaps.
pub fn emit(ir: &mut Ir, o: u32, size: u64, head: &[u8], s: &Sis, eh: &[u8], e: &Ets, dir: &[u8], tiles: &[Tile]) -> Res<Vec<u32>> {
    ir.describe_in_place(o)?;
    let mut claimed: Vec<(u64, u64)> = Vec::new();
    ir.value(o, "header", NO_INDEX, SIS_TYPE, 0, (0, SIS_HEADER), Some(&head[..SIS_HEADER as usize]))?;
    claimed.push((0, SIS_HEADER));
    let (eo, en) = (s.ets_offset as u64, s.ets_size as u64);
    ir.value(o, "ets", NO_INDEX, ETS_TYPE, 0, (eo, ETS_HEADER), Some(&eh[..ETS_HEADER as usize]))?;
    if en > ETS_HEADER {
        ir.value(o, "ets_extra", NO_INDEX, &format!("bytes[{}]", en - ETS_HEADER), 0, (eo + ETS_HEADER, en - ETS_HEADER), Some(&eh[ETS_HEADER as usize..en as usize]))?;
    }
    claimed.push((eo, en));
    let (to, tn) = (s.tiles_offset as u64, s.record() * s.tiles as u64);
    if tn > 0 {
        let ty = format!("{{coordinates:<i4,coordinate:<i4[{}],offset:<i8,size:<i4,index:<i4}}[{}]", s.coordinates, s.tiles);
        ir.value(o, "directory", NO_INDEX, &ty, 0, (to, tn), Some(&dir[..tn as usize]))?;
        claimed.push((to, tn));
    }
    let f = form(e);
    let mut els = Vec::with_capacity(tiles.len());
    for (k, t) in tiles.iter().enumerate() {
        let d = ir.data(o, "tiles/", k as u64, 0, (t.offset as u64, t.size as u64), &f)?;
        els.push(d);
        claimed.push((t.offset as u64, t.size as u64));
    }
    claimed.sort_unstable();
    let mut pos = 0u64;
    for (a, n) in claimed {
        if a > pos {
            ir.add(GAP, o, "gaps/", pos, "", 0, Some((pos, a - pos)))?;
        }
        pos = pos.max(a + n);
    }
    if size > pos {
        ir.add(GAP, o, "gaps/", pos, "", 0, Some((pos, size - pos)))?;
    }
    Ok(els)
}

/// The header's source metadata (§6.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/vsi.md#61-the-root>).
pub fn header_json(key: &str, s: &Sis, e: &Ets, size: u64) -> J {
    json!({
        "key": key, "size": size, "version": s.version, "coordinates": s.coordinates, "tiles": s.tiles,
        "ets_version": e.version, "pixel_type": e.pixel_type, "data_type": e.data_type, "samples": e.channels,
        "colorspace": e.colorspace, "compression": e.compression, "codec": e.codec, "quality": e.quality,
        "tile_width": e.tile_width, "tile_height": e.tile_height, "tile_depth": e.tile_depth,
        "background": crate::mirror::base64(&e.background), "component_order": e.component_order,
        "use_pyramid": e.use_pyramid, "dimensions": e.dimensions,
        "dimension_sizes": e.dimension_sizes[..(e.dimensions.clamp(0, 10) as usize)].to_vec(),
    })
}
