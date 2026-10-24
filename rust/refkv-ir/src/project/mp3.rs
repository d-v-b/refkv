//! The MP3 projection (spec/virtualize/mp3.md §4
//! <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mp3.md#4-the-layout>, and the root's
//! source metadata of §5
//! <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mp3.md#5-source-metadata>) from a valid
//! IR: one array of samples whose chunks are runs of frames, each referenced with the frames
//! before it that a decoder needs (the lead-in, §8.2
//! <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mp3.md#82-chunks>).

use super::{arr, audio, json_text, n, Copied};
use crate::ir::Ir;
use crate::mp3::{MODES, RESERVOIR, VERSIONS};
use crate::out::{array_json, declare, group_doc, Out};
use crate::refs::Refs;
use serde_json::{json, Map, Value as J};

/// The array's path.
pub const ARRAY: &str = "samples";

/// The ID3v1 tag's fields as the root's source metadata writes them (§5.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mp3.md#51-the-root>).
fn id3v1_json(b: &[u8]) -> J {
    let mut m = Map::new();
    m.insert("title".into(), json_text(&b[3..33]));
    m.insert("artist".into(), json_text(&b[33..63]));
    m.insert("album".into(), json_text(&b[63..93]));
    m.insert("year".into(), json_text(&b[93..97]));
    m.insert("comment".into(), json_text(&b[97..127]));
    // ID3v1.1: a NUL at byte 125 makes byte 126 the track number
    m.insert("track".into(), if b[125] == 0 && b[126] != 0 { json!(b[126]) } else { J::Null });
    m.insert("genre".into(), json!(b[127]));
    J::Object(m)
}

pub fn project(ir: &Ir, facts: &J, url: &str, _copied: &Copied) -> Result<Out, String> {
    let first = n(&facts["frames"]) as usize;
    let count = n(&facts["count"]) as usize;
    let (spf, channels, rate) = (n(&facts["spf"]), n(&facts["channels"]), n(&facts["sample_rate"]));
    let version = n(&facts["version"]) as usize;
    let main: Vec<u64> = arr(&facts["main"]).iter().map(n).collect();
    let per_chunk = n(&facts["chunk_frames"]).max(1) as usize;
    if count == 0 || main.len() != count {
        return Err("internal: the facts do not describe the frames".into());
    }
    let samples = count as u64 * spf;

    // the root (§5.1 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mp3.md#51-the-root>)
    let mut own = Map::new();
    own.insert("version".into(), json!(VERSIONS[version]));
    own.insert("layer".into(), json!(3));
    own.insert("sample_rate".into(), json!(rate));
    own.insert("channels".into(), json!(channels));
    own.insert("channel_mode".into(), json!(MODES[n(&facts["mode"]) as usize]));
    own.insert("samples_per_frame".into(), json!(spf));
    own.insert("frames".into(), json!(count));
    own.insert("samples".into(), json!(samples));
    own.insert("duration".into(), json!(samples as f64 / rate as f64));
    let kbps = &facts["kbps"];
    own.insert("bitrate_mode".into(), json!(if kbps.is_null() { "variable" } else { "constant" }));
    own.insert("bitrate_kbps".into(), kbps.clone());
    own.insert("crc_frames".into(), facts["crc_frames"].clone());
    if !facts["info"].is_null() {
        own.insert("info".into(), facts["info"].clone());
    }
    if arr(&facts["id3v2"]).first().is_some() {
        own.insert("id3v2".into(), facts["id3v2"].clone());
    }
    if let Some(i) = facts["id3v1"].as_u64() {
        let b = ir.value_bytes(i as u32).ok_or("internal: the ID3v1 tag is not held")?;
        own.insert("id3v1".into(), id3v1_json(b));
    }
    if !facts["ape"].is_null() {
        own.insert("ape".into(), facts["ape"].clone());
    }
    own.insert("stray_bytes".into(), facts["stray"].clone());
    let mut out = Out::new(url);
    out.json("zarr.json", &group_doc(declare(Map::new(), "mp3", Some(url), Some(J::Object(own)))));

    // the array (§4.1 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mp3.md#41-the-array>)
    let shape = [samples, channels];
    let array = array_json(&shape, "float32", json!([per_chunk as u64 * spf, channels]), json!([{"name": "refkv.stream", "configuration": {"format": "mp3"}}]), &["time", "channel"]);
    out.json(&format!("{ARRAY}/zarr.json"), &array);
    let form = ir.form_id(first as u32).ok_or("internal: a frame has no form")?;
    let mut refs = Refs::new(ir, out);
    let chunks = audio::chunks(&mut refs, ir, ARRAY, first, count, per_chunk, spf, form, |a| {
        // the lead-in holds the version's reservoir of main data (§8.2
        // <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mp3.md#82-chunks>), by exact sizes
        audio::reservoir_lead(a, RESERVOIR[version], |l| main[l])
    })?;
    let mut out = refs.out;
    let mut tags = Vec::new();
    if arr(&facts["id3v2"]).first().is_some() {
        tags.push("id3v2");
    }
    if !facts["id3v1"].is_null() {
        tags.push("id3v1");
    }
    if !facts["ape"].is_null() {
        tags.push("ape");
    }
    out.summary = json!({
        "version": VERSIONS[version], "sampleRate": rate, "channels": channels, "frames": count, "samples": samples,
        "duration": samples as f64 / rate as f64, "bitrate": if kbps.is_null() { json!("variable") } else { kbps.clone() },
        "chunkFrames": per_chunk, "chunks": chunks, "info": facts["info"]["tag"].clone(), "tags": tags,
        "strayBytes": facts["stray"].clone(),
    });
    Ok(out)
}
