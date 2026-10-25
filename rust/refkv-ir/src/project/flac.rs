//! The FLAC projection (spec/zarrify/flac.md §4
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/flac.md#4-the-layout>, and the root's
//! source metadata of §5
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/flac.md#5-source-metadata>) from a valid
//! IR: one array of samples, sharded, each frame an inner chunk and each run of frames a shard
//! (§8.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/flac.md#82-shards>; the run
//! shards the array as spec/zarrify.md §2.1
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#21-virtual-shards> says), and the
//! pictures' data as arrays of bytes.

use super::{arr, base64, n, Copied};
use crate::flac::data_type;
use crate::ir::Ir;
use crate::out::{array_json, blob_chunks, bytes_codec, declared_group, group_doc, stream_parts, Chunk, Out};
use crate::refs::Refs;
use serde_json::{json, Map, Value as J};

/// The array's path.
pub const ARRAY: &str = "samples";
/// The group of the pictures' arrays.
pub const PICTURES: &str = "pictures";

/// The `refkv.stream` setup of a stream: `fLaC`, then its STREAMINFO block (the last-block flag
/// set), as a decoder's configuration (RFC 9639 §10.1; WebCodecs' `description`).
pub fn setup(streaminfo: &[u8]) -> Vec<u8> {
    let mut b = b"fLaC\x80\x00\x00\x22".to_vec();
    b.extend_from_slice(streaminfo);
    b
}

pub fn project(ir: &Ir, facts: &J, url: &str, _copied: &Copied) -> Result<Out, String> {
    let first = n(&facts["frames"]) as usize;
    let count = n(&facts["count"]) as usize;
    let block = n(&facts["block"]);
    let samples = n(&facts["samples"]);
    let si = &facts["streaminfo"];
    let (rate, channels, bits) = (n(&si["sample_rate"]), n(&si["channels"]), n(&si["bits_per_sample"]));
    // a shard of `shard_frames` frames, or of every frame when there are fewer
    let per = (n(&facts["shard_frames"]).max(1) as usize).min(count.max(1));
    if count == 0 || first + count > ir.start.len() {
        return Err("internal: the facts do not describe the frames".into());
    }

    // the root (§5.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/flac.md#51-the-root>)
    let mut own = Map::new();
    own.insert("sample_rate".into(), json!(rate));
    own.insert("channels".into(), json!(channels));
    own.insert("bits_per_sample".into(), json!(bits));
    own.insert("block_size".into(), json!(block));
    own.insert("frames".into(), json!(count));
    own.insert("samples".into(), json!(samples));
    own.insert("duration".into(), if rate > 0 { json!(samples as f64 / rate as f64) } else { J::Null });
    own.insert("streaminfo".into(), si.clone());
    own.insert("blocks".into(), facts["blocks"].clone());
    for k in ["seek_table", "vorbis_comment"] {
        if !facts[k].is_null() {
            own.insert(k.into(), facts[k].clone());
        }
    }
    if !arr(&facts["cuesheets"]).is_empty() {
        own.insert("cuesheets".into(), facts["cuesheets"].clone());
    }
    let pictures = arr(&facts["pictures"]);
    if !pictures.is_empty() {
        let p: Vec<J> = pictures.iter().map(|p| {
            let mut p = p.clone();
            if let Some(m) = p.as_object_mut() {
                m.remove("element");
            }
            p
        }).collect();
        own.insert("pictures".into(), J::Array(p));
    }
    if !arr(&facts["applications"]).is_empty() {
        own.insert("applications".into(), facts["applications"].clone());
    }
    if !arr(&facts["id3v2"]).is_empty() {
        own.insert("id3v2".into(), facts["id3v2"].clone());
    }
    if let Some(i) = facts["id3v1"].as_u64() {
        let b = ir.value_bytes(i as u32).ok_or("internal: the ID3v1 tag is not held")?;
        own.insert("id3v1".into(), super::mp3::id3v1_json(b));
    }
    let mut out = Out::new(url);
    out.json("zarr.json", &declared_group(Map::new(), "flac", Some(url), Some(J::Object(own))));

    // the array (§4.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/flac.md#41-the-array>)
    let streaminfo = ir.value_bytes(n(&facts["info_value"]) as u32).ok_or("internal: STREAMINFO is not held")?;
    let dtype = data_type(bits);
    let codecs = json!([{"name": "refkv.stream", "configuration": {"format": "flac", "setup": base64(&setup(streaminfo))}}]);
    let array = array_json(&[samples, channels], dtype, json!([block, channels]), codecs, &["time", "channel"]);
    out.json(&format!("{ARRAY}/zarr.json"), &array);
    let form = ir.form_id(first as u32).ok_or("internal: a frame has no form")?;
    let mut refs = Refs::new(ir, out);
    // each frame an inner chunk, a refkv.stream chunk of one frame; `per` frames a shard
    // (spec/zarrify.md §2.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#21-virtual-shards>)
    for j in 0..count {
        let (o, len) = (ir.start[first + j], ir.len[first + j]);
        refs.chunk(format!("{ARRAY}/c/{j}/0"), o, len, form, Some(stream_parts(&[(o, len, 0)])), j as u64)?;
    }
    refs.out.shard(ARRAY, &[per as u64, 1]);
    let shards = count.div_ceil(per);

    // the pictures (§4.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/flac.md#42-the-pictures>)
    if pictures.iter().any(|p| p["element"].is_u64()) {
        refs.out.json(&format!("{PICTURES}/zarr.json"), &group_doc(Map::new()));
    }
    for (i, p) in pictures.iter().enumerate() {
        // a picture of no data has no array (conventions §7: bytes are at least 1)
        let Some(e) = p["element"].as_u64() else { continue };
        let (start, size) = (ir.start[e as usize], ir.len[e as usize]);
        let (chunk, chunks) = blob_chunks(start, size);
        let path = format!("{PICTURES}/{i}");
        refs.out.json(&format!("{path}/zarr.json"), &array_json(&[size], "uint8", json!([chunk]), json!([bytes_codec(1, "little")]), &["byte"]));
        for (c, k) in chunks {
            if let Chunk::Ranges(parts) = k {
                // its own ranges, so no form's recipe is read (u32::MAX: none)
                refs.chunk(format!("{path}/c/{}", c[0]), start, size, u32::MAX, Some(parts), c[0])?;
            }
        }
    }
    let mut out = refs.out;
    let mut tags = Vec::new();
    if !arr(&facts["id3v2"]).is_empty() {
        tags.push("id3v2");
    }
    if !facts["id3v1"].is_null() {
        tags.push("id3v1");
    }
    out.summary = json!({
        "sampleRate": rate, "channels": channels, "bitsPerSample": bits, "blockSize": block, "frames": count,
        "samples": samples, "duration": if rate > 0 { json!(samples as f64 / rate as f64) } else { J::Null },
        "shardFrames": per, "shards": shards, "pictures": pictures.len(), "tags": tags,
    });
    Ok(out)
}
