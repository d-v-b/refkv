//! Text helpers the parsers share: bytes as Latin-1, as UTF-8 or else Latin-1 (as text or as
//! a source metadata member), and the grammar of a decimal number.

use serde_json::{Value as J, json};

/// Bytes as Latin-1 (ISO 8859-1) text: each byte the character of its value.
pub fn latin1(b: &[u8]) -> String {
    b.iter().map(|&c| c as char).collect()
}

/// Bytes as text: UTF-8 if valid, else Latin-1.
pub fn utf8_or_latin1(b: &[u8]) -> String {
    match std::str::from_utf8(b) {
        Ok(s) => s.to_string(),
        Err(_) => latin1(b),
    }
}

/// Bytes as a source metadata text (conventions §6
/// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#6-source-metadata-as-json>):
/// UTF-8 when valid, else {"latin1": ...}.
pub fn text_json(b: &[u8]) -> J {
    match std::str::from_utf8(b) {
        Ok(s) => json!(s),
        Err(_) => json!({"latin1": latin1(b)}),
    }
}

/// Whether `t` is a decimal number, whole: `[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?`.
pub fn is_decimal(t: impl AsRef<[u8]>) -> bool {
    decimal_grammar(t.as_ref(), false)
}

/// Whether `t` is a decimal number with digits on both sides of its point, whole:
/// `[+-]?[0-9]+(\.[0-9]+)?([eE][+-]?[0-9]+)?`.
pub fn is_strict_decimal(t: impl AsRef<[u8]>) -> bool {
    decimal_grammar(t.as_ref(), true)
}

fn decimal_grammar(b: &[u8], strict: bool) -> bool {
    let mut i = 0;
    let digits = |i: &mut usize| {
        let s = *i;
        while *i < b.len() && b[*i].is_ascii_digit() {
            *i += 1;
        }
        *i - s
    };
    if i < b.len() && (b[i] == b'+' || b[i] == b'-') {
        i += 1;
    }
    let int_digits = digits(&mut i);
    let mut frac_digits = None;
    if i < b.len() && b[i] == b'.' {
        i += 1;
        frac_digits = Some(digits(&mut i));
    }
    let ok = if strict {
        int_digits > 0 && frac_digits != Some(0)
    } else {
        int_digits > 0 || frac_digits.is_some_and(|n| n > 0)
    };
    if !ok {
        return false;
    }
    if i < b.len() && (b[i] == b'e' || b[i] == b'E') {
        i += 1;
        if i < b.len() && (b[i] == b'+' || b[i] == b'-') {
            i += 1;
        }
        if digits(&mut i) == 0 {
            return false;
        }
    }
    i == b.len()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn decimals_by_grammar() {
        // (text, a decimal, a strict decimal)
        for (t, loose, strict) in [
            ("1", true, true),
            ("-1.5", true, true),
            ("+2.5e-3", true, true),
            ("1.", true, false),
            (".5", true, false),
            ("1.e3", true, false),
            ("1E+7", true, true),
            (".", false, false),
            ("", false, false),
            ("-", false, false),
            ("1e", false, false),
            ("1e+", false, false),
            ("1.5x", false, false),
            (" 1", false, false),
            ("nan", false, false),
        ] {
            assert_eq!((is_decimal(t), is_strict_decimal(t)), (loose, strict), "{t:?}");
        }
        assert_eq!((latin1(&[0x41, 0xb5]), utf8_or_latin1(&[0xb5]), utf8_or_latin1("é".as_bytes())), ("A\u{b5}".into(), "\u{b5}".into(), "é".into()));
        assert_eq!((text_json(b"a"), text_json(&[0xff])), (json!("a"), json!({"latin1": "\u{ff}"})));
    }
}
