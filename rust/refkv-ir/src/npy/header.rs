//! An NPY header (spec/zarrify/npy.md §2
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/npy.md#2-the-npy-format>): the subset
//! of Python's literals NumPy writes, the dictionary of `descr`, `fortran_order` and `shape`, and
//! the Zarr data type of its `descr` (§3
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/npy.md#3-data-types>).

use crate::rules::Rules;
use serde_json::{json, Map, Value as J};

/// A Python literal of the header's subset.
#[derive(Clone, Debug, PartialEq)]
pub enum Lit {
    Str(String),
    Int(u128),
    Bool(bool),
    Tuple(Vec<Lit>),
    List(Vec<Lit>),
    Dict(Vec<(String, Lit)>),
}

impl Lit {
    /// As JSON: a string, an integer, a boolean, or an array of a tuple's or list's values.
    pub fn json(&self) -> J {
        match self {
            Lit::Str(s) => json!(s),
            Lit::Int(i) => u64::try_from(*i).map_or_else(|_| json!(i.to_string()), |v| json!(v)),
            Lit::Bool(b) => json!(b),
            Lit::Tuple(v) | Lit::List(v) => J::Array(v.iter().map(Lit::json).collect()),
            Lit::Dict(m) => J::Object(m.iter().map(|(k, v)| (k.clone(), v.json())).collect()),
        }
    }

    /// As Python's `repr` writes it (a problem's message shows a `descr` so).
    pub fn repr(&self) -> String {
        let join = |v: &[Lit]| v.iter().map(Lit::repr).collect::<Vec<_>>().join(", ");
        match self {
            Lit::Str(s) => {
                // single quotes, but double when the text has a single quote and no double one
                let q = if s.contains('\'') && !s.contains('"') { '"' } else { '\'' };
                let mut out = String::from(q);
                for c in s.chars() {
                    match c {
                        '\\' => out.push_str("\\\\"),
                        '\n' => out.push_str("\\n"),
                        '\r' => out.push_str("\\r"),
                        '\t' => out.push_str("\\t"),
                        _ if c == q => {
                            out.push('\\');
                            out.push(c);
                        }
                        _ if (c as u32) < 0x20 || c as u32 == 0x7f => out.push_str(&format!("\\x{:02x}", c as u32)),
                        _ => out.push(c),
                    }
                }
                out.push(q);
                out
            }
            Lit::Int(i) => i.to_string(),
            Lit::Bool(b) => if *b { "True" } else { "False" }.into(),
            Lit::Tuple(v) if v.len() == 1 => format!("({},)", v[0].repr()),
            Lit::Tuple(v) => format!("({})", join(v)),
            Lit::List(v) => format!("[{}]", join(v)),
            Lit::Dict(m) => format!("{{{}}}", m.iter().map(|(k, v)| format!("{}: {}", Lit::Str(k.clone()).repr(), v.repr())).collect::<Vec<_>>().join(", ")),
        }
    }
}

/// A header's three members.
#[derive(Clone, Debug, PartialEq)]
pub struct Header {
    pub descr: Lit,
    pub fortran_order: bool,
    pub shape: Vec<u64>,
}

struct P<'a> {
    s: &'a [char],
    i: usize,
}

impl P<'_> {
    fn ws(&mut self) {
        while self.i < self.s.len() && matches!(self.s[self.i], ' ' | '\t' | '\n' | '\r') {
            self.i += 1;
        }
    }

    fn peek(&self) -> Option<char> {
        self.s.get(self.i).copied()
    }

    fn eat(&mut self, c: char) -> bool {
        self.ws();
        if self.peek() == Some(c) {
            self.i += 1;
            true
        } else {
            false
        }
    }

    fn expect(&mut self, c: char) -> Result<(), String> {
        if self.eat(c) {
            Ok(())
        } else {
            Err(self.wanted(&format!("'{c}'")))
        }
    }

    fn wanted(&self, what: &str) -> String {
        match self.peek() {
            Some(c) => format!("{what} expected at character {}, not {c:?}", self.i),
            None => format!("{what} expected at character {}, at the end", self.i),
        }
    }

    fn word(&mut self, w: &str) -> bool {
        let n = w.chars().count();
        if self.s.len() >= self.i + n && self.s[self.i..self.i + n].iter().copied().eq(w.chars()) {
            self.i += n;
            true
        } else {
            false
        }
    }

    fn value(&mut self) -> Result<Lit, String> {
        self.ws();
        match self.peek() {
            Some('\'') | Some('"') => self.string().map(Lit::Str),
            Some(c) if c.is_ascii_digit() => {
                let mut v: u128 = 0;
                while let Some(d) = self.peek().and_then(|c| c.to_digit(10)) {
                    v = v.checked_mul(10).and_then(|v| v.checked_add(d as u128)).ok_or("an integer past 2^128")?;
                    self.i += 1;
                }
                // a Python 2 long, as files written by Python 2 have them (NumPy reads them)
                if self.peek() == Some('L') {
                    self.i += 1;
                }
                Ok(Lit::Int(v))
            }
            Some('(') => {
                self.i += 1;
                let (items, comma) = self.items(')')?;
                // `(x)` is x in Python, not a tuple
                if items.len() == 1 && !comma {
                    return Err(format!("a tuple of one value without its comma, before character {}", self.i));
                }
                Ok(Lit::Tuple(items))
            }
            Some('[') => {
                self.i += 1;
                Ok(Lit::List(self.items(']')?.0))
            }
            _ if self.word("True") => Ok(Lit::Bool(true)),
            _ if self.word("False") => Ok(Lit::Bool(false)),
            _ => Err(self.wanted("a value")),
        }
    }

    /// The values up to `close`, and whether the last is followed by a comma.
    fn items(&mut self, close: char) -> Result<(Vec<Lit>, bool), String> {
        let mut out = Vec::new();
        let mut comma = false;
        loop {
            if self.eat(close) {
                return Ok((out, comma));
            }
            if !out.is_empty() && !comma {
                return Err(self.wanted(&format!("',' or '{close}'")));
            }
            out.push(self.value()?);
            comma = self.eat(',');
        }
    }

    fn string(&mut self) -> Result<String, String> {
        let q = self.s[self.i];
        self.i += 1;
        let mut out = String::new();
        loop {
            let Some(c) = self.peek() else { return Err("a string without its closing quote".into()) };
            self.i += 1;
            match c {
                _ if c == q => return Ok(out),
                '\n' | '\r' => return Err("a line break in a string".into()),
                '\\' => {
                    let Some(e) = self.peek() else { return Err("a string without its closing quote".into()) };
                    self.i += 1;
                    let hex = |p: &mut Self, n: usize| -> Result<char, String> {
                        let digits: String = p.s.get(p.i..p.i + n).ok_or("a short escape")?.iter().collect();
                        p.i += n;
                        u32::from_str_radix(&digits, 16).ok().filter(|_| digits.chars().all(|d| d.is_ascii_hexdigit()))
                            .and_then(char::from_u32).ok_or_else(|| format!("the escape of {digits:?} is not a character"))
                    };
                    out.push(match e {
                        '\\' | '\'' | '"' => e,
                        'n' => '\n',
                        'r' => '\r',
                        't' => '\t',
                        'x' => hex(self, 2)?,
                        'u' => hex(self, 4)?,
                        'U' => hex(self, 8)?,
                        _ => return Err(format!("the escape \\{e} in a string")),
                    });
                }
                _ => out.push(c),
            }
        }
    }
}

/// The header text's dictionary, and its three members (the problem when it is not one).
pub fn parse(text: &str, max_rank: usize) -> Result<Header, String> {
    let chars: Vec<char> = text.chars().collect();
    let mut p = P { s: &chars, i: 0 };
    p.expect('{')?;
    let mut members: Vec<(String, Lit)> = Vec::new();
    let mut comma = true;
    loop {
        if p.eat('}') {
            break;
        }
        if !comma {
            return Err(p.wanted("',' or '}'"));
        }
        p.ws();
        if !matches!(p.peek(), Some('\'') | Some('"')) {
            return Err(p.wanted("a string key"));
        }
        let k = p.string()?;
        p.expect(':')?;
        let v = p.value()?;
        if members.iter().any(|(m, _)| *m == k) {
            return Err(format!("the key {k:?} twice"));
        }
        members.push((k, v));
        comma = p.eat(',');
    }
    p.ws();
    if p.i != chars.len() {
        return Err(format!("text after the dictionary, at character {}", p.i));
    }
    let mut keys: Vec<&str> = members.iter().map(|(k, _)| k.as_str()).collect();
    keys.sort_unstable();
    if keys != ["descr", "fortran_order", "shape"] {
        return Err(format!("its keys are {keys:?}, not descr, fortran_order and shape"));
    }
    let get = |k: &str| members.iter().find(|(m, _)| m == k).map(|(_, v)| v.clone()).unwrap();
    let fortran_order = match get("fortran_order") {
        Lit::Bool(b) => b,
        v => return Err(format!("fortran_order is {}, not True or False", v.repr())),
    };
    let shape = match get("shape") {
        Lit::Tuple(v) => v
            .iter()
            .map(|d| match d {
                Lit::Int(i) => u64::try_from(*i).map_err(|_| format!("a size of {i} in shape")),
                other => Err(format!("{} in shape, not an integer", other.repr())),
            })
            .collect::<Result<Vec<u64>, String>>()?,
        v => return Err(format!("shape is {}, not a tuple", v.repr())),
    };
    if shape.len() > max_rank {
        return Err(format!("a shape of {} axes, more than {max_rank}", shape.len()));
    }
    let descr = get("descr");
    if !matches!(descr, Lit::Str(_) | Lit::List(_)) {
        return Err(format!("descr is {}, not a string or a list", descr.repr()));
    }
    Ok(Header { descr, fortran_order, shape })
}

/// A presented data type (§3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/npy.md#3-data-types>).
#[derive(Clone, Debug, PartialEq)]
pub struct Dtype {
    /// the Zarr data type
    pub zarr: J,
    /// the element's size `e`
    pub item: u64,
    /// its byte order: Some(true) little-endian, Some(false) big-endian, None for none
    pub little: Option<bool>,
    /// the trailing axis's size, for `|S<n>` and `|V<n>`
    pub trailing: Option<u64>,
    pub fill: J,
}

/// The byte order a type string's first character gives: `<` Some(true), `>` Some(false), `|`
/// None; any other is a problem.
fn order(c: char) -> Result<Option<bool>, String> {
    match c {
        '<' => Ok(Some(true)),
        '>' => Ok(Some(false)),
        '|' => Ok(None),
        _ => Err(format!("its byte order {c:?} is not <, > or |")),
    }
}

/// A type string's data type, `scalar` restricting it to the numbers, booleans, datetimes and
/// timedeltas a structure's fields may be.
fn simple(rules: &Rules, s: &str, scalar: bool) -> Result<Dtype, String> {
    let mut chars = s.chars();
    let Some(first) = chars.next() else { return Err("an empty type string".into()) };
    let o = order(first)?;
    let rest = chars.as_str();
    if let Some(why) = rules.entry("unsupported", rest).and_then(J::as_str) {
        return Err(why.to_string());
    }
    let kind: String = rest.chars().take_while(|c| c.is_ascii_alphabetic()).collect();
    let after = &rest[kind.len()..];
    let digits: String = after.chars().take_while(|c| c.is_ascii_digit()).collect();
    let unit = &after[digits.len()..];
    let size: u64 = digits.parse().map_err(|_| format!("{s:?} is not a NumPy type string"))?;
    let need_order = |o: Option<bool>, item: u64| -> Result<Option<bool>, String> {
        if item > 1 && o.is_none() {
            return Err("a type of more than one byte with no byte order".into());
        }
        Ok(if item > 1 { o } else { None })
    };
    match kind.as_str() {
        "M" | "m" if size == 8 => {
            let name = if kind == "M" { "numpy.datetime64" } else { "numpy.timedelta64" };
            let inner = unit.strip_prefix('[').and_then(|u| u.strip_suffix(']')).ok_or("a datetime or timedelta without a unit")?;
            let k: String = inner.chars().take_while(|c| c.is_ascii_digit()).collect();
            let u = &inner[k.len()..];
            let scale: u64 = if k.is_empty() { 1 } else { k.parse().map_err(|_| "a unit's multiple past 2^64")? };
            if rules.entry("units", u).is_none() || scale == 0 {
                return Err(format!("the unit {inner:?} is not one of NumPy's"));
            }
            Ok(Dtype { zarr: json!({"name": name, "configuration": {"unit": u, "scale_factor": scale}}), item: 8, little: need_order(o, 8)?, trailing: None, fill: json!(0) })
        }
        _ if !unit.is_empty() => Err(format!("{s:?} is not a NumPy type string")),
        "U" | "S" | "a" | "V" if scalar => Err(format!("a field of type {s}")),
        "U" | "S" | "a" | "V" if size == 0 => Err("a size of 0".into()),
        "U" => {
            let item = size.checked_mul(4).ok_or("a size past 2^64")?;
            let little = o.ok_or("a type of more than one byte with no byte order")?;
            Ok(Dtype { zarr: json!({"name": "fixed_length_utf32", "configuration": {"length_bytes": item}}), item, little: Some(little), trailing: None, fill: json!("") })
        }
        "S" | "a" | "V" => Ok(Dtype { zarr: json!("uint8"), item: size, little: None, trailing: Some(size), fill: json!(0) }),
        _ => {
            let t = rules.entry("types", &format!("{kind}{size}")).ok_or_else(|| format!("{s:?} is not a NumPy type string"))?;
            let item = t["bytes"].as_u64().unwrap_or(1);
            let name = t["dtype"].as_str().unwrap_or("");
            let fill = match name {
                "bool" => json!(false),
                n if n.starts_with("complex") => json!([0, 0]),
                _ => json!(0),
            };
            Ok(Dtype { zarr: json!(name), item, little: need_order(o, item)?, trailing: None, fill })
        }
    }
}

/// A structure's data type: its fields in order, each a scalar type string or a nested list,
/// their multi-byte fields of one byte order.
fn structure(rules: &Rules, fields: &[Lit]) -> Result<Dtype, String> {
    if fields.is_empty() {
        return Err("a structure with no fields".into());
    }
    let (mut out, mut fill) = (Vec::new(), Map::new());
    let (mut item, mut little): (u64, Option<bool>) = (0, None);
    for f in fields {
        let Lit::Tuple(parts) = f else { return Err(format!("the field {} is not a tuple", f.repr())) };
        let (name, d) = match &parts[..] {
            [Lit::Str(n), d] => (n, d),
            [_, _, _] => return Err("a field of shape".into()),
            [Lit::Tuple(_), _] => return Err("a field with a title".into()),
            _ => return Err(format!("the field {} is not (name, descr)", f.repr())),
        };
        if name.is_empty() {
            return Err("a padding field".into());
        }
        if fill.contains_key(name) {
            return Err(format!("two fields named {name:?}"));
        }
        let t = match d {
            Lit::Str(s) => simple(rules, s, true)?,
            Lit::List(l) => structure(rules, l)?,
            other => return Err(format!("the field {name:?}'s type {} is not a string or a list", other.repr())),
        };
        if let Some(l) = t.little {
            if little.is_some_and(|m| m != l) {
                return Err("fields of two byte orders".into());
            }
            little = Some(l);
        }
        item = item.checked_add(t.item).ok_or("a structure past 2^64 bytes")?;
        out.push(json!({"name": name, "data_type": t.zarr}));
        fill.insert(name.clone(), t.fill);
    }
    Ok(Dtype { zarr: json!({"name": "struct", "configuration": {"fields": out}}), item, little, trailing: None, fill: J::Object(fill) })
}

/// A header's `descr` as a data type, or why it is not presented.
pub fn dtype(rules: &Rules, descr: &Lit) -> Result<Dtype, String> {
    match descr {
        Lit::Str(s) => simple(rules, s, false),
        Lit::List(l) => structure(rules, l),
        other => Err(format!("{} is not a type string or a list", other.repr())),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rules() -> &'static Rules {
        crate::rules::of("npy")
    }

    /// The literals NumPy writes, and the variations the grammar allows, each read.
    #[test]
    fn reads_headers() {
        let cases: &[(&str, Header)] = &[
            ("{'descr': '<f8', 'fortran_order': False, 'shape': (3, 4), }          \n",
             Header { descr: Lit::Str("<f8".into()), fortran_order: false, shape: vec![3, 4] }),
            ("{'descr': '|u1', 'fortran_order': True, 'shape': (7,), }", Header { descr: Lit::Str("|u1".into()), fortran_order: true, shape: vec![7] }),
            ("{\"shape\": (), \"descr\": \"<i2\", \"fortran_order\": False}", Header { descr: Lit::Str("<i2".into()), fortran_order: false, shape: vec![] }),
            ("{'descr': [('a', '<f4'), ('b\\'c', [('d', '|u1')])], 'fortran_order': False, 'shape': (2,)}",
             Header {
                 descr: Lit::List(vec![
                     Lit::Tuple(vec![Lit::Str("a".into()), Lit::Str("<f4".into())]),
                     Lit::Tuple(vec![Lit::Str("b'c".into()), Lit::List(vec![Lit::Tuple(vec![Lit::Str("d".into()), Lit::Str("|u1".into())])])]),
                 ]),
                 fortran_order: false,
                 shape: vec![2],
             }),
            ("{'descr': '\\u00e9', 'fortran_order': False, 'shape': (0, 2)}", Header { descr: Lit::Str("é".into()), fortran_order: false, shape: vec![0, 2] }),
            ("{'descr': '<f8', 'fortran_order': False, 'shape': (2L, 3L), }", Header { descr: Lit::Str("<f8".into()), fortran_order: false, shape: vec![2, 3] }),
        ];
        for (text, want) in cases {
            assert_eq!(&parse(text, 64).unwrap(), want, "{text}");
        }
    }

    #[test]
    fn rejects_a_header_that_is_not_a_dictionary() {
        assert!(parse("['descr']", 64).unwrap_err().starts_with("'{' expected"));
    }

    #[test]
    fn rejects_a_missing_key() {
        assert_eq!(parse("{'descr': '<f8', 'shape': (1,)}", 64).unwrap_err(), "its keys are [\"descr\", \"shape\"], not descr, fortran_order and shape");
    }

    #[test]
    fn rejects_a_repeated_key() {
        assert_eq!(parse("{'descr': '<f8', 'descr': '<f8'}", 64).unwrap_err(), "the key \"descr\" twice");
    }

    #[test]
    fn rejects_a_one_value_tuple_without_its_comma() {
        assert!(parse("{'descr': '<f8', 'fortran_order': False, 'shape': (3)}", 64).unwrap_err().starts_with("a tuple of one value without its comma"));
    }

    #[test]
    fn rejects_a_shape_that_is_not_integers() {
        assert_eq!(parse("{'descr': '<f8', 'fortran_order': False, 'shape': ('a',)}", 64).unwrap_err(), "'a' in shape, not an integer");
    }

    #[test]
    fn rejects_a_shape_past_the_rank_limit() {
        let text = format!("{{'descr': '<f8', 'fortran_order': False, 'shape': ({}), }}", "1, ".repeat(65));
        assert_eq!(parse(&text, 64).unwrap_err(), "a shape of 65 axes, more than 64");
    }

    #[test]
    fn rejects_text_after_the_dictionary() {
        assert_eq!(parse("{'descr': '<f8', 'fortran_order': False, 'shape': ()} x", 64).unwrap_err(), "text after the dictionary, at character 54");
    }

    #[test]
    fn rejects_an_unknown_escape() {
        assert_eq!(parse("{'d\\q': 1}", 64).unwrap_err(), "the escape \\q in a string");
    }

    /// Each presented type string and structure: its Zarr data type, element size, byte order,
    /// trailing axis and fill value.
    #[test]
    fn maps_data_types() {
        let s = |x: &str| Lit::Str(x.into());
        let cases: Vec<(Lit, J, u64, Option<bool>, Option<u64>, J)> = vec![
            (s("|b1"), json!("bool"), 1, None, None, json!(false)),
            (s("<i1"), json!("int8"), 1, None, None, json!(0)),
            (s(">u2"), json!("uint16"), 2, Some(false), None, json!(0)),
            (s("<f8"), json!("float64"), 8, Some(true), None, json!(0)),
            (s(">c16"), json!("complex128"), 16, Some(false), None, json!([0, 0])),
            (s("<M8[ns]"), json!({"name": "numpy.datetime64", "configuration": {"unit": "ns", "scale_factor": 1}}), 8, Some(true), None, json!(0)),
            (s(">m8[10s]"), json!({"name": "numpy.timedelta64", "configuration": {"unit": "s", "scale_factor": 10}}), 8, Some(false), None, json!(0)),
            (s("<U3"), json!({"name": "fixed_length_utf32", "configuration": {"length_bytes": 12}}), 12, Some(true), None, json!("")),
            (s("|S5"), json!("uint8"), 5, None, Some(5), json!(0)),
            (s("|V2"), json!("uint8"), 2, None, Some(2), json!(0)),
            (Lit::List(vec![
                Lit::Tuple(vec![s("x"), s(">f4")]),
                Lit::Tuple(vec![s("y"), Lit::List(vec![Lit::Tuple(vec![s("z"), s("|u1")])])]),
            ]),
             json!({"name": "struct", "configuration": {"fields": [{"name": "x", "data_type": "float32"}, {"name": "y", "data_type": {"name": "struct", "configuration": {"fields": [{"name": "z", "data_type": "uint8"}]}}}]}}),
             5, Some(false), None, json!({"x": 0, "y": {"z": 0}})),
        ];
        for (descr, zarr, item, little, trailing, fill) in cases {
            assert_eq!(dtype(rules(), &descr).unwrap(), Dtype { zarr, item, little, trailing, fill }, "{descr:?}");
        }
    }

    #[test]
    fn does_not_present_an_object_array() {
        assert_eq!(dtype(rules(), &Lit::Str("|O".into())).unwrap_err(), "an object array holds a pickle");
    }

    #[test]
    fn does_not_present_a_long_double() {
        assert_eq!(dtype(rules(), &Lit::Str("<f16".into())).unwrap_err(), "a long double has no Zarr data type");
    }

    #[test]
    fn does_not_present_a_datetime_without_a_unit() {
        assert_eq!(dtype(rules(), &Lit::Str("<M8".into())).unwrap_err(), "a datetime or timedelta without a unit");
    }

    #[test]
    fn does_not_present_a_native_byte_order() {
        assert_eq!(dtype(rules(), &Lit::Str("=f8".into())).unwrap_err(), "its byte order '=' is not <, > or |");
    }

    #[test]
    fn does_not_present_a_multi_byte_type_without_a_byte_order() {
        assert_eq!(dtype(rules(), &Lit::Str("|f8".into())).unwrap_err(), "a type of more than one byte with no byte order");
    }

    #[test]
    fn does_not_present_a_size_of_zero() {
        assert_eq!(dtype(rules(), &Lit::Str("|S0".into())).unwrap_err(), "a size of 0");
    }

    #[test]
    fn does_not_present_a_padding_field() {
        let d = Lit::List(vec![Lit::Tuple(vec![Lit::Str("".into()), Lit::Str("|V4".into())])]);
        assert_eq!(dtype(rules(), &d).unwrap_err(), "a padding field");
    }

    #[test]
    fn does_not_present_a_field_of_shape() {
        let d = Lit::List(vec![Lit::Tuple(vec![Lit::Str("a".into()), Lit::Str("<f4".into()), Lit::Tuple(vec![Lit::Int(2)])])]);
        assert_eq!(dtype(rules(), &d).unwrap_err(), "a field of shape");
    }

    #[test]
    fn does_not_present_a_string_field() {
        let d = Lit::List(vec![Lit::Tuple(vec![Lit::Str("a".into()), Lit::Str("|S3".into())])]);
        assert_eq!(dtype(rules(), &d).unwrap_err(), "a field of type |S3");
    }

    #[test]
    fn does_not_present_fields_of_two_byte_orders() {
        let d = Lit::List(vec![
            Lit::Tuple(vec![Lit::Str("a".into()), Lit::Str("<f4".into())]),
            Lit::Tuple(vec![Lit::Str("b".into()), Lit::Str(">i2".into())]),
        ]);
        assert_eq!(dtype(rules(), &d).unwrap_err(), "fields of two byte orders");
    }

    #[test]
    fn shows_a_descr_as_python_does() {
        let d = Lit::List(vec![Lit::Tuple(vec![Lit::Str("a".into()), Lit::Str("<f4".into()), Lit::Tuple(vec![Lit::Int(2)])])]);
        assert_eq!(d.repr(), "[('a', '<f4', (2,))]");
    }
}
