//! The description of a TIFF, a QPTIFF or an NDPI (spec/zarrify/tiff.md §7.6
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#76-the-description>,
//! spec/zarrify/ndpi.md §7.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#71-the-description>):
//! what the parser found of the file's known structures, in TIFF's own terms, beside the IR. It
//! holds every IFD of the tree with the values of its tags the profile reads, the structures the
//! first IFD's ImageDescription and the file's header hold (OME-XML, an ImageJ hyperstack, a
//! Micro-Manager stack, an Aperio description, a QPI description of each IFD), and, for an NDPI,
//! the headers of its images' streams. It holds none of an output's decisions: which IFDs are
//! levels or planes, the axes, the chunks and their padding, the scales and the position are the
//! projection's.

use super::*;
use crate::mirror::base64;
use crate::text::text_json;

/// The description's form of a loaded value: integers as a list, a rational as `[numerator,
/// denominator]` pairs, an ASCII value as a text value (up to its first NUL), and other bytes
/// (JPEGTables) in base64.
fn loaded_json(tag: u16, v: &Loaded) -> J {
    match v {
        Loaded::Ints(v) => json!(v),
        Loaded::Pairs(p) => J::Array(p.iter().map(|&(a, b)| json!([a, b])).collect()),
        Loaded::Bytes(b) if tag == 270 => text_json(b.split(|&c| c == 0).next().unwrap_or(&[])),
        Loaded::Bytes(b) => J::String(base64(b)),
    }
}

/// A list of (name, value) pairs as a JSON object, in order.
fn pairs_json(p: &[(String, String)]) -> J {
    J::Object(p.iter().map(|(k, v)| (k.clone(), J::String(v.clone()))).collect())
}

/// An NDPI tag's values as the description holds them.
fn nv_json(v: &Nv) -> J {
    match v {
        Nv::Ints(v) => json!(v),
        Nv::Signed(v) => json!(v),
        Nv::Floats(v) => J::Array(v.iter().map(|&x| crate::project::float_json(x.to_bits(), 8, x)).collect()),
        Nv::Rats(r) => J::Array(r.iter().map(|&(a, b)| json!([a, b])).collect()),
    }
}

impl Tiff {
    /// The tags of the tree's IFDs whose values the description holds: the table's, but SubIFDs
    /// (which `subifds` holds), with the EER tags 65007 to 65009 where they are unsigned.
    fn described_tags(&self, k: usize, m: &Vals) -> J {
        let f = &self.ifds[k];
        let mut tags: Vec<(u16, usize)> = f.first.iter().map(|(&t, &i)| (t, i)).collect();
        tags.sort_unstable_by_key(|x| x.1);
        let mut out = serde_json::Map::new();
        for (tag, i) in tags {
            let e = &f.entries[i];
            let value = match m.get(&tag) {
                Some(v) => loaded_json(tag, v),
                None if matches!(tag, 65007..=65009) && unsigned(e.typ) => match self.ints(e, true) {
                    Some(v) => json!(v),
                    None => continue,
                },
                None => continue,
            };
            out.insert(tag.to_string(), json!({"type": e.typ, "count": e.count, "value": value}));
        }
        J::Object(out)
    }

    /// The short last strips of the tree's IFDs in LZW strips, each cut where its rows end and
    /// continued with zero rows to RowsPerStrip rows (`lzw::scan`, `lzw::tail`): for each IFD in
    /// strips of compression 5 whose last strip of a sample plane holds fewer rows than the others,
    /// that strip, read to at most 3 × its decoded size + 64 bytes. A strip whose zeros would
    /// pass `max_padding`, whose read would pass `eager_decode`, of no bytes or outside the file is
    /// left out, and so are the strips past the file's size in reads, in tree order. Asks for the
    /// bytes not read yet.
    fn lzw_tails(&self, tree: &[usize], vals: &HashMap<usize, Vals>) -> Got<Vec<J>> {
        let bytes = |x: u128| u64::try_from(x).unwrap_or(u64::MAX);
        let (max_padding, eager) = (self.rules.limit("max_padding") as u64, self.rules.limit("eager_decode") as u64);
        let mut need = Vec::new();
        let mut out = Vec::new();
        let mut read_total: u128 = 0;
        let mut seen: HashSet<(u64, u64)> = HashSet::new();
        for (i, &k) in tree.iter().enumerate() {
            let m = &vals[&k];
            if first_int(m, 259).unwrap_or(1) != 5 || self.tiled(k) {
                continue;
            }
            let (Some(w), Some(h)) = (first_int(m, 256), first_int(m, 257)) else { continue };
            let th = first_int(m, 278).unwrap_or(u32::MAX as u64).min(h);
            if th == 0 {
                continue;
            }
            let down = h.div_ceil(th);
            let short = h - th * (down - 1);
            if down < 2 || short == th {
                continue;
            }
            let bits = match m.get(&258) {
                Some(Loaded::Ints(v)) => v.first().copied().unwrap_or(1),
                _ => 1,
            };
            let spp = first_int(m, 277).unwrap_or(1);
            let planar = if spp > 1 { first_int(m, 284).unwrap_or(1) } else { 1 };
            let contig = spp > 1 && planar == 1;
            let samples = if spp > 1 && !contig { spp } else { 1 };
            let row = row_size(w, bits, if contig { spp } else { 1 });
            let missing = bytes((th - short) as u128 * row);
            let target = bytes(short as u128 * row);
            let (Some(Loaded::Ints(offsets)), Some(Loaded::Ints(counts))) = (m.get(&273), m.get(&279)) else { continue };
            for s in 0..samples {
                let j = (s * down + down - 1) as usize;
                let (Some(&o), Some(&c)) = (offsets.get(j), counts.get(j)) else { continue };
                let r = c.min(bytes(3 * target as u128 + 64));
                if c == 0 || missing > max_padding || r > eager || !self.within(o, r) {
                    continue;
                }
                if seen.insert((o, r)) {
                    if read_total + r as u128 > self.size as u128 {
                        continue;
                    }
                    read_total += r as u128;
                }
                let Some(b) = self.heads.get(&(o, r)) else {
                    need.push((o, r));
                    continue;
                };
                let mut entry = json!({"ifd": i, "strip": j, "offset": o, "read": r});
                match lzw::scan(b, target).and_then(|cut| lzw::tail(b, cut, missing)) {
                    Ok((prefix, tail)) => {
                        entry["prefix"] = json!(prefix);
                        entry["tail"] = J::String(base64(&tail));
                    }
                    Err(e) => entry["problem"] = json!(e),
                }
                out.push(entry);
            }
        }
        if need.is_empty() { Ok(out) } else { Err(Stop::Need(need)) }
    }

    /// The description of a TIFF or QPTIFF (`describe`, but for an NDPI): its header, its tree's
    /// IFDs with their tags, and the structures it holds, once the bytes they need are read.
    /// `imagej` is the hyperstack the layout found, which the IR's planes follow.
    pub(super) fn describe(&self, imagej: Option<&ImageJ>) -> Got<J> {
        if self.ndpi {
            return self.describe_ndpi();
        }
        let tree: Vec<usize> = (0..self.ifds.len()).filter(|&k| self.ifds[k].tree).collect();
        let index: HashMap<usize, usize> = tree.iter().enumerate().map(|(i, &k)| (k, i)).collect();
        // every tree IFD's values, and each one's ASCII ImageDescription, in one batch, unchecked:
        // the profile checks those of the IFDs its image uses
        let loaded = self.load_as(&tree, true, true, false)?;
        let vals: HashMap<usize, Vals> = tree.iter().copied().zip(loaded).collect();
        let tails = self.lzw_tails(&tree, &vals)?;
        let text_of = |k: usize| match vals[&k].get(&270) {
            Some(Loaded::Bytes(d)) if self.ifds[k].field(270).is_some_and(|e| e.typ == 2) => {
                std::str::from_utf8(d.split(|&c| c == 0).next().unwrap_or(&[])).ok().map(str::to_string)
            }
            _ => None,
        };
        let ifds: Vec<J> = tree
            .iter()
            .map(|&k| {
                let f = &self.ifds[k];
                let mut d = json!({
                    "offset": f.offset,
                    "element": f.el,
                    "subifds": f.subs.iter().filter_map(|s| index.get(s)).collect::<Vec<_>>(),
                    "tags": self.described_tags(k, &vals[&k]),
                    "tiles": null,
                    "strips": null,
                });
                if self.qptiff {
                    d["qpi"] = text_of(k).as_deref().and_then(qpi_description).map_or(J::Null, |q| {
                        json!({"image_type": q.image_type, "name": q.name, "color": q.color, "slide_id": q.slide_id})
                    });
                }
                d
            })
            .collect();
        let mut out = json!({
            "byte_order": if self.le { "little" } else { "big" },
            "bigtiff": self.big,
            "size": self.size,
            "chain": self.chain.iter().filter_map(|k| index.get(k)).collect::<Vec<_>>(),
            "ifds": ifds,
            "lzw_tails": tails,
        });
        if self.qptiff {
            return Ok(out);
        }
        let description = self.chain.first().and_then(|&i0| text_of(i0));
        let ome = match description.as_deref() {
            Some(t) => read_ome(t, self.rules)?,
            None => None,
        };
        out["ome"] = ome.as_ref().map_or(J::Null, |o| {
            json!({
                "uuid": o.uuid,
                "name": o.name,
                "pixels": pairs_json(&o.pixels),
                "tiff_data": o.tiff_data.iter().enumerate().map(|(k, (a, file))| json!({
                    "attributes": pairs_json(a), "file": file, "uuid": o.uuid_texts.get(&k),
                })).collect::<Vec<_>>(),
                "plane": o.plane.as_deref().map(pairs_json),
                "channels": o.channels.iter().map(|c| pairs_json(c)).collect::<Vec<_>>(),
            })
        });
        let mm = if ome.is_none() && !self.chain.is_empty() { self.micromanager()? } else { None };
        out["micromanager"] = mm.as_ref().map_or(J::Null, |m| {
            json!({
                "summary": m.summary,
                "index_map": m.entries.iter().map(|e| [e.channel, e.slice, e.frame, e.position, e.ifd]).collect::<Vec<_>>(),
            })
        });
        out["imagej"] = imagej.map_or(J::Null, |ij| {
            json!({
                "images": ij.images, "frames": ij.sizes.0, "channels": ij.sizes.1, "slices": ij.sizes.2,
                "plane_bytes": ij.plane_bytes, "planes": null,
            })
        });
        out["aperio"] = description.as_deref().and_then(aperio_fields).map_or(J::Null, |f| pairs_json(&f));
        Ok(out)
    }

    /// The description of an NDPI (spec/zarrify/ndpi.md §7.1
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#71-the-description>): its
    /// main chain's IFDs with the values of NDPI's table, and of each image with a Magnification
    /// above 0, its tiles with their high bytes, McuStarts with McuStartsHighBytes, its focal
    /// offset, its acquisition tags and its stream's header.
    fn describe_ndpi(&self) -> Got<J> {
        let mut need = Vec::new();
        let mut ifds = Vec::with_capacity(self.chain.len());
        for &k in &self.chain {
            let m = self.nload(k, None, &mut need, false)?;
            let f = &self.ifds[k];
            let mut tags = serde_json::Map::new();
            let mut entries: Vec<(u16, usize)> = f.first.iter().map(|(&t, &i)| (t, i)).filter(|&(t, _)| m.contains_key(&t)).collect();
            entries.sort_unstable_by_key(|x| x.1);
            for (tag, i) in entries {
                let e = &f.entries[i];
                tags.insert(tag.to_string(), json!({"type": e.typ, "count": e.count, "value": nv_json(&m[&tag])}));
            }
            let mut d = json!({"offset": f.offset, "element": f.el, "tags": tags, "tiles": null});
            let image = m.get(&65421).and_then(|v| v.first()).is_some_and(|x| x > 0.0);
            if image {
                let (s0, n) = (Self::nint(&m, 273).unwrap_or(0), Self::nint(&m, 279).unwrap_or(0));
                d["tile_tables"] = self.ndpi_tiles(k).map_or(J::Null, |t| {
                    json!({"offsets": t.offsets, "byte_counts": t.counts, "high_tables_fit": t.matched})
                });
                let starts = m.get(&65426).and_then(|v| v.ints()).map(|s| {
                    self.mcu_starts(k, s.to_vec(), m.get(&65432).and_then(|v| v.ints()))
                });
                d["mcu_starts"] = starts.as_ref().map_or(J::Null, |x| {
                    json!({"starts": x.starts, "same_length": x.same_length, "high_word": x.high_word, "high_added": x.added})
                });
                d["elements"] = json!({"mcu_starts": self.ent_facts(k, 65426), "mcu_starts_high": self.ent_facts(k, 65432)});
                d["focal_offset"] = json!(self.ndpi_focal(k, &mut need));
                d["acquisition"] = J::Object(self.ndpi_acquisition(k, &mut need).0);
                // the stream's header: a JPEG XR stream's, a JPEG strip's before its first restart
                // interval, or its frame header
                let fits = (s0 as u128 + n as u128) <= self.size as u128;
                if Self::nint(&m, 259) == Some(ndpi::JPEGXR) {
                    let tiled = [322u16, 323].iter().all(|&t| Self::nint(&m, t).is_some_and(|v| v >= 1)) && f.has(324) && f.has(325);
                    let first = self.ndpi_first(k, &m, tiled).filter(|x| x.1 > 0);
                    let header = match first.filter(|&(o, c)| self.within(o, c)) {
                        Some((o, c)) => match self.ndpi_stream(o, c) {
                            Ok(h) => h,
                            Err(r) => {
                                need.push(r);
                                None
                            }
                        },
                        None => None,
                    };
                    d["jpegxr"] = json!({
                        "first": first.map(|(o, c)| [o, c]),
                        "header": header.map(|(format, w, h)| json!({"pixel_format": ndpi::hex(&format), "width": w, "height": h})),
                    });
                } else if let Some(x) = starts.as_ref().filter(|x| x.same_length && x.high_word) {
                    let first = x.starts.first().copied().filter(|&f| f >= 2 && f <= n);
                    d["jpeg_header"] = match first.filter(|_| fits) {
                        Some(f) => match self.heads.get(&(s0, f)) {
                            Some(b) => J::String(base64(b)),
                            None => {
                                need.push((s0, f));
                                J::Null
                            }
                        },
                        None => J::Null,
                    };
                } else if starts.is_none() && fits && n >= 4 {
                    d["jpeg_frame"] = match self.ndpi_frame(s0, n) {
                        Ok(f) => f.map_or(J::Null, |f| json!({"at": f.at, "height": f.height, "width": f.width, "mcu_width": f.mw, "mcu_height": f.mh})),
                        Err(r) => {
                            need.push(r);
                            J::Null
                        }
                    };
                }
            }
            ifds.push(d);
        }
        if !need.is_empty() {
            return Err(Stop::Need(need));
        }
        Ok(json!({"byte_order": "little", "bigtiff": false, "size": self.size, "ifds": ifds}))
    }

    /// The description with the IR's elements the layout's reads did not know yet: each IFD's
    /// `tiles` and `strips`, and an ImageJ hyperstack's planes.
    pub(super) fn finished_description(&self) -> J {
        let Some(mut d) = self.description.clone() else { return J::Null };
        let tree: Vec<usize> = if self.ndpi { self.chain.clone() } else { (0..self.ifds.len()).filter(|&k| self.ifds[k].tree).collect() };
        if let Some(ifds) = d["ifds"].as_array_mut() {
            for (e, &k) in ifds.iter_mut().zip(&tree) {
                let (tiles, strips) = self.set.tiles.get(&k).copied().unwrap_or((None, None));
                e["tiles"] = json!(tiles);
                if !self.ndpi {
                    e["strips"] = json!(strips);
                }
            }
        }
        if d["imagej"].is_object() {
            // plane 0 is IFD 0's image; in one IFD, each later plane is its own struct, whose
            // strips are IFD 0's shifted
            let planes: Vec<J> = self.imagej_planes.iter().map(|(p, data)| json!({"element": p, "strips": data})).collect();
            d["imagej"]["planes"] = J::Array(planes);
        }
        d
    }
}
