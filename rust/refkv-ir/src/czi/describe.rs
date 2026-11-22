//! The description of a CZI file (spec/zarrify/czi.md §7.10
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/czi.md#710-the-description>): what the
//! parser found of the file's known structures, in CZI's own terms, beside the IR. It holds the
//! file header, the segments the profile read, the subblock directory's entries with their
//! dimension entries and, of each, its subblock's header, whether its copy of its entry agrees and
//! what its stream's codec header gives; the metadata segment with the values the profile reads of
//! its XML; and the attachment directory's entries. It holds none of an output's decisions: which
//! subblocks are placed, the series, layers, levels and tiles, the axes, data types, codecs and
//! chunks, the scales and position, the `omero` and the root's source metadata are the
//! projection's.

use super::*;
use crate::text::text_json;

/// A float as JSON: a number, or `NaN`, `Infinity` or `-Infinity` as text.
fn float_json(v: f64) -> J {
    if v.is_nan() {
        json!("NaN")
    } else if v.is_infinite() {
        json!(if v > 0.0 { "Infinity" } else { "-Infinity" })
    } else {
        json!(v)
    }
}

/// Bytes without their trailing NULs, as a text value (any bytes after an earlier NUL are kept, as
/// the IR keeps them).
fn cstr_json(b: &[u8]) -> J {
    let n = b.iter().rposition(|&c| c != 0).map_or(0, |k| k + 1);
    text_json(&b[..n])
}

/// An attachment entry's 128 bytes (spec/zarrify/czi.md §2.6
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/czi.md#26-attachments>): its schema, and
/// of an `A1` entry its fields.
pub(super) fn attachment_entry(e: &[u8]) -> J {
    let schema = text_json(&e[..2]);
    if &e[..2] != b"A1" {
        return json!({"schema": schema});
    }
    json!({
        "schema": schema,
        "file_position": i64::le_at(e, 12),
        "file_part": i32::le_at(e, 20),
        "content_guid": guid(&e[24..40]),
        "content_file_type": cstr_json(&e[40..48]),
        "name": cstr_json(&e[48..128]),
    })
}

/// The values the profile reads of the metadata XML (spec/zarrify/czi.md §2.7
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/czi.md#27-the-metadata-xml>), as read.
fn values_json(v: &cxml::Values) -> J {
    let ch = |c: &cxml::Channel| {
        json!({"name": c.name, "color": c.color, "bits": c.bits, "low": c.low.map(float_json), "high": c.high.map(float_json)})
    };
    let sizes: serde_json::Map<String, J> = v.sizes.iter().map(|(l, n)| ((*l as char).to_string(), json!(n))).collect();
    json!({
        "px": v.px, "py": v.py, "pz": v.pz, "inc": v.inc, "bits": v.bits,
        "info": v.info.iter().map(ch).collect::<Vec<_>>(),
        "display": v.display.iter().map(ch).collect::<Vec<_>>(),
        "scenes": v.scenes.iter().map(|(i, n)| json!({"index": i, "name": n})).collect::<Vec<_>>(),
        "sizes": sizes,
        "document": {"name": v.document[0], "creation_date": v.document[1]},
    })
}

impl Czi {
    /// The description of the file, once its parse is done.
    pub(super) fn description(&self) -> J {
        let (major, minor, primary, file, file_part, dir, meta, update, att) = self.fields.clone().unwrap_or_default();
        let header = json!({
            "major": major, "minor": minor, "primary_file_guid": guid(&primary), "file_guid": guid(&file),
            "file_part": file_part, "directory_position": dir, "metadata_position": meta,
            "update_pending": update, "attachment_directory_position": att,
        });
        // the segments read: those referenced, and those the walk found, in file order
        let mut segments: Vec<(u64, J)> = self
            .segs
            .iter()
            .map(|(&o, s)| (o, json!({"offset": o, "id": s.id, "allocated_size": s.allocated, "used_size": s.used, "referenced": true})))
            .collect();
        segments.extend(self.walk_found.iter().map(|(o, id, a, u)| {
            (*o, json!({"offset": o, "id": id, "allocated_size": a, "used_size": u, "referenced": false}))
        }));
        segments.sort_by_key(|s| s.0);
        let directory = self.dir.map(|(p, _, _, count)| json!({"position": p, "entry_count": count}));
        let entries: Vec<J> = (0..self.entries.len()).map(|i| self.entry_json(i)).collect();
        let metadata = self.meta.map(|(p, _, x, b)| {
            json!({"position": p, "xml_size": x, "attachment_size": b, "xml": self.meta_xml, "values": values_json(&self.values)})
        });
        let attachments = self.att.map(|(p, _, k)| json!({"position": p, "entry_count": k, "entries": self.att_entries}));
        json!({
            "size": self.size,
            "given": self.given,
            "file_header": header,
            "segments": segments.into_iter().map(|s| s.1).collect::<Vec<_>>(),
            "directory": directory,
            "entries": entries,
            "metadata": metadata,
            "attachments": attachments,
        })
    }

    /// Entry `i` (spec/zarrify/czi.md §2.3
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/czi.md#23-the-subblock-directory>), with
    /// its subblock's header where this file holds it (spec/zarrify/czi.md §2.4
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/czi.md#24-subblocks>).
    fn entry_json(&self, i: usize) -> J {
        let e = &self.entries[i];
        let b = self.entry_bytes(i);
        let dimensions: Vec<J> = e
            .dims
            .iter()
            .enumerate()
            .map(|(k, &(l, start, size, stored))| {
                let coordinate = f32::from_le_bytes(b[32 + 20 * k + 12..32 + 20 * k + 16].try_into().unwrap());
                json!({"dimension": (l as char).to_string(), "start": start, "size": size,
                       "start_coordinate": float_json(coordinate as f64), "stored_size": stored})
            })
            .collect();
        let subblock = self.subs.get(i).filter(|s| !self.elsewhere(e) && s.key.is_some()).map(|s| {
            let stream = s.coded.filter(|_| s.agrees).map(|(w, h, flag, skip)| {
                let mut st = json!({"width": w, "height": h});
                match e.comp {
                    coding::JPEG => st["lossless"] = json!(flag),
                    coding::ZSTD1 => {
                        st["hilo"] = json!(flag);
                        st["header_size"] = json!(skip);
                    }
                    _ => {}
                }
                st
            });
            json!({
                "element": s.key,
                "metadata_size": s.m, "attachment_size": s.a, "data_size": s.n, "header_length": s.length,
                "agrees": s.el.map(|_| s.agrees),
                "stream": stream,
                "data": s.data_el,
            })
        });
        json!({
            "element": e.raw.is_none().then_some(e.el),
            "schema": text_json(&b[..2]),
            "pixel_type": e.pt,
            "file_position": e.pos,
            "file_part": i32::le_at(&b, 14),
            "compression": e.comp,
            "pyramid_type": b[22],
            "dimensions": dimensions,
            "subblock": subblock,
        })
    }
}
