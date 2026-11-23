//! The MP3 projection (spec/zarrify/mp3.md §4
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mp3.md#4-the-layout>, and the root's
//! source metadata of §5
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mp3.md#5-source-metadata>) from a valid
//! IR and the parser's description of the file (§8.4
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mp3.md#84-the-description>): one array
//! of samples whose chunks are runs of frames, each referenced with the frames before it that a
//! decoder needs (the lead-in, §8.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mp3.md#82-chunks>),
//! as refkv's plan of the file (`plan`) cuts them.

use super::{arr, audio, n, Copied};
use crate::ir::Ir;
use crate::mp3::{RESERVOIR, VERSIONS};
use crate::out::{array_json, declared_group, Chunking, Out};
use crate::refs::Refs;
use serde_json::{json, Map, Value as J};

/// The array's path.
pub const ARRAY: &str = "samples";

/// refkv's plan of the file a description describes (spec/zarrify/mp3.md §8.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mp3.md#82-chunks>): `chunk_frames`, the
/// audio frames of a chunk (`chunking`'s), and `leads`, the first frame of each chunk's lead-in, which holds the
/// version's bit reservoir of main data before the two frames before the chunk. The plan is public
/// data that belongs to refkv's convention, not to the IR.
pub fn plan(description: &J, chunking: &Chunking) -> Result<J, String> {
    let count = n(&description["frames"]) as usize;
    let main: Vec<u64> = arr(&description["main_data"]).iter().map(n).collect();
    let version = VERSIONS.iter().position(|v| description["version"] == *v).ok_or("internal: the description has no version")?;
    if count == 0 || main.len() != count {
        return Err("internal: the description does not describe the frames".into());
    }
    let per = chunking.chunk_frames.max(1);
    let leads: Vec<usize> = (0..count).step_by(per).map(|a| audio::reservoir_lead(a, RESERVOIR[version], |l| main[l])).collect();
    Ok(json!({"chunk_frames": per, "leads": leads}))
}

pub fn project(ir: &Ir, description: &J, url: &str, _copied: &Copied, chunking: &Chunking) -> Result<Out, String> {
    let d = description;
    let plan = plan(d, chunking)?;
    let per_chunk = n(&plan["chunk_frames"]) as usize;
    let leads: Vec<usize> = arr(&plan["leads"]).iter().map(|l| n(l) as usize).collect();
    let first = n(&d["elements"]["frames"]) as usize;
    let count = n(&d["frames"]) as usize;
    let (spf, channels, rate) = (n(&d["samples_per_frame"]), n(&d["channels"]), n(&d["sample_rate"]));
    let samples = count as u64 * spf;
    // a constant bitrate: every audio frame's the first's
    let kbps = arr(&d["kbps"]);
    let constant = kbps.windows(2).all(|w| w[0] == w[1]);
    let bitrate = if constant { kbps[0].clone() } else { J::Null };

    // the root (§5.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mp3.md#51-the-root>)
    let mut own = Map::new();
    for k in ["version", "layer", "sample_rate", "channels", "channel_mode", "samples_per_frame", "frames"] {
        own.insert(k.into(), d[k].clone());
    }
    own.insert("samples".into(), json!(samples));
    own.insert("duration".into(), json!(samples as f64 / rate as f64));
    own.insert("bitrate_mode".into(), json!(if constant { "constant" } else { "variable" }));
    own.insert("bitrate_kbps".into(), bitrate.clone());
    own.insert("crc_frames".into(), d["crc_frames"].clone());
    let has_id3v2 = !arr(&d["id3v2"]).is_empty();
    for (k, present) in [("info", !d["info"].is_null()), ("id3v2", has_id3v2), ("id3v1", !d["id3v1"].is_null()), ("ape", !d["ape"].is_null())] {
        if present {
            own.insert(k.into(), d[k].clone());
        }
    }
    own.insert("stray_bytes".into(), d["stray_bytes"].clone());
    let mut out = Out::new(url);
    out.json("zarr.json", &declared_group(Map::new(), "mp3", Some(url), Some(J::Object(own))));

    // the array (§4.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mp3.md#41-the-array>)
    let shape = [samples, channels];
    let array = array_json(&shape, "float32", json!([per_chunk as u64 * spf, channels]), json!([{"name": "refkv.stream", "configuration": {"format": "mp3"}}]), &["time", "channel"]);
    out.json(&format!("{ARRAY}/zarr.json"), &array);
    let form = ir.form_id(first as u32).ok_or("internal: a frame has no form")?;
    let mut refs = Refs::new(ir, out);
    let chunks = audio::chunks(&mut refs, ir, ARRAY, first, count, per_chunk, spf, form, |a| leads[a / per_chunk])?;
    let mut out = refs.out;
    let tags: Vec<&str> = [("id3v2", has_id3v2), ("id3v1", !d["id3v1"].is_null()), ("ape", !d["ape"].is_null())]
        .into_iter()
        .filter_map(|(k, present)| present.then_some(k))
        .collect();
    out.summary = json!({
        "version": d["version"], "sampleRate": rate, "channels": channels, "frames": count, "samples": samples,
        "duration": samples as f64 / rate as f64, "bitrate": if constant { bitrate } else { json!("variable") },
        "chunkFrames": per_chunk, "chunks": chunks, "info": d["info"]["tag"].clone(), "tags": tags,
        "strayBytes": d["stray_bytes"].clone(),
    });
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::plan;
    use crate::mp3::DEFAULT_CHUNK_FRAMES;
    use crate::out::Chunking;
    use serde_json::json;

    /// 600 MPEG-1 frames of 100 bytes of main data in chunks of 256: three chunks, the first with
    /// no lead-in, the others with the two frames before them and the six that hold 511 bytes;
    /// MPEG-2's reservoir of 255 takes three.
    #[test]
    fn the_plan_cuts_chunks_and_their_lead_ins() {
        let d = |version| json!({"version": version, "frames": 600, "main_data": vec![100; 600]});
        assert_eq!(plan(&d("MPEG-1"), &Chunking::default()).unwrap(), json!({"chunk_frames": DEFAULT_CHUNK_FRAMES, "leads": [0, 248, 504]}));
        assert_eq!(plan(&d("MPEG-2"), &Chunking::default()).unwrap()["leads"], json!([0, 251, 507]));
    }

    #[test]
    fn a_description_without_its_frames_main_data_has_no_plan() {
        let d = json!({"version": "MPEG-1", "frames": 3, "main_data": [100, 100]});
        assert!(plan(&d, &Chunking::default()).unwrap_err().contains("does not describe the frames"));
    }
}
