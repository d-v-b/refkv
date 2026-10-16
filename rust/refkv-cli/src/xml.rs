//! The XML subset of a store listing (spec/virtualize.md §1.5
//! <https://github.com/d-v-b/refkv/blob/main/spec/virtualize.md#15-listing-a-store>), and the
//! listing it holds: python/src/refkv/virtualize/store.py's `_Xml` and `parse_listing`. A document
//! outside the subset is rejected.

const MAX_DEPTH: usize = 256;
const MAX_SAFE: u64 = (1 << 53) - 1;

#[derive(Debug, Default)]
pub struct Element {
    pub name: String,
    pub children: Vec<Element>,
    pub text: Vec<String>,
    pub attributes: Vec<(String, String)>,
}

fn is_char(c: u32) -> bool {
    matches!(c, 0x9 | 0xa | 0xd) || (0x20..=0xd7ff).contains(&c) || (0xe000..=0xfffd).contains(&c) || (0x10000..=0x10ffff).contains(&c)
}

fn name_start(c: char) -> bool {
    c.is_ascii_alphabetic() || c == '_' || c == ':'
}

fn name_char(c: char) -> bool {
    name_start(c) || c.is_ascii_digit() || c == '.' || c == '-'
}

fn ws(c: char) -> bool {
    matches!(c, ' ' | '\t' | '\r' | '\n')
}

struct Xml<'a> {
    s: Vec<char>,
    pos: usize,
    what: &'a str,
    depth: usize,
}

type Res<T> = Result<T, String>;

impl Xml<'_> {
    fn fail<T>(&self, why: impl std::fmt::Display) -> Res<T> {
        Err(format!("{} is not well formed: {why} at {}", self.what, self.pos))
    }

    fn at(&self, t: &str) -> bool {
        (self.pos..).zip(t.chars()).all(|(i, c)| self.s.get(i) == Some(&c))
    }

    fn find(&self, t: &str, from: usize) -> Option<usize> {
        let t: Vec<char> = t.chars().collect();
        (from..self.s.len().saturating_sub(t.len() - 1)).find(|&i| self.s[i..i + t.len()] == t[..])
    }

    fn skip_ws(&mut self) {
        while self.pos < self.s.len() && ws(self.s[self.pos]) {
            self.pos += 1;
        }
    }

    fn chars(&self, from: usize, to: usize) -> Res<()> {
        for &c in &self.s[from..to] {
            if !is_char(c as u32) {
                return self.fail(format!("character U+{:04X}", c as u32));
            }
        }
        Ok(())
    }

    fn comment(&mut self) -> Res<()> {
        match self.find("--", self.pos + 4) {
            Some(end) if self.s[end..].starts_with(&['-', '-', '>']) => {
                self.chars(self.pos + 4, end)?;
                self.pos = end + 3;
                Ok(())
            }
            _ => self.fail("bad comment"),
        }
    }

    fn misc(&mut self) -> Res<()> {
        loop {
            self.skip_ws();
            if self.at("<!--") {
                self.comment()?;
            } else {
                return Ok(());
            }
        }
    }

    fn name(&mut self) -> Res<String> {
        let start = self.pos;
        if start >= self.s.len() || !name_start(self.s[start]) {
            return self.fail("expected a name");
        }
        self.pos += 1;
        while self.pos < self.s.len() && name_char(self.s[self.pos]) {
            self.pos += 1;
        }
        Ok(self.s[start..self.pos].iter().collect())
    }

    fn reference(&mut self) -> Res<String> {
        let Some(end) = (self.pos..self.s.len()).find(|&i| self.s[i] == ';') else {
            return self.fail("unterminated reference");
        };
        let body: String = self.s[self.pos + 1..end].iter().collect();
        self.pos = end + 1;
        match body.as_str() {
            "lt" => return Ok("<".into()),
            "gt" => return Ok(">".into()),
            "amp" => return Ok("&".into()),
            "quot" => return Ok("\"".into()),
            "apos" => return Ok("'".into()),
            _ => {}
        }
        let v: i64 = if let Some(h) = body.strip_prefix("#x").filter(|h| !h.is_empty() && h.chars().all(|c| c.is_ascii_hexdigit())) {
            if body.len() < 12 { i64::from_str_radix(h, 16).unwrap_or(-1) } else { -1 }
        } else if let Some(d) = body.strip_prefix('#').filter(|d| !d.is_empty() && d.chars().all(|c| c.is_ascii_digit())) {
            if body.len() < 12 { d.parse().unwrap_or(-1) } else { -1 }
        } else {
            return self.fail(format!("unknown reference &{body};"));
        };
        match u32::try_from(v).ok().filter(|&c| is_char(c)).and_then(char::from_u32) {
            Some(c) => Ok(c.to_string()),
            None => self.fail(format!("reference &{body}; is not a character")),
        }
    }

    /// Text up to `<` or `&` (or the quote `stop`), checked.
    fn chardata(&mut self, stop: char) -> Res<String> {
        let start = self.pos;
        while self.pos < self.s.len() && !matches!(self.s[self.pos], '<' | '&') && self.s[self.pos] != stop {
            self.pos += 1;
        }
        self.chars(start, self.pos)?;
        Ok(self.s[start..self.pos].iter().collect())
    }

    fn element(&mut self) -> Res<Element> {
        if !self.at("<") {
            return self.fail("expected an element");
        }
        self.depth += 1;
        if self.depth > MAX_DEPTH {
            return self.fail(format!("elements nest more than {MAX_DEPTH} deep"));
        }
        let el = self.element_body()?;
        self.depth -= 1;
        Ok(el)
    }

    fn element_body(&mut self) -> Res<Element> {
        self.pos += 1;
        let mut el = Element { name: self.name()?, ..Default::default() };
        while self.pos < self.s.len() && ws(self.s[self.pos]) {
            self.skip_ws();
            if self.pos < self.s.len() && name_start(self.s[self.pos]) {
                let name = self.name()?;
                self.skip_ws();
                if !self.at("=") {
                    return self.fail("expected =");
                }
                self.pos += 1;
                self.skip_ws();
                let q = self.s.get(self.pos).copied().unwrap_or('\0');
                if q != '\'' && q != '"' {
                    return self.fail("expected a quoted value");
                }
                self.pos += 1;
                let mut value = String::new();
                loop {
                    value += &self.chardata(q)?;
                    if self.at("&") {
                        value += &self.reference()?;
                    } else if self.s.get(self.pos) == Some(&q) {
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
            if self.pos >= self.s.len() {
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
                let child = self.element()?;
                el.children.push(child);
            } else if self.at("&") {
                let r = self.reference()?;
                el.text.push(r);
            } else {
                let t = self.chardata('<')?;
                if t.contains("]]>") {
                    return self.fail("]]> in character data");
                }
                el.text.push(t);
            }
        }
    }

    fn document(&mut self) -> Res<Element> {
        self.skip_ws();
        if self.at("<?xml") && self.s.get(self.pos + 5).is_some_and(|&c| ws(c)) {
            let Some(end) = self.find("?>", self.pos + 5) else {
                return self.fail("unterminated XML declaration");
            };
            self.chars(self.pos, end)?;
            self.pos = end + 2;
        }
        self.misc()?;
        let root = self.element()?;
        self.misc()?;
        if self.pos != self.s.len() {
            return self.fail("content after the root element");
        }
        Ok(root)
    }
}

/// The root element of a document in the XML subset, or a rejection naming `what`.
pub fn parse_xml(text: &str, what: &str) -> Result<Element, String> {
    Xml { s: text.chars().collect(), pos: 0, what, depth: 0 }.document()
}

fn text(el: &Element) -> Result<String, String> {
    if !el.children.is_empty() {
        return Err(format!("listing: <{}> has a child element", el.name));
    }
    Ok(el.text.concat())
}

/// The objects one ListObjectsV2 response lists, and its continuation token when the listing is
/// truncated (spec/virtualize.md §1.5 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize.md#15-listing-a-store>); an error is a rejection.
#[allow(clippy::type_complexity)]
pub fn parse_listing(body: &[u8], prefix: &str) -> Result<(Vec<(String, u64)>, Option<String>), String> {
    let text_ = std::str::from_utf8(body).map_err(|_| "listing is not UTF-8".to_string())?;
    let root = parse_xml(text_, "listing")?;
    if root.name != "ListBucketResult" {
        return Err(format!("listing root element is <{}>, not <ListBucketResult>", root.name));
    }
    fn named<'a>(root: &'a Element, n: &'a str) -> impl Iterator<Item = &'a Element> {
        root.children.iter().filter(move |c| c.name == n)
    }
    let truncated: Vec<&Element> = named(&root, "IsTruncated").collect();
    let t = match truncated.as_slice() {
        [t] => text(t)?,
        _ => String::new(),
    };
    if truncated.len() != 1 || (t != "true" && t != "false") {
        return Err("listing needs exactly one IsTruncated of true or false".into());
    }
    let tokens: Vec<&Element> = named(&root, "NextContinuationToken").collect();
    if tokens.len() > 1 {
        return Err("listing has more than one NextContinuationToken".into());
    }
    let token = tokens.first().map(|e| text(e)).transpose()?;
    let mut objects = Vec::new();
    for c in named(&root, "Contents") {
        let keys: Vec<&Element> = c.children.iter().filter(|x| x.name == "Key").collect();
        let sizes: Vec<&Element> = c.children.iter().filter(|x| x.name == "Size").collect();
        if keys.len() != 1 || sizes.len() != 1 {
            return Err("listing Contents needs exactly one Key and one Size".into());
        }
        let (key, size) = (text(keys[0])?, text(sizes[0])?);
        let n = (!size.is_empty() && size.bytes().all(|c| c.is_ascii_digit()) && size.trim_start_matches('0').len() <= 16)
            .then(|| size.parse::<u64>().ok())
            .flatten()
            .filter(|&n| n <= MAX_SAFE);
        let Some(n) = n else {
            return Err(format!("listing Size {:?} is not a size", size.chars().take(40).collect::<String>()));
        };
        if !key.starts_with(prefix) {
            return Err(format!("listed key {key:?} is outside the prefix {prefix:?}"));
        }
        objects.push((key, n));
    }
    if t == "true" {
        if token.as_deref().unwrap_or("").is_empty() {
            return Err("truncated listing without a NextContinuationToken".into());
        }
        if objects.is_empty() {
            // a page that lists nothing could be followed forever
            return Err("truncated listing without a Contents".into());
        }
        return Ok((objects, token));
    }
    Ok((objects, None))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn listing(contents: &str, truncated: &str, token: &str) -> Vec<u8> {
        format!("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<!-- c --><ListBucketResult xmlns=\"x\"><IsTruncated>{truncated}</IsTruncated>{token}{contents}</ListBucketResult> ").into_bytes()
    }

    #[test]
    fn listings() {
        let one = "<Contents><Key>p/a&amp;b</Key><Size>0012</Size></Contents><Contents><Key>p/&#x3b1;&#946;</Key><Size>9007199254740991</Size></Contents>";
        let (objects, token) = parse_listing(&listing(one, "false", ""), "p/").unwrap();
        assert_eq!(objects, vec![("p/a&b".into(), 12), ("p/αβ".into(), (1 << 53) - 1)]);
        assert_eq!(token, None);
        let (_, token) = parse_listing(&listing(one, "true", "<NextContinuationToken>t&lt;1</NextContinuationToken>"), "p/").unwrap();
        assert_eq!(token.as_deref(), Some("t<1"));
        let (objects, _) = parse_listing(&listing("", "false", "<NextContinuationToken>x</NextContinuationToken>"), "").unwrap();
        assert!(objects.is_empty());
        let el = parse_xml("<a x='1&amp;' y=\"2\"><b/><!-- k --></a>", "doc").unwrap();
        assert_eq!(el.attributes, vec![("x".into(), "1&".into()), ("y".into(), "2".into())]);
        assert_eq!(el.children[0].name, "b");
    }

    fn rejects(body: &str) {
        assert!(parse_listing(body.as_bytes(), "p/").is_err(), "{body}");
    }

    #[test]
    fn rejects_not_utf8() {
        assert!(parse_listing(b"<ListBucketResult>\xff</ListBucketResult>", "").is_err());
    }

    #[test]
    fn rejects_another_root() {
        rejects("<Other><IsTruncated>false</IsTruncated></Other>");
    }

    #[test]
    fn rejects_no_is_truncated() {
        rejects("<ListBucketResult></ListBucketResult>");
    }

    #[test]
    fn rejects_a_bad_is_truncated() {
        rejects("<ListBucketResult><IsTruncated>yes</IsTruncated></ListBucketResult>");
    }

    #[test]
    fn rejects_two_tokens() {
        rejects("<ListBucketResult><IsTruncated>false</IsTruncated><NextContinuationToken>a</NextContinuationToken><NextContinuationToken>b</NextContinuationToken></ListBucketResult>");
    }

    #[test]
    fn rejects_contents_without_a_size() {
        rejects("<ListBucketResult><IsTruncated>false</IsTruncated><Contents><Key>p/a</Key></Contents></ListBucketResult>");
    }

    #[test]
    fn rejects_a_size_over_2_53() {
        rejects("<ListBucketResult><IsTruncated>false</IsTruncated><Contents><Key>p/a</Key><Size>9007199254740992</Size></Contents></ListBucketResult>");
    }

    #[test]
    fn rejects_a_negative_size() {
        rejects("<ListBucketResult><IsTruncated>false</IsTruncated><Contents><Key>p/a</Key><Size>-1</Size></Contents></ListBucketResult>");
    }

    #[test]
    fn rejects_a_key_outside_the_prefix() {
        rejects("<ListBucketResult><IsTruncated>false</IsTruncated><Contents><Key>q/a</Key><Size>1</Size></Contents></ListBucketResult>");
    }

    #[test]
    fn rejects_a_text_with_a_child() {
        rejects("<ListBucketResult><IsTruncated>false<b/></IsTruncated></ListBucketResult>");
    }

    #[test]
    fn rejects_truncated_without_a_token() {
        rejects("<ListBucketResult><IsTruncated>true</IsTruncated><Contents><Key>p/a</Key><Size>1</Size></Contents></ListBucketResult>");
    }

    #[test]
    fn rejects_truncated_without_contents() {
        rejects("<ListBucketResult><IsTruncated>true</IsTruncated><NextContinuationToken>t</NextContinuationToken></ListBucketResult>");
    }

    #[test]
    fn rejects_a_doctype() {
        rejects("<!DOCTYPE x><ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>");
    }

    #[test]
    fn rejects_a_processing_instruction_inside() {
        rejects("<ListBucketResult><?pi?><IsTruncated>false</IsTruncated></ListBucketResult>");
    }

    #[test]
    fn rejects_mismatched_tags() {
        rejects("<ListBucketResult><IsTruncated>false</IsTrunc></ListBucketResult>");
    }

    #[test]
    fn rejects_an_unknown_entity() {
        rejects("<ListBucketResult><IsTruncated>false&nbsp;</IsTruncated></ListBucketResult>");
    }

    #[test]
    fn rejects_a_reference_to_no_character() {
        rejects("<ListBucketResult><IsTruncated>&#0;</IsTruncated></ListBucketResult>");
    }

    #[test]
    fn rejects_a_control_character() {
        rejects("<ListBucketResult><IsTruncated>\u{1}false</IsTruncated></ListBucketResult>");
    }

    #[test]
    fn rejects_cdata_end_in_text() {
        rejects("<ListBucketResult><IsTruncated>false</IsTruncated>]]></ListBucketResult>");
    }

    #[test]
    fn rejects_a_bad_comment() {
        rejects("<!-- a -- b --><ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>");
    }

    #[test]
    fn rejects_content_after_the_root() {
        rejects("<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult><x/>");
    }

    #[test]
    fn rejects_an_unterminated_element() {
        rejects("<ListBucketResult><IsTruncated>false</IsTruncated>");
    }

    #[test]
    fn rejects_deep_nesting() {
        rejects(&format!("<ListBucketResult>{}{}</ListBucketResult>", "<a>".repeat(300), "</a>".repeat(300)));
    }

    #[test]
    fn rejects_an_unquoted_attribute() {
        rejects("<ListBucketResult a=1><IsTruncated>false</IsTruncated></ListBucketResult>");
    }
}
