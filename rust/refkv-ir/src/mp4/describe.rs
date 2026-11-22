//! The description of an MP4 or QuickTime file (spec/zarrify/mp4.md §8.5
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mp4.md#85-the-description>): what the
//! parser found of each track, its sample entry and the codec configuration it holds as the
//! codecs' own documents lay them out, its samples' presentation times and sync samples, with none
//! of an output's decisions (which tracks are presented, and their arrays' data types and chunks).

use super::*;

/// A box's payload, base64.
fn b64(b: &[u8]) -> J {
    json!(crate::mirror::base64(b))
}

/// The fields of a parameter set or configuration record that the profile reads.
fn sps_json(s: &Sps) -> J {
    json!({"profile": s.profile, "level": s.level, "chroma": s.chroma, "luma_bits": s.luma, "chroma_bits": s.chroma_depth})
}

/// A video sample entry's configuration box, the one its type names (avcC of avc1, hvcC of hvc1
/// and hev1, av1C of av01, vpcC of vp09): its type, its payload, and the fields read from it, null
/// when the profile cannot read them; null for another type, or without the box.
fn video_config(e: &Entry) -> J {
    let (t, read): (&[u8; 4], fn(&[u8]) -> Option<Sps>) = match &e.typ {
        b"avc1" => (b"avcC", h264_sps),
        b"hvc1" | b"hev1" => (b"hvcC", hevc_sps),
        b"av01" => (b"av1C", av1_config),
        b"vp09" => (b"vpcC", vp9_config),
        _ => return J::Null,
    };
    e.child(t).map_or(J::Null, |b| json!({"box": latin1(t), "data": b64(b), "fields": read(b).as_ref().map_or(J::Null, sps_json)}))
}

/// An esds box's decoder configuration: its object type indication and its decoder specific
/// information, and, of AAC, the AudioSpecificConfig's fields; each null when the profile cannot
/// read the box; null when there is no esds box.
fn esds_json(e: &Entry) -> J {
    let Some(esds) = e.child(b"esds") else { return J::Null };
    let Some((oti, asc)) = esds_config(esds) else {
        return json!({"object_type_indication": null, "config": null, "aac": null});
    };
    let aac = aac_config(&asc).map_or(J::Null, |a| {
        json!({"object_type": a.aot, "sample_rate": a.rate, "channels": a.channels, "short_frames": a.short,
               "sbr_sample_rate": a.sbr_rate, "parametric_stereo": a.ps})
    });
    json!({"object_type_indication": oti, "config": b64(&asc), "aac": aac})
}

/// A sample entry as the description gives it.
fn entry_json(e: &Entry, jpeg: Option<&Option<JpegFrame>>) -> J {
    let pcmc = e.child(b"pcmC").filter(|c| c.len() >= 6).map_or(J::Null, |c| json!({"format_flags": c[4], "sample_size": c[5]}));
    let enda = e.child(b"enda").filter(|b| b.len() >= 2).map_or(J::Null, |b| json!(uint(b, 0, 2, false) == 1));
    let jpeg = match jpeg {
        Some(Some(f)) => json!({"marker": f.marker, "precision": f.precision, "width": f.width, "height": f.height, "components": f.components}),
        _ => J::Null,
    };
    json!({
        "type": latin1(&e.typ), "width": e.width, "height": e.height, "channels": e.channels, "sample_rate": e.rate,
        "sound_version": e.sound_version, "sample_size": e.bits, "lpcm_flags": e.lpcm_flags, "lpcm_packet": e.lpcm_packet,
        "video_config": video_config(e), "esds": esds_json(e), "pcmC": pcmc, "enda": enda, "jpeg": jpeg,
    })
}

/// The description of track `k`, `t`, whose samples are at `offsets` with the sizes `sizes`, the
/// IR's elements `elements`.
pub(super) fn track(k: usize, t: &Track, offsets: &[u64], sizes: &[u64], elements: &[u32]) -> J {
    // the presentation times and sync samples of a video track, or of a Motion JPEG sample entry's
    let video = &t.handler == b"vide" || t.entries.first().is_some_and(is_mjpeg);
    let mut seen = std::collections::HashSet::new();
    let distinct: Vec<u64> = sizes.iter().copied().filter(|&n| seen.insert(n)).collect();
    // the presentation times only of a track whose tables count its samples, one an element: the
    // stts box's counts are checked against the samples only when the track has an stsz box
    let counted = t.stts.iter().map(|e| e.0).sum::<u64>() + t.frag_pts.len() as u64 == offsets.len() as u64;
    let video = video && counted && !offsets.is_empty();
    json!({
        "index": k, "id": t.id, "handler": latin1(&t.handler), "timescale": t.timescale, "duration": int_json(t.duration as i128),
        "edits": t.edits, "samples": offsets.len(), "sizes": distinct, "bytes": sizes.iter().sum::<u64>(),
        "stz2": t.stz2, "stsz_sample_size": t.fixed_size.map(|f| f.0),
        "sample_descriptions": t.stsd_count, "entries": t.entries.len(),
        "other_description": t.stsc.iter().any(|e| e.2 != 1) || t.frag_other_entry,
        "entry": t.entries.first().map_or(J::Null, |e| entry_json(e, t.jpeg.as_ref())),
        "pts": if video { json!(presentation(t)) } else { J::Null },
        "sync": if video { json!(sync_samples(t, offsets.len() as u64)) } else { J::Null },
        "elements": {"trak": t.el, "first": elements.first()},
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A video track's presentation times are those of the samples its tables lay out: none when
    /// its stts box counts samples it has not (with no stsz box, 2^32 − 1 of them), so that no
    /// count the parser does not check sizes what it builds.
    #[test]
    fn presentation_times_are_only_of_the_samples_laid_out() {
        let mut t = Track { handler: *b"vide", stts: vec![(u32::MAX as u64, 1)], ..Track::default() };
        let d = track(0, &t, &[], &[], &[]);
        assert!(d["pts"].is_null() && d["sync"].is_null());
        t.stts = vec![(2, 10)];
        t.ctts = vec![(1, 5), (1, 0)];
        let d = track(0, &t, &[100, 200], &[8, 8], &[7, 8]);
        assert_eq!((d["pts"].clone(), d["sync"].clone()), (json!([5, 10]), json!([0, 1])));
        assert_eq!(d["sizes"], json!([8]));
    }
}
