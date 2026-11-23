# H5J: stored arrays

What the H5J parser describes as stored arrays ([stored-array.md](../../stored-array.md)), part of [the H5J profile](../h5j.md).

An H5J file is described as the two formats it
is made of. Its datasets are described as HDF5's ([hdf5/stored-arrays.md](../hdf5/stored-arrays.md)), by the same code: a channel's dataset is an
array of one dimension of 8-bit elements, the bytes of its MP4 file, whose one row is the struct
that holds that file in the IR ([h5j.md §5.3](../h5j.md#53-elements)). Each channel
the profile zarrifies ([h5j.md §8.2](../h5j.md#82-channels)) also has its MP4 file's
tracks described as MP4's ([mp4/stored-arrays.md](../mp4/stored-arrays.md)), keyed by each track's `trak` box in that struct, every element and
range renumbered to the H5J file's IR and bytes: a track of 4:0:0 pictures is one array, the luma
plane, in an `hevc` chain. A channel the profile records keeps its dataset's description alone:
its bytes, as an MP4 file, are not described.
