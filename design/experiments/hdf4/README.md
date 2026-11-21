# HDF4 survey scripts

The scripts behind [design/plans/hdf4.md](../../plans/hdf4.md). They read sample files from a
directory you download them into; nothing here downloads.

- `layout.py FILE...`: walks each file's data descriptors and reports its tags, number types,
  special elements (compressed, chunked, linked blocks, external) and Vdata headers. Standard
  library only.
- `survey.py FILE...`: lists each file's datasets, attributes, Vdatas and Vgroups with pyhdf.
- `check.py FILE...`: rebuilds every dataset from the byte ranges `layout.py` finds, decodes
  them, and compares the values with pyhdf.
- `strings.py FILE...`: classifies every `char8` field (padding, texts per value, encoding,
  whether its bytes are in one range).

pyhdf needs Python 3.12 or earlier for its wheels:

    uv run --no-project --python 3.12 --with pyhdf --with numpy python -I check.py DIR/*.hdf

The samples: GDAL's set at https://download.osgeo.org/gdal/data/hdf4/ and the test files of the
HDF4 library (`*.hdf` in https://github.com/HDFGroup/hdf4).
