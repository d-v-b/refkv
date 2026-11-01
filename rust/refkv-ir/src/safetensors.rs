//! A sans-IO parser of safetensors files into the IR (spec/zarrify/safetensors.md §8
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#8-safetensors-profile>,
//! and the source model of §5.3
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#53-elements>): an 8-byte
//! header length, a JSON header naming each tensor's data type, shape and range, and the buffer
//! of the tensors' data.
//!
//! Rounds: the file's first 64 KiB, which hold the length and, in every file surveyed, the
//! header; then the rest of a longer header. No tensor's data is read. The schema
//! (`schema/safetensors.json`) holds every check, in the profile's order.

use crate::fed::{Fed, Overlap, uint};
use crate::ir::*;
use crate::nd2::Step;
use crate::rules::{self, Rules, flag, num, text};
use crate::sans::{self, Asked};
use serde::de::{self, Deserialize, Deserializer, MapAccess, SeqAccess, Visitor};
use serde_json::{Map, Value as J, json};
use std::collections::BTreeSet;

/// The header's member that is not a tensor.
const METADATA: &str = "__metadata__";

/// Whether a file's first bytes are a safetensors file's (spec/zarrify/safetensors.md §8.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#81-signature>): a
/// header length from 2 to the library's limit, then `{` and a JSON string, `}` or whitespace.
pub fn detect(head: &[u8]) -> bool {
    if head.len() < 10 {
        return false;
    }
    let n = uint(head, 0, 8, true);
    (2..=100_000_000).contains(&n) && head[8] == b'{' && matches!(head[9], b'"' | b'}' | b' ' | b'\t' | b'\n' | b'\r')
}

/// A JSON value whose objects have no member twice ("Duplicate keys are disallowed", the
/// format), their members in the order written.
struct Strict(J);

impl<'de> Deserialize<'de> for Strict {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        d.deserialize_any(StrictVisitor).map(Strict)
    }
}

struct StrictVisitor;

impl<'de> Visitor<'de> for StrictVisitor {
    type Value = J;

    fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
        f.write_str("a JSON value")
    }
    fn visit_bool<E>(self, v: bool) -> Result<J, E> {
        Ok(J::Bool(v))
    }
    fn visit_i64<E>(self, v: i64) -> Result<J, E> {
        Ok(J::from(v))
    }
    fn visit_u64<E>(self, v: u64) -> Result<J, E> {
        Ok(J::from(v))
    }
    fn visit_f64<E>(self, v: f64) -> Result<J, E> {
        Ok(serde_json::Number::from_f64(v).map_or(J::Null, J::Number))
    }
    fn visit_str<E>(self, v: &str) -> Result<J, E> {
        Ok(J::String(v.to_string()))
    }
    fn visit_string<E>(self, v: String) -> Result<J, E> {
        Ok(J::String(v))
    }
    fn visit_unit<E>(self) -> Result<J, E> {
        Ok(J::Null)
    }
    fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<J, A::Error> {
        let mut v = Vec::new();
        while let Some(Strict(x)) = seq.next_element()? {
            v.push(x);
        }
        Ok(J::Array(v))
    }
    fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<J, A::Error> {
        let mut m = Map::new();
        while let Some(k) = map.next_key::<String>()? {
            if m.contains_key(&k) {
                let short: String = k.chars().take(200).collect();
                return Err(de::Error::custom(format!("the member {short:?} appears twice")));
            }
            let Strict(v) = map.next_value()?;
            m.insert(k, v);
        }
        Ok(J::Object(m))
    }
}

/// A tensor of the header, in its order: its entry's members, and, once checked, its data's
/// range in the file, its path when presented, else its reason.
struct Tensor {
    name: String,
    dtype: String,
    shape: Vec<u64>,
    offsets: (u64, u64),
    extra: Map<String, J>,
    path: Option<String>,
    reason: Option<&'static str>,
}

/// An integer from 0 to 2^53 − 1, as the header writes `shape` and `data_offsets`.
fn safe_int(v: &J) -> Option<u64> {
    v.as_u64().filter(|&x| x <= (1 << 53) - 1)
}

/// A tensor of the header's member `name`, `value`, or what is wrong with its entry.
fn entry(name: &str, value: &J) -> Result<Tensor, &'static str> {
    let J::Object(m) = value else { return Err("is not an object") };
    let dtype = m.get("dtype").and_then(J::as_str).ok_or("has no dtype string")?;
    let shape: Option<Vec<u64>> = m.get("shape").and_then(J::as_array).and_then(|a| a.iter().map(safe_int).collect());
    let shape = shape.ok_or("has no shape of integers from 0 to 2^53 - 1")?;
    let offsets: Option<Vec<u64>> = m.get("data_offsets").and_then(J::as_array).and_then(|a| a.iter().map(safe_int).collect());
    let offsets = offsets.filter(|o| o.len() == 2).ok_or("has no data_offsets of two integers from 0 to 2^53 - 1")?;
    let extra = m.iter().filter(|(k, _)| !matches!(k.as_str(), "dtype" | "shape" | "data_offsets")).map(|(k, v)| (k.clone(), v.clone())).collect();
    Ok(Tensor { name: name.to_string(), dtype: dtype.to_string(), shape, offsets: (offsets[0], offsets[1]), extra, path: None, reason: None })
}

/// A tensor's path, its name with `/` between node names, or why it has none
/// (spec/zarrify/safetensors.md §4.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#41-paths>).
fn tensor_path(name: &str) -> Result<String, &'static str> {
    let parts: Vec<&str> = name.split('/').collect();
    if parts.iter().any(|p| p.is_empty() || *p == "." || *p == ".." || p.starts_with("__") || *p == "zarr.json") {
        return Err("name");
    }
    if parts[0] == crate::out::SOURCE_NODE {
        return Err("reserved");
    }
    Ok(name.to_string())
}

/// The proper prefixes of a path that end before one of its `/`: the groups that hold it.
fn groups_of(path: &str) -> impl Iterator<Item = &str> {
    path.match_indices('/').map(move |(i, _)| &path[..i])
}

#[derive(Clone, Copy, PartialEq, Debug)]
enum Phase {
    Start,
    Head,
    Done,
}

pub struct Safetensors {
    size: u64,
    phase: Phase,
    fed: Fed,
    ir: Ir,
    rules: &'static Rules,
    asked: Asked,
    facts: J,
}

impl Safetensors {
    pub fn new(size: u64) -> Self {
        Safetensors {
            size, phase: Phase::Start, fed: Fed::new(Overlap::MergeKeep), ir: Ir::new(size), rules: rules::of("safetensors"),
            asked: Asked::default(), facts: J::Null,
        }
    }

    pub fn feed(&mut self, offset: u64, data: Vec<u8>) {
        self.fed.feed(offset, data);
    }

    pub fn step(&mut self) -> Result<Step, String> {
        let size = self.size;
        let reads = match self.phase {
            Phase::Start => {
                self.phase = Phase::Head;
                vec![(0, size.min(self.rules.limit("first_read") as u64))]
            }
            Phase::Head => {
                self.rules.check("length", rules::env([("size", num(size as f64))]))?;
                let first = self.fed.get(0, 8).ok_or("internal: the header's length was not fed")?;
                let n = uint(first, 0, 8, true);
                self.rules.check("header", rules::env([("length", num(n as f64)), ("size", num(size as f64))]))?;
                if !self.fed.has(8, n) {
                    let have = self.fed.reach(8).max(8);
                    vec![(have, 8 + n - have)]
                } else {
                    return self.done(n);
                }
            }
            Phase::Done => return Ok(Step::Done),
        };
        Ok(self.asked.ask(reads))
    }

    /// The header's checks, in the profile's order (spec/zarrify/safetensors.md §8.2
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#82-checks>), the
    /// elements, and the facts.
    fn done(&mut self, n: u64) -> Result<Step, String> {
        self.phase = Phase::Done;
        let r = self.rules;
        let raw = self.fed.get(8, n).ok_or("internal: the header was not fed")?.to_vec();
        let header = std::str::from_utf8(&raw).ok();
        r.check("text", rules::env([("utf8", flag(header.is_some())), ("brace", flag(raw.first() == Some(&b'{')))]))?;
        let parsed = serde_json::from_str::<Strict>(header.unwrap_or_default()).map(|s| s.0);
        let problem = parsed.as_ref().err().map_or(crate::expr::V::Absent, |e| text(&e.to_string()));
        r.check("json", rules::env([("problem", problem)]))?;
        let Ok(J::Object(members)) = parsed else { return Err("internal: the header is not an object".into()) };
        let metadata = members.get(METADATA);
        if let Some(m) = metadata {
            let strings = m.as_object().is_some_and(|o| o.values().all(J::is_string));
            r.check("metadata", rules::env([("strings", flag(strings))]))?;
        }
        let short = |s: &str| -> String { s.chars().take(200).collect() };
        let mut tensors = Vec::new();
        for (name, value) in members.iter().filter(|(k, _)| k.as_str() != METADATA) {
            let t = entry(name, value);
            let problem = t.as_ref().err().map_or(crate::expr::V::Absent, |e| text(e));
            r.check("entry", rules::env([("name", text(&short(name))), ("problem", problem)]))?;
            tensors.push(t.map_err(|e| e.to_string())?);
        }
        for t in &tensors {
            let known = r.entry("types", &t.dtype).is_some();
            r.check("data_type", rules::env([("name", text(&short(&t.name))), ("dtype", text(&t.dtype)), ("known", flag(known))]))?;
        }
        // the offsets and sizes in data_offsets order, ties in the header's (the sort is stable)
        let mut order: Vec<usize> = (0..tensors.len()).collect();
        order.sort_by_key(|&i| tensors[i].offsets);
        let mut start = 0u64;
        for &i in &order {
            let t = &tensors[i];
            let (begin, end) = t.offsets;
            r.check("offsets", rules::env([("name", text(&short(&t.name))), ("begin", num(begin as f64)), ("end", num(end as f64)), ("start", num(start as f64))]))?;
            start = end;
            let k = r.entry("types", &t.dtype).and_then(|e| e["bits"].as_u64()).unwrap_or(8) as u128;
            let bits = t.shape.iter().try_fold(k, |acc, &s| acc.checked_mul(s as u128));
            let shape = format!("[{}]", t.shape.iter().map(u64::to_string).collect::<Vec<_>>().join(", "));
            let length = end - begin;
            r.check("size", rules::env([
                ("name", text(&short(&t.name))), ("dtype", text(&t.dtype)), ("shape", text(&shape)),
                ("whole", flag(bits.is_none_or(|b| b % 8 == 0))),
                ("bits", text(&bits.map_or("more than 2^128".into(), |b| b.to_string()))),
                ("same", flag(bits == Some(length as u128 * 8))),
                ("bytes", text(&bits.map_or("more than 2^125".into(), |b| (b / 8).to_string()))),
                ("length", num(length as f64)),
            ]))?;
        }
        r.check("buffer", rules::env([("end", num(start as f64)), ("buffer", num((self.size - 8 - n) as f64))]))?;
        // the paths (spec/zarrify/safetensors.md §4.1
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#41-paths>)
        for t in tensors.iter_mut() {
            match tensor_path(&t.name) {
                Ok(p) => t.path = Some(p),
                Err(why) => t.reason = Some(why),
            }
        }
        let held: BTreeSet<String> = tensors.iter().filter_map(|t| t.path.as_deref()).flat_map(groups_of).map(str::to_string).collect();
        for t in tensors.iter_mut() {
            if t.path.as_ref().is_some_and(|p| held.contains(p)) {
                t.path = None;
                t.reason = Some("path");
            }
        }
        let groups: BTreeSet<&str> = tensors.iter().filter_map(|t| t.path.as_deref()).flat_map(groups_of).collect();
        // the elements: the length, the header, and every tensor's data, in the buffer's order
        self.ir.value(0, "length", NO_INDEX, "<u8", 0, (0, 8), Some(&n.to_le_bytes()))?;
        if n > 0 {
            self.ir.value(0, "header", NO_INDEX, &format!("ascii[{n}]"), 0, (8, n), Some(&raw))?;
        }
        let mut data = vec![None; tensors.len()];
        for &i in &order {
            let t = &tensors[i];
            let (begin, end) = t.offsets;
            if end == begin {
                continue;
            }
            let ty = r.entry("types", &t.dtype).ok_or("internal: an unknown data type")?;
            let bits = ty["bits"].as_u64().unwrap_or(8);
            let form = json!({
                "geometry": {"data_type": ty["dtype"], "shape": t.shape, "bits": bits},
                "codec": {"endian": if bits > 8 { J::from("little") } else { J::Null }},
                "recipe": [["src", 0, null]],
            });
            data[i] = Some(self.ir.data(0, "tensors/", i as u64, 0, (8 + n + begin, end - begin), &form.to_string())?);
        }
        let mut arrays = Vec::new();
        let mut listed = Vec::new();
        for (t, d) in tensors.iter().zip(&data) {
            let mut j = Map::new();
            j.insert("name".into(), json!(t.name));
            j.insert("dtype".into(), json!(t.dtype));
            j.insert("shape".into(), json!(t.shape));
            j.insert("data_offsets".into(), json!([t.offsets.0, t.offsets.1]));
            if !t.extra.is_empty() {
                j.insert("extra".into(), J::Object(t.extra.clone()));
            }
            match (&t.path, t.reason) {
                (Some(p), _) => {
                    j.insert("path".into(), json!(p));
                    let ty = r.entry("types", &t.dtype).ok_or("internal: an unknown data type")?;
                    arrays.push(json!({
                        "path": p, "data": d, "offset": 8 + n + t.offsets.0, "length": t.offsets.1 - t.offsets.0,
                        "dtype": t.dtype, "data_type": ty["dtype"], "bits": ty["bits"], "fill": ty["fill"], "shape": t.shape,
                    }));
                }
                (None, why) => {
                    j.insert("reason".into(), json!(why.unwrap_or("name")));
                }
            }
            listed.push(J::Object(j));
        }
        let mut own = Map::new();
        own.insert("header_length".into(), json!(n));
        if let Some(m) = metadata {
            own.insert("metadata".into(), m.clone());
        }
        own.insert("tensors".into(), J::Array(listed));
        let mut facts = json!({"own": own, "arrays": arrays, "groups": groups, "copy_threshold": crate::out::copy_threshold()});
        self.asked.put(&mut facts);
        self.facts = facts;
        Ok(Step::Done)
    }

    pub fn finish(self) -> Result<(Ir, J), String> {
        sans::finish(self.phase == Phase::Done, self.ir, self.facts)
    }
}

crate::parser!(Safetensors);

/// The safetensors profile's file run (spec/zarrify/safetensors.md).
pub struct Profile;

impl crate::run::FileProfile for Profile {
    /// A header length, then `{"` (`detect`).
    fn detect(&self, head: &[u8]) -> bool {
        detect(head)
    }
    fn parser(&self, size: u64) -> crate::run::AnyParser {
        crate::run::AnyParser::of::<Safetensors>(size)
    }
    fn project(&self, ir: &crate::ir::Ir, facts: &serde_json::Value, url: &str, copied: &crate::project::Copied) -> Result<crate::out::Out, String> {
        crate::project::safetensors::project(ir, facts, url, copied)
    }
    fn reads(&self, ir: &crate::ir::Ir, facts: &serde_json::Value) -> Result<Vec<(u64, u64)>, String> {
        crate::project::safetensors::reads(ir, facts)
    }
}
