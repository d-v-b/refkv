//! The FLAC projection (spec/zarrify/flac.md §4
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/flac.md#4-the-layout>, and the root's
//! source metadata of §5
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/flac.md#5-source-metadata>) from a valid
//! IR: one array of samples, sharded, each frame an inner chunk and each run of frames a shard
//! (§8.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/flac.md#82-shards>; the run
//! shards the array as spec/zarrify.md §2.1
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#21-virtual-shards> says), and the
//! pictures' data as arrays of bytes; from the parser's description of the file (§8.4
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/flac.md#84-the-description>) and refkv's
//! plan of it (`plan`).

use crate::error::Error;

use super::{arr, base64, n, Copied};
use crate::flac::data_type;
use crate::ir::Ir;
use crate::out::{array_json, blob_chunks, bytes_codec, declared_group, group_doc, stream_parts, Chunk, Chunking, Out};
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

/// refkv's plan of the file a description describes (spec/zarrify/flac.md §4.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/flac.md#41-the-array>, §8.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/flac.md#82-shards>): `shard_frames`,
/// the frames of a shard (`chunking`'s, 512, or every frame when there are fewer), and `data_type`, the
/// samples' Zarr data type, the smallest signed integer that holds STREAMINFO's bits. The plan is
/// public data that belongs to refkv's convention, not to the IR.
pub fn plan(description: &J, chunking: &Chunking) -> crate::error::Res<J> {
    let count = n(&description["frames"]) as usize;
    if count == 0 {
        return Err(Error::internal("the description has no frames"));
    }
    let bits = n(&description["streaminfo"]["bits_per_sample"]);
    Ok(json!({"shard_frames": chunking.shard_frames.max(1).min(count), "data_type": data_type(bits)}))
}

pub fn project(ir: &Ir, description: &J, url: &str, _copied: &Copied, chunking: &Chunking) -> crate::error::Res<Out> {
    let d = description;
    let plan = plan(d, chunking)?;
    let per = n(&plan["shard_frames"]) as usize;
    let first = n(&d["elements"]["frames"]) as usize;
    let count = n(&d["frames"]) as usize;
    let (block, samples) = (n(&d["block_size"]), n(&d["samples"]));
    let si = &d["streaminfo"];
    let (rate, channels, bits) = (n(&si["sample_rate"]), n(&si["channels"]), n(&si["bits_per_sample"]));
    if first + count > ir.start.len() {
        return Err(Error::internal("the description does not describe the frames"));
    }
    let pictures = arr(&d["pictures"]);
    let elements = arr(&d["elements"]["pictures"]);

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
    own.insert("blocks".into(), d["blocks"].clone());
    for k in ["seek_table", "vorbis_comment"] {
        if !d[k].is_null() {
            own.insert(k.into(), d[k].clone());
        }
    }
    for k in ["cuesheets", "pictures", "applications", "id3v2"] {
        if !arr(&d[k]).is_empty() {
            own.insert(k.into(), d[k].clone());
        }
    }
    if !d["id3v1"].is_null() {
        own.insert("id3v1".into(), d["id3v1"].clone());
    }
    let mut out = Out::new(url);
    out.json("zarr.json", &declared_group(Map::new(), "flac", Some(url), Some(J::Object(own))));

    // the array (§4.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/flac.md#41-the-array>)
    let streaminfo = ir.value_bytes(n(&d["elements"]["streaminfo"]) as u32).ok_or_else(|| Error::internal("STREAMINFO is not held"))?;
    let dtype = plan["data_type"].as_str().unwrap_or("int32");
    let codecs = json!([{"name": "refkv.stream", "configuration": {"format": "flac", "setup": base64(&setup(streaminfo))}}]);
    let array = array_json(&[samples, channels], dtype, json!([block, channels]), codecs, &["time", "channel"]);
    out.json(&format!("{ARRAY}/zarr.json"), &array);
    let form = ir.form_id(first as u32).ok_or_else(|| Error::internal("a frame has no form"))?;
    let mut refs = Refs::new(ir, out);
    // each frame an inner chunk, a refkv.stream chunk of one frame; `per` frames a shard
    // (spec/zarrify.md §2.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#21-virtual-shards>)
    for j in 0..count {
        let (o, len) = (ir.start[first + j], ir.len[first + j]);
        refs.chunk(format!("{ARRAY}/c/{j}/0"), o, len, form, Some(stream_parts(&[(o, len, 0)])))?;
    }
    refs.out.shard(ARRAY, &[per as u64, 1]);
    let shards = count.div_ceil(per);

    // the pictures (§4.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/flac.md#42-the-pictures>)
    if elements.iter().any(J::is_u64) {
        refs.out.json(&format!("{PICTURES}/zarr.json"), &group_doc(Map::new()));
    }
    for (i, e) in elements.iter().enumerate() {
        // a picture of no data has no array (conventions §7
        // <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#7-source-values-as-arrays>:
        // bytes are at least 1)
        let Some(e) = e.as_u64() else { continue };
        let (start, size) = (ir.start[e as usize], ir.len[e as usize]);
        let (chunk, chunks) = blob_chunks(start, size);
        let path = format!("{PICTURES}/{i}");
        refs.out.json(&format!("{path}/zarr.json"), &array_json(&[size], "uint8", json!([chunk]), json!([bytes_codec(1, "little")]), &["byte"]));
        for (c, k) in chunks {
            if let Chunk::Ranges(parts) = k {
                // its own ranges, so no form's recipe is read (u32::MAX: none)
                refs.chunk(format!("{path}/c/{}", c[0]), start, size, u32::MAX, Some(parts))?;
            }
        }
    }
    let mut out = refs.out;
    let mut tags = Vec::new();
    if !arr(&d["id3v2"]).is_empty() {
        tags.push("id3v2");
    }
    if !d["id3v1"].is_null() {
        tags.push("id3v1");
    }
    out.summary = json!({
        "sampleRate": rate, "channels": channels, "bitsPerSample": bits, "blockSize": block, "frames": count,
        "samples": samples, "duration": if rate > 0 { json!(samples as f64 / rate as f64) } else { J::Null },
        "shardFrames": per, "shards": shards, "pictures": pictures.len(), "tags": tags,
    });
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::plan;
    use crate::flac::DEFAULT_SHARD_FRAMES;
    use crate::out::Chunking;
    use serde_json::json;

    /// A shard holds 512 frames, or every frame when there are fewer; the data type is the smallest
    /// signed integer that holds the samples' bits.
    #[test]
    fn the_plan_sizes_shards_and_picks_the_data_type() {
        let d = |frames, bits| json!({"frames": frames, "streaminfo": {"bits_per_sample": bits}});
        let cases = [(1000, 16, DEFAULT_SHARD_FRAMES, "int16"), (7, 8, 7, "int8"), (600, 12, DEFAULT_SHARD_FRAMES, "int16"),
                     (3, 24, 3, "int32"), (512, 32, 512, "int32")];
        for (frames, bits, shard, dtype) in cases {
            assert_eq!(plan(&d(frames, bits), &Chunking::default()).unwrap(), json!({"shard_frames": shard, "data_type": dtype}), "{frames} {bits}");
        }
    }

    #[test]
    fn a_description_of_no_frames_has_no_plan() {
        let d = json!({"frames": 0, "streaminfo": {"bits_per_sample": 16}});
        assert!(plan(&d, &Chunking::default()).unwrap_err().to_string().contains("no frames"));
    }
}
