//! JSON text as Python's `json.dumps` writes it by default, for the summary `python -m
//! refkv.zarrify` prints: `", "` and `": "` separators, every character outside printable ASCII
//! escaped, and floats as Python's `repr` writes them.

use serde_json::Value;

/// A float as Python's `repr`: the shortest digits that round-trip, positional when the decimal
/// exponent is in [-4, 16), else in scientific notation with a signed exponent of two digits or
/// more.
pub fn float_repr(f: f64) -> String {
    if f.is_nan() {
        return "NaN".into();
    }
    if f.is_infinite() {
        return if f > 0.0 { "Infinity".into() } else { "-Infinity".into() };
    }
    let e = format!("{f:e}"); // shortest round-trip digits: "-1.25e-7"
    let (mantissa, exp) = e.split_once('e').unwrap();
    let exp: i32 = exp.parse().unwrap();
    let (sign, mantissa) = mantissa.strip_prefix('-').map_or(("", mantissa), |m| ("-", m));
    let digits: String = mantissa.chars().filter(|c| *c != '.').collect();
    if (-4..16).contains(&exp) {
        let point = exp + 1; // digits before the decimal point
        let s = if point <= 0 {
            format!("0.{}{digits}", "0".repeat(-point as usize))
        } else if point as usize >= digits.len() {
            format!("{digits}{}.0", "0".repeat(point as usize - digits.len()))
        } else {
            format!("{}.{}", &digits[..point as usize], &digits[point as usize..])
        };
        return format!("{sign}{s}");
    }
    let m = if digits.len() == 1 { digits.clone() } else { format!("{}.{}", &digits[..1], &digits[1..]) };
    format!("{sign}{m}e{}{:02}", if exp < 0 { '-' } else { '+' }, exp.abs())
}

fn string(s: &str, out: &mut String) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            ' '..='~' => out.push(c),
            _ => {
                let mut units = [0u16; 2];
                for u in c.encode_utf16(&mut units) {
                    out.push_str(&format!("\\u{u:04x}"));
                }
            }
        }
    }
    out.push('"');
}

fn write(v: &Value, out: &mut String) {
    match v {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => match (n.as_i64(), n.as_u64(), n.as_f64()) {
            (Some(i), _, _) => out.push_str(&i.to_string()),
            (_, Some(u), _) => out.push_str(&u.to_string()),
            (_, _, Some(f)) => out.push_str(&float_repr(f)),
            _ => out.push_str(&n.to_string()),
        },
        Value::String(s) => string(s, out),
        Value::Array(a) => {
            out.push('[');
            for (i, x) in a.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                write(x, out);
            }
            out.push(']');
        }
        Value::Object(m) => {
            out.push('{');
            for (i, (k, x)) in m.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                string(k, out);
                out.push_str(": ");
                write(x, out);
            }
            out.push('}');
        }
    }
}

/// `json.dumps(v)`.
pub fn dumps(v: &Value) -> String {
    let mut s = String::new();
    write(v, &mut s);
    s
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn floats() {
        for (f, want) in [
            (0.0, "0.0"),
            (-0.0, "-0.0"),
            (1.0, "1.0"),
            (0.1, "0.1"),
            (1.5e-5, "1.5e-05"),
            (0.0001, "0.0001"),
            (123456.789, "123456.789"),
            (1e16, "1e+16"),
            (1.2345e16, "1.2345e+16"),
            (9999999999999998.0, "9999999999999998.0"),
            (1e100, "1e+100"),
            (-2.5e-300, "-2.5e-300"),
            (f64::INFINITY, "Infinity"),
        ] {
            assert_eq!(float_repr(f), want, "{f}");
        }
    }

    #[test]
    fn documents() {
        let v: Value = serde_json::from_str(r#"{"format": "tiff", "a": [1, -2, 18446744073709551615, 0.5, true, null], "é\n": {}, "e": []}"#).unwrap();
        // `U` stands for a backslash and a `u`: the escapes of the characters outside ASCII
        let u = |s: &str| s.replace('U', "\\u");
        assert_eq!(dumps(&v), u(r#"{"format": "tiff", "a": [1, -2, 18446744073709551615, 0.5, true, null], "U00e9\n": {}, "e": []}"#));
        assert_eq!(dumps(&Value::String("\u{1f600}\u{7f}\"\\".into())), u(r#""Ud83dUde00U007f\"\\""#));
    }
}
