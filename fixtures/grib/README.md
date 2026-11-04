# GRIB fixtures

Written by [`fixtures/generators/grib/write_fixtures.py`](../generators/grib/write_fixtures.py),
which says what each file covers. `expected/` holds the values eccodes decodes each field to, in
the order of the `refkv.grib` codec ([spec/conventions.md §12](../../spec/conventions.md#12-grib-messages)).

The `grib_*` files whose names do not name a feed are synthetic: eccodes 2.50.0 wrote them, or
the generator assembled them from sections eccodes wrote.

The others are samples of public forecasts, one whole message each, copied unchanged from the
byte range their feed's index gives. All are the 2026-10-29 00z runs.

| fixture | source | bytes | field | license |
|---|---|---|---|---|
| `grib_gfs_hindex.grib2` | [`gfs.t00z.pgrb2.0p25.f006`](https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.20261029/00/atmos/gfs.t00z.pgrb2.0p25.f006) (index `.idx`, message 525) | 376931486–377135919 | HINDEX, surface | public domain (NOAA) |
| `grib_gfs_icetmp.grib2` | the same (message 731) | 518323874–518511709 | ICETMP, surface | public domain (NOAA) |
| `grib_gfs_land.grib2` | the same (message 728) | 517458469–517490504 | LAND, surface | public domain (NOAA) |
| `grib_hrrr_land.grib2` | [`hrrr.t00z.wrfsfcf06.grib2`](https://noaa-hrrr-bdp-pds.s3.amazonaws.com/hrrr.20261029/conus/hrrr.t00z.wrfsfcf06.grib2) (index `.idx`, message 168) | 145880938–145931413 | LAND, surface | public domain (NOAA) |
| `grib_hrrr_dswrf.grib2` | the same (message 126) | 94978102–94978289 | DSWRF, surface | public domain (NOAA) |
| `grib_ecmwf_2t.grib2` | [`20261029000000-6h-oper-fc.grib2`](https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com/20261029/00z/ifs/0p25/oper/20261029000000-6h-oper-fc.grib2) (offsets from [data.ecmwf.int's `.index`](https://data.ecmwf.int/forecasts/20261029/00z/ifs/0p25/oper/20261029000000-6h-oper-fc.index)) | 22335104–22648869 | 2t, step 6 | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), © ECMWF |
| `grib_ecmwf_sithick.grib2` | the same | 10074626–10254021 | sithick, step 6 | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), © ECMWF |
| `grib_rap_apcp.grib2` | [`rap.t00z.awp130pgrbf06.grib2`](https://noaa-rap-pds.s3.amazonaws.com/rap.20261029/rap.t00z.awp130pgrbf06.grib2) (index `.idx`, message 311) | 18449662–18538677 | APCP, surface, 3–6 h | public domain (NOAA) |
| `grib_cfs_wind.grib2` | [`pgbf2026103000.01.2026102900.grb2`](https://noaa-cfs-pds.s3.amazonaws.com/cfs.20261029/00/6hrly_grib_01/pgbf2026103000.01.2026102900.grb2) (index `.idx`, message 359) | 17421074–17555141 | UGRD and VGRD, 1829 m | public domain (NOAA) |
| `grib_nbm_tmp.grib2` | [`blend.t00z.core.f006.co.grib2`](https://noaa-nbm-grib2-pds.s3.amazonaws.com/blend.20261029/00/core/blend.t00z.core.f006.co.grib2) (index `.idx`, message 186) | 161452866–161870327 | TMP, surface | public domain (NOAA) |

The byte ranges are inclusive. The ECMWF samples are ECMWF's open data, used under the Creative
Commons Attribution 4.0 International license: source www.ecmwf.int, unchanged.
