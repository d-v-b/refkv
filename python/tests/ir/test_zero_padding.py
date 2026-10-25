"""The zero padding of the NIfTI, MRC and FITS profiles' edge chunks: a range of the output's one
zero data source, of the most padding any reference holds (spec/zarrify.md §1.2
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>,
spec/zarrify/nifti.md §7.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nifti.md#72-chunks>,
spec/zarrify/mrc.md §8.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mrc.md#82-chunks>,
spec/zarrify/fits.md §9.4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/fits.md#94-chunks>)."""

import dataclasses
from pathlib import Path
from typing import cast

import mrcfile
import nibabel
import numpy as np
import pytest
from astropy.io import fits
from ir_helpers import literal_padding, zarrified

from refkv._narrow import ndarray


def ramp(shape: tuple[int, ...]) -> np.ndarray:
    return (np.arange(int(np.prod(shape))) % 251).astype(np.uint8).reshape(shape)


def nifti(path: Path) -> dict[str, np.ndarray]:
    """8 volumes of 37 slices of 64 x 64 bytes: each volume cut into chunks of 19 slices, its
    last padded with a slice of 4096 zero bytes."""
    data = ramp((64, 64, 37, 8))
    nibabel.Nifti1Image(data, np.eye(4)).to_filename(path)
    return {"0": data.transpose(3, 2, 1, 0)}


def mrc(path: Path) -> dict[str, np.ndarray]:
    """A stack of 3 volumes of 131 sections of 64 x 128 bytes: each volume cut into chunks of 66
    sections, its last padded with a section of 8192 zero bytes, at two keys (the image's and the
    dataset layout's)."""
    data = ramp((3, 131, 64, 128)).view(np.int8)
    with mrcfile.new(path) as m:
        m.set_data(data)
        header = cast("np.recarray", m.header)
        header.ispg = 401
        header.mz = 131
    return {"0": data}


def fits_file(path: Path) -> dict[str, np.ndarray]:
    """The volumes of `mrc` as the primary image, and a row of 2^20 + 1 bytes padded with one
    zero byte: one zero data source for both, of the larger padding."""
    a, b = ramp((3, 131, 64, 128)), ramp((1, 2**20 + 1))
    fits.HDUList([fits.PrimaryHDU(a), fits.ImageHDU(b)]).writeto(path)
    return {"0": a, "1": b}


# file name, its writer, its profile, and the paddings of its padded references
CASES = {
    "nifti": ("a.nii", nifti, "nifti", [4096] * 8),
    "mrc": ("a.mrc", mrc, "mrc", [8192] * 6),
    "fits": ("a.fits", fits_file, "fits", [8192] * 3 + [1]),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_padding_is_a_range_of_one_zero_data_source(case, tmp_path):
    """Every padded edge chunk's reference is its range then a prefix of the output's one data
    source, of the most padding; the arrays are the file's values; and the archive is smaller
    than with the padding as literal ranges, by every padding but the one the source holds."""
    name, write, fmt, paddings = CASES[case]
    path = tmp_path / name
    arrays = write(path)
    out, root, _ = zarrified(path, tmp_path, fmt)
    assert list(out.data.items()) == [(bytes(max(paddings)), 1)]
    padded = []
    for key, v in out.refs.items():
        if key.startswith("refkv_source/"):
            continue
        match v:
            case [(_, _)]:
                pass
            case [(_, _), (1, 0, int(p))]:
                padded.append(p)
            case _:
                pytest.fail(f"{key}: {v}")
    assert sorted(padded) == sorted(paddings)
    for key, want in arrays.items():
        assert np.array_equal(ndarray(root.get_array(key)[...]), want), key
    shared, literal = tmp_path / "shared.vzip", tmp_path / "literal.vzip"
    out.write(str(shared))
    refs, data = literal_padding(out)
    assert data == []
    dataclasses.replace(out, refs=refs, data={}).write(str(literal))
    assert shared.stat().st_size + sum(paddings) - max(paddings) <= literal.stat().st_size
