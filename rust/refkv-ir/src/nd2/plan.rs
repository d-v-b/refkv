//! refkv's plan of an ND2 file (spec/zarrify/nd2.md §4
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nd2.md#4-the-image>): the decisions of
//! refkv's convention, made from the parser's description (§7.5
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nd2.md#75-the-description>) alone, by the
//! schema's own derived quantities (`schema/nd2.json`'s `derive`). The parser checked the file with
//! the same quantities, so a plan of a description it returned does not fail.

use crate::error::Error;

use crate::expr::V;
use crate::schema::{self, Loop};
use serde_json::Value as J;

/// A loop of the plan: its kind, count and whether its index runs backward (a z loop whose
/// `dZStep` is negative).
pub struct PlanLoop {
    pub kind: String,
    pub count: u64,
    pub flip: bool,
}

/// refkv's decisions of an ND2 file.
pub struct Plan {
    pub width: u64,
    pub height: u64,
    pub width_bytes: u64,
    /// a row's samples' bytes, `R`
    pub row: u64,
    pub components: u64,
    pub bits: u64,
    /// `uiBpcSignificant`, which bounds the window
    pub significant: f64,
    pub compressed: bool,
    pub data_type: &'static str,
    pub loops: Vec<PlanLoop>,
    pub positions: u64,
    /// the frames the loops count, `N`, as the summary writes it
    pub frames: J,
    pub calibrated: bool,
    /// whether the time loop has a positive period and the z loop a positive step: whether their
    /// axes have a scale and a unit (§4.3
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nd2.md#43-output>)
    pub timed: bool,
    pub stepped: bool,
    /// the scales along `x`, `y`, `z` and `t` (s), each 1 where the convention gives none
    pub scales: [J; 4],
    /// each position's `{"x", "y"}` translation, or empty for none
    pub translations: Vec<J>,
    /// the channels' labels and colors (§4.2
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nd2.md#42-channels>)
    pub labels: Vec<String>,
    pub colors: Vec<String>,
}

/// The description's loops as the schema's flattening gave them.
fn loops(description: &J) -> Vec<Loop> {
    let num = |v: &J| v.as_f64();
    description["loops"]
        .as_array()
        .into_iter()
        .flatten()
        .map(|l| Loop {
            kind: l["kind"].as_str().unwrap_or("").to_string(),
            count: num(&l["count"]).unwrap_or(0.0),
            scale: num(&l["period"]).or_else(|| num(&l["step"])).unwrap_or(0.0),
            depth: l["depth"].as_u64().unwrap_or(0) as u32,
            flip: l["flip"].as_bool().unwrap_or(false),
            stage: l["stage"]
                .as_array()
                .into_iter()
                .flatten()
                .map(|q| (num(&q[0]), num(&q[1])))
                .collect(),
        })
        .collect()
}

/// A JSON object's members as the schema's values.
fn members(v: &J) -> Vec<(String, V)> {
    v.as_object().into_iter().flatten().map(|(k, x)| (k.clone(), V::from_json(x))).collect()
}

/// refkv's plan of the file the description describes.
pub fn plan(description: &J) -> crate::error::Res<Plan> {
    let loops = loops(description);
    let picture = match &description["picture"] {
        J::Null => schema::picture_defaults()?,
        p => members(p),
    };
    let mut env = schema::env(&members(&description["attributes"]));
    env.extend(picture);
    schema::loop_env(&mut env, &loops);
    schema::derive(&mut env)?;
    let get = |k: &str| env.get(k).map(|v| v.to_json()).unwrap_or(J::Null);
    let num = |k: &str| env.get(k).and_then(|v| v.num()).unwrap_or(0.0);
    let bits = num("uiBpcInMemory") as u64;
    let data_type = match bits {
        8 => "uint8",
        16 => "uint16",
        32 => "float32",
        b => return Err(Error::internal(format!("{b} bits"))),
    };
    let components = num("uiComp") as u64;
    let (translations, _) = schema::translations(&env, &loops);
    let (labels, colors) = channels(&description["picture"]["sPicturePlanes"], components);
    Ok(Plan {
        width: num("uiWidth") as u64,
        height: num("uiHeight") as u64,
        width_bytes: num("uiWidthBytes") as u64,
        row: num("R") as u64,
        components,
        bits,
        significant: num("uiBpcSignificant"),
        compressed: env.get("compressed").is_some_and(|v| v.truthy()),
        data_type,
        loops: loops.iter().map(|l| PlanLoop { kind: l.kind.clone(), count: l.count as u64, flip: l.flip }).collect(),
        positions: num("P") as u64,
        frames: get("N"),
        calibrated: env.get("calibrated").is_some_and(|v| v.truthy()),
        timed: env.get("t_scale").and_then(|v| v.num()).is_some_and(|x| x > 0.0),
        stepped: env.get("z_scale").and_then(|v| v.num()).is_some_and(|x| x > 0.0),
        scales: [get("scale_x"), get("scale_y"), get("scale_z"), get("scale_t")],
        translations: translations.as_array().cloned().unwrap_or_default(),
        labels,
        colors,
    })
}

/// Labels and colors (spec/zarrify/nd2.md §4.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nd2.md#42-channels>): each plane's, where
/// the planes are `a0` to `a<uiCount - 1>`, each of one or three components, together the image's;
/// else `C<k>` in white.
fn channels(planes: &J, comp: u64) -> (Vec<String>, Vec<String>) {
    let count = planes["uiCount"].as_u64().unwrap_or(0);
    let ps: Vec<&J> = (0..count).map_while(|i| planes["sPlaneNew"].get(format!("a{i}"))).collect();
    let k = |p: &J| p["uiCompCount"].as_u64().unwrap_or(1);
    if count >= 1 && ps.len() as u64 == count && ps.iter().all(|p| k(p) == 1 || k(p) == 3) && ps.iter().map(|p| k(p)).sum::<u64>() == comp {
        let (mut labels, mut colors) = (Vec::new(), Vec::new());
        for p in ps {
            let desc = p["sDescription"].as_str().unwrap_or("");
            if k(p) == 3 {
                for (l, c) in &crate::out::RGBA[..3] {
                    labels.push(format!("{desc} {l}"));
                    colors.push(c.to_string());
                }
            } else {
                let abgr = p["uiColor"].as_u64().unwrap_or(0xFF_FFFF);
                labels.push(desc.to_string());
                colors.push(format!("{:02X}{:02X}{:02X}", abgr & 255, (abgr >> 8) & 255, (abgr >> 16) & 255));
            }
        }
        return (labels, colors);
    }
    ((0..comp).map(|k| format!("C{k}")).collect(), vec!["FFFFFF".to_string(); comp as usize])
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    /// The plan of a description takes the schema's derived quantities: a time loop's period in
    /// seconds, a calibrated image's scales, and the planes' labels and colors.
    #[test]
    fn a_plan_derives_the_convention_from_the_description() {
        let d = json!({
            "attributes": {"uiWidth": 5, "uiHeight": 6, "uiWidthBytes": 20, "uiComp": 2, "uiBpcInMemory": 16,
                           "uiBpcSignificant": 12.0, "eCompression": 2, "uiTileWidth": 0, "uiTileHeight": 0},
            "loops": [{"kind": "t", "count": 3, "depth": 0, "period": 2500.0},
                      {"kind": "z", "count": 4, "depth": 1, "step": 0.5, "flip": true}],
            "picture": {"bCalibrated": true, "dCalibration": 0.25, "dAspect": 2.0, "dStgLgCT11": 1.0,
                        "dStgLgCT12": 0.0, "dStgLgCT21": 0.0, "dStgLgCT22": 1.0, "dXPos": null, "dYPos": null,
                        "sPicturePlanes": {"uiCount": 2, "sPlaneNew": {
                            "a0": {"sDescription": "DAPI", "uiColor": 16711680, "uiCompCount": 1},
                            "a1": {"sDescription": "GFP", "uiColor": 65280, "uiCompCount": 1}}}},
            "chunks": []
        });
        let p = plan(&d).unwrap();
        assert_eq!((p.row, p.compressed, p.data_type, p.positions, p.frames.as_f64()), (20, false, "uint16", 1, Some(12.0)));
        assert_eq!(p.scales, [json!(0.25), json!(0.5), json!(0.5), json!(2.5)]);
        assert!(p.calibrated && p.timed && p.stepped && p.translations.is_empty());
        assert_eq!((p.labels, p.colors), (vec!["DAPI".to_string(), "GFP".into()], vec!["0000FF".to_string(), "00FF00".into()]));
        let none = json!({"attributes": d["attributes"], "loops": [], "picture": null, "chunks": []});
        let p = plan(&none).unwrap();
        assert_eq!((p.calibrated, p.timed, p.stepped, p.labels), (false, false, false, vec!["C0".to_string(), "C1".into()]));
    }
}
