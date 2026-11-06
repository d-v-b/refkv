"""`refkv.zarrify.zarrify` returns a `refkv.ReferenceStore`, which reads in memory
as its saved archive reads, and whose `save_vzip` writes the archive the profile output's
own writer writes, byte for byte."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import refkv
from refkv.errors import ResolutionError
from refkv.pb import Range, Source
from refkv.policy import Policy
from refkv.uri import is_uri_reference
from refkv.zarrify import _zarrify, zarrify
from refkv.zarrify.common import Output, reference_store

sys.path.insert(0, str(Path(__file__).parents[2] / "conformance" / "zarrify"))
from proxy import Proxy

FIXTURES = Path(__file__).parents[2] / "fixtures"
LOOPBACK = Policy(allow_private_hosts=True)  # the proxy serves the stores on 127.0.0.1


@pytest.fixture(scope="module")
def proxy(tmp_path_factory):
    return Proxy(FIXTURES, tmp_path_factory.mktemp("cache"))


# (fixture, a store, checksums, format): a TIFF through the Rust core, NIfTI, MRC, an MP3, a FLAC file
# (sharded), an MP4, a VSI slide (a file whose tile files make it a store output), NRRD (attached, and a
# detached header and its data file), Imaris IMS, N5 stores (one with objects it ignores, one with an empty
# block), an OME-Zarr 0.4 store and a Zarr v2 store (all the core's), and a SAFE product as a store (whose
# ranges start past byte 0, beside data sources) and as a zip file
INPUTS = [
    ("tiff/jpeg_gray.tif", False, False, "tiff"),
    ("tiff/jpeg_gray.tif", False, True, "tiff"),
    ("nifti/nifti_block_x_split.nii", False, False, "nifti"),
    ("mrc/mrc_chunk_sections.mrc", False, True, "mrc"),
    ("mp3/mp3_tags_id3v2_id3v1.mp3", False, False, "mp3"),
    ("flac/flac_blocks.flac", False, False, "flac"),
    ("flac/flac_blocks.flac", False, True, "flac"),
    ("mp4/mp4_h264_aac.mp4", False, True, "mp4"),
    ("vsi/vsi_rgb_jpeg.vsi", False, False, "vsi"),
    ("vsi/vsi_raw_tzc.vsi", False, True, "vsi"),
    ("nrrd/nrrd_gzip_3d.nrrd", False, False, "nrrd"),
    ("nrrd/nrrd_detached_raw.nhdr", False, True, "nrrd"),
    ("ims/ims_latest_uint8.ims", False, False, "ims"),
    ("ims/ims_latest_uint8.ims", False, True, "ims"),
    ("n5/n5_hierarchy", True, False, "n5"),
    ("n5/n5_hierarchy", True, True, "n5"),
    ("n5/n5_objects", True, False, "n5"),
    ("n5/n5_root_dataset_empty_block", True, False, "n5"),
    ("ome-zarr/ome_zarr_image_2d", True, False, "ome-zarr"),
    ("ome-zarr/ome_zarr_image_2d", True, True, "ome-zarr"),
    ("zarr2/zarr2_hierarchy", True, False, "zarr2"),
    ("zarr2/zarr2_hierarchy", True, True, "zarr2"),
    ("safe/safe_l2a_pb0207", True, False, "safe"),
    ("safe/safe_l2a_pb0207", True, True, "safe"),
    ("safe/safe_l2a_pb0207.SAFE.zip", False, False, "safe"),
]


@pytest.mark.parametrize(("name", "is_store", "checksums", "fmt"), INPUTS, ids=str)
def test_zarrify_returns_the_store_it_saves(proxy, tmp_path, name, is_store, checksums, fmt):
    location, policy = (proxy.local_store(name), LOOPBACK) if is_store else (str(FIXTURES / name), None)
    store = zarrify(location, checksums=checksums, policy=policy)
    assert isinstance(store, refkv.ReferenceStore)
    _, out = _zarrify(location, checksums=checksums, policy=policy)
    assert store.info == {"format": fmt, **out.summary}

    store.save_vzip(tmp_path / "new.vzip", policy=policy)
    out.write(str(tmp_path / "old.vzip"))
    assert (tmp_path / "new.vzip").read_bytes() == (tmp_path / "old.vzip").read_bytes()

    reading = policy or Policy(prefixes=((FIXTURES / name).parent.as_uri() + "/",))
    saved = refkv.open(str(tmp_path / "new.vzip"), policy=reading)
    keys = store.list()
    assert keys == saved.list()
    assert any(store.classify(k) == "reference" for k in keys)
    for k in keys:
        assert store.classify(k) == saved.classify(k), k
        assert store.get(k) == saved.get(k), k


def test_a_local_input_is_named_by_its_percent_encoded_path(tmp_path, monkeypatch):
    """Without `url`, a local input's sources are named by its path, percent-encoded (spec §5
    <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#5-source-table>): a store reads them
    in memory and once saved, whatever the names of the file and its directory hold, given as
    an absolute or a relative path. A VSI slide's tile files are named beside it."""
    monkeypatch.chdir(tmp_path)
    nifti, vsi = FIXTURES / "nifti" / "nifti_block_x_split.nii", FIXTURES / "vsi" / "vsi_raw_bgr.vsi"
    table = [  # (directory, file name, the fixture, relative)
        ("d", "a b.nii", nifti, False),
        ("d d", "a.nii", nifti, False),
        ("100%", "#1.nii", nifti, True),
        ("é ü", "ß.nii", nifti, False),
        ("s p%#é", vsi.name, vsi, False),
        ("s p%#é", vsi.name, vsi, True),
    ]
    if sys.platform != "win32":  # Windows forbids `?` and `:` in a file name
        table += [("é ü", "ß?.nii", nifti, False), ("x:y", "a.nii", nifti, True)]
    for i, (dirname, name, fixture, relative) in enumerate(table):
        d = tmp_path / f"{i}{dirname}"  # the first segment of a relative path
        d.mkdir()
        shutil.copy(fixture, d / name)
        if fixture == vsi:
            shutil.copytree(fixture.parent / "_vsi_raw_bgr_", d / "_vsi_raw_bgr_")
        path = d / name
        store = zarrify(str(path.relative_to(tmp_path) if relative else path))
        want = zarrify(str(fixture))
        assert all(is_uri_reference(s.url or "") for s in store.sources), (dirname, name)
        store.save_vzip(tmp_path / f"{i}.vzip")
        saved = refkv.open(str(tmp_path / f"{i}.vzip"), policy=Policy(schemes=frozenset({"file"})))
        keys = want.list()
        assert store.list() == saved.list() == keys
        for k in keys:  # the metadata names the source, so only the data matches the fixture's
            assert store.get(k) == saved.get(k), (dirname, name, k)
            if want.classify(k) == "reference":
                assert store.get(k) == want.get(k), (dirname, name, k)


def test_a_zarrified_store_reads_its_files_and_its_saved_archive_needs_a_policy(tmp_path):
    """The store `zarrify` returns reads its input with no policy argument. Whoever opens what it
    saved names the files (spec §6.7
    <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy> rule 1)."""
    path = FIXTURES / "nifti" / "nifti_block_x_split.nii"
    store = zarrify(str(path))
    key = next(k for k in store.list() if store.classify(k) == "reference")
    data = store.get(key)
    assert data
    store.save_vzip(tmp_path / "a.vzip")
    with pytest.raises(ResolutionError, match="reads a local file only when it is allowed"):
        refkv.open(tmp_path / "a.vzip").get(key)
    assert refkv.open(tmp_path / "a.vzip", policy=Policy(prefixes=(path.parent.as_uri() + "/",))).get(key) == data


@pytest.mark.skipif(sys.platform == "win32", reason="making a symbolic link takes a privilege")
def test_a_zarrified_store_refuses_a_link_put_in_place_of_its_file(tmp_path):
    """The store `zarrify` returns allows its input's directory as a prefix, not every local file,
    so a file replaced after zarrifying by a link out of that directory is refused (spec §6.7
    <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy> rule 1)."""
    (tmp_path / "in").mkdir()
    path = tmp_path / "in" / "x.nii"
    shutil.copy(FIXTURES / "nifti" / "nifti_block_x_split.nii", path)
    store = zarrify(str(path))
    assert store.policy.prefixes == (f"{(tmp_path / 'in').as_uri()}/",)
    key = next(k for k in store.list() if store.classify(k) == "reference")
    assert store.get(key)
    shutil.copy(path, tmp_path / "outside.nii")
    path.unlink()
    path.symlink_to(tmp_path / "outside.nii")
    with pytest.raises(ResolutionError, match="not under an allowed prefix"):
        store.get(key)


@pytest.mark.parametrize("entries", [[("a", b"x", False)], []], ids=["entry", "reference"])
def test_reference_store_rejects_a_duplicate_key(entries):
    ref = ("a", [Range(source=0, offset=0, length=1)])
    with pytest.raises(ValueError, match="duplicate key 'a'"):
        reference_store([ref] + ([] if entries else [ref]), entries, [Source(url="https://data.test/a")])


@pytest.mark.parametrize("module", ["refkv.zarrify", "refkv.ir"])
def test_cli_refuses_to_save_sources_its_policy_refuses(tmp_path, module):
    out = tmp_path / "out.vzip"
    p = subprocess.run([sys.executable, "-m", module, str(FIXTURES / "nifti/nifti_block_x_split.nii"),
                        str(out), "--url", "http://127.0.0.1/x.nii"], capture_output=True, text=True, check=False)
    assert p.returncode == 1, p.stderr
    assert "error: not saving" in p.stderr, p.stderr
    assert not out.exists(), p.stderr


@pytest.mark.parametrize("module", ["refkv.zarrify", "refkv.ir"])
def test_cli_refuses_to_read_an_input_its_policy_refuses(tmp_path, module):
    out = tmp_path / "out.vzip"
    p = subprocess.run([sys.executable, "-m", module, "http://127.0.0.1:9/x.nii", str(out)],
                       capture_output=True, text=True, check=False)
    assert p.returncode == 1, p.stderr
    assert p.stderr.startswith("error: "), p.stderr
    assert not out.exists(), p.stderr


def test_a_failed_write_keeps_the_previous_file(tmp_path):
    def unreadable(_url, _offset, _length):
        raise OSError("unreadable")

    path = tmp_path / "o.vzip"
    path.write_bytes(b"previous")
    out = Output("http://h/a.bin", refs={"a": [(0, 4)]}, checksum=unreadable)
    with pytest.raises(OSError, match="unreadable"):
        out.write(str(path))
    assert path.read_bytes() == b"previous"
    assert [p.name for p in tmp_path.iterdir()] == ["o.vzip"]
