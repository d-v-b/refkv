//! The names the GRIB convention gives arrays and dimensions (spec/zarrify/grib.md §3.2
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/grib.md#32-names>): a parameter's
//! short name, name and units, from the table spec/tools/grib_names.py generates from ecCodes'
//! definitions (spec/zarrify/grib/parameters.tsv, as `names_table.rs`); a fixed surface type's name
//! and units (code table 4.5, spec/zarrify/grib/surfaces.tsv); a statistical process's (code table
//! 4.10, spec/zarrify/grib/statistics.tsv). The tests hold the code to the spec's tables.

/// (centre, discipline, category, number, short name, name, units): a parameter of WMO's code
/// table 4.2 (centre 0), or of a centre's local table (its categories or numbers 192–254).
type Parameter = (u16, u8, u8, u8, &'static str, &'static str, &'static str);

include!("names_table.rs");

/// The short name, name and units of a parameter, if the table has it: the centre's own entry,
/// else WMO's (centre 0).
pub fn parameter(centre: u64, discipline: u8, category: u8, number: u8) -> Option<(&'static str, &'static str, &'static str)> {
    let find = |c: u64| PARAMETERS.binary_search_by_key(&(c, discipline, category, number), |p| (p.0 as u64, p.1, p.2, p.3)).ok();
    let k = find(centre).or_else(|| find(0))?;
    Some((PARAMETERS[k].4, PARAMETERS[k].5, PARAMETERS[k].6))
}

/// The name and the units of a fixed surface's value, for a type of code table 4.5 (and NCEP's
/// local types, for centre 7), if this profile names it.
pub fn surface(centre: u64, kind: u8) -> Option<(&'static str, Option<&'static str>)> {
    let m = |n: &'static str| Some((n, Some("m")));
    let one = |n: &'static str| Some((n, Some("1")));
    let none = |n: &'static str| Some((n, None));
    match kind {
        1 => none("surface"),
        2 => none("cloudBase"),
        3 => none("cloudTop"),
        4 => none("isothermZero"),
        5 => none("adiabaticCondensation"),
        6 => none("maxWind"),
        7 => none("tropopause"),
        8 => none("nominalTop"),
        9 => none("seaBottom"),
        10 => none("entireAtmosphere"),
        11 => none("cumulonimbusBase"),
        12 => none("cumulonimbusTop"),
        20 => Some(("isothermal", Some("K"))),
        100 => Some(("isobaric", Some("Pa"))),
        101 => none("meanSea"),
        102 => m("heightAboveSea"),
        103 => m("heightAboveGround"),
        104 => one("sigma"),
        105 => one("hybrid"),
        106 => m("depthBelowLand"),
        107 => Some(("theta", Some("K"))),
        108 => Some(("pressureFromGround", Some("Pa"))),
        109 => Some(("potentialVorticity", Some("K m2 kg-1 s-1"))),
        111 => one("eta"),
        114 => one("snow"),
        117 => m("mixedLayerDepth"),
        118 => one("hybridHeight"),
        119 => one("hybridPressure"),
        150 => one("generalVertical"),
        151 => one("soil"),
        160 => m("depthBelowSea"),
        161 => m("depthBelowWaterSurface"),
        162 => none("lakeBottom"),
        163 => none("sedimentBottom"),
        _ if centre != 7 => None,
        200 => none("atmosphereSingleLayer"),
        204 => none("highestTroposphericFreezing"),
        206 => none("gridScaleCloudBottom"),
        207 => none("gridScaleCloudTop"),
        209 => none("boundaryLayerCloudBottom"),
        210 => none("boundaryLayerCloudTop"),
        211 => none("boundaryLayerCloudLayer"),
        212 => none("lowCloudBottom"),
        213 => none("lowCloudTop"),
        214 => none("lowCloudLayer"),
        215 => none("cloudCeiling"),
        220 => none("planetaryBoundaryLayer"),
        222 => none("middleCloudBottom"),
        223 => none("middleCloudTop"),
        224 => none("middleCloudLayer"),
        232 => none("highCloudBottom"),
        233 => none("highCloudTop"),
        234 => none("highCloudLayer"),
        242 => none("convectiveCloudBottom"),
        243 => none("convectiveCloudTop"),
        244 => none("convectiveCloudLayer"),
        245 => none("lowestLevelWetBulbZero"),
        246 => none("maxEquivalentPotentialTemperature"),
        247 => none("equilibrium"),
        248 => none("shallowConvectiveCloudBottom"),
        249 => none("shallowConvectiveCloudTop"),
        251 => none("deepConvectiveCloudBottom"),
        252 => none("deepConvectiveCloudTop"),
        253 => none("supercooledLiquidWaterBottom"),
        254 => none("supercooledLiquidWaterTop"),
        _ => None,
    }
}

/// A statistical process's name (code table 4.10).
pub fn statistic(p: u8) -> String {
    match p {
        0 => "avg".into(),
        1 => "accum".into(),
        2 => "max".into(),
        3 => "min".into(),
        4 => "diff".into(),
        5 => "rms".into(),
        6 => "sd".into(),
        7 => "cov".into(),
        8 => "diffStartEnd".into(),
        9 => "ratio".into(),
        10 => "anomaly".into(),
        11 => "sum".into(),
        _ => format!("stat{p}"),
    }
}

// The spec's tables are read only here: the crate's sdist (python/pyproject.toml) holds no spec/.
#[cfg(test)]
mod tests {
    use super::*;

    /// The rows of a spec table, without its header, split at tabs.
    fn rows(tsv: &'static str, header: &str) -> Vec<Vec<&'static str>> {
        let mut lines = tsv.lines();
        assert_eq!(lines.next(), Some(header));
        lines.map(|l| l.split('\t').collect()).collect()
    }

    #[test]
    fn parameters_are_the_spec_table() {
        let tsv = include_str!("../../../../spec/zarrify/grib/parameters.tsv");
        let spec: Vec<Parameter> = rows(tsv, "centre\tdiscipline\tcategory\tnumber\tshort_name\tname\tunits")
            .into_iter()
            .map(|r| match r[..] {
                [c, d, k, n, s, name, u] => (c.parse().unwrap(), d.parse().unwrap(), k.parse().unwrap(), n.parse().unwrap(), s, name, u),
                _ => panic!("a row of parameters.tsv has {} columns", r.len()),
            })
            .collect();
        // a difference: names_table.rs is out of date with the table (run spec/tools/grib_names.py)
        for (k, (ours, theirs)) in PARAMETERS.iter().zip(&spec).enumerate() {
            assert_eq!(ours, theirs, "line {} of parameters.tsv", k + 2);
        }
        assert_eq!(PARAMETERS.len(), spec.len());
    }

    #[test]
    fn surfaces_are_the_spec_table() {
        let tsv = include_str!("../../../../spec/zarrify/grib/surfaces.tsv");
        let spec: Vec<(u64, u8, &str, Option<&str>)> = rows(tsv, "centre\ttype\tname\tunits")
            .into_iter()
            .map(|r| match r[..] {
                [c, t, name, u] => (c.parse().unwrap(), t.parse().unwrap(), name, (!u.is_empty()).then_some(u)),
                _ => panic!("a row of surfaces.tsv has {} columns", r.len()),
            })
            .collect();
        for centre in [0, 7, 98] {
            for kind in 0..=255u8 {
                // centre 0's rows are every centre's; another centre's, its own
                let want = spec.iter().find(|r| r.1 == kind && (r.0 == 0 || r.0 == centre)).map(|r| (r.2, r.3));
                assert_eq!(surface(centre, kind), want, "centre {centre}, type {kind}");
            }
        }
    }

    #[test]
    fn statistics_are_the_spec_table() {
        let tsv = include_str!("../../../../spec/zarrify/grib/statistics.tsv");
        let spec: Vec<(u8, &str)> = rows(tsv, "process\tname")
            .into_iter()
            .map(|r| match r[..] {
                [p, name] => (p.parse().unwrap(), name),
                _ => panic!("a row of statistics.tsv has {} columns", r.len()),
            })
            .collect();
        for p in 0..=255u8 {
            let want = spec.iter().find(|r| r.0 == p).map_or(format!("stat{p}"), |r| r.1.to_string());
            assert_eq!(statistic(p), want, "process {p}");
        }
    }
}
