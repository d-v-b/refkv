# IMS: stored arrays

What the IMS parser describes as stored arrays ([stored-array.md](../../stored-array.md)), part of [the IMS profile](../ims.md).

An Imaris file's datasets are described as
HDF5's, by the same code, in a file run; a filter the list has no name for is `source` with
`format` `hdf5` there too. The facts give each image dataset's key as the sixth
member of its entry of `images`: the id of its object.
