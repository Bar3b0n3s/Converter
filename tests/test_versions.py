"""Each converter says which versions it understands, and when one is outside.

None of these checks change what is parsed. That is the point: a file whose
header disagrees with the layout being applied is being read on an assumption
nobody stated, and the assumption is worth stating.
"""

import struct

import pytest

import fixtures as F
from wotlkconv.adt.convert import AdtParts, convert_adt
from wotlkconv.adt.wdl import convert_wdl
from wotlkconv.adt.wdt import convert_wdt
from wotlkconv.limits import (M2_VERSION_NEWEST_KNOWN, WMO_VERSION,
                              WMO_VERSION_OLDEST_READABLE)
from wotlkconv.listfile import Listfile
from wotlkconv.m2 import convert_m2
from wotlkconv.options import Options
from wotlkconv.report import Status
from wotlkconv.wmo import convert_wmo_root


def note(res, code: str):
    return next((n for n in res.notes if n.code == code), None)


def retag(data: bytes, chunk: str, version: int) -> bytes:
    """Rewrite the version inside a file's MVER chunk."""
    magic = chunk[::-1].encode()
    at = data.index(magic) + 8
    return data[:at] + struct.pack("<I", version) + data[at + 4:]


# ---------------------------------------------------------------------------
# M2
# ---------------------------------------------------------------------------
def test_a_model_newer_than_any_known_layout_says_so():
    model = F.build_modern_model(version=M2_VERSION_NEWEST_KNOWN + 1)
    raw = F.serialise_modern_m2(model, skeleton_id=0)
    _out, res, _extra = convert_m2(raw, "t.m2", Options(), Listfile())
    warning = note(res, "m2.version.newer")
    assert warning is not None
    assert str(M2_VERSION_NEWEST_KNOWN + 1) in warning.message
    assert "blend times" in warning.message


def test_a_model_of_a_known_version_says_nothing():
    raw = F.serialise_modern_m2(F.build_modern_model(version=272),
                                skeleton_id=0)
    _out, res, _extra = convert_m2(raw, "t.m2", Options(), Listfile())
    assert note(res, "m2.version.newer") is None


def test_a_model_older_than_wrath_is_refused():
    """This tool converts downwards; it does not raise old assets up."""
    from wotlkconv.errors import UnsupportedFormatError
    from wotlkconv.m2.model import parse_m2

    raw = bytearray(F.serialise_modern_m2(F.build_modern_model(version=264),
                                          chunked=False))
    struct.pack_into("<I", raw, 4, 260)
    with pytest.raises(UnsupportedFormatError, match="predates Wrath"):
        parse_m2(bytes(raw), "t.m2")


# ---------------------------------------------------------------------------
# WMO
# ---------------------------------------------------------------------------
def test_an_alpha_world_object_is_refused_rather_than_misread(listfile):
    """v14 wraps its groups in MOMO; reading it as v17 would not fail."""
    raw = retag(F.build_modern_wmo_root(groups=1), "MVER",
                WMO_VERSION_OLDEST_READABLE - 3)
    out, res, _extra = convert_wmo_root(raw, "t.wmo", Options(), listfile)
    assert res.status is Status.FAILED and out == b""
    message = next(n.message for n in res.notes if n.level == "error")
    assert "loads and renders nonsense" in message


def test_a_newer_world_object_is_read_as_v17_and_flagged(listfile):
    raw = retag(F.build_modern_wmo_root(groups=1), "MVER", WMO_VERSION + 1)
    _out, res, _extra = convert_wmo_root(raw, "t.wmo", Options(), listfile)
    warning = note(res, "wmo.version")
    assert warning is not None and "newer than" in warning.message


def test_a_v17_world_object_says_nothing(listfile):
    raw = F.build_modern_wmo_root(groups=1)
    _out, res, _extra = convert_wmo_root(raw, "t.wmo", Options(), listfile)
    assert note(res, "wmo.version") is None
    assert note(res, "wmo.version.too_old") is None


# ---------------------------------------------------------------------------
# Terrain
# ---------------------------------------------------------------------------
def test_a_tile_declaring_another_version_is_flagged(listfile):
    root, tex, obj = F.build_split_adt(chunks=4)
    _out, res = convert_adt(AdtParts(retag(root, "MVER", 23), tex, obj),
                            "t.adt", Options(), listfile)
    warning = note(res, "adt.version")
    assert warning is not None and "23" in warning.message


def test_a_tile_of_the_usual_version_says_nothing(listfile):
    root, tex, obj = F.build_split_adt(chunks=4)
    _out, res = convert_adt(AdtParts(root, tex, obj), "t.adt", Options(),
                            listfile)
    assert note(res, "adt.version") is None


def test_a_map_index_declaring_another_version_is_flagged():
    _out, res = convert_wdt(retag(F.build_modern_wdt(), "MVER", 23), "t.wdt",
                            Options())
    warning = note(res, "wdt.version")
    assert warning is not None and "23" in warning.message


def test_a_heightmap_declaring_another_version_is_flagged():
    _out, res = convert_wdl(F.build_wdl(version=23), "t.wdl", Options())
    warning = note(res, "wdl.version")
    assert warning is not None and "23" in warning.message


def test_a_heightmap_of_the_usual_version_says_nothing():
    _out, res = convert_wdl(F.build_wdl(version=18), "t.wdl", Options())
    assert note(res, "wdl.version") is None
