//! The XML subset of spec/zarrify.md §1.5
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store> as a tree: the
//! reading of a SAFE product's metadata documents (spec/zarrify/safe.md §2.1
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safe.md#21-objects>). Elements,
//! attributes, character data, the five entities and character references, and comments;
//! anything else (CDATA, processing instructions, a DOCTYPE) is not in the subset, nor is
//! nesting deeper than 256.

pub const MAX_DEPTH: usize = 256;

#[derive(Debug, Default, Clone)]
pub struct Element {
    pub name: String,
    pub children: Vec<Element>,
    /// the character data, references replaced, in pieces
    pub text: Vec<String>,
    /// (name, value) in document order, values with references replaced
    pub attributes: Vec<(String, String)>,
}

fn is_char(c: u32) -> bool {
    matches!(c, 0x9 | 0xA | 0xD) || (0x20..=0xD7FF).contains(&c) || (0xE000..=0xFFFD).contains(&c) || (0x10000..=0x10FFFF).contains(&c)
}

fn name_start(b: u8) -> bool {
    b.is_ascii_alphabetic() || b == b'_' || b == b':'
}

fn name_char(b: u8) -> bool {
    name_start(b) || b.is_ascii_digit() || b == b'.' || b == b'-'
}

fn ws(b: u8) -> bool {
    matches!(b, b' ' | b'\t' | b'\r' | b'\n')
}

struct Xml<'a> {
    s: &'a str,
    b: &'a [u8],
    pos: usize,
    what: &'a str,
    depth: usize,
}

type R<T> = Result<T, String>;

impl<'a> Xml<'a> {
    fn fail<T>(&self, why: &str) -> R<T> {
        Err(format!("{} is not well formed: {why} at {}", self.what, self.pos))
    }

    fn at(&self, t: &str) -> bool {
        self.b[self.pos..].starts_with(t.as_bytes())
    }

    fn skip_ws(&mut self) {
        while self.pos < self.b.len() && ws(self.b[self.pos]) {
            self.pos += 1;
        }
    }

    fn chars(&self, t: &str) -> R<()> {
        for ch in t.chars() {
            if !is_char(ch as u32) {
                return self.fail(&format!("character U+{:04X}", ch as u32));
            }
        }
        Ok(())
    }

    fn find(&self, pat: &str, from: usize) -> Option<usize> {
        self.s.get(from..).and_then(|r| r.find(pat)).map(|k| k + from)
    }

    fn comment(&mut self) -> R<()> {
        let end = self.find("--", self.pos + 4);
        match end {
            Some(e) if self.b[e..].starts_with(b"-->") => {
                self.chars(&self.s[self.pos + 4..e])?;
                self.pos = e + 3;
                Ok(())
            }
            _ => self.fail("bad comment"),
        }
    }

    fn misc(&mut self) -> R<()> {
        loop {
            self.skip_ws();
            if self.at("<!--") {
                self.comment()?;
            } else {
                return Ok(());
            }
        }
    }

    fn name(&mut self) -> R<String> {
        let start = self.pos;
        if start >= self.b.len() || !name_start(self.b[start]) {
            return self.fail("expected a name");
        }
        self.pos += 1;
        while self.pos < self.b.len() && name_char(self.b[self.pos]) {
            self.pos += 1;
        }
        Ok(self.s[start..self.pos].to_string())
    }

    fn reference(&mut self) -> R<String> {
        let Some(end) = self.find(";", self.pos) else { return self.fail("unterminated reference") };
        let body = &self.s[self.pos + 1..end];
        self.pos = end + 1;
        let named = match body {
            "lt" => Some("<"),
            "gt" => Some(">"),
            "amp" => Some("&"),
            "quot" => Some("\""),
            "apos" => Some("'"),
            _ => None,
        };
        if let Some(t) = named {
            return Ok(t.to_string());
        }
        let v: i64 = if let Some(h) = body.strip_prefix("#x").filter(|h| !h.is_empty() && h.bytes().all(|c| c.is_ascii_hexdigit())) {
            if body.len() < 12 { i64::from_str_radix(h, 16).unwrap_or(-1) } else { -1 }
        } else if let Some(d) = body.strip_prefix('#').filter(|d| !d.is_empty() && d.bytes().all(|c| c.is_ascii_digit())) {
            if body.len() < 12 { d.parse().unwrap_or(-1) } else { -1 }
        } else {
            return self.fail(&format!("unknown reference &{body};"));
        };
        match u32::try_from(v).ok().filter(|&c| is_char(c)).and_then(char::from_u32) {
            Some(c) => Ok(c.to_string()),
            None => self.fail(&format!("reference &{body}; is not a character")),
        }
    }

    /// Text up to `<` or `&` (or the quote `stop`), checked.
    fn chardata(&mut self, stop: u8) -> R<String> {
        let start = self.pos;
        while self.pos < self.b.len() && self.b[self.pos] != b'<' && self.b[self.pos] != b'&' && self.b[self.pos] != stop {
            self.pos += 1;
        }
        let t = &self.s[start..self.pos];
        self.chars(t)?;
        Ok(t.to_string())
    }

    fn element(&mut self) -> R<Element> {
        if !self.at("<") {
            return self.fail("expected an element");
        }
        self.depth += 1;
        if self.depth > MAX_DEPTH {
            return self.fail(&format!("elements nest more than {MAX_DEPTH} deep"));
        }
        let el = self.element_body()?;
        self.depth -= 1;
        Ok(el)
    }

    fn element_body(&mut self) -> R<Element> {
        self.pos += 1;
        let mut el = Element { name: self.name()?, ..Default::default() };
        let n = self.b.len();
        while self.pos < n && ws(self.b[self.pos]) {
            self.skip_ws();
            if self.pos < n && name_start(self.b[self.pos]) {
                let name = self.name()?;
                self.skip_ws();
                if !self.at("=") {
                    return self.fail("expected =");
                }
                self.pos += 1;
                self.skip_ws();
                let q = self.b.get(self.pos).copied().unwrap_or(0);
                if q != b'\'' && q != b'"' {
                    return self.fail("expected a quoted value");
                }
                self.pos += 1;
                let mut value = String::new();
                loop {
                    value.push_str(&self.chardata(q)?);
                    if self.at("&") {
                        value.push_str(&self.reference()?);
                    } else if self.pos < n && self.b[self.pos] == q {
                        self.pos += 1;
                        break;
                    } else {
                        return self.fail("bad attribute value");
                    }
                }
                el.attributes.push((name, value));
                continue;
            }
            break;
        }
        if self.at("/>") {
            self.pos += 2;
            return Ok(el);
        }
        if !self.at(">") {
            return self.fail("expected >");
        }
        self.pos += 1;
        loop {
            if self.pos >= n {
                return self.fail("unterminated element");
            }
            if self.at("</") {
                self.pos += 2;
                if self.name()? != el.name {
                    return self.fail("mismatched end tag");
                }
                self.skip_ws();
                if !self.at(">") {
                    return self.fail("expected >");
                }
                self.pos += 1;
                return Ok(el);
            }
            if self.at("<!--") {
                self.comment()?;
            } else if self.at("<!") || self.at("<?") {
                return self.fail("unsupported markup");
            } else if self.at("<") {
                let c = self.element()?;
                el.children.push(c);
            } else if self.at("&") {
                let t = self.reference()?;
                el.text.push(t);
            } else {
                let t = self.chardata(b'<')?;
                if t.contains("]]>") {
                    return self.fail("]]> in character data");
                }
                el.text.push(t);
            }
        }
    }

    fn document(&mut self) -> R<Element> {
        self.skip_ws();
        if self.at("<?xml") && self.b.get(self.pos + 5).is_some_and(|&c| ws(c)) {
            let Some(end) = self.find("?>", self.pos + 5) else { return self.fail("unterminated XML declaration") };
            self.chars(&self.s[self.pos..end])?;
            self.pos = end + 2;
        }
        self.misc()?;
        let root = self.element()?;
        self.misc()?;
        if self.pos != self.b.len() {
            return self.fail("content after the root element");
        }
        Ok(root)
    }
}

/// The root element of a document in the subset, or the rejection (naming `what`).
pub fn parse(s: &str, what: &str) -> Result<Element, String> {
    Xml { s, b: s.as_bytes(), pos: 0, what, depth: 0 }.document()
}

impl Element {
    /// The value of the attribute `name` (as written), the first if repeated.
    pub fn attr(&self, name: &str) -> Option<&str> {
        self.attributes.iter().find(|(n, _)| n == name).map(|(_, v)| v.as_str())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_the_subset() {
        let e = parse("<?xml version=\"1.0\"?>\n<!-- c --><a x='1' y=\"&lt;&#65;&#x42;\"><b>t&amp;u</b> v<c/></a>\n", "d").unwrap();
        assert_eq!(e.name, "a");
        assert_eq!(e.attributes, vec![("x".into(), "1".into()), ("y".into(), "<AB".into())]);
        assert_eq!(e.children.len(), 2);
        assert_eq!(e.children[0].text.concat(), "t&u");
        assert_eq!(e.text.concat(), " v");
    }

    #[test]
    fn rejects_a_doctype() {
        assert!(parse("<!DOCTYPE a><a/>", "d").unwrap_err().contains("not well formed"));
    }

    #[test]
    fn rejects_deep_nesting() {
        let s = "<a>".repeat(257) + &"</a>".repeat(257);
        assert!(parse(&s, "d").unwrap_err().contains("nest more than 256"));
    }

    #[test]
    fn rejects_an_unclosed_element() {
        assert!(parse("<a><b></b>", "d").unwrap_err().contains("unterminated element"));
    }

    #[test]
    fn rejects_a_bad_reference() {
        assert!(parse("<a>&#0;</a>", "d").unwrap_err().contains("is not a character"));
    }
}
