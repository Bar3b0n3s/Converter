import struct

import pytest

from wotlkconv.binio import Reader, Writer
from wotlkconv.errors import TruncatedFileError


def test_reader_scalars_round_trip():
    w = Writer()
    w.u8(0xFE); w.i8(-2); w.u16(0xBEEF); w.i16(-300)
    w.u32(0xDEADBEEF); w.i32(-70000); w.f32(1.5)
    r = Reader(w.getvalue())
    assert r.u8() == 0xFE
    assert r.i8() == -2
    assert r.u16() == 0xBEEF
    assert r.i16() == -300
    assert r.u32() == 0xDEADBEEF
    assert r.i32() == -70000
    assert r.f32() == 1.5
    assert r.eof()


def test_reader_reports_the_file_that_ran_out():
    r = Reader(b"\x01\x02", name="broken.m2")
    with pytest.raises(TruncatedFileError, match="broken.m2"):
        r.u32()


def test_reader_at_is_independent_of_the_cursor():
    r = Reader(struct.pack("<II", 7, 9))
    other = r.at(4)
    assert other.u32() == 9
    assert r.u32() == 7


def test_cstring_at_stops_at_the_terminator():
    r = Reader(b"world\\foo.blp\0trailing")
    assert r.cstring_at(0) == "world\\foo.blp"


def test_writer_patch_and_align():
    w = Writer()
    pos = w.reserve(4)
    w.raw(b"abc")
    assert w.align(4) == 8
    w.patch_u32(pos, 0x11223344)
    data = w.getvalue()
    assert struct.unpack_from("<I", data, 0)[0] == 0x11223344
    assert len(data) == 8


def test_magic_must_be_four_bytes():
    with pytest.raises(ValueError):
        Writer().magic("MD2")
