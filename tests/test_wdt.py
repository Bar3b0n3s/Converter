import struct

import pytest

import fixtures as F
from wotlkconv.adt.wdt import (FLAG_ADT_HAS_BIG_ALPHA, FLAG_GLOBAL_WMO,
                               convert_wdt, inspect_wdt, parse_wdt)
from wotlkconv.errors import UnsupportedFormatError
from wotlkconv.report import Status


def flags_of(data: bytes) -> int:
    return struct.unpack_from("<I", parse_wdt(data, "w")["MPHD"].data, 0)[0]


def test_modern_map_index_is_recognised():
    info = inspect_wdt(F.build_modern_wdt(), "w.wdt")
    assert info["tiles_present"] == 2
    assert info["modern_chunks"] == ["MAID", "MPL2"]
    assert info["wotlk_compatible"] is False


def test_maid_and_other_modern_chunks_are_dropped(opts):
    out, res = convert_wdt(F.build_modern_wdt(), "w.wdt", opts)
    names = set(parse_wdt(out, "o"))
    assert "MAID" not in names and "MPL2" not in names
    assert {"MVER", "MPHD", "MAIN", "MWMO"} <= names
    assert any(n.code == "wdt.chunks.dropped" for n in res.notes)


def test_the_big_alpha_flag_is_set(opts):
    """Cataclysm+ terrain writes 8-bit MCAL; without this bit it renders wrong."""
    out, res = convert_wdt(F.build_modern_wdt(flags=0x0200), "w.wdt", opts)
    assert flags_of(out) & FLAG_ADT_HAS_BIG_ALPHA
    assert any(n.code == "wdt.big_alpha" for n in res.notes)


def test_post_wrath_flags_are_cleared(opts):
    out, res = convert_wdt(F.build_modern_wdt(flags=0x0202), "w.wdt", opts)
    assert flags_of(out) == (0x2 | FLAG_ADT_HAS_BIG_ALPHA)
    assert any(n.code == "wdt.flags" for n in res.notes)


def test_the_cata_map_texture_id_is_cleared(opts):
    out, _res = convert_wdt(F.build_modern_wdt(), "w.wdt", opts)
    mphd = parse_wdt(out, "o")["MPHD"].data
    assert struct.unpack_from("<I", mphd, 4)[0] == 0


def test_tile_presence_survives(opts):
    out, res = convert_wdt(F.build_modern_wdt(tiles=((1, 2), (3, 4), (5, 6))),
                           "w.wdt", opts)
    main = parse_wdt(out, "o")["MAIN"].data
    assert len(main) == 64 * 64 * 8
    present = {(i // 8) % 64 for i in range(0, len(main), 8)
               if struct.unpack_from("<I", main, i)[0] & 1}
    assert present == {1, 3, 5}
    assert res.extra["tiles"] == 3


def test_a_global_wmo_map_keeps_its_reference(opts):
    out, _res = convert_wdt(F.build_modern_wdt(global_wmo=True), "w.wdt", opts)
    chunks = parse_wdt(out, "o")
    assert flags_of(out) & FLAG_GLOBAL_WMO
    assert chunks["MWMO"].data.startswith(b"world\\wmo\\global.wmo")
    assert "MODF" in chunks


def test_a_wrath_map_index_is_passed_through(opts):
    raw = F.build_modern_wdt(flags=FLAG_ADT_HAS_BIG_ALPHA, with_maid=False)
    from wotlkconv.chunks import ChunkReader, ChunkWriter
    cw = ChunkWriter(reverse=True)
    for c in ChunkReader(raw, reverse=True):
        if c.name != "MPL2":
            cw.add(c.name, c.data)
    out = bytearray(cw.getvalue())
    _out, res = convert_wdt(bytes(out), "w.wdt", opts)
    assert res.status is Status.PASSTHROUGH


def test_a_non_wdt_is_rejected():
    with pytest.raises(UnsupportedFormatError):
        parse_wdt(b"REVM" + b"\x04\0\0\0" + b"\x12\0\0\0", "w.wdt")
