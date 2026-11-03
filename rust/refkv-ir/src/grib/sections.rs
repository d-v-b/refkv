//! The sections of a GRIB edition 2 message (WMO-No. 306, FM 92 GRIB edition 2): section 0's
//! fields, and each field's sections, the ones a repeat in the message did not restate taken
//! from before it.

/// Section 0, the indicator: `GRIB`, two reserved octets, the discipline, the edition, and the
/// message's length.
pub const INDICATOR: u64 = 16;
/// Section 8, the end.
pub const END: &[u8; 4] = b"7777";

/// A big-endian unsigned integer of `n` octets at `at` of `b` (n ≤ 8).
pub fn uint(b: &[u8], at: usize, n: usize) -> u64 {
    b[at..at + n].iter().fold(0, |v, &x| (v << 8) | x as u64)
}

/// A signed integer of `n` octets at `at`, as GRIB writes them: the first bit the sign, the rest
/// the magnitude (n ≤ 8).
pub fn signed(b: &[u8], at: usize, n: usize) -> i64 {
    let v = uint(b, at, n);
    let sign = 1u64 << (8 * n - 1);
    let m = (v & (sign - 1)) as i64;
    if v & sign != 0 { -m } else { m }
}

/// A message's section 0: its discipline, edition and length.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Indicator {
    pub discipline: u8,
    pub edition: u8,
    pub length: u64,
}

/// Section 0 from a message's first 16 bytes, or why they are not one.
pub fn indicator(b: &[u8]) -> Result<Indicator, String> {
    if b.len() < INDICATOR as usize || &b[..4] != b"GRIB" {
        return Err("not a GRIB message: no GRIB indicator".into());
    }
    let edition = b[7];
    if edition != 2 {
        return Err(format!("a GRIB edition {edition} message: only edition 2 is read"));
    }
    Ok(Indicator { discipline: b[6], edition, length: uint(b, 8, 8) })
}

/// One section: its number, and its offset and length in the message.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Section {
    pub number: u8,
    pub offset: u64,
    pub length: u64,
}

/// A field of a message: the sections that describe it, each the last of its number at or
/// before its section 7. Section 2 is absent when the message has none; section 6 is always
/// present in a valid message.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Field {
    pub s1: Option<Section>,
    pub s2: Option<Section>,
    pub s3: Option<Section>,
    pub s4: Option<Section>,
    pub s5: Option<Section>,
    pub s6: Option<Section>,
    pub s7: Option<Section>,
}

/// The sections of a message, `b` its bytes from its first (`b.len()` may be less than its
/// length: the sections are then those that `b` holds whole, and the last may be cut off, given
/// with its stated length). Stops at `7777`, or at the first section whose header `b` does not
/// hold. A section that is shorter than its header, or that does not end within the message's
/// length, is an error.
pub fn sections(b: &[u8], length: u64) -> Result<Vec<Section>, String> {
    let mut out = Vec::new();
    let mut pos = INDICATOR;
    loop {
        if pos + 4 <= b.len() as u64 && &b[pos as usize..pos as usize + 4] == END {
            if pos + 4 != length {
                return Err(format!("a GRIB message's end (7777) is at byte {pos} of its {length}"));
            }
            return Ok(out);
        }
        if pos + 5 > b.len() as u64 {
            return Ok(out);
        }
        let n = uint(b, pos as usize, 4);
        let number = b[pos as usize + 4];
        if n < 5 || pos + n > length.saturating_sub(4) {
            return Err(format!("a GRIB message's section {number} at byte {pos} has the length {n}, past the message's end"));
        }
        out.push(Section { number, offset: pos, length: n });
        pos += n;
    }
}

/// The fields of a message whose sections are `s`: one per section 7, in order. After a field,
/// the message may restate sections 2 to 7, 3 to 7 or 4 to 7 (FM 92 regulation 92.1.3) for the
/// next; any other order, or a number other than 1–7, is an error.
pub fn fields(s: &[Section]) -> Result<Vec<Field>, String> {
    let mut out = Vec::new();
    let mut cur = Field::default();
    let mut last = 0u8;
    for &x in s {
        let ok = match x.number {
            1 => last == 0,
            2 => last == 1 || last == 7,
            3 => last == 1 || last == 2 || last == 7,
            4 => last == 3 || last == 7,
            5 => last == 4,
            6 => last == 5,
            7 => last == 6,
            _ => false,
        };
        if !ok {
            return Err(format!("its section {} follows section {last}", x.number));
        }
        match x.number {
            1 => cur.s1 = Some(x),
            2 => cur.s2 = Some(x),
            3 => cur.s3 = Some(x),
            4 => cur.s4 = Some(x),
            5 => cur.s5 = Some(x),
            6 => cur.s6 = Some(x),
            _ => {
                cur.s7 = Some(x);
                out.push(cur);
                // the next field restates sections 2, 3 or 4 on, and always 5, 6 and 7
                cur.s7 = None;
            }
        }
        last = x.number;
    }
    if last != 7 {
        return Err(format!("it ends after section {last}, not section 7"));
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn signed_is_sign_and_magnitude() {
        assert_eq!(signed(&[0x80, 0x02], 0, 2), -2);
        assert_eq!(signed(&[0x00, 0x02], 0, 2), 2);
        assert_eq!(signed(&[0x80, 0x00], 0, 2), 0);
    }
}
