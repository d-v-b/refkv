//! The MP4 projection (spec/zarrify/mp4.md §4
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mp4.md#4-the-layout>, and the source
//! metadata of §5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mp4.md#5-source-metadata>)
//! from a valid IR: a group per track the profile zarrifies, with the planes of an H.264 or
//! HEVC track's pictures and their presentation times, or an audio track's samples, each chunk a
//! `refkv.stream` chunk of the track's samples (spec/zarrify/mp4.md §8.3
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mp4.md#83-chunks>).

use super::{arr, audio, n, Copied};
use crate::ir::Ir;
use crate::out::{array_json, declared_group, group_doc, gzip_array, rectilinear_array_json, stream_parts, Out};
use crate::refs::Refs;
use serde_json::{json, Map, Value as J};

/// The most bytes of luma a video chunk decodes to (§4.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mp4.md#41-video-tracks>), and the most
/// values of a chunk of the time array.
const CHUNK_LUMA: u64 = 1 << 25;
const TIME_CHUNK: u64 = 1 << 17;
/// The bytes of main data an MP3 frame holds at least, by its length (its header, CRC and
/// side information are at most 38 bytes), and the reservoir its lead-in holds (MPEG-1's).
const MP3_OVERHEAD: u64 = 38;
const MP3_RESERVOIR: u64 = 511;

/// The most pictures of a video chunk: the profile's limit, and at most `CHUNK_LUMA` bytes of
/// luma, `bytes` a sample.
pub fn video_chunk(limit: u64, width: u64, height: u64, bytes: u64) -> u64 {
    limit.min((CHUNK_LUMA / (width * height * bytes).max(1)).max(1))
}

/// The lengths of the video chunks of `count` presented pictures, at most `per` each, the
/// sync samples presented at `sync` (ascending, from 0) (§4.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mp4.md#41-video-tracks>): from picture
/// `a`, a chunk runs to the end when at most `per` pictures are left; else to the last sync
/// sample after `a` and at most `per` pictures on, so that it holds whole groups of pictures
/// and the next starts with a sync sample; else `per` pictures, splitting a group longer than
/// `per`.
pub fn video_lengths(count: usize, per: usize, sync: &[usize]) -> Vec<u64> {
    let mut lengths = Vec::new();
    let mut a = 0;
    while a < count {
        let b = if count - a <= per {
            count
        } else {
            match sync[..sync.partition_point(|&s| s <= a + per)].last() {
                Some(&s) if s > a => s,
                _ => a + per,
            }
        };
        lengths.push((b - a) as u64);
        a = b;
    }
    lengths
}

/// The chunk lengths of a video plane's time axis as its grid gives them: the integer `L` when
/// every chunk but the last is `L` pictures and the last at most `L` (a regular grid), else the
/// zarr-extensions `rectilinear` grid's list, each run of `r > 1` equal lengths `L` as `[L, r]`.
pub fn grid_lengths(lengths: &[u64]) -> J {
    let first = lengths.first().copied().unwrap_or(1);
    let (last, rest) = lengths.split_last().unwrap_or((&first, &[]));
    if rest.iter().all(|&l| l == first) && *last <= first {
        return json!(first);
    }
    let mut runs: Vec<(u64, u64)> = Vec::new();
    for &l in lengths {
        match runs.last_mut() {
            Some((v, r)) if *v == l => *r += 1,
            _ => runs.push((l, 1)),
        }
    }
    J::Array(runs.into_iter().map(|(v, r)| if r > 1 { json!([v, r]) } else { json!(v) }).collect())
}

/// The samples of a video chunk of the presented frames `[a, b)` (§8.3
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mp4.md#83-chunks>): in decode order,
/// from the last sync sample at or before the first of them in decode order (or, while the
/// chunk holds a leading picture of that sync sample, the one before it) to the last of
/// them, each with its position (its presentation index less `a`, or -1 when it is not in
/// the chunk). `order` is the decode index of each presented frame, `rank` the presentation
/// index of each decode index, `sync` the sync samples.
pub fn video_units(a: usize, b: usize, order: &[usize], rank: &[usize], sync: &[u64]) -> Vec<(usize, i64)> {
    let lo = order[a..b].iter().copied().min().unwrap_or(0);
    let hi = order[a..b].iter().copied().max().unwrap_or(0);
    // the last sync sample at or before the first of them in decode order; while the chunk
    // holds a leading picture of it (decoded after it, presented before it), which may
    // reference the group before (an open group), the sync sample before that; that one's
    // own leading pictures are decoded before this one (`mp4::gops`), so it walks back once
    let mut k = sync.partition_point(|&x| x as usize <= lo).saturating_sub(1);
    let leads = |s: usize| order[a..b].iter().any(|&d| d > s && rank[d] < rank[s]);
    while k > 0 && leads(sync[k] as usize) {
        k -= 1;
    }
    let s = sync.get(k).map_or(0, |&x| x as usize);
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
    audio::reservoir_lead(a, MP3_RESERVOIR, |l| sizes[l].saturating_sub(MP3_OVERHEAD))
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
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mp4.md#52-the-tracks>).
fn track_json(t: &J, chunk: u64) -> J {
    let mut m = Map::new();
    for k in ["id", "handler", "codec", "format", "timescale", "duration", "edits", "count"] {
        m.insert(if k == "count" { "frames".into() } else { k.into() }, t[k].clone());
    }
    match t["format"].as_str() {
        Some("h264" | "hevc") => {
            m.insert("width".into(), t["geometry"]["width"].clone());
            m.insert("height".into(), t["geometry"]["height"].clone());
            m.insert("bit_depth".into(), t["bit_depth"].clone());
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
    // the root (§5.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mp4.md#51-the-root>)
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
    out.json("zarr.json", &declared_group(Map::new(), "mp4", Some(url), Some(J::Object(own))));
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
        if format == "h264" || format == "hevc" {
            // the planes of the pictures, in presentation order (§4.1
            // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mp4.md#41-video-tracks>):
            // uint8 for 8 bits a sample, uint16 for more
            let (w, h) = (n(&t["geometry"]["width"]), n(&t["geometry"]["height"]));
            let data_type = t["geometry"]["data_type"].as_str().unwrap_or("uint8");
            chunk = video_chunk(limit, w, h, if data_type == "uint16" { 2 } else { 1 });
            let pts: Vec<i64> = arr(&t["pts"]).iter().map(|v| v.as_i64().unwrap_or(0)).collect();
            let sync: Vec<u64> = arr(&t["sync"]).iter().map(n).collect();
            let mut order: Vec<usize> = (0..count).collect();
            order.sort_by_key(|&d| pts[d]);
            let mut rank = vec![0usize; count];
            for (p, &d) in order.iter().enumerate() {
                rank[d] = p;
            }
            // the chunks: runs of whole groups of pictures, a group longer than a chunk split
            let mut presented: Vec<usize> = sync.iter().map(|&d| rank[d as usize]).collect();
            presented.sort_unstable();
            let lengths = video_lengths(count, chunk as usize, &presented);
            let along = grid_lengths(&lengths);
            refs.out.json(&format!("{dir}/zarr.json"), &declared_group(Map::new(), "mp4", None, Some(track_json(t, chunk))));
            let planes = [("y", w, h), ("u", w.div_ceil(2), h.div_ceil(2)), ("v", w.div_ceil(2), h.div_ceil(2))];
            for (p, pw, ph) in planes {
                let doc = rectilinear_array_json(&[count as u64, ph, pw], data_type, &json!([along, ph, pw]), stream_codec(format, &t["setup"], Some(p)), &["time", "y", "x"]);
                refs.out.json(&format!("{dir}/{p}/zarr.json"), &doc);
            }
            chunks = lengths.len();
            let mut b = 0;
            for (q, &len) in lengths.iter().enumerate() {
                let a = b;
                b = a + len as usize;
                let units: Vec<(u64, u64, i64)> = video_units(a, b, &order, &rank, &sync)
                    .into_iter().map(|(d, pos)| { let (o, len) = sample(d); (o, len, pos) }).collect();
                let parts = stream_parts(&units);
                let (o, len) = sample(order[a]);
                refs.chunk(format!("{dir}/y/c/{q}/0/0"), o, len, form, Some(parts.clone()), q as u64)?;
                refs.out.refs(&format!("{dir}/u/c/{q}/0/0"), parts.clone());
                refs.out.refs(&format!("{dir}/v/c/{q}/0/0"), parts);
            }
            // the presentation times, in the track's timescale, delta-free little-endian int64
            // deflated (§4.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mp4.md#41-video-tracks>)
            gzip_array(&mut refs.out, &format!("{dir}/time"), "int64", count as u64, TIME_CHUNK, "time", vec![], json!({}), |q, c| {
                let (from, to) = ((q * c) as usize, (((q + 1) * c) as usize).min(count));
                let mut raw: Vec<u8> = order[from..to].iter().flat_map(|&d| pts[d].to_le_bytes()).collect();
                raw.resize(c as usize * 8, 0);
                raw
            });
        } else {
            // the samples (§4.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mp4.md#42-audio-tracks>)
            let (spf, channels) = (n(&t["spf"]), n(&t["channels"]));
            chunk = limit;
            refs.out.json(&format!("{dir}/zarr.json"), &declared_group(Map::new(), "mp4", None, Some(track_json(t, chunk))));
            let doc = array_json(&[count as u64 * spf, channels], "float32", json!([chunk * spf, channels]), stream_codec(format, &t["setup"], None), &["time", "channel"]);
            refs.out.json(&format!("{dir}/samples/zarr.json"), &doc);
            let sizes: Vec<u64> = (0..count).map(|j| sample(j).1).collect();
            chunks = audio::chunks(&mut refs, ir, &format!("{dir}/samples"), first, count, chunk as usize, spf, form, |a| audio_lead(a, format, &sizes))?;
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
        // an open group: I0 P3 B1 B2 | I6 B4 B5 P9 B7 B8 in decode order, B4 and B5 leading
        // pictures of I6 that may reference P3: a chunk of 4 and 5 decodes from I0; one of 6
        // to 8 from I6, dropping B4, B5 and P9
        let pts = [0usize, 3, 1, 2, 6, 4, 5, 9, 7, 8];
        let mut order: Vec<usize> = (0..10).collect();
        order.sort_by_key(|&d| pts[d]);
        let (rank, sync) = (pts.to_vec(), [0u64, 4]);
        let all: Vec<usize> = video_units(4, 6, &order, &rank, &sync).iter().map(|u| u.0).collect();
        assert_eq!(all, (0..=6).collect::<Vec<_>>());
        assert_eq!(video_units(6, 9, &order, &rank, &sync), vec![(4, 0), (5, -1), (6, -1), (7, -1), (8, 1), (9, 2)]);
        assert_eq!(video_chunk(256, 1920, 1080, 1), 16);
        assert_eq!(video_chunk(256, 1920, 1080, 2), 8);
        assert_eq!(video_chunk(256, 64, 48, 1), 256);
    }

    /// Video chunks hold whole groups of pictures up to `per`, splitting a longer group, and the
    /// grid is regular when every chunk but a shorter last one has one length.
    #[test]
    fn video_chunks_start_at_sync_samples() {
        // (pictures, per, sync samples presented at) -> lengths
        let cases: [(usize, usize, &[usize], &[u64]); 7] = [
            // the whole track fits one chunk, whatever its groups
            (30, 256, &[0, 10, 20], &[30]),
            // groups of 10 packed to 25: two groups a chunk, then the rest
            (60, 25, &[0, 10, 20, 30, 40, 50], &[20, 20, 20]),
            // groups of 10 in chunks of 7: each group split, its second piece short
            (30, 7, &[0, 10, 20], &[7, 3, 7, 3, 7, 3]),
            // irregular groups
            (60, 7, &[0, 7, 9, 23, 40], &[7, 2, 7, 7, 7, 7, 3, 7, 7, 6]),
            // one group of 300 in chunks of 256: split, the rest one chunk
            (300, 256, &[0], &[256, 44]),
            // every picture a sync sample
            (15, 7, &(0..15).collect::<Vec<_>>(), &[7, 7, 1]),
            // groups of 50 in chunks of 256: five groups, then the rest
            (400, 256, &[0, 50, 100, 150, 200, 250, 300, 350], &[250, 150]),
        ];
        for (count, per, sync, want) in cases {
            assert_eq!(video_lengths(count, per, sync), want, "{count} {per} {sync:?}");
        }
        assert_eq!(grid_lengths(&[20, 20, 20]), json!(20));
        assert_eq!(grid_lengths(&[250, 150]), json!(250));
        assert_eq!(grid_lengths(&[30]), json!(30));
        assert_eq!(grid_lengths(&[7, 3, 7, 3]), json!([7, 3, 7, 3]));
        assert_eq!(grid_lengths(&[7, 2, 7, 7, 7, 7, 3, 7, 7, 6]), json!([7, 2, [7, 4], 3, [7, 2], 6]));
        assert_eq!(grid_lengths(&[5, 9]), json!([5, 9]));
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
