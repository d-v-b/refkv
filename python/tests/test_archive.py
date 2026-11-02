import io
import struct
import zipfile
from typing import override

import pytest

from refkv._sync import run
from refkv.errors import ArchiveError, EntryError
from refkv.pb import Pinned, Range, Reference
from refkv.reader import OffsetRequest, RangeRequest, SuffixRequest
from refkv.uri import file_uri
from refkv.vzip.archive import VZipWriter, parse_central_directory, parse_tail
from refkv.vzip.store import VZipStore, _check_index

U32 = 0xFFFFFFFF


def _get(store, key, request=None):
    return store.get(key, request)


def _ref(store: VZipStore, key: str) -> Reference | None:
    """The reference of the entry `key`, which the store has."""
    e = store.entry(key)
    assert e is not None, key
    return e.ref


def test_overlay_semantics(tmp_path):
    target = tmp_path / "blob.bin"
    blob = bytes(range(256)) * 4
    target.write_bytes(blob)
    arc = tmp_path / "x.vzip"
    with open(arc, "wb") as f, VZipWriter(f) as w:
        w.add_bytes("a/zarr.json", b"{}")
        w.add_ref("a/c/0", file_uri(target), 10, 100)
        w.add_ref("a/c/1", "blob.bin", 500, 20)  # relative to the archive
        ext = w.url(file_uri(target))
        w.add_ranges("a/c/2", [Range(data=b"HEAD"), Range(source=ext, offset=0, length=4)])
        # internal references: a deflated hidden entry, and a forward reference to a
        # zarr key written later (checked when the archive is closed)
        hidden = w.internal(w.add_hidden("tail", b"0123456789"))
        w.add_ranges("a/c/3", [Range(source=hidden, offset=2, length=5),
                               Range(source=w.internal("a/late.bin"), length=2)])
        # a decoding header stored once in the source table, shared by two chunks
        hdr = w.blob(b"HDR:")
        w.add_ranges("a/c/4", [Range(source=hdr, length=4), Range(source=ext, offset=4, length=2)])
        w.add_ranges("a/c/5", [Range(source=hdr, offset=1, length=3), Range(source=ext, offset=8, length=1)])
        w.add_bytes("a/late.bin", b"{}", late=True)

    s = VZipStore(str(arc))
    cases = {
        ("a/zarr.json", None): b"{}",
        ("a/c/0", None): blob[10:110],
        ("a/c/0", RangeRequest(5, 15)): blob[15:25],
        ("a/c/0", OffsetRequest(90)): blob[100:110],
        ("a/c/0", SuffixRequest(3)): blob[107:110],
        ("a/c/1", None): blob[500:520],
        ("a/c/2", None): b"HEAD" + blob[:4],
        ("a/c/2", RangeRequest(2, 6)): b"AD" + blob[:2],
        ("a/c/3", None): b"23456{}",
        ("a/c/3", SuffixRequest(4)): b"56{}",
        ("a/c/4", None): b"HDR:" + blob[4:6],
        ("a/c/5", None): b"DR:" + blob[8:9],
        ("a/c/9", None): None,
        ("__vz__/sources", None): None,  # reserved, hidden from the resolved view
    }
    for (k, br), want in cases.items():
        assert _get(s, k, br) == want, (k, br)
    assert {k: s.classify(k) for k in ["a/zarr.json", "a/c/0", "a/c/2", "nope"]} == {
        "a/zarr.json": "bytes", "a/c/0": "reference", "a/c/2": "reference", "nope": "missing"}
    # one range -> bare Range (0x7a76); several -> Concat (0x7a77)
    assert {k: type(_ref(s, k)).__name__ for k in ["a/c/0", "a/c/1", "a/c/2", "a/c/3"]} == {
        "a/c/0": "Range", "a/c/1": "Range", "a/c/2": "Concat", "a/c/3": "Concat"}
    with zipfile.ZipFile(arc) as zf:
        assert zf.getinfo("a/c/0").extra[:2] == b"\x76\x7a"
        assert zf.getinfo("a/c/2").extra[:2] == b"\x77\x7a"

    naive = VZipStore(str(arc), resolve=False)
    with zipfile.ZipFile(arc) as zf:
        for name in zf.namelist():
            assert _get(naive, name) == zf.read(name)


def test_zip64_many_entries_paged(tmp_path):
    arc = tmp_path / "many.vzip"
    n = 70_000
    with open(arc, "wb") as f, VZipWriter(f, page_size=1 << 16) as w:
        w.add_bytes("zarr.json", b"{}", late=True)
        for i in range(n):
            w.add_ref(f"c/{i}", "file:///nowhere", i, 1)
    with zipfile.ZipFile(arc) as zf:
        assert len(zf.namelist()) == n + 3  # + urls, index
    s = VZipStore(str(arc))
    run(s._ensure_open())
    opened = s.stats.archive_requests
    assert _get(s, "zarr.json") == b"{}"  # pinned: no page load
    assert s.classify("c/69999") == "reference"
    assert s.classify("c/69999x") == "missing"
    ref = _ref(s, "c/69999")
    assert isinstance(ref, Range)
    assert ref.offset == 69999
    assert s.stats.archive_requests - opened == 1  # one page, shared by both lookups
    assert len(s._loaded) == 1 < len(s._pages)
    keys = s.list()
    assert len(keys) == n + 1


def test_unregistered_source_rejected():
    with pytest.raises(ValueError, match="unregistered source"):
        VZipWriter(io.BytesIO()).add_ranges("k", [Range(source=0, length=1)])


def test_close_accepts_internal_and_data_sources(tmp_path):
    """Internal references to an entry, a hidden entry and a late one, and a data source, pass
    `close`'s checks, which run before the late entries are written."""
    arc = tmp_path / "x.vzip"
    with open(arc, "wb") as f, VZipWriter(f) as w:
        w.add_bytes("a/zarr.json", b"{}", late=True)
        w.add_bytes("blob", b"0123456789")
        hidden = w.add_hidden("idx", b"abcd")
        w.add_ranges("c/0", [Range(source=w.internal("blob"), offset=2, length=3)])
        w.add_ranges("c/1", [Range(source=w.internal(hidden), length=4)])
        w.add_ranges("c/2", [Range(source=w.internal("a/zarr.json"), length=2)])
        w.add_ranges("c/3", [Range(source=w.blob(b"xyz"), offset=1, length=2)])
    s = VZipStore(str(arc))
    assert [_get(s, f"c/{i}") for i in range(4)] == [b"234", b"abcd", b"{}", b"yz"]
    assert f.closed  # the `with open` closed it; close() left it open


def _close_refused(w: VZipWriter, f: io.BytesIO, match: str) -> None:
    """`w.close()` raises ValueError matching `match`, having written nothing more to `f`."""
    written = f.getvalue()
    with pytest.raises(ValueError, match=match):
        w.close()
    assert f.getvalue() == written
    assert not f.closed


def test_close_refuses_a_missing_entry():
    f = io.BytesIO()
    w = VZipWriter(f)
    w.add_bytes("a/zarr.json", b"{}", late=True)
    w.add_ranges("k", [Range(source=w.internal("nope"), length=1)])
    _close_refused(w, f, "missing entries: \\['nope'\\]")


def test_close_refuses_an_invalid_url_source():
    f = io.BytesIO()
    w = VZipWriter(f)
    w.add_ref("k", "http://[bad", 0, 1)
    _close_refused(w, f, "invalid url sources")


def test_close_refuses_an_empty_key_source():
    f = io.BytesIO()
    w = VZipWriter(f)
    w.add_ranges("k", [Range(source=w.internal(""), length=1)])
    _close_refused(w, f, "empty key source")


def test_close_refuses_a_key_source_naming_a_format_entry():
    f = io.BytesIO()
    w = VZipWriter(f)
    w.add_ranges("k", [Range(source=w.internal("__vz__/sources"), length=1)])
    _close_refused(w, f, "format entries")


def test_close_refuses_a_key_source_naming_a_reference():
    f = io.BytesIO()
    w = VZipWriter(f)
    w.add_ref("r", "http://example.org/x", 0, 1)
    w.add_ranges("k", [Range(source=w.internal("r"), length=1)])
    _close_refused(w, f, "internal references to reference entries")


def test_close_refuses_a_range_past_its_source():
    f = io.BytesIO()
    w = VZipWriter(f)
    w.add_bytes("blob", b"0123")
    w.add_ranges("k", [Range(source=w.internal("blob"), offset=2, length=3)])
    _close_refused(w, f, "past the end of source")


def test_duplicate_key_rejected():
    w = VZipWriter(io.BytesIO())
    w.add_bytes("k", b"1")
    with pytest.raises(ValueError, match="duplicate"):
        w.add_ref("k", "u", 0, 1)


def test_reserved_key_rejected():
    with pytest.raises(ValueError, match="reserved"):
        VZipWriter(io.BytesIO()).add_bytes("__vz__/x", b"")


# ---------------------------------------------------------------- ZIP64 (spec §10.2
# <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#102-zip64>)


def _small(path, page_size=None):
    with open(path, "wb") as f, VZipWriter(f, page_size=page_size) as w:
        w.add_bytes("z", b"hello")
        w.add_bytes("zarr.json", b"{}", late=True)
        w.add_ref("x", "file:///nowhere", 3, 2)
    return bytearray(path.read_bytes())


def _tail(b: bytes | bytearray) -> tuple[int, int, int]:
    """The offsets of the zip64 record, its locator and the end record."""
    for clen in (38, 22):
        eocd = len(b) - 22 - clen
        if b[eocd : eocd + 4] == b"PK\x05\x06" and struct.unpack_from("<H", b, eocd + 20)[0] == clen:
            break
    return eocd - 76, eocd - 20, eocd


def test_end_records(tmp_path):
    # every archive ends with the zip64 record, its locator and an all-ones end record,
    # whatever its size; readers take the directory from the zip64 record and ignore the
    # end record's counts, size and offset, whatever they hold
    for page_size, eocd_fields in [(None, None), (64, None), (None, (0, 0, 0, 0)),
                                   (64, (7, 9, U32 - 15, 0x12345678))]:
        path = tmp_path / "a.vzip"
        b = _small(path, page_size)
        z, loc, eocd = _tail(b)
        assert struct.unpack_from("<IQHH", b, z) == (0x06064B50, 44, 20, 45)
        n, n_total, cd_size, cd_off = struct.unpack_from("<QQQQ", b, z + 24)
        assert n == n_total == 4 + (page_size is not None)
        assert cd_off + cd_size == z
        assert struct.unpack_from("<IIQI", b, loc) == (0x07064B50, 0, z, 1)
        assert struct.unpack_from("<HHHHII", b, eocd + 4) == (0, 0, 0xFFFF, 0xFFFF, U32, U32)
        if eocd_fields:
            struct.pack_into("<HHII", b, eocd + 8, *eocd_fields)
            path.write_bytes(bytes(b))
        d = parse_tail(bytes(b), len(b))
        assert (d.cd_offset, d.cd_size) == (cd_off, cd_size)
        s = VZipStore(str(path))
        assert (_get(s, "z"), _get(s, "zarr.json"), s.classify("x")) == (b"hello", b"{}", "reference")
        with zipfile.ZipFile(path) as zf:
            assert zf.testzip() is None


def _break_tail(tmp_path, mutate):
    path = tmp_path / "a.vzip"
    b = _small(path)
    out = mutate(b, *_tail(b))
    path.write_bytes(bytes(b if out is None else out))
    return str(path)


def test_open_fails_without_zip64_locator(tmp_path):
    arc = _break_tail(tmp_path, lambda b, z, _loc, eocd: b[:z] + b[eocd:])
    with pytest.raises(ArchiveError, match="locator"):
        run(VZipStore(arc)._ensure_open())


def test_open_fails_with_bad_zip64_record_signature(tmp_path):
    arc = _break_tail(tmp_path, lambda b, z, _loc, _eocd: struct.pack_into("<I", b, z, 0x06064B51))
    with pytest.raises(ArchiveError, match="zip64"):
        run(VZipStore(arc)._ensure_open())


def test_open_fails_with_zip64_record_size_not_44(tmp_path):
    arc = _break_tail(tmp_path, lambda b, z, _loc, _eocd: struct.pack_into("<Q", b, z + 4, 45))
    with pytest.raises(ArchiveError, match="zip64"):
        run(VZipStore(arc)._ensure_open())


def test_open_fails_with_zip64_record_outside_file(tmp_path):
    arc = _break_tail(tmp_path, lambda b, _z, loc, _eocd: struct.pack_into("<Q", b, loc + 8, len(b) - 55))
    with pytest.raises(ArchiveError, match="zip64"):
        run(VZipStore(arc)._ensure_open())


# ---------------------------------------------------------------- large entries (spec §10.1
# <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#101-structure> rule 7)


def test_large_entry_headers():
    # a large entry's sizes go in ZIP64 fields of both headers; the record's block holds
    # the sizes, then an offset that does not fit, and the body is 20 bytes further on
    big, far = 0x100000000 + 7, 0x200000000
    out = io.BytesIO()
    VZipWriter(out)._local_header(b"k", 0, 0x1234, big, big)
    lfh = out.getvalue()
    assert struct.unpack_from("<H", lfh, 4)[0] == 45  # version needed
    assert struct.unpack_from("<II", lfh, 18) == (U32, U32)
    assert struct.unpack_from("<HH", lfh, 26) == (1, 20)
    assert lfh[31:] == struct.pack("<HHQQ", 1, 16, big, big)
    for size, offset, block in [
        (big, 0, struct.pack("<QQ", big, big)),
        (big, far, struct.pack("<QQQ", big, big, far)),
        (U32, 0, struct.pack("<QQ", U32, U32)),
        (5, far, struct.pack("<Q", far)),
        (5, 0, b""),
    ]:
        rec = VZipWriter._cd_record("k", offset, size, size, 0, 0x1234, b"")
        assert rec[47:] == (struct.pack("<HH", 1, len(block)) + block if block else b"")
        e = parse_central_directory(rec, trust_offsets=True)["k"]
        assert e.error is None
        assert (e.size, e.csize) == (size, size)
        assert e.data_offset == offset + 31 + (20 if size >= U32 else 0)


def _sparse(w, key, size, tail):
    """A STORED entry of `size` bytes, zeros then `tail`, the zeros left as a hole in the
    file. Its CRC-32 is not computed: readers need not verify it (spec §13.4
    <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#134-violations-readers-need-not-detect>)."""
    w._check_new_key(key)
    w._names.add(key)
    off = w._pos
    w._local_header(key.encode(), 0, 0, size, size)
    w._f.seek(size - len(tail), 1)
    w._f.write(tail)
    w._pos += size
    w._cd.append((key, off, size, size, 0, 0, b""))


def test_reads_large_entries(tmp_path):
    # entries of exactly 0xFFFFFFFF bytes and of 2^32 + 3, and an entry after them whose
    # offset needs ZIP64 too, in a sparse file of 8 GiB that takes no disk space
    for page_size in (None, 64):
        path = tmp_path / "large.vzip"
        with open(path, "wb") as f, VZipWriter(f, page_size=page_size) as w:
            _sparse(w, "edge", U32, b"EDGE")
            _sparse(w, "big", (1 << 32) + 3, b"tail!")
            w.add_bytes("after", b"past 8 GiB")
            w.add_bytes("zarr.json", b"{}", late=True)
            w.add_ref("x", "file:///nowhere", 3, 2)
        s = VZipStore(str(path))
        assert s.classify("big") == "bytes"
        assert _get(s, "big", SuffixRequest(5)) == b"tail!"
        assert _get(s, "big", RangeRequest((1 << 32) - 4, 1 << 32)) == b"\0\0ta"
        assert _get(s, "edge", OffsetRequest(U32 - 6)) == b"\0\0EDGE"
        assert _get(s, "after") == b"past 8 GiB"
        assert _get(s, "zarr.json") == b"{}"
        assert s.list() == ["after", "big", "edge", "x", "zarr.json"]
        path.unlink()


def _patched(tmp_path, key, mutate):
    """An archive whose record for `key` has the size fields, offset field, extra field
    and method that `mutate(size, csize, offset, extra, method)` returns."""
    path = tmp_path / "a.vzip"
    b = _small(path)
    z, _loc, _eocd = _tail(b)
    cd_size, cd_off = struct.unpack_from("<QQ", b, z + 40)
    i = b.index(b"PK\x01\x02", cd_off)
    while b[i + 46 : i + 46 + struct.unpack_from("<H", b, i + 28)[0]] != key.encode():
        i = b.index(b"PK\x01\x02", i + 4)
    method = struct.unpack_from("<H", b, i + 10)[0]
    csize, size = struct.unpack_from("<II", b, i + 20)
    nlen, xlen = struct.unpack_from("<HH", b, i + 28)
    off = struct.unpack_from("<I", b, i + 42)[0]
    size, csize, off, extra, method = mutate(size, csize, off, bytes(b[i + 46 + nlen : i + 46 + nlen + xlen]), method)
    struct.pack_into("<H", b, i + 10, method)
    struct.pack_into("<II", b, i + 20, csize, size)
    struct.pack_into("<H", b, i + 30, len(extra))
    struct.pack_into("<I", b, i + 42, off)
    b[i + 46 + nlen : i + 46 + nlen + xlen] = extra
    # the directory changed size, and the zip64 record moved
    z += len(extra) - xlen
    struct.pack_into("<Q", b, z + 40, cd_size + len(extra) - xlen)
    struct.pack_into("<Q", b, z + 56 + 8, z)
    path.write_bytes(bytes(b))
    s = VZipStore(str(path))
    assert _get(s, "zarr.json") == b"{}"  # other keys are unaffected
    return s


def _z64(*values):
    return struct.pack("<HH", 1, 8 * len(values)) + struct.pack(f"<{len(values)}Q", *values)


def test_entry_error_exactly_one_size_all_ones(tmp_path):
    s = _patched(tmp_path, "z", lambda _n, c, o, x, m: (U32, c, o, _z64(U32, c) + x, m))
    with pytest.raises(EntryError, match="exactly one size"):
        _get(s, "z")


def test_entry_error_zip64_block_too_short_for_sizes(tmp_path):
    s = _patched(tmp_path, "z", lambda n, _c, o, x, m: (U32, U32, o, _z64(n) + x, m))
    with pytest.raises(EntryError, match="ZIP64 extra block"):
        _get(s, "z")


def test_entry_error_zip64_block_without_offset(tmp_path):
    s = _patched(tmp_path, "z", lambda n, c, _o, x, m: (U32, U32, U32, _z64(n, c) + x, m))
    with pytest.raises(EntryError, match="ZIP64 extra block"):
        _get(s, "z")


def test_entry_error_large_without_zip64_block(tmp_path):
    s = _patched(tmp_path, "z", lambda _n, _c, o, x, m: (U32, U32, o, x, m))
    with pytest.raises(EntryError, match="ZIP64 extra block"):
        _get(s, "z")


def test_entry_error_large_reference(tmp_path):
    s = _patched(tmp_path, "x", lambda n, c, o, x, m: (U32, U32, o, _z64(n, c) + x, m))
    with pytest.raises(EntryError, match="reference entry is large"):
        s.classify("x")


def test_entry_error_large_deflate(tmp_path):
    # "hello" DEFLATEd is 7 bytes: a well-formed body, refused because the entry is large
    s = _patched(tmp_path, "z", lambda _n, _c, o, x, _m: (U32, U32, o, _z64(5, 7) + x, 8))
    with pytest.raises(EntryError, match="large entry is not STORED"):
        _get(s, "z")


def test_open_fails_with_pinned_large_deflate():
    # a pinned key's record is never consulted, so the page index is where a large
    # DEFLATE entry is caught (spec §12.2
    # <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#122-lookup>)
    for size, csize in [(U32, 1), (5, U32)]:
        with pytest.raises(ArchiveError, match="large and not STORED"):
            _check_index([], [Pinned("k", 0, size, csize, 8)], 0, 1 << 40)
    _check_index([], [Pinned("k", 0, U32, U32, 0)], 0, 1 << 40)  # STORED: fine


def test_add_file(tmp_path):
    files = {"empty": b"", "small": b"abc", "blocks": bytes(range(256)) * 3}
    for name, data in files.items():
        (tmp_path / name).write_bytes(data)
    arc = tmp_path / "f.vzip"
    with open(arc, "wb") as f, VZipWriter(f, page_size=64) as w:
        for name in files:
            w.add_file(f"k/{name}", tmp_path / name, block=100)
    with zipfile.ZipFile(arc) as z:
        assert z.testzip() is None
        assert {n: z.read(f"k/{n}") for n in files} == files
    s = VZipStore(str(arc))
    assert {n: _get(s, f"k/{n}") for n in files} == files


def test_add_file_reserved_key_rejected(tmp_path):
    (tmp_path / "a").write_bytes(b"x")
    with pytest.raises(ValueError, match="reserved"):
        VZipWriter(io.BytesIO()).add_file("__vz__/x", tmp_path / "a")


def test_add_file_duplicate_key_rejected(tmp_path):
    (tmp_path / "a").write_bytes(b"x")
    w = VZipWriter(io.BytesIO())
    w.add_bytes("k", b"1")
    with pytest.raises(ValueError, match="duplicate"):
        w.add_file("k", tmp_path / "a")


def test_large_deflate_entry_rejected():
    class Large(bytes):  # the length of a large body, without its bytes
        @override
        def __len__(self):
            return 1 << 32
    with pytest.raises(ValueError, match="must be STORED"):
        VZipWriter(io.BytesIO())._entry("k", Large(), compress=True)
