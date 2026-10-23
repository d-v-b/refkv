//! The MP4 projection (spec/virtualize/mp4.md §4
//! <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mp4.md#4-the-layout>, and the source
//! metadata of §5 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mp4.md#5-source-metadata>)
//! from a valid IR: a group per track the profile virtualizes, with the planes of an H.264
//! track's pictures and their presentation times, or an audio track's samples, each chunk a
//! `refkv.stream` chunk of the track's samples (spec/virtualize/mp4.md §18.3
//! <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mp4.md#183-chunks>).

use super::mrc::gzip;
use super::{arr, n, Copied};
use crate::ir::Ir;
use crate::out::{array_json, declare, group_doc, stream_parts, Out};
use crate::refs::Refs;
use serde_json::{json, Map, Value as J};

/// The most bytes of luma a video chunk decodes to (§4.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mp4.md#41-video-tracks>), and the most
/// values of a chunk of the time array.
const CHUNK_LUMA: u64 = 1 << 25;
const TIME_CHUNK: u64 = 1 << 17;
/// The bytes of main data an MP3 frame holds at least, by its length (its header, CRC and
/// side information are at most 38 bytes), and the reservoir its lead-in holds (MPEG-1's).
const MP3_OVERHEAD: u64 = 38;
const MP3_RESERVOIR: u64 = 511;

/// The frames of a video chunk: the profile's limit, and at most `CHUNK_LUMA` bytes of luma.
pub fn video_chunk(limit: u64, width: u64, height: u64) -> u64 {
    limit.min((CHUNK_LUMA / (width * height).max(1)).max(1))
}

/// The samples of a video chunk of the presented frames `[a, b)` (§18.3
/// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mp4.md#183-chunks>): in decode order,
/// from the last sync sample at or before the first of them in decode order to the last of
/// them, each with its position (its presentation index less `a`, or -1 when it is not in
/// the chunk). `order` is the decode index of each presented frame, `rank` the presentation
/// index of each decode index, `sync` the sync samples.
pub fn video_units(a: usize, b: usize, order: &[usize], rank: &[usize], sync: &[u64]) -> Vec<(usize, i64)> {
    let lo = order[a..b].iter().copied().min().unwrap_or(0);
    let hi = order[a..b].iter().copied().max().unwrap_or(0);
    let s = match sync.partition_point(|&x| x as usize <= lo) {
        0 => 0,
        k => sync[k - 1] as usize,
    };
    (s..=hi).map(|d| (d, if (a..b).contains(&rank[d]) { (rank[d] - a) as i64 } else { -1 })).collect()
}

/// The first frame of an audio chunk's lead-in, the chunk starting at frame `a`: none for the
/// first chunk; for AAC the frame before it (its overlap); for MP3 the two frames before it
/// and before them the fewest whose main data (at least each frame's length less 38 bytes)
/// holds 511 bytes, or every frame from the first.
pub fn audio_lead(a: usize, format: &str, sizes: &[u64]) -> usize {
    if a == 0 {
        return 0;
    }
    if format == "aac" {
        return a - 1;
    }
    let mut l = a.saturating_sub(2);
    let mut have = 0;
    while l > 0 && have < MP3_RESERVOIR {
        l -= 1;
        have += sizes[l].saturating_sub(MP3_OVERHEAD);
    }
    l
}

fn stream_codec(format: &str, setup: &J, plane: Option<&str>) -> J {
    let mut c = Map::new();
    c.insert("format".into(), json!(format));
    if let Some(s) = setup.as_str() {
        c.insert("setup".into(), json!(s));
    }
    if let Some(p) = plane {
        c.insert("plane".into(), json!(p));
    }
    json!([{"name": "refkv.stream", "configuration": c}])
}

/// A track's source metadata (§5.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mp4.md#52-the-tracks>).
fn track_json(t: &J, chunk: u64) -> J {
    let mut m = Map::new();
    for k in ["id", "handler", "codec", "format", "timescale", "duration", "edits", "count"] {
        m.insert(if k == "count" { "frames".into() } else { k.into() }, t[k].clone());
    }
    match t["format"].as_str() {
        Some("h264") => {
            m.insert("width".into(), t["geometry"]["width"].clone());
            m.insert("height".into(), t["geometry"]["height"].clone());
            m.insert("profile".into(), t["profile"].clone());
            m.insert("level".into(), t["level"].clone());
            m.insert("sync_samples".into(), json!(arr(&t["sync"]).len()));
        }
        _ => {
            m.insert("sample_rate".into(), t["sample_rate"].clone());
            m.insert("channels".into(), t["channels"].clone());
            m.insert("samples_per_frame".into(), t["spf"].clone());
            m.insert("samples".into(), json!(n(&t["count"]) * n(&t["spf"])));
        }
    }
    m.insert("chunk_frames".into(), json!(chunk));
    J::Object(m)
}

pub fn project(ir: &Ir, facts: &J, url: &str, _copied: &Copied) -> Result<Out, String> {
    let limit = n(&facts["chunk_frames"]).max(1);
    let tracks = arr(&facts["tracks"]);
    // the root (§5.1 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mp4.md#51-the-root>)
    let mut own = Map::new();
    if !facts["brand"].is_null() {
        own.insert("brand".into(), facts["brand"].clone());
    }
    own.insert("timescale".into(), facts["timescale"].clone());
    own.insert("duration".into(), facts["duration"].clone());
    let listed: Vec<J> = tracks.iter().map(|t| {
        let projected = t["reason"].is_null();
        json!({"id": t["id"], "handler": t["handler"], "codec": t["codec"],
               "path": if projected { json!(format!("tracks/{}", t["id"])) } else { J::Null }, "reason": t["reason"]})
    }).collect();
    own.insert("tracks".into(), J::Array(listed));
    let mut out = Out::new(url);
    out.json("zarr.json", &group_doc(declare(Map::new(), "mp4", Some(url), Some(J::Object(own)))));
    out.json("tracks/zarr.json", &group_doc(Map::new()));
    let mut refs = Refs::new(ir, out);
    let mut summary = Vec::new();
    for t in tracks.iter().filter(|t| t["reason"].is_null()) {
        let first = n(&t["first"]) as usize;
        let count = n(&t["count"]) as usize;
        let id = n(&t["id"]);
        let dir = format!("tracks/{id}");
        let format = t["format"].as_str().unwrap_or("");
        let form = ir.form_id(first as u32).ok_or("internal: a sample has no form")?;
        let sample = |j: usize| (ir.start[first + j], ir.len[first + j]);
        let chunks;
        let chunk;
        if format == "h264" {
            // the planes of the pictures, in presentation order (§4.1
            // <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mp4.md#41-video-tracks>)
            let (w, h) = (n(&t["geometry"]["width"]), n(&t["geometry"]["height"]));
            chunk = video_chunk(limit, w, h);
            let pts: Vec<i64> = arr(&t["pts"]).iter().map(|v| v.as_i64().unwrap_or(0)).collect();
            let sync: Vec<u64> = arr(&t["sync"]).iter().map(n).collect();
            let mut order: Vec<usize> = (0..count).collect();
            order.sort_by_key(|&d| pts[d]);
            let mut rank = vec![0usize; count];
            for (p, &d) in order.iter().enumerate() {
                rank[d] = p;
            }
            refs.out.json(&format!("{dir}/zarr.json"), &group_doc(declare(Map::new(), "mp4", None, Some(track_json(t, chunk)))));
            let planes = [("y", w, h), ("u", w.div_ceil(2), h.div_ceil(2)), ("v", w.div_ceil(2), h.div_ceil(2))];
            for (p, pw, ph) in planes {
                let doc = array_json(&[count as u64, ph, pw], "uint8", json!([chunk, ph, pw]), stream_codec("h264", &t["setup"], Some(p)), &["time", "y", "x"]);
                refs.out.json(&format!("{dir}/{p}/zarr.json"), &doc);
            }
            chunks = (count as u64).div_ceil(chunk) as usize;
            for q in 0..chunks {
                let a = q * chunk as usize;
                let b = (a + chunk as usize).min(count);
                let units: Vec<(u64, u64, i64)> = video_units(a, b, &order, &rank, &sync)
                    .into_iter().map(|(d, pos)| { let (o, len) = sample(d); (o, len, pos) }).collect();
                let parts = stream_parts(&units);
                let (o, len) = sample(order[a]);
                refs.chunk(format!("{dir}/y/c/{q}/0/0"), o, len, form, Some(parts.clone()), q as u64)?;
                refs.out.refs(&format!("{dir}/u/c/{q}/0/0"), parts.clone());
                refs.out.refs(&format!("{dir}/v/c/{q}/0/0"), parts);
            }
            // the presentation times, in the track's timescale, delta-free little-endian int64
            // deflated (§4.1 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mp4.md#41-video-tracks>)
            let tc = (count as u64).min(TIME_CHUNK);
            let codecs = json!([{"name": "bytes", "configuration": {"endian": "little"}}, {"name": "gzip", "configuration": {"level": 6}}]);
            refs.out.json(&format!("{dir}/time/zarr.json"), &array_json(&[count as u64], "int64", json!([tc]), codecs, &["time"]));
            for (q, run) in order.chunks(tc as usize).enumerate() {
                let mut raw: Vec<u8> = run.iter().flat_map(|&d| pts[d].to_le_bytes()).collect();
                raw.resize(tc as usize * 8, 0);
                refs.out.bytes(&format!("{dir}/time/c/{q}"), gzip(&raw));
            }
        } else {
            // the samples (§4.2 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mp4.md#42-audio-tracks>)
            let (spf, channels) = (n(&t["spf"]), n(&t["channels"]));
            chunk = limit;
            refs.out.json(&format!("{dir}/zarr.json"), &group_doc(declare(Map::new(), "mp4", None, Some(track_json(t, chunk)))));
            let doc = array_json(&[count as u64 * spf, channels], "float32", json!([chunk * spf, channels]), stream_codec(format, &t["setup"], None), &["time", "channel"]);
            refs.out.json(&format!("{dir}/samples/zarr.json"), &doc);
            let sizes: Vec<u64> = (0..count).map(|j| sample(j).1).collect();
            chunks = (count as u64).div_ceil(chunk) as usize;
            for q in 0..chunks {
                let a = q * chunk as usize;
                let b = (a + chunk as usize).min(count);
                let l = audio_lead(a, format, &sizes);
                let units: Vec<(u64, u64, i64)> = (l..b)
                    .map(|j| { let (o, len) = sample(j); (o, len, if j < a { -1 } else { ((j - a) as u64 * spf) as i64 }) })
                    .collect();
                let (o, len) = sample(a);
                refs.chunk(format!("{dir}/samples/c/{q}/0"), o, len, form, Some(stream_parts(&units)), q as u64)?;
            }
        }
        summary.push(json!({"id": id, "format": format, "path": dir, "frames": count, "chunkFrames": chunk, "chunks": chunks}));
    }
    let mut out = refs.out;
    let skipped: Vec<J> = tracks.iter().filter(|t| !t["reason"].is_null()).map(|t| json!({"id": t["id"], "codec": t["codec"], "reason": t["reason"]})).collect();
    out.summary = json!({
        "brand": facts["brand"]["major"].clone(), "tracks": summary, "skipped": skipped,
    });
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Decode order I0 P3 B1 B2 | I4 P7 B5 B6 (presented 0 to 7), one group a sync sample: a
    /// chunk of the presented frames 2 and 3 decodes I0 P3 B1 B2 and keeps B2 and P3; one of
    /// 3 and 4 decodes the first group whole and I4, keeping P3 and I4; one of 5 decodes the
    /// second group up to B5, dropping I4 and P7.
    #[test]
    fn a_video_chunk_decodes_from_the_sync_sample_before_it() {
        let pts = [0usize, 3, 1, 2, 4, 7, 5, 6];
        let mut order: Vec<usize> = (0..8).collect();
        order.sort_by_key(|&d| pts[d]);
        let rank = pts.to_vec();
        let sync = [0u64, 4];
        assert_eq!(video_units(2, 4, &order, &rank, &sync), vec![(0, -1), (1, 1), (2, -1), (3, 0)]);
        assert_eq!(video_units(3, 5, &order, &rank, &sync), vec![(0, -1), (1, 0), (2, -1), (3, -1), (4, 1)]);
        assert_eq!(video_units(5, 6, &order, &rank, &sync), vec![(4, -1), (5, -1), (6, 0)]);
        assert_eq!(video_chunk(256, 1920, 1080), 16);
        assert_eq!(video_chunk(256, 64, 48), 256);
    }

    /// AAC's lead-in is the frame before the chunk; MP3's holds the reservoir before the two
    /// frames before it.
    #[test]
    fn an_audio_chunk_leads_in_by_its_format() {
        let sizes = vec![144u64; 20];
        assert_eq!(audio_lead(0, "aac", &sizes), 0);
        assert_eq!(audio_lead(8, "aac", &sizes), 7);
        // 106 bytes of main data a frame at least: 5 frames hold 530
        assert_eq!(audio_lead(12, "mp3", &sizes), 5);
    }
}
