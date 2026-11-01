r"""The GRIB parameters' short names, names and units (spec/zarrify/grib.md §3.2
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/grib.md#32-names>): writes
spec/zarrify/grib/parameters.tsv from ecCodes, and rust/refkv-ir/src/grib/names_table.rs from it.

    uv run --no-project -p 3.13 --with eccodes==2.50.0 --with eccodeslib==2.50.0.33 \
        python spec/tools/grib_names.py eccodes
    python spec/tools/grib_names.py

The first writes the table, then the Rust; the second, which needs no ecCodes, the Rust alone.
The table holds the names ecCodes gives a field that names nothing but its discipline, category
and number. ecCodes (Apache 2.0) names a parameter from its definitions, some of them a centre's
own: so the table holds WMO's names (centre 0, for every centre) and, where they differ or WMO has
none, those of the centres whose files the corpus holds (NCEP, 7, and ECMWF, 98). A field whose
level or statistic ecCodes' name depends on (ECMWF's `2t` is `t` at 2 m) gets the name of the
parameter alone, since the level and the statistic are the array's other names; one that ecCodes
names only at some level or for some statistic (ECMWF's sea ice cover, `ci`, at the surface) gets
the name it has at the first of a few common surface types and statistics. The table is pinned
with the ecCodes release named above; regenerating it with another is a change of the convention.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

# only `eccodes` mode needs ecCodes: without it, the script still writes the Rust from the table
eccodes: ModuleType | None
try:
    import eccodes
except ImportError:
    eccodes = None

ROOT = Path(__file__).resolve().parents[2]
TABLE = ROOT / "spec" / "zarrify" / "grib" / "parameters.tsv"
OUT = ROOT / "rust" / "refkv-ir" / "src" / "grib" / "names_table.rs"
COLUMNS = ["centre", "discipline", "category", "number", "short_name", "name", "units"]
ECCODES = "2.50.0"
DISCIPLINES = [0, 1, 2, 3, 4, 10, 20]
CENTRES = [0, 7, 98]
# the surfaces (code table 4.5) and statistics (4.10) tried, in order, for a parameter ecCodes does
# not name alone: the ground, a height above it, an isobaric surface, a depth below the land, the
# sea, the entire atmosphere; an accumulation, an average, a maximum, a minimum
SURFACES = [1, 103, 100, 106, 101, 10, 160]
STATISTICS = [1, 0, 2, 3]
# the parameters tried again: WMO's first categories and numbers, and the local ones
FALLBACK = [(c, n) for c in [*range(31), *range(192, 255)] for n in [*range(64), *range(192, 255)]]


def names(ec: ModuleType, centre: int) -> dict[tuple[int, int, int], tuple[str, str, str]]:
    """Every (discipline, category, number) ecCodes names for `centre`: its short name, name, units."""
    h = ec.codes_grib_new_from_samples("GRIB2")
    ec.codes_set(h, "centre", centre or 255)
    ec.codes_set(h, "productDefinitionTemplateNumber", 0)
    ec.codes_set(h, "typeOfFirstFixedSurface", 255)
    ec.codes_set(h, "typeOfSecondFixedSurface", 255)
    out = {}
    for d in DISCIPLINES:
        ec.codes_set(h, "discipline", d)
        for c in range(255):
            ec.codes_set(h, "parameterCategory", c)
            for n in range(255):
                ec.codes_set(h, "parameterNumber", n)
                short = ec.codes_get_string(h, "shortName")
                if short in ("unknown", "~", ""):
                    continue
                out[(d, c, n)] = (short, ec.codes_get_string(h, "name"), ec.codes_get_string(h, "units"))
    ec.codes_release(h)
    tries = []
    combinations = [(0, s, None) for s in SURFACES] + [(8, s, p) for p in STATISTICS for s in SURFACES]
    for template, surface, process in combinations:
        t = ec.codes_grib_new_from_samples("GRIB2")
        ec.codes_set(t, "centre", centre or 255)
        ec.codes_set(t, "productDefinitionTemplateNumber", template)
        if process is not None:
            ec.codes_set(t, "typeOfStatisticalProcessing", process)
        ec.codes_set(t, "typeOfFirstFixedSurface", surface)
        ec.codes_set(t, "typeOfSecondFixedSurface", 255)
        tries.append(t)
    for d in DISCIPLINES:
        for c, n in FALLBACK:
            if (d, c, n) not in out and (found := again(ec, tries, d, c, n)):
                out[(d, c, n)] = found
    for t in tries:
        ec.codes_release(t)
    return out


def again(ec: ModuleType, tries: list[int], d: int, c: int, n: int) -> tuple[str, str, str] | None:
    """The name ecCodes gives the parameter at the first of SURFACES, or, with a statistic, the
    first of STATISTICS, that names it (`tries`, a message of each)."""
    for h in tries:
        for k, v in (("discipline", d), ("parameterCategory", c), ("parameterNumber", n)):
            ec.codes_set(h, k, v)
        short = ec.codes_get_string(h, "shortName")
        if short not in ("unknown", "~", ""):
            return short, ec.codes_get_string(h, "name"), ec.codes_get_string(h, "units")
    return None


Row = tuple[int, int, int, int, str, str, str]


def parameters(ec: ModuleType) -> list[Row]:
    """The parameters ecCodes names, sorted: WMO's, then each centre's that differ from them."""
    if (version := ec.codes_get_api_version()) != ECCODES:
        raise SystemExit(f"ecCodes {version}: the table is pinned with ecCodes {ECCODES}")
    wmo = names(ec, 0)
    rows = [(0, *k, *v) for k, v in wmo.items()]
    for centre in CENTRES[1:]:
        rows += [(centre, *k, *v) for k, v in names(ec, centre).items() if wmo.get(k) != v]
    return sorted(rows)


def write_table(rows: list[Row]) -> None:
    for row in rows:
        assert not any("\t" in str(v) or "\n" in str(v) for v in row), row
    lines = ["\t".join(COLUMNS), *("\t".join(map(str, row)) for row in rows)]
    TABLE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {len(rows)} parameters to {TABLE.relative_to(ROOT)}")


def read_table() -> list[Row]:
    header, *lines = TABLE.read_text(encoding="utf-8").splitlines()
    assert header.split("\t") == COLUMNS, header
    rows = []
    for line in lines:
        c, d, k, n, short, name, units = line.split("\t")
        rows.append((int(c), int(d), int(k), int(n), short, name, units))
    return rows


def rust(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def rust_table(rows: list[Row]) -> str:
    """names_table.rs: the table, as the Rust array PARAMETERS."""
    lines = [
        "// Generated by spec/tools/grib_names.py from spec/zarrify/grib/parameters.tsv: do not edit.",
        "/// The parameters, by (centre, discipline, category, number).",
        "static PARAMETERS: &[Parameter] = &[",
    ]
    lines += [f"    ({c}, {d}, {k}, {n}, {rust(s)}, {rust(name)}, {rust(u)})," for c, d, k, n, s, name, u in rows]
    lines.append("];")
    return "\n".join(lines) + "\n"


def main() -> None:
    if sys.argv[1:] == ["eccodes"]:
        if eccodes is None:
            raise SystemExit("ecCodes is not installed: see the command in this script's docstring")
        write_table(parameters(eccodes))
    elif sys.argv[1:]:
        raise SystemExit("usage: grib_names.py [eccodes]")
    rows = read_table()
    OUT.write_text(rust_table(rows), encoding="utf-8")
    print(f"wrote {len(rows)} parameters to {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
