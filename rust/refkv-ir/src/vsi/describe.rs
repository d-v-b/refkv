//! The description of a VSI slide (spec/zarrify/vsi.md §9.6
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#96-the-description>): what the
//! parser found of the `.vsi` file's and the tile files' known structures, in Olympus's own terms,
//! beside the IR. The `.vsi` file's TIFF header and IFDs as the profile reads them, its metadata
//! tree, the fields of each stack the profile reads, and each tile file's SIS and ETS headers and
//! tile directory, with the IR's elements that hold them. None of an output's decisions: which
//! images are presented, their axes, levels, data types, codecs, scales and channels are the
//! projection's.

use super::tiff::{Emitted, Ifd};
use super::{EtsFile, Vsi};
use crate::fed::Word;
use crate::tiff::Unit;
use serde_json::{Map, Value as J, json};
use std::collections::HashMap;

/// The IFD tags the profile reads (spec/zarrify/vsi.md §2.3
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#23-the-tiff-images>, §4.3
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#43-the-macro-image>): the image's
/// size, samples, bits, compression, photometric interpretation, planar configuration, sample
/// format, its strips' and tiles' tables, JPEGTables, and the EXIF IFD's offset.
pub const TAGS: [u16; 17] = [256, 257, 258, 259, 262, 273, 277, 278, 279, 284, 322, 323, 324, 325, 339, 347, 34665];

/// JPEGTables, whose value the description gives as bytes.
const JPEG_TABLES: u16 = 347;

/// The elements of a table of strips or tiles: each entry's `data` element, or null for one the
/// IR has none of (of no bytes, or outside the file); null without the table, or when its two
/// fields differ in length.
fn table(units: &Option<Option<Vec<Unit>>>) -> J {
    match units {
        Some(Some(u)) => J::Array(
            u.iter()
                .map(|x| match x {
                    Unit::Held(e, _) => json!(e),
                    _ => J::Null,
                })
                .collect(),
        ),
        _ => J::Null,
    }
}

/// One IFD of the chain (an EXIF IFD too): where it is, the tags of `TAGS` it has (the first of
/// each), and, of a main-chain IFD that lies in a TIFF region, its IR struct and its strips' and
/// tiles' data elements.
fn ifd_json(ifd: &Ifd, b: &[u8], made: Option<&Emitted>) -> J {
    let mut tags = Map::new();
    for &tag in &TAGS {
        let Some(e) = ifd.entry(tag) else { continue };
        let value = if tag == JPEG_TABLES {
            ifd.bytes(b, tag).map_or(J::Null, |v| json!(crate::mirror::base64(v)))
        } else {
            ifd.ints(b, tag).map_or(J::Null, |v| json!(v))
        };
        tags.insert(tag.to_string(), json!({"type": e.typ, "count": e.count, "value": value}));
    }
    json!({
        "index": ifd.index, "exif": ifd.exif, "offset": ifd.offset, "next": ifd.next,
        "tags": tags,
        "element": made.map(|m| m.el),
        "strips": made.map_or(J::Null, |m| table(&m.strips)),
        "tiles": made.map_or(J::Null, |m| table(&m.tiles)),
    })
}

/// A tile file: its object, its SIS and ETS headers as the profile reads them, and its directory,
/// each tile `[element, offset, size, c0, c1, ...]` with its coordinates in the record's order.
fn tile_file_json(f: &EtsFile, object: u32, header: u32, tiles: &[u32]) -> J {
    let (s, e) = (&f.sis, &f.ets);
    json!({
        "key": f.key, "size": f.size, "element": object, "stack": f.stack,
        "sis": {
            "header_size": s.header_size, "version": s.version, "coordinates": s.coordinates,
            "ets_offset": s.ets_offset, "ets_size": s.ets_size, "tiles_offset": s.tiles_offset,
            "tiles": s.tiles, "size": s.size,
        },
        "ets": {
            "version": e.version, "pixel_type": e.pixel_type, "channels": e.channels, "colorspace": e.colorspace,
            "compression": e.compression, "quality": e.quality, "tile_width": e.tile_width,
            "tile_height": e.tile_height, "tile_depth": e.tile_depth,
            "background": crate::mirror::base64(&e.background), "component_order": e.component_order,
            "use_pyramid": e.use_pyramid, "dimensions": e.dimensions,
            "dimension_sizes": e.dimension_sizes[..(e.dimensions.clamp(0, 10) as usize)].to_vec(),
        },
        "header": header,
        "tiles": f.tiles.iter().zip(tiles).map(|(t, &el)| {
            let mut v = vec![json!(el), json!(t.offset), json!(t.size)];
            v.extend(t.coords.iter().map(|c| json!(c)));
            J::Array(v)
        }).collect::<Vec<_>>(),
    })
}

impl Vsi {
    /// The description of the slide, once its IR is emitted: `header` is the `.vsi` file's
    /// `header` element, `made` what `emit_region` made of each main-chain IFD in a TIFF region,
    /// by its index, and `tiles_of` each tile file's `ets` element and its tiles' elements.
    pub(super) fn description(&self, header: u32, made: &HashMap<usize, Emitted>, tiles_of: &[(u32, Vec<u32>)]) -> J {
        let b = self.file.as_slice();
        let tree = self.tree.as_ref();
        let macro_ifd = tree
            .and_then(|t| t.volume(super::COLLECTION_VOLUME))
            .and_then(|c| c.fields.iter().find(|x| x.tag == super::DEFAULT_SAMPLE_IFD && x.kind == super::Kind::TiffIfd))
            .and_then(|x| x.tag2);
        let stacks: Vec<J> = self
            .stacks
            .iter()
            .map(|s| {
                let mut d = s.described.clone();
                d["tile_file"] = json!(s.file);
                d
            })
            .collect();
        json!({
            "key": self.keys[self.vsi].0, "size": self.keys[self.vsi].1, "element": self.elems[self.vsi],
            "header": {
                "byte_order": String::from_utf8_lossy(&b[..2]), "magic": u16::le_at(b, 2), "first_ifd": u32::le_at(b, 4),
                "element": header,
            },
            "ifds": self.ifds.iter().map(|f| ifd_json(f, b, if f.exif { None } else { made.get(&f.index) })).collect::<Vec<_>>(),
            "tree": tree.map_or(J::Null, |t| super::tree::container_json(self.rules, b, t)),
            "macro_ifd": macro_ifd,
            "stacks": stacks,
            "tile_files": self.files.iter().zip(tiles_of).map(|(f, (h, t))| tile_file_json(f, self.elems[f.object], *h, t)).collect::<Vec<_>>(),
        })
    }
}
