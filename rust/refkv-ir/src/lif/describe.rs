//! The description of a LIF, LOF or LIFEXT file (spec/zarrify/lif.md §9
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#9-the-description>): what the
//! parser found of the file's known structures, in Leica's own terms, beside the IR. It holds the
//! header block, the XML's root, a LOF's version fields and XML block, every memory block the walk
//! found, the tail, and each image element of the XML with its attributes, its `Memory`, its
//! dimensions' and channels' attributes as the XML writes them, its `TileScanInfo` attachment and
//! the memory block it names. It holds none of an output's decisions: the data type, the axes and
//! their order, the channels' order and colors, the bands, the fields, the scales and units, and
//! which images are presented are the projection's.

use super::*;

/// A UTF-16LE ID's text, or null when it is not well-formed UTF-16.
fn id_json(id: &[u8]) -> J {
    let units: Vec<u16> = id.chunks_exact(2).map(|c| u16::le_at(c, 0)).collect();
    String::from_utf16(&units).map_or(J::Null, J::String)
}

/// An image element as the description holds it: its source metadata (`read_image`), its
/// `TileScanInfo` attachment, and the memory block it names, an index into `blocks`.
pub(super) fn image(im: &Image, block: Option<usize>) -> J {
    let field = |k: &str| im.meta.get(k).cloned().unwrap_or(J::Null);
    let scan = im.scan.as_ref().map_or(J::Null, |(a, tiles)| json!({"attributes": a, "tiles": tiles}));
    json!({
        "name": im.name,
        "path": field("path"),
        "parent": field("parent"),
        "attributes": field("attributes"),
        "memory": field("memory"),
        "dimensions": field("dimensions"),
        "channels": field("channels"),
        "tile_scan": scan,
        "block": block,
    })
}

impl Lif {
    /// The description of the file, once the walk and the IR's emission are done; `images` are
    /// the images' (`image`), in document order.
    pub(super) fn describe(&self, images: Vec<J>) -> J {
        let object = (self.file == File::Lof).then(|| {
            let v = &self.object.versions;
            json!({
                "versions": [u32::le_at(v, 1), u32::le_at(v, 6)],
                "xml_at": self.object.at,
                "xml_characters": self.object.chars,
                "older": self.older,
            })
        });
        let blocks: Vec<J> = self
            .blocks
            .iter()
            .map(|b| {
                // a LOF's one block has no ID field
                let id = if self.file == File::Lof { J::Null } else { id_json(&b.id) };
                json!({"offset": b.start, "id": id, "size": b.size, "data": b.data()})
            })
            .collect();
        let file = match self.file {
            File::Lif => "lif",
            File::Lof => "lof",
            File::Lifext => "lifext",
        };
        json!({
            "file": file,
            "size": self.size,
            "xml_characters": self.chars,
            "root": self.root,
            "version": self.version,
            "unique_id": self.unique,
            "object": object,
            "blocks": blocks,
            "tail": self.tail.map(|(o, n)| json!({"offset": o, "length": n})),
            "images": images,
        })
    }
}
