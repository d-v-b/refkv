"""The DICOM store form (spec/zarrify/dicom.md §9
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/dicom.md#9-the-store-form>): series
grouped, slices sorted and stacked, a slide's levels as multiscales, each file's hierarchy kept, and
one test per thing that keeps a series apart. Every plane is compared with pydicom's reading of its
file."""

import json
from typing import Any

import numpy as np
import pydicom
import pytest
import zarr
from ir_helpers import FIXTURES

import refkv.zarr
from refkv._narrow import at, ndarray, sequence
from refkv._sync import run
from refkv.zarrify import DicomSource, Rejected, zarrify, zarrify_async, zarrify_store
from refkv.zarrify.dicom_series import zarrify_series_async

SERIES = FIXTURES / "dicom_series"
URL = "https://example.org/series/"


def joined(name: str) -> tuple[Any, Any]:
    """(the store's root group, its output's summary), its files read where they are, so that the
    references read too."""
    directory = SERIES / name
    keys = sorted(p.relative_to(directory).as_posix() for p in directory.rglob("*") if p.suffix == ".dcm")
    others = sorted(p.relative_to(directory).as_posix() for p in directory.rglob("*")
                    if p.is_file() and p.suffix != ".dcm")
    read = lambda k: zarrify_async(DicomSource(str(directory / k)))  # noqa: E731
    out = run(zarrify_series_async(keys, others, URL, read, workers=4))
    store = out.to_store(info={"format": "dicom"})
    return zarr.open_group(refkv.zarr.store(store), mode="r", zarr_format=3), out.summary


def own(group: Any) -> Any:
    return at(group.attrs, "refkv_zarrified", "dicom")


@pytest.mark.parametrize(("name", "uid", "order"), [
    ("slices", "1.2.3.4", ["c.dcm", "a.dcm", "e.dcm", "b.dcm", "d.dcm"]),
    ("uneven_rgb", "1.2.3.5", ["c.dcm", "b.dcm", "a.dcm"]),
    ("numbered", "1.2.3.6", ["y.dcm", "x.dcm", "z.dcm"]),
])
def test_stacks_a_series_by_position_or_number(name, uid, order):
    """The array `0` holds each file's image at its place, along `z`, after `c`; each file's own
    hierarchy is under `instances/`, and each chunk references its file."""
    root, _ = joined(name)
    group = root[uid]
    described = own(group)
    assert described["kind"] == "stack"
    assert [at(i, "key") for i in sequence(described["instances"])] == order
    array = group["0"]
    planes = [pydicom.dcmread(SERIES / name / key).pixel_array for key in order]
    want = np.stack(planes)
    rgb = want.ndim == 4
    want = np.moveaxis(want, -1, 0) if rgb else want
    np.testing.assert_array_equal(ndarray(array[...]), want)
    assert array.metadata.dimension_names == (("c", "z", "y", "x") if rgb else ("z", "y", "x"))
    image: Any = at(group.attrs, "ome", "multiscales", 0)
    assert [at(a, "name") for a in sequence(at(image, "axes"))] == list(array.metadata.dimension_names)
    for j, key in enumerate(order):
        instance = group[f"instances/{j}"]
        plane = np.moveaxis(planes[j], -1, 0) if rgb else planes[j]
        np.testing.assert_array_equal(ndarray(instance["0"][...]), plane)
        assert at(instance.attrs, "refkv_zarrified", "profile") == "dicom"
        assert "refkv_source" in instance, key
    scale: Any = at(image, "datasets", 0, "coordinateTransformations", 0, "scale")
    if name == "slices":
        assert described["spacing"] == pytest.approx(2.5)
        assert scale == pytest.approx([2.5, 0.5, 0.25])
        assert at(image, "axes", 0, "unit") == "millimeter"
        assert [at(i, "position") for i in sequence(described["instances"])] == pytest.approx([0, 2.5, 5, 7.5, 10])
    else:
        assert "spacing" not in described  # uneven gaps, or no positions
        assert scale[0 + rgb] == 1
        axis: Any = at(image, "axes", 0 + rgb)
        assert "unit" not in axis


def test_records_what_it_did_not_read():
    """A file the profile rejects and an object that is not a file are recorded."""
    root, summary = joined("slices")
    described = own(root)
    assert described["series"] == [{"series_instance_uid": "1.2.3.4", "path": "1.2.3.4", "kind": "stack",
                                    "instances": 5}]
    assert described["rejected"] == [{"key": "broken.dcm", "reason": "not a DICOM file"}]
    assert described["other_objects"] == ["notes.txt"]
    assert summary["rejected"] == 1
    assert summary["others"] == 1


def test_groups_files_by_series_and_keeps_a_single_file_as_it_is():
    root, _ = joined("two_series")
    assert [at(s, "kind") for s in sequence(own(root)["series"])] == ["stack", "single"]
    single = root["1.2.3.8"]
    assert own(single)["kind"] == "single"
    np.testing.assert_array_equal(ndarray(single["0"][...]), pydicom.dcmread(SERIES / "two_series/b/1.dcm").pixel_array)


def test_makes_a_slides_levels_multiscales():
    """Largest first, with each level's own scale; the label stays out of them."""
    root, _ = joined("slide")
    group = root["1.2.3.13"]
    described = own(group)
    assert described["kind"] == "pyramid"
    assert [at(i, "key") for i in sequence(described["instances"])] == ["c.dcm", "a.dcm", "b.dcm", "label.dcm"]
    image = at(group.attrs, "ome", "multiscales", 0)
    datasets = sequence(at(image, "datasets"))
    assert [at(d, "path") for d in datasets] == ["0", "1", "2"]
    assert [at(d, "coordinateTransformations", 0, "scale") for d in datasets] == [[0.25, 0.25], [0.5, 0.5], [1, 1]]
    assert "3" not in group
    for k, shape in enumerate([(16, 16), (8, 8), (4, 4)]):
        level = group[str(k)]
        assert level.shape == shape
        np.testing.assert_array_equal(ndarray(level[...]), ndarray(group[f"instances/{k}/0"][...]))


@pytest.mark.parametrize(("name", "uid", "reason"), [
    ("repeated_position", "1.2.3.9", "its files repeat a position"),
    ("different_shapes", "1.2.3.10", "b.dcm differs from a.dcm in shape"),
    ("multi_frame", "1.2.3.11", "a series of multi-frame files"),
    ("no_order", "1.2.3.12", "its files have neither one orientation and a position, nor an instance number"),
    ("slide_paths", "1.2.3.14", "its VOLUME files repeat a size, as optical paths or focal planes do"),
])
def test_keeps_a_series_apart_when_it_cannot_be_joined(name, uid, reason):
    """Each case is its own reason; the files stay, and no array is at the group's root."""
    root, _ = joined(name)
    group = root[uid]
    described = own(group)
    assert described["kind"] == "instances"
    assert described["reason"] == reason
    assert "0" not in group
    assert "ome" not in group.attrs
    assert len(sequence(described["instances"])) == 2
    assert isinstance(group["instances/1/0"], zarr.Array)


def test_rejects_a_store_with_no_file_it_reads():
    with pytest.raises(Rejected, match=r"no file of the store is a DICOM file the profile reads: a.dcm: not a DICOM"):
        joined("only_broken")


def test_chooses_the_form_for_a_store_of_dcm_files():
    """`zarrify` and `zarrify_store` of a directory, its URL naming the sources."""
    for call in (zarrify, zarrify_store):
        store = call(str(SERIES / "slices"), URL)
        assert store.info["format"] == "dicom"
        document = store.mapping["1.2.3.4/0/zarr.json"]
        assert isinstance(document, bytes)
        root = json.loads(document)
        assert root["shape"] == [5, 4, 6]
        reference = store.mapping["1.2.3.4/0/c/0/0/0"]
        assert not isinstance(reference, bytes)
        assert sorted(s.url for s in store.sources if s.url) == [f"{URL}{k}.dcm" for k in "abcde"]
