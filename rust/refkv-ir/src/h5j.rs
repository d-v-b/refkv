//! The H5J profile (spec/zarrify/h5j.md <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/h5j.md>):
//! Janelia's H5J file is an HDF5 file whose `/Channels` group holds a dataset a channel, each
//! dataset's bytes a whole MP4 file of one HEVC track, a picture a slice. The HDF5 parser
//! (`hdf5/file.rs`) reads the file as it reads any other, and finds it to be an H5J file
//! ([`is_h5j`]); then [`Channels`] reads each channel's MP4 file with the MP4 parser (`mp4.rs`),
//! its reads moved by the dataset's address, and the HDF5 IR takes each MP4 IR in place of its
//! channel's data ([`crate::ir::Ir::graft_file`]). The projection is `project/h5j.rs`.

use crate::fed::Fed;
use crate::hdf5::walk::lenient;
use crate::hdf5::{R, H5};
use crate::ir::Ir;
use crate::mp4::Mp4;
use crate::step::Step;
use crate::rules::{self, num, text, Rules};
use serde_json::{json, Value as J};
use std::collections::HashMap;

/// The group of the channels, and the prefix of a channel's name.
pub const GROUP: &str = "Channels";
pub const PREFIX: &str = "Channel_";

/// The number `n` of a channel's name `Channel_<n>` (digits, without a sign), if it is one.
pub fn channel_number(name: &[u8]) -> Option<u64> {
    let digits = name.strip_prefix(PREFIX.as_bytes())?;
    if digits.is_empty() || digits.len() > 9 || !digits.iter().all(u8::is_ascii_digit) {
        return None;
    }
    std::str::from_utf8(digits).ok()?.parse().ok()
}

/// Whether the HDF5 file whose root group's header is at `root` is an H5J file (spec/zarrify/h5j.md
/// §8.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/h5j.md#81-choosing-the-profile>): the
/// root has a link `Channels` to a group with at least one link, each named `Channel_<n>`. A
/// structure that can't be read is no H5J file's (the HDF5 profile then reads or rejects it).
pub fn is_h5j(h: &mut H5, root: u64) -> R<bool> {
    let Some(links) = lenient(h.links(root))? else { return Ok(false) };
    if !links.has(GROUP) {
        return Ok(false);
    }
    let Some(at) = lenient(h.follow(&links, GROUP.as_bytes()))? else { return Ok(false) };
    let Some(channels) = lenient(h.links(at))? else { return Ok(false) };
    Ok(!channels.list.is_empty() && channels.list.iter().all(|(name, _)| channel_number(name).is_some()))
}

/// A channel's dataset, and its MP4 file's parse.
struct Channel {
    /// its name and number, its dataset header's address, and where its bytes are in the file
    name: String,
    number: u64,
    header: u64,
    offset: u64,
    length: u64,
    attributes: J,
    parser: Option<Mp4>,
    /// the reads the parser asked for, in its file's offsets, not yet fed
    pending: Vec<(u64, u64)>,
    /// the parse of its MP4 file, once done: its IR and description; or why its bytes don't read
    /// as an MP4 file
    parsed: Option<(Ir, J)>,
    unread: Option<String>,
}

/// The channels of an H5J file, read from the HDF5 hierarchy's nodes, and their MP4 files' parses.
pub struct Channels {
    list: Vec<Channel>,
    rules: &'static Rules,
    /// the padding `/Channels` gives (right, bottom), when its attributes give it
    pad: Result<(u64, u64), String>,
}

/// The padding (right, bottom) the attributes of `/Channels`, as the HDF5 hierarchy presents them,
/// give: 0 for one it lacks; Err when one is not one integer of at least 0 (spec/zarrify/h5j.md §8.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/h5j.md#82-channels>).
pub fn pad(group: &J) -> Result<(u64, u64), String> {
    let right = one_integer(group, "pad_right").transpose()?.unwrap_or(0);
    let bottom = one_integer(group, "pad_bottom").transpose()?.unwrap_or(0);
    Ok((right, bottom))
}

/// An integer attribute of one element, as the HDF5 hierarchy presents it (a JSON array of one integer).
fn one_integer(attributes: &J, name: &str) -> Option<Result<u64, String>> {
    let v = attributes.get(name)?;
    let x = v.as_array().filter(|a| a.len() == 1).and_then(|a| a[0].as_i64()).or_else(|| v.as_i64());
    Some(match x {
        Some(x) if x >= 0 => Ok(x as u64),
        _ => Err(format!("/{GROUP}'s {name} is {v}, not one integer of at least 0")),
    })
}

impl Channels {
    /// The channels the HDF5 hierarchy's `nodes` hold (`crate::project::hdf5_nodes`), each a
    /// dataset `Channels/Channel_<n>`, in order of `n` (spec/zarrify/h5j.md §8.2
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/h5j.md#82-channels>): a contiguous
    /// dataset of one dimension of `uint8` is read as an MP4 file; any other is recorded with the
    /// reason.
    pub fn new(nodes: &[J]) -> Result<Channels, String> {
        let rules = rules::of("h5j");
        let group = nodes.iter().find(|n| n["path"] == GROUP).map(|n| &n["attributes"]).unwrap_or(&J::Null);
        let pad = pad(group);
        let mut list = Vec::new();
        for n in nodes {
            let Some(name) = n["path"].as_str().and_then(|p| p.strip_prefix(GROUP)).and_then(|p| p.strip_prefix('/')) else { continue };
            let Some(number) = channel_number(name.as_bytes()) else { continue };
            let a = &n["array"];
            let store = &a["store"];
            let unread = if a.is_null() {
                Some("not a dataset of bytes".to_string())
            } else if a["data_type"] != "uint8" || a["shape"].as_array().is_none_or(|s| s.len() != 1) {
                Some(format!("a dataset of {} of shape {}, not bytes of one dimension", a["data_type"].as_str().unwrap_or("records"), a["shape"]))
            } else if store["kind"] != "contiguous" {
                Some(format!("a dataset of {} storage, not contiguous", store["kind"].as_str().unwrap_or("no")))
            } else {
                None
            };
            let (offset, length) = (store["offset"].as_u64().unwrap_or(0), store["length"].as_u64().unwrap_or(0));
            let parser = unread.is_none().then(|| Mp4::new(length));
            list.push(Channel {
                name: name.to_string(), number, header: store["at"].as_u64().unwrap_or(0), offset, length,
                attributes: n["attributes"].clone(), parser, pending: Vec::new(), parsed: None, unread,
            });
        }
        list.sort_by_key(|c| c.number);
        rules.check("channels", rules::env([("channels", num(list.len() as f64))]))?;
        Ok(Channels { list, rules, pad })
    }

    /// Feeds each channel's parser the bytes it asked for that `store` holds, and steps it, until
    /// every parse is done (true) or waits for bytes, which `want` gets as ranges of the file.
    /// A parse that rejects its file records the message as why the channel doesn't read.
    pub fn advance(&mut self, store: &Fed, want: &mut Vec<(u64, u64)>) -> bool {
        let mut done = true;
        for c in self.list.iter_mut() {
            let Some(parser) = c.parser.as_mut() else { continue };
            loop {
                let mut missing = Vec::new();
                for (o, n) in std::mem::take(&mut c.pending) {
                    match store.get(c.offset + o, n) {
                        Some(b) => parser.feed(o, b.to_vec()),
                        None => missing.push((o, n)),
                    }
                }
                if !missing.is_empty() {
                    want.extend(missing.iter().map(|&(o, n)| (c.offset + o, n)));
                    c.pending = missing;
                    done = false;
                    break;
                }
                match parser.step() {
                    Ok(Step::Read(r)) if !r.is_empty() => c.pending = r,
                    Ok(Step::Read(_) | Step::Done) => {
                        let parser = c.parser.take().expect("a parse in progress");
                        match parser.finish() {
                            Ok(p) => c.parsed = Some(p),
                            Err(m) => c.unread = Some(format!("its MP4 file: {m}")),
                        }
                        break;
                    }
                    Ok(Step::Beside) => {
                        c.parser = None;
                        c.unread = Some("its MP4 file names other files".into());
                        break;
                    }
                    Err(m) => {
                        c.parser = None;
                        c.unread = Some(format!("its MP4 file: {m}"));
                        break;
                    }
                }
            }
        }
        done
    }

    /// The MP4 IR to graft in place of each channel's data whose MP4 file reads, by its dataset
    /// header's address, whatever the output presents; a channel whose bytes don't read keeps its
    /// data.
    pub fn inner(&self) -> HashMap<u64, &Ir> {
        self.list.iter().filter_map(|c| Some((c.header, &c.parsed.as_ref()?.0))).collect()
    }

    /// The stored arrays of each grafted IR, the ids the graft gave it and its offset.
    pub fn grafts<'a>(&'a self, grafted: &'a HashMap<u64, Vec<u32>>) -> Vec<(&'a crate::array::Stored, &'a [u32], u64)> {
        self.list
            .iter()
            .filter_map(|c| Some((&*c.parsed.as_ref()?.0.stored, grafted.get(&c.header)?.as_slice(), c.offset)))
            .collect()
    }

    /// The channels as the description gives them, with each MP4 description's element ids
    /// those `remap` gives.
    fn described(&self, remap: impl Fn(&Channel, u64) -> Result<u64, String>) -> Result<Vec<J>, String> {
        let mut out = Vec::new();
        for c in &self.list {
            let mp4 = match &c.parsed {
                Some((_, d)) => {
                    let mut d = d.clone();
                    for t in d["tracks"].as_array_mut().into_iter().flatten() {
                        for k in ["trak", "first"] {
                            if let Some(e) = t["elements"][k].as_u64() {
                                t["elements"][k] = json!(remap(c, e)?);
                            }
                        }
                    }
                    d
                }
                None => J::Null,
            };
            out.push(json!({
                "name": c.name, "path": format!("{GROUP}/{}", c.name), "offset": c.offset, "length": c.length,
                "attributes": c.attributes, "unread": c.unread, "mp4": mp4,
            }));
        }
        Ok(out)
    }

    /// With no channel that refkv's plan presents (`crate::project::h5j::channels`), once every
    /// parse is done, the rejection that names each channel's reason (spec/zarrify/h5j.md §8.3
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/h5j.md#83-checks>), which chooses
    /// the profile (spec/zarrify/h5j.md §8.1
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/h5j.md#81-choosing-the-profile>); else
    /// None.
    pub fn none_presented(&self) -> Option<String> {
        let described = self.described(|_, e| Ok(e)).ok()?;
        // which channels are presented does not depend on the chunking
        let planned = crate::project::h5j::channels(&described, &self.pad, &crate::out::Chunking::default());
        let presented = planned.iter().filter(|c| c["reason"].is_null()).count();
        let reasons: Vec<String> = planned.iter().filter_map(|c| Some(format!("{}: {}", c["name"].as_str()?, c["reason"].as_str()?))).collect();
        self.rules.check("presented", rules::env([("presented", num(presented as f64)), ("reasons", text(&reasons.join("; ")))])).err()
    }

    /// The description's `h5j` member (spec/zarrify/h5j.md §8.6
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/h5j.md#86-the-description>): each
    /// channel's name, dataset path, offset, length and attributes, and its MP4 file's description,
    /// its element ids the H5J IR's (`grafted` maps each MP4 IR's), or `unread`, why its bytes don't
    /// read as an MP4 file.
    pub fn description(&self, grafted: &HashMap<u64, Vec<u32>>) -> Result<J, String> {
        let channels = self.described(|c, e| {
            let map = grafted.get(&c.header).ok_or("internal: a channel's MP4 IR was not grafted")?;
            map.get(e as usize).map(|&x| x as u64).ok_or_else(|| "internal: a channel's element was not grafted".into())
        })?;
        Ok(json!({"channels": channels}))
    }
}

/// The H5J profile's file run: the HDF5 parser, finding an H5J file (or, chosen by name, required
/// to).
pub struct Profile;

impl crate::run::FileProfile for Profile {
    fn parser(&self, size: u64) -> crate::run::AnyParser {
        crate::run::AnyParser::of::<crate::hdf5::file::H5jParser>(size)
    }
    fn project(&self, ir: &Ir, description: &J, url: &str, _copied: &crate::project::Copied, chunking: &crate::out::Chunking) -> Result<crate::out::Out, String> {
        crate::project::h5j::project(ir, description, url, chunking)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn channel_names_are_channel_and_digits() {
        for (name, want) in [("Channel_0", Some(0)), ("Channel_12", Some(12)), ("Channel_007", Some(7)), ("Channel_", None),
                             ("Channel_-1", None), ("Channel_1a", None), ("channel_1", None), ("Channel_1234567890", None)] {
            assert_eq!(channel_number(name.as_bytes()), want, "{name}");
        }
    }
}
