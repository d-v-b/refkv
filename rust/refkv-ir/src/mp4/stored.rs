//! The stored arrays of an MP4 file (spec/stored-array.md §7
//! <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>): for every video
//! or audio track, descriptions keyed by its `trak` box. A video track of a codec the list names
//! has a description for each plane of its pictures, the samples in decode order, one a unit; a
//! Motion JPEG track one of its pictures; any other track of one sample description one of its
//! samples, which the codec `source` makes elements of; and a track of none, or of several, one
//! with no grid.

use super::{av1_config, h264_sps, hevc_sps, is_mjpeg, vp9_config, Entry, Sps, Track};
use crate::array::{AxisUnits, Chain, ChunkMap, Codec, Edge, Fill, Grid, Param, Place, Row, Scalar, StoredArray, UnitParameter};
use crate::ir::Res;
use crate::text::latin1;
use serde_json::{json, Value as J};
use std::collections::{BTreeMap, HashSet};

/// What a track's descriptions need, taken as its samples are laid out.
pub(super) struct Media {
    /// the track's `trak` box
    pub element: u32,
    /// the elements that declare its samples: its sample entry's fields, and its codec's
    /// configuration box's payload
    pub declarations: Vec<u32>,
    /// its samples' elements, in decode order
    pub samples: Vec<u32>,
    pub kind: Kind,
}

/// How a track's samples are stored, as far as the IR models it.
pub(super) enum Kind {
    /// pictures of a codec the list names: its name, the element of its configuration, the luma
    /// plane's width and height, the bits of a decoded sample, the chroma planes' width and height
    /// and the samples a decoder may start at
    Video { codec: &'static str, setup: u32, luma: (u64, u64), bits: u64, chroma: (u64, u64), sync: HashSet<u64> },
    /// a JPEG stream a sample: its width, height and components
    Jpeg { width: u64, height: u64, components: u64 },
    /// samples of an encoding the list has no name for, or whose elements the IR does not model:
    /// the sample entry's type
    Source { id: String },
    /// a track whose samples are not one array: none, or of several sample descriptions
    Other,
}

/// The codec of a video sample entry the list names, with its configuration box's type and what
/// that box says of its pictures.
fn video(e: &Entry) -> Option<(&'static str, &'static [u8; 4], Sps)> {
    let read = |name, config: &'static [u8; 4], sps: fn(&[u8]) -> Option<Sps>| Some((name, config, sps(e.child_here(config)?)?));
    match &e.typ {
        b"avc1" => read("h264", b"avcC", h264_sps),
        b"hvc1" | b"hev1" => read("hevc", b"hvcC", hevc_sps),
        b"av01" => read("av1", b"av1C", av1_config),
        b"vp09" => read("vp9", b"vpcC", vp9_config),
        _ => None,
    }
}

/// What the IR models of how a track's samples are stored: `count` of them, the sync samples
/// among them `sync`; `ragged`, whether its tables size a PCM sample as no whole number of frames.
pub(super) fn kind(t: &Track, count: u64, sync: Vec<u64>, ragged: bool) -> Kind {
    // one sample entry describes every sample, whose sizes the parser reads, or the samples are
    // not one array the IR models
    let one = !t.stz2 && t.stsd_count == 1 && t.entries.len() == 1 && t.stsc.iter().all(|e| e.2 == 1) && !t.frag_other_entry;
    // PCM samples that are no whole frames are ones readers do not agree on: ffmpeg's demuxer
    // reads the frames by the sample entry's size, whatever the tables say
    let Some(e) = t.entries.first().filter(|_| one && count > 0 && !ragged) else { return Kind::Other };
    // the general fallback: the samples, as the sample entry's type encodes them
    let source = || Kind::Source { id: latin1(&e.typ) };
    if &t.handler != b"vide" || e.width == 0 || e.height == 0 {
        return source();
    }
    if is_mjpeg(e) {
        // a stream of the sample entry's size, of whole bytes, as the list's `jpeg` decodes
        return match t.jpeg.flatten() {
            Some(f) if (f.width, f.height) == (e.width, e.height) && f.precision == 8 && matches!(f.components, 1 | 3) => {
                Kind::Jpeg { width: e.width, height: e.height, components: f.components as u64 }
            }
            _ => source(),
        };
    }
    let Some((codec, config, sps)) = video(e) else { return source() };
    let Some(setup) = e.config_element(config) else { return source() };
    // three planes of one bit depth: 4:2:0, 4:2:2 or 4:4:4
    let halved = |n: u64, by: bool| if by { n.div_ceil(2) } else { n };
    let chroma = match sps.chroma {
        1 | 2 | 3 if sps.luma == sps.chroma_depth && (8..=16).contains(&sps.luma) => {
            (halved(e.width, sps.chroma < 3), halved(e.height, sps.chroma == 1))
        }
        _ => return source(),
    };
    let bits = if sps.luma > 8 { 16 } else { 8 };
    Kind::Video { codec, setup, luma: (e.width, e.height), bits, chroma, sync: sync.into_iter().collect() }
}

/// The descriptions of the tracks' samples, their chains and their rows.
pub(super) fn described(media: &[Media]) -> Res<(Vec<StoredArray>, Vec<Chain>, ChunkMap)> {
    let mut arrays: Vec<StoredArray> = Vec::new();
    let mut chains: Vec<Chain> = Vec::new();
    let mut chain_index: BTreeMap<String, u32> = BTreeMap::new();
    let mut map = ChunkMap::default();
    let mut chain_of = |chain: Chain| {
        let key = J::Array(chain.iter().map(Codec::to_json).collect()).to_string();
        *chain_index.entry(key).or_insert_with(|| {
            chains.push(chain);
            (chains.len() - 1) as u32
        })
    };
    let place = |element: u32| Place::Element { element, member: None, range: None };
    for m in media {
        let n = m.samples.len() as u64;
        // each array of the track: its shape after the samples' axis, its sample size where the
        // IR knows it, its chain and whether its units are a stream's, which say where a decoder
        // may start
        let mut planes: Vec<(Vec<u64>, Option<u64>, Chain, Option<String>)> = Vec::new();
        match &m.kind {
            Kind::Video { codec, setup, luma, bits, chroma, .. } => {
                for (plane, &(w, h)) in [luma, chroma, chroma].into_iter().enumerate() {
                    let parameters = [("setup".to_string(), Param::Source(place(*setup))), ("plane".to_string(), Param::Value(json!(plane)))];
                    let chain = vec![Codec { name: codec.to_string(), parameters: parameters.into_iter().collect() }];
                    planes.push((vec![h, w], Some(*bits), chain, Some(format!("p.{codec}.sync"))));
                }
            }
            Kind::Jpeg { width, height, components } => {
                let mut shape = vec![*height, *width];
                if *components > 1 {
                    shape.push(*components);
                }
                planes.push((shape, Some(8), vec![Codec { name: "jpeg".into(), parameters: BTreeMap::new() }], None));
            }
            Kind::Source { id } => {
                // an element a sample, of whatever the encoding decodes one to
                let parameters = [("format".to_string(), Param::Value(json!("mp4"))), ("id".to_string(), Param::Value(json!(id)))];
                planes.push((Vec::new(), None, vec![Codec { name: "source".into(), parameters: parameters.into_iter().collect() }], None));
            }
            Kind::Other => {}
        }
        let sync = match &m.kind {
            Kind::Video { sync, .. } => Some(sync),
            _ => None,
        };
        if planes.is_empty() {
            // a track whose samples are not one array: what declares it, and no more
            arrays.push(StoredArray {
                element: m.element,
                index: Some(0),
                declarations: m.declarations.iter().map(|&e| place(e)).collect(),
                shape: None,
                axes: None,
                item_bits: None,
                pad_from: None,
                grid: None,
                chains: Vec::new(),
                fill: Fill::Undefined,
                unit_parameters: Vec::new(),
            });
        }
        for (index, (within, bits, chain, column)) in planes.into_iter().enumerate() {
            let array = arrays.len() as u32;
            // a sample a unit, whole
            let axes = std::iter::once(1).chain(within.iter().copied()).map(AxisUnits::Each).collect();
            let unit_parameters = column.iter().map(|_| UnitParameter { chain: 0, codec: 0, name: "sync".into() }).collect();
            arrays.push(StoredArray {
                element: m.element,
                index: Some(index as u64),
                declarations: m.declarations.iter().map(|&e| place(e)).collect(),
                shape: Some(std::iter::once(n).chain(within.iter().copied()).collect()),
                axes: None,
                item_bits: bits,
                pad_from: None,
                grid: Some(Grid { axes, edge: Edge::Short }),
                chains: vec![chain_of(chain)],
                fill: Fill::Undefined,
                unit_parameters,
            });
            for (j, &e) in m.samples.iter().enumerate() {
                let j = j as u64;
                let mut coords = vec![j];
                coords.resize(1 + within.len(), 0);
                let started = sync.is_some_and(|s| s.contains(&j));
                let params = column.iter().map(|name| (name.clone(), Scalar::UInt64(started as u64))).collect();
                map.push(Row { array, coords, element: Some(e), params, ..Row::default() })?;
            }
        }
    }
    Ok((arrays, chains, map))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A track of `handler` with one sample entry of type `typ`.
    fn track(handler: &[u8; 4], typ: &[u8; 4]) -> Track {
        Track { handler: *handler, stsd_count: 1, entries: vec![Entry { typ: *typ, width: 64, height: 48, ..Entry::default() }], ..Track::default() }
    }

    fn id(k: Kind) -> Option<String> {
        match k {
            Kind::Source { id } => Some(id),
            _ => None,
        }
    }

    /// A track of one sample description whose samples the IR has no codec for is its samples, as
    /// the sample entry's type encodes them: an audio track, a video track of another codec, and
    /// one of a codec the list names whose configuration box is absent.
    #[test]
    fn samples_the_ir_does_not_model_are_the_sample_entrys_type() {
        for (handler, typ) in [(b"soun", b"mp4a"), (b"soun", b"sowt"), (b"vide", b"mjpb"), (b"vide", b"avc1"), (b"soun", b"raw ")] {
            assert_eq!(id(kind(&track(handler, typ), 3, vec![0], false)), Some(crate::text::latin1(typ)), "{handler:?} {typ:?}");
        }
    }

    #[test]
    fn a_track_of_no_samples_is_no_array() {
        assert!(matches!(kind(&track(b"soun", b"mp4a"), 0, vec![], false), Kind::Other));
    }

    #[test]
    fn a_track_of_several_sample_descriptions_is_no_array() {
        let mut t = track(b"soun", b"mp4a");
        t.stsd_count = 2;
        assert!(matches!(kind(&t, 3, vec![0], false), Kind::Other));
        let mut t = track(b"soun", b"mp4a");
        t.stsc = vec![(1, 1, 2)];
        assert!(matches!(kind(&t, 3, vec![0], false), Kind::Other));
        let mut t = track(b"soun", b"mp4a");
        t.frag_other_entry = true;
        assert!(matches!(kind(&t, 3, vec![0], false), Kind::Other));
    }

    #[test]
    fn a_track_of_compact_sample_sizes_is_no_array() {
        let mut t = track(b"soun", b"mp4a");
        t.stz2 = true;
        assert!(matches!(kind(&t, 3, vec![0], false), Kind::Other));
    }

    #[test]
    fn pcm_samples_that_are_no_whole_frames_are_no_array() {
        assert!(matches!(kind(&track(b"soun", b"sowt"), 3, vec![0], true), Kind::Other));
    }
}
