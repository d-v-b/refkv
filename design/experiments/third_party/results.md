# Third-party readers: the full sweep

The output of [`measure.py`](measure.py) at a 20 ms round trip, on a warm cache (a second run),
on 2026-10-27, for [THIRD_PARTY_READERS.md](../../THIRD_PARTY_READERS.md): each package through
each transport. Wall times are end to end; the core's include building the whole archive and its
mirror, which the packages do not do.

| file | size | reader | requests | bytes read | wall time at 20 ms RTT | chunks |
|---|---:|---|---:|---:|---:|---:|
| ome-tiff | 487 MB | core | 9 | 457 kB | 1.3 s | 5037 |
| ome-tiff | 487 MB | tifffile, fsspec none | 53 | 373 kB | 9.8 s | 5037 |
| ome-tiff | 487 MB | tifffile, fsspec readahead 64 KiB | 6 | 546 kB | 1.1 s | 5037 |
| ome-tiff | 487 MB | tifffile, fsspec readahead 1 MiB | 4 | 1,770 kB | 0.7 s | 5037 |
| ome-tiff | 487 MB | tifffile, fsspec blockcache 64 KiB | 8 | 456 kB | 1.3 s | 5037 |
| ome-tiff | 487 MB | tifffile, fsspec blockcache 1 MiB | 3 | 1,832 kB | 0.5 s | 5037 |
| ome-tiff | 487 MB | tifffile, obstore 64 KiB | 42 | 2,798 kB | 6.7 s | 5037 |
| ome-tiff | 487 MB | tifffile, obstore 1 MiB | 42 | 14,610 kB | 6.5 s | 5037 |
| svs | 538 MB | core | 16 | 720 kB | 2.4 s | 78312 |
| svs | 538 MB | tifffile, fsspec none | 63 | 632 kB | 10.2 s | 78426 (3 levels fail write_fsspec) |
| svs | 538 MB | tifffile, fsspec readahead 64 KiB | 20 | 1,572 kB | 3.7 s | 78426 (3 levels fail write_fsspec) |
| svs | 538 MB | tifffile, fsspec readahead 1 MiB | 12 | 9,204 kB | 2.5 s | 78426 (3 levels fail write_fsspec) |
| svs | 538 MB | tifffile, fsspec blockcache 64 KiB | 20 | 1,186 kB | 3.1 s | 78426 (3 levels fail write_fsspec) |
| svs | 538 MB | tifffile, fsspec blockcache 1 MiB | 7 | 5,905 kB | 1.6 s | 78426 (3 levels fail write_fsspec) |
| svs | 538 MB | tifffile, obstore 64 KiB | 49 | 3,146 kB | 8.8 s | 78426 (3 levels fail write_fsspec) |
| svs | 538 MB | tifffile, obstore 1 MiB | 49 | 32,206 kB | 9.2 s | 78426 (3 levels fail write_fsspec) |
| ndpi | 221 MB | core | 19 | 428 kB | 2.1 s | 588 |
| ndpi | 221 MB | tifffile, fsspec none | 54 | 349 kB | 10.0 s | 85682 |
| ndpi | 221 MB | tifffile, fsspec readahead 64 KiB | 36 | 2,509 kB | 6.6 s | 85682 |
| ndpi | 221 MB | tifffile, fsspec readahead 1 MiB | 31 | 27,149 kB | 5.3 s | 85682 |
| ndpi | 221 MB | tifffile, fsspec blockcache 64 KiB | 12 | 656 kB | 3.5 s | 85682 |
| ndpi | 221 MB | tifffile, fsspec blockcache 1 MiB | 5 | 3,671 kB | 1.6 s | 85682 |
| ndpi | 221 MB | tifffile, obstore 64 KiB | 44 | 2,884 kB | 7.7 s | 85682 |
| ndpi | 221 MB | tifffile, obstore 1 MiB | 44 | 34,062 kB | 8.6 s | 85682 |
| qptiff | 362 MB | core | 32 | 4,559 kB | 10.1 s | 268860 |
| qptiff | 362 MB | tifffile, fsspec none | 247 | 4,359 kB | 41.5 s | 268291 |
| qptiff | 362 MB | tifffile, fsspec readahead 64 KiB | 49 | 6,749 kB | 10.2 s | 268291 |
| qptiff | 362 MB | tifffile, fsspec readahead 1 MiB | 21 | 20,926 kB | 5.7 s | 268291 |
| qptiff | 362 MB | tifffile, fsspec blockcache 64 KiB | 82 | 5,274 kB | 14.7 s | 268291 |
| qptiff | 362 MB | tifffile, fsspec blockcache 1 MiB | 12 | 11,189 kB | 4.0 s | 268291 |
| qptiff | 362 MB | tifffile, obstore 64 KiB | 186 | 10,358 kB | 31.1 s | 268291 |
| qptiff | 362 MB | tifffile, obstore 1 MiB | 186 | 52,179 kB | 36.3 s | 268291 |
| nd2-a1 | 263 MB | core | 26 | 192 kB | 1.4 s | 25 |
| nd2-a1 | 263 MB | nd2, its own fsspec file | 5 | 532 kB | 0.8 s | 25 |
| nd2-a1 | 263 MB | nd2, fsspec none | 6 | 8 kB | 1.0 s | 25 |
| nd2-a1 | 263 MB | nd2, fsspec readahead 64 KiB | 4 | 74 kB | 0.6 s | 25 |
| nd2-a1 | 263 MB | nd2, fsspec readahead 1 MiB | 4 | 1,057 kB | 0.7 s | 25 |
| nd2-a1 | 263 MB | nd2, fsspec blockcache 64 KiB | 4 | 135 kB | 0.6 s | 25 |
| nd2-a1 | 263 MB | nd2, fsspec blockcache 1 MiB | 3 | 1,511 kB | 0.5 s | 25 |
| nd2-a1 | 263 MB | nd2, obstore 64 KiB | 4 | 74 kB | 0.6 s | 25 |
| nd2-a1 | 263 MB | nd2, obstore 1 MiB | 4 | 1,057 kB | 0.7 s | 25 |
| nd2-pollen | 138 MB | core | 24 | 127 kB | 1.8 s | 57 |
| nd2-pollen | 138 MB | nd2, its own fsspec file | 5 | 532 kB | 0.8 s | 57 |
| nd2-pollen | 138 MB | nd2, fsspec none | 6 | 8 kB | 1.0 s | 57 |
| nd2-pollen | 138 MB | nd2, fsspec readahead 64 KiB | 4 | 74 kB | 0.6 s | 57 |
| nd2-pollen | 138 MB | nd2, fsspec readahead 1 MiB | 4 | 1,057 kB | 0.7 s | 57 |
| nd2-pollen | 138 MB | nd2, fsspec blockcache 64 KiB | 3 | 86 kB | 0.5 s | 57 |
| nd2-pollen | 138 MB | nd2, fsspec blockcache 1 MiB | 3 | 2,052 kB | 0.5 s | 57 |
| nd2-pollen | 138 MB | nd2, obstore 64 KiB | 4 | 74 kB | 0.6 s | 57 |
| nd2-pollen | 138 MB | nd2, obstore 1 MiB | 4 | 1,057 kB | 0.6 s | 57 |
| czi-axioscan | 505 MB | core | 19 | 1,602 kB | 1.2 s | 481 |
| czi-axioscan | 505 MB | czifile, fsspec none | 487 | 92 kB | 75.7 s | 481 |
| czi-axioscan | 505 MB | czifile, fsspec readahead 64 KiB | 485 | 31,746 kB | 73.5 s | 481 |
| czi-axioscan | 505 MB | czifile, fsspec readahead 1 MiB | 288 | 300,955 kB | 45.4 s | 481 |
| czi-axioscan | 505 MB | czifile, fsspec blockcache 64 KiB | 485 | 31,719 kB | 73.9 s | 481 |
| czi-axioscan | 505 MB | czifile, fsspec blockcache 1 MiB | 359 | 375,390 kB | 56.4 s | 481 |
| czi-axioscan | 505 MB | czifile, obstore 64 KiB | 487 | 31,854 kB | 73.9 s | 481 |
| czi-axioscan | 505 MB | czifile, obstore 1 MiB | 487 | 509,608 kB | 77.5 s | 481 |
| czi-idr0011 | 44 MB | core | 13 | 315 kB | 1.0 s | 63 |
| czi-idr0011 | 44 MB | czifile, fsspec none | 69 | 14 kB | 10.8 s | 63 |
| czi-idr0011 | 44 MB | czifile, fsspec readahead 64 KiB | 65 | 4,197 kB | 9.8 s | 63 |
| czi-idr0011 | 44 MB | czifile, fsspec readahead 1 MiB | 33 | 33,202 kB | 5.1 s | 63 |
| czi-idr0011 | 44 MB | czifile, fsspec blockcache 64 KiB | 65 | 4,194 kB | 10.0 s | 63 |
| czi-idr0011 | 44 MB | czifile, fsspec blockcache 1 MiB | 42 | 42,992 kB | 6.8 s | 63 |
| czi-idr0011 | 44 MB | czifile, obstore 64 KiB | 69 | 4,456 kB | 10.5 s | 63 |
| czi-idr0011 | 44 MB | czifile, obstore 1 MiB | 69 | 70,950 kB | 10.5 s | 63 |
| ims | 64 MB | core | 46 | 180 kB | 2.0 s | 240 |
| ims | 64 MB | h5py, fsspec none | 278 | 168 kB | 42.6 s | 240 |
| ims | 64 MB | h5py, fsspec readahead 64 KiB | 173 | 11,337 kB | 26.5 s | 240 |
| ims | 64 MB | h5py, fsspec readahead 1 MiB | 170 | 177,273 kB | 26.9 s | 240 |
| ims | 64 MB | h5py, fsspec blockcache 64 KiB | 40 | 2,556 kB | 6.0 s | 240 |
| ims | 64 MB | h5py, fsspec blockcache 1 MiB | 33 | 33,554 kB | 5.0 s | 240 |
| ims | 64 MB | h5py, obstore 64 KiB | 278 | 18,153 kB | 43.5 s | 240 |
| ims | 64 MB | h5py, obstore 1 MiB | 278 | 290,456 kB | 43.4 s | 240 |
