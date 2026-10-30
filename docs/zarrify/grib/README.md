# GRIB

refkv turns a file into a Zarr hierarchy by writing a small archive of references to the
file's bytes, without copying its values ([overview](../README.md)). This page is about GRIB
edition 2, the format of most operational weather forecasts.

## Who this is for

NOAA and ECMWF publish their forecasts as GRIB2 files in public buckets: GFS, HRRR, GEFS, NAM
and NBM on AWS, and ECMWF's open data on its own server and on AWS, Google Cloud and Azure. A
GRIB file is a sequence of messages, each one field (a 2-D grid of one variable, at one level and
one time) packed on its own. Reading one variable means finding its message among hundreds, and
cfgrib does that by downloading the file and indexing it. refkv reads only the messages' headers
(about 0.3% of a file) and writes an archive in which every Zarr chunk is one message, decoded
when it is read by the `refkv.grib` codec. A reader then fetches only the fields it opens, from
the original file.

## What you get

A Zarr group per grid (`grid0`, and `grid1`, … when a file mixes grids), each a dataset that
xarray opens (`xr.open_zarr(store, group="grid0")`), with:

- **Variables:** one array per kind of field: the fields whose parameter, level type, product
  and grid agree. They are named by the parameter's short name (ecCodes'), the level type and,
  for a statistic, the statistic and its period: `t_isobaric`, `u_heightAboveGround`,
  `tp_surface_accum6h`, `ceil_cloudCeiling_problt152p4` (a probability), `t_heightAboveGround_p90`
  (a percentile).
- **Dimensions:** `time` (the reference time), `step`, the level (named by its type, such as
  `isobaric` in Pa or `heightAboveGround` in m) when the fields have one, `number` for an
  ensemble member, then the grid: `latitude` and `longitude`, or `y` and `x` for a Lambert grid.
  `time` and `step` are always there, of length 1 for a single forecast hour, so that the files of
  a forecast run line up.
- **Values:** `float64`, as ecCodes decodes them (refkv's decoder matches ecCodes value for value
  on every fixture and sample), NaN where a field's bitmap or packing marks a point missing. The
  rows are in the file's order: GFS and ECMWF run north to south, HRRR south to north.
- **Coordinates:** `time`, `step` and `valid_time` (CF units, which xarray decodes), the levels,
  and the grid's positions as CF `scale_factor` and `add_offset` over an index (a few kilobytes
  whatever the grid's size).
- **Georeferencing:** the GeoZarr `proj:wkt2` (the CRS, on the file's Earth shape) and
  `spatial:transform`, as for Sentinel-2.
- **Source metadata:** the count of messages and fields, the index the run used, and the fields
  that are not presented, with their reasons, under `refkv_zarrified.grib`. Every message's
  sections are in the IR mirror on `refkv_source`, from which the file is rebuilt byte for byte.

**Packings.** Simple (5.0), complex (5.2), complex with spatial differencing (5.3, NOAA's), IEEE
floats (5.4), PNG (5.41) and CCSDS (5.42, ECMWF's). JPEG 2000 (5.40), which NOAA's RAP uses, is
not decoded yet: such a field is listed as not presented, and a file of nothing else is rejected.

**Not presented.** A field is listed with its reason, rather than rejecting the file, when its
grid is not a regular latitude/longitude or Lambert conformal grid, its packing is not one of the
above, its product template has dimensions refkv does not name yet, or its bitmap is predefined.

**Indexes.** NOAA publishes `<file>.idx` beside each file, and ECMWF `<file>.index`. refkv
reads one when it is there and asks for every message's header at once, rather than walking the
file a message at a time. The index is a hint: every message is read and checked either way, and
the archive is the same.

## Measured

The 2026-10-29 00z runs, from this machine (in Europe) over HTTP, with each feed's index:

| file | size | messages | arrays | requests | read | time |
|---|---|---|---|---|---|---|
| GFS 0.25° (`gfs.t00z.pgrb2.0p25.f006`) | 528 MB | 743 | 172 | 18 | 12.4 MB | 15 s |
| HRRR CONUS (`hrrr.t00z.wrfsfcf06.grib2`) | 154 MB | 173 | 133 | 16 | 2.9 MB | 5 s |
| GEFS 0.5° member (`gep01.t00z.pgrb2a.0p50.f006`) | 15 MB | 85 | 35 | 15 | 1.5 MB | 3 s |
| NAM (`nam.t00z.awphys06.tm00.grib2`) | 61 MB | 404 (454 fields) | 109 | 19 | 7.0 MB | 9 s |
| NBM CONUS (`blend.t00z.core.f006.co.grib2`) | 197 MB | 208 | 204 | 11 | 3.5 MB | 6 s |
| ECMWF IFS 0.25° (`20261029000000-6h-oper-fc.grib2`) | 98 MB | 191 | 53 | 15 | 3.3 MB | 7 s |

## Example

[example.py](example.py) zarrifies an HRRR forecast from NOAA's bucket and plots its 2 m
temperature with xarray:

```
uv run docs/zarrify/grib/example.py
```

The profile is [spec/zarrify/grib.md](../../../spec/zarrify/grib.md), and the codec
[spec/conventions.md §12](../../../spec/conventions.md#12-grib-messages).
