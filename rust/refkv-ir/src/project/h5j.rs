//! The H5J projection (spec/zarrify/h5j.md §4
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/h5j.md#4-the-layout>) from a valid IR: an
//! OME-NGFF image a channel, its one array the luma planes of its MP4 file's pictures, a picture a
//! slice, the padding past the array's end, each chunk a `refkv.stream` chunk of the pictures as
//! the MP4 projection cuts them (spec/zarrify/mp4.md §8.3
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mp4.md#83-chunks>).

use crate::error::Error;

use super::mp4::{grid_lengths, stream_codec, video_chunk, video_chunks};
use super::{arr, n};
use crate::ir::Ir;
use crate::out::{declare, declared_group, full_window, group_doc, image_ome, omero_channel, omero_json, rectilinear_array_json, Out};
use crate::refs::Refs;
use serde_json::{json, Map, Value as J};

/// The axes of a channel's image.
const AXES: [&str; 3] = ["z", "y", "x"];

/// The OME-NGFF unit of an H5J file's `unit` (spec/zarrify/h5j.md §4.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/h5j.md#41-a-channel>), if it is one the
/// convention knows.
pub fn length_unit(s: &str) -> Option<&'static str> {
    Some(match s {
        "micron" | "microns" | "micrometer" | "um" | "\u{b5}m" | "\u{3bc}m" => "micrometer",
        "nanometer" | "nm" => "nanometer",
        "millimeter" | "mm" => "millimeter",
        _ => return None,
    })
}

/// The scale of a channel's axes (z, y, x): the root's `voxel_size` (x, y, z) when it is three
/// finite numbers greater than 0, else 1 each.
pub fn scale(voxel_size: &J) -> Vec<f64> {
    let v: Option<Vec<f64>> = voxel_size.as_array().filter(|a| a.len() == 3).and_then(|a| a.iter().map(J::as_f64).collect());
    match v {
        Some(v) if v.iter().all(|x| x.is_finite() && *x > 0.0) => vec![v[2], v[1], v[0]],
        _ => vec![1.0; 3],
    }
}

/// A node's attributes, as the HDF5 hierarchy presents them, by its path.
fn attributes<'a>(nodes: &'a [J], path: &str) -> &'a J {
    nodes.iter().find(|n| n["path"] == path).map(|n| &n["attributes"]).unwrap_or(&J::Null)
}

/// An MP4 file as refkv's MP4 convention reads it, from the parser's description: its brand, movie
/// timescale and duration, the most frames of a chunk, and each track's description and refkv's
/// plan of it together (`project::mp4::planned_track`); an error, the convention's, when it
/// zarrifies no track (spec/zarrify/mp4.md §8.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mp4.md#82-tracks>).
fn planned(description: &J) -> crate::error::Res<J> {
    use super::mp4::{plan, planned_track};
    let p = plan(description)?;
    let tracks: Vec<J> = arr(&description["tracks"]).iter().zip(arr(&p["tracks"])).map(|(t, f)| planned_track(t, f)).collect();
    Ok(json!({
        "brand": description["brand"], "timescale": description["timescale"], "duration": description["duration"],
        "chunk_frames": p["chunk_frames"], "tracks": tracks,
    }))
}

/// refkv's plan of each channel (spec/zarrify/h5j.md §8.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/h5j.md#82-channels>), from the
/// description's channels and the padding `/Channels` gives: its `name`, `path`, `offset`, `length`
/// and `attributes`; its `reason`, null for a channel the convention zarrifies; and of such a
/// channel its MP4 file's `brand`, `timescale`, `duration` and `chunk_frames`, and its one track as
/// refkv's MP4 plan reads it (`track`, null for another).
pub fn channels(described: &[J], pad: &crate::error::Res<(u64, u64)>) -> Vec<J> {
    described.iter().map(|c| {
        let mut m = Map::new();
        for k in ["name", "path", "offset", "length", "attributes"] {
            m.insert(k.into(), c[k].clone());
        }
        let file = match (c["unread"].as_str(), &c["mp4"]) {
            (Some(r), _) => Err(Error::rejected(r)),
            (None, d) => planned(d).map_err(|e| e.context(|m| format!("its MP4 file: {m}"))),
        };
        let reason = match &file {
            Err(r) => Some(r.to_string()),
            Ok(f) => match arr(&f["tracks"]) {
                [t] if !t["reason"].is_null() => Some(format!("its MP4 file's track {}: {}", t["id"], t["reason"].as_str().unwrap_or(""))),
                [t] if !matches!(t["format"].as_str(), Some("h264" | "hevc" | "av1" | "vp9")) => {
                    Some(format!("its MP4 file's track is {}, not video", t["format"].as_str().unwrap_or("none")))
                }
                [t] if t["geometry"]["chroma"] != "4:0:0" => {
                    Some(format!("its pictures are {}, not 4:0:0 (monochrome)", t["geometry"]["chroma"].as_str().unwrap_or("")))
                }
                [t] => match pad {
                    Err(m) => Some(m.to_string()),
                    Ok((right, bottom)) => {
                        let (w, h) = (n(&t["geometry"]["width"]), n(&t["geometry"]["height"]));
                        (*right >= w || *bottom >= h).then(|| format!("a padding of {right} by {bottom} of pictures of {w} by {h}"))
                    }
                },
                tracks => Some(format!("its MP4 file has {} tracks, not one", tracks.len())),
            },
        };
        let mut track = J::Null;
        if let (None, Ok(f)) = (&reason, &file) {
            for k in ["brand", "timescale", "duration", "chunk_frames"] {
                m.insert(k.into(), f[k].clone());
            }
            track = arr(&f["tracks"])[0].clone();
        }
        m.insert("reason".into(), json!(reason));
        m.insert("track".into(), track);
        J::Object(m)
    }).collect()
}

/// refkv's plan of the file: its channels (`channels`) and the padding (right, bottom), null when
/// `/Channels`'s attributes give none that reads.
pub fn plan(description: &J, nodes: &[J]) -> J {
    let pad = crate::h5j::pad(attributes(nodes, crate::h5j::GROUP));
    let pad_json = pad.as_ref().map_or(J::Null, |&(r, b)| json!([r, b]));
    json!({"channels": channels(arr(&description["h5j"]["channels"]), &pad), "pad": pad_json})
}

/// The output of the file `description` describes, through refkv's plan of it (`plan`).
pub fn project(ir: &Ir, description: &J, url: &str) -> crate::error::Res<Out> {
    let nodes = super::hdf5_nodes::nodes(description)?.nodes;
    let h = &plan(description, &nodes);
    let root = attributes(&nodes, "");
    let channels = arr(&h["channels"]);
    let (pad_right, pad_bottom) = (n(&h["pad"][0]), n(&h["pad"][1]));
    // the root (§5.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/h5j.md#51-the-root>)
    let listed: Vec<J> = channels
        .iter()
        .map(|c| json!({"name": c["name"], "path": if c["reason"].is_null() { c["name"].clone() } else { J::Null }, "reason": c["reason"]}))
        .collect();
    let own = json!({"attributes": root.clone(), "channels_attributes": attributes(&nodes, crate::h5j::GROUP).clone(), "channels": listed});
    let mut out = Out::new(url);
    out.json("zarr.json", &declared_group(Map::new(), "h5j", Some(url), Some(own)));
    let units: Map<String, J> = match root["unit"].as_str().and_then(length_unit) {
        Some(u) => AXES.iter().map(|a| (a.to_string(), json!(u))).collect(),
        None => Map::new(),
    };
    let scales = [json!(scale(&root["voxel_size"]))];
    let mut refs = Refs::new(ir, out);
    let mut summary = Vec::new();
    for c in channels.iter().filter(|c| c["reason"].is_null()) {
        let name = c["name"].as_str().ok_or_else(|| Error::internal("a channel has no name"))?;
        let t = &c["track"];
        let first = n(&t["first"]) as usize;
        let count = n(&t["count"]) as usize;
        let (w, hgt) = (n(&t["geometry"]["width"]), n(&t["geometry"]["height"]));
        let data_type = t["geometry"]["data_type"].as_str().unwrap_or("uint8");
        let bits = n(&t["bit_depth"]);
        let limit = n(&c["chunk_frames"]).max(1);
        let chunk = video_chunk(limit, w, hgt, if data_type == "uint16" { 2 } else { 1 });
        // the pictures in presentation order, a picture a slice, in the MP4 profile's chunks (§4.1
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/h5j.md#41-a-channel>, §8.4
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/h5j.md#84-chunks>)
        let (cut, _) = video_chunks(ir, t, first, count, chunk)?;
        let lengths: Vec<u64> = cut.iter().map(|c| c.0).collect();
        let sync = arr(&t["sync"]).len();
        // the image group, its source metadata (§5.2
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/h5j.md#52-a-channel>)
        let mut ome = image_ome(&AXES, &units, &scales, Some(&json!(name)), None)?;
        let label = c["attributes"]["content_type"].as_str().unwrap_or(name);
        let top = (1u64 << bits.min(16)) - 1;
        ome["omero"] = omero_json(vec![omero_channel(label, "FFFFFF", Some(&full_window(json!(0), json!(top))))]);
        let mut track = Map::new();
        for k in ["id", "codec", "format", "timescale", "duration", "edits", "bit_depth", "profile", "level"] {
            track.insert(k.into(), t[k].clone());
        }
        track.insert("width".into(), t["geometry"]["width"].clone());
        track.insert("height".into(), t["geometry"]["height"].clone());
        track.insert("chroma".into(), t["geometry"]["chroma"].clone());
        track.insert("frames".into(), json!(count));
        track.insert("sync_samples".into(), json!(sync));
        let own = json!({
            "dataset": c["path"], "offset": c["offset"], "length": c["length"], "attributes": c["attributes"],
            "brand": c["brand"], "movie_timescale": c["timescale"], "movie_duration": c["duration"],
            "track": J::Object(track), "chunk_frames": chunk,
        });
        let mut a = Map::new();
        a.insert("ome".into(), ome);
        refs.out.json(&format!("{name}/zarr.json"), &group_doc(declare(a, "h5j", None, Some(own))));
        // the array: the padding is past its end, in each chunk
        let shape = [count as u64, hgt - pad_bottom, w - pad_right];
        let codecs = stream_codec(t["format"].as_str().unwrap_or("hevc"), &t["setup"], Some("y"));
        let doc = rectilinear_array_json(&shape, data_type, &json!([grid_lengths(&lengths), hgt, w]), codecs, &AXES);
        refs.out.json(&format!("{name}/0/zarr.json"), &doc);
        let form = ir.form_id(first as u32).ok_or_else(|| Error::internal("a sample has no form"))?;
        for (q, (_, (o, len), parts)) in cut.into_iter().enumerate() {
            refs.chunk(format!("{name}/0/c/{q}/0/0"), o, len, form, Some(parts))?;
        }
        summary.push(json!({"name": name, "path": format!("{name}/0"), "frames": count, "chunkFrames": chunk, "chunks": lengths.len()}));
    }
    let mut out = refs.out;
    let skipped: Vec<J> = channels.iter().filter(|c| !c["reason"].is_null()).map(|c| json!({"name": c["name"], "reason": c["reason"]})).collect();
    out.summary = json!({"channels": summary, "skipped": skipped});
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn voxel_sizes_give_the_scale_and_units_the_unit() {
        assert_eq!(scale(&json!([0.5, 0.25, 2.0])), vec![2.0, 0.25, 0.5]);
        for bad in [json!(null), json!([1.0, 2.0]), json!([1.0, 0.0, 1.0]), json!([1.0, "a", 1.0]), json!([-1.0, 1.0, 1.0])] {
            assert_eq!(scale(&bad), vec![1.0; 3], "{bad}");
        }
        assert_eq!(length_unit("micron"), Some("micrometer"));
        assert_eq!(length_unit("nm"), Some("nanometer"));
        assert_eq!(length_unit("pixels"), None);
    }
}
