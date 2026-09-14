"""Liquid volumes: 3.3.5a reads them, so they belong in a patch."""

import pytest

import fixtures as F
from wotlkconv.detect import LIQUID, classify, detect
from wotlkconv.errors import MalformedFileError, UnsupportedFormatError
from wotlkconv.liquid import (WOTLK_LIQUID_VERSIONS, convert_liquid,
                              inspect_liquid, parse_header)
from wotlkconv.options import Options
from wotlkconv.report import Status


def test_a_liquid_volume_is_recognised_without_a_name():
    assert detect(F.build_liquid(), "") == LIQUID


def test_the_other_byte_order_is_recognised_too():
    """Tools disagree about which way the signature reads."""
    assert detect(F.build_liquid(magic=b"*QIL"), "") == LIQUID


def test_a_liquid_volume_is_converted_not_merely_copied():
    assert classify(LIQUID, "world/maps/az/az.wlw")[0] == "convert"


@pytest.mark.parametrize("version", WOTLK_LIQUID_VERSIONS)
def test_a_version_wrath_reads_is_used_unchanged(version):
    source = F.build_liquid(version=version, blocks=3)
    out, res = convert_liquid(source, "az.wlw", Options())
    assert out == source
    assert res.status is Status.PASSTHROUGH
    assert res.extra["blocks"] == 3 and res.extra["version"] == version
    assert any(n.code == "liquid.compatible" for n in res.notes)


def test_a_newer_version_is_refused_rather_than_shipped():
    """Water in the wrong place is worse than no water."""
    out, res = convert_liquid(F.build_liquid(version=5), "az.wlw", Options())
    assert res.status is Status.FAILED and out == b""
    message = next(n.message for n in res.notes if n.level == "error")
    assert "version 5" in message and "3.3.5a reads" in message


def test_the_liquid_type_is_carried_into_the_report():
    _out, res = convert_liquid(F.build_liquid(liquid_type=7), "az.wlw",
                               Options())
    assert res.extra["liquid_type"] == 7


def test_a_file_that_is_not_a_liquid_volume_says_so():
    with pytest.raises(UnsupportedFormatError, match="not a liquid volume"):
        convert_liquid(b"NOPE" + b"\0" * 16, "az.wlw", Options())


def test_a_truncated_header_is_malformed():
    with pytest.raises(MalformedFileError, match="too short"):
        parse_header(b"LIQ*\x01\x00", "az.wlw")


def test_a_block_count_that_cannot_fit_is_refused():
    """Catches truncation without claiming to know the block layout."""
    claims_many = F.build_liquid(blocks=2)[:12].replace(
        b"\x02\x00\x00\x00", b"\xff\xff\x00\x00") + b"\0" * 32
    with pytest.raises(MalformedFileError, match="cannot fit"):
        parse_header(claims_many, "az.wlw")


def test_inspect_reports_whether_the_old_client_can_read_it():
    assert inspect_liquid(F.build_liquid(version=1), "a.wlw")["reads_in_wotlk"]
    assert not inspect_liquid(F.build_liquid(version=9), "a.wlw")["reads_in_wotlk"]
    assert inspect_liquid(b"junk", "a.wlw")["readable"] is False
